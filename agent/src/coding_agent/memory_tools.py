"""The `memory` MCP server: two tools, on keys the model cannot name.

This is the **first in-process MCP server in the repository**, and in-process is not a
convenience here. `subagents.AWS_DOCS_SERVER` is a *stdio* server -- a subprocess spawned
with a command and an argument list -- and that shape cannot carry what this server needs:
a subprocess cannot hold the task's state, and the only way to hand it the container's
credentials would be to write them into an `env` key, which is precisely the thing that
server's missing `env` exists to prevent. The SDK's own in-process facility
(`create_sdk_mcp_server`) runs the tool handlers inside this process, so they close over a
`TaskMemory` built from the state and use the execution role that is already here.

**Invariant 10b, in full, because this ships an MCP tool that calls an AWS API.** The
invariant was written against a *general-purpose* AWS client handed to a prompt-injected
agent. What is here is the opposite shape, and the difference is mechanical rather than
asserted in prose:

* **Two fixed operations exist in the whole module** -- `CreateEvent` and
  `RetrieveMemoryRecords`, both reached only through `agentcore_memory.TaskMemory`. There is
  no generic call, no operation name on the wire, and nothing else in `bedrock-agentcore` is
  reachable from a tool.
* **The model names neither the memory, the actor, the session nor the namespace.**
  `memoryId`, `actorId`, `sessionId` and `namespace` are all supplied by the process from
  the state. The schemas below take a `query` string, a `scope` from a two-value enum, a
  `top_k` integer and a `text` string, and nothing else -- so a prompt-injected role cannot
  read another repository's memory, cannot write into one, and cannot widen its own reach.

That absence is the enforcement, and it is asserted by a test rather than trusted to this
comment; do not add a parameter that could name a memory.

**`build_server` returns `None` when the task has no memory.** That is how a memoryless run
-- a repository with no memory resource, or `python -m coding_agent.local_run`, which has no
tables and no AWS -- ends up with no `memory` server and no `mcp__memory__*` entry at all,
rather than with two tools that fail on every call and spend a role's turns discovering it.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

# `create_sdk_mcp_server` and `tool` are re-exported at the top level; this TypedDict is
# not, so it comes from the types module by name -- the same shape as `subagents.py`'s
# `McpStdioServerConfig` import and `hooks.HookEvent`.
from claude_agent_sdk.types import McpSdkServerConfig

from .agentcore_memory import DEFAULT_TOP_K, TaskMemory, format_records
from .state import TaskState

log = logging.getLogger(__name__)

MEMORY_SERVER_NAME = "memory"
# The wildcard Claude Code derives from the server name above. `role_tools` is where it is
# used, so it lives here next to the name it is derived from.
MEMORY_TOOLS = "mcp__memory__*"


# The two descriptions are PROMPT SURFACE -- a model reads them and decides from them
# alone whether to call the tool and with which scope -- so they are module constants a
# test can assert the content of rather than string literals buried in a decorator call.
_SEARCH_DESCRIPTION = (
    "Search this repository's AgentCore memory for what an earlier run learned. There are "
    "two scopes. `episodic` is what this repository has learned across every task it has "
    "ever run -- durable findings recorded by previous tasks, shared by every session on "
    "this repository -- and it is the one to reach for before planning or before guessing "
    "at how this codebase works. `session` is what this task has recorded during this run, "
    "and nothing else. Returns the matching records as plain text, or a sentence saying "
    "nothing matched."
)
_RECORD_DESCRIPTION = (
    "Record one durable finding in this repository's memory: something you learned that is "
    "worth carrying to a future task on this repository -- a build step with a hidden "
    "prerequisite, a test that needs a fixture, an approach that does not work and why. "
    "This is NOT for narration or progress reporting: do not record what you are about to "
    "do, what you just edited, or that a step succeeded. Every write costs a round trip "
    "inside your turn, so record a finding a later run would otherwise rediscover, and "
    "nothing else."
)

# Note what is ABSENT from both schemas: no `memoryId`, no `actorId`, no `sessionId`, no
# `namespace`, no `configId`, no `taskId`. All of those come from the process, off the
# state, which is the invariant 10b argument in the module docstring made mechanical -- a
# prompt-injected role cannot name another repository's memory because there is no
# parameter in which to name it.
#
# The schemas are the EXPLICIT JSON Schema form and not the SDK's dict-of-types shorthand,
# and that is a real trap rather than a style choice: `create_sdk_mcp_server`'s
# `_build_input_schema` marks *every* key of a shorthand schema `required`, so
# `{"query": str, "scope": str, "top_k": int}` would force the model to pass all three on
# every call. A dict carrying both `type` (a `str`) and `properties` is passed through
# untouched, which is what these are.
_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "What you want to know, in plain words -- this is a semantic search, not a "
                "grep, so a question reads better than a keyword."
            ),
        },
        "scope": {
            "type": "string",
            "enum": ["episodic", "session"],
            "default": "episodic",
            "description": (
                "`episodic` for what this repository has learned across every task; "
                "`session` for what this task has recorded during this run."
            ),
        },
        "top_k": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "default": DEFAULT_TOP_K,
            "description": "How many records to return at most.",
        },
    },
    "required": ["query"],
}
_RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "The finding, written so a future run on this repository can act on it "
                "without your context: what is true, and what it means for the work."
            ),
        },
    },
    "required": ["text"],
}

_RECORDED = "Recorded. A future task on this repository can find it with an episodic search."
_NOT_RECORDED = (
    "The memory write did not land, so this finding was not saved. Nothing else is wrong "
    "and the run continues -- carry on with the work rather than retrying."
)


def _text(body: str) -> dict[str, Any]:
    """The one MCP content wrapper, so both tools answer in the same shape."""
    return {"content": [{"type": "text", "text": body}]}


def _tools(memory: TaskMemory) -> list[Any]:
    """The two tools, as `SdkMcpTool` objects closing over one `TaskMemory`.

    Module-private and the offline test seam, the same role `subagents._collect` plays: a
    test can reach each tool's `.name`, `.description`, `.input_schema` and `.handler`
    without standing up an MCP transport. The handlers cannot be module-level functions --
    closing over `memory` is what makes the server per-task, and it is what keeps the
    memory id off the wire the model writes on.
    """

    @tool("memory_search", _SEARCH_DESCRIPTION, _SEARCH_SCHEMA)
    async def memory_search(args: dict[str, Any]) -> dict[str, Any]:
        records = memory.search(
            str(args.get("query") or ""),
            scope=str(args.get("scope") or "episodic"),
            top_k=int(args.get("top_k") or DEFAULT_TOP_K),
        )
        # `format_records` is `agentcore_memory`'s, deliberately: the tool and any prompt
        # that pastes recalled memory in must not spell a record two ways.
        return _text(format_records(records))

    @tool("memory_record", _RECORD_DESCRIPTION, _RECORD_SCHEMA)
    async def memory_record(args: dict[str, Any]) -> dict[str, Any]:
        # NOT an `is_error` result when the write fails. A failed memory write is
        # degradation (D11) -- `TaskMemory.record` has already logged it -- and flagging it
        # as a tool error invites the model to retry-loop over something that is not its
        # problem and cannot be fixed from inside its turn.
        wrote = memory.record(str(args.get("text") or ""))
        return _text(_RECORDED if wrote else _NOT_RECORDED)

    # Neither handler can raise: `TaskMemory` degrades to `False`/`[]` on every failure it
    # can see. `create_sdk_mcp_server` would convert a raised exception into an `isError`
    # result anyway, so that path is a backstop and never the normal one.
    return [memory_search, memory_record]


def build_server(state: TaskState | None) -> McpSdkServerConfig | None:
    """The in-process memory server for one task, or `None` when the task has no memory.

    Constructs **no AWS client**: `TaskMemory` builds its boto3 client inside `record` and
    `search`, per call, so this is a pure object graph and nothing here reaches AWS. That is
    what lets `build_options` call it and stay pure.
    """
    memory = TaskMemory.for_state(state)
    if not memory.enabled:
        return None
    return create_sdk_mcp_server(MEMORY_SERVER_NAME, tools=_tools(memory))
