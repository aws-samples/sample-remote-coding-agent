"""One option surface for all four roles, and the fold from an SDK stream to a result.

Two pure seams carry the whole of this module's testability, and both matter more than
they look. `build_options` opens no session and reaches no network, so the
security-relevant fields -- `permission_mode`, `setting_sources`, `strict_mcp_config`, and
the *absence* of `WebFetch`/`WebSearch` -- are asserted on every run rather than reviewed
by eye. It performs no I/O at all: no filesystem probe, no environment read, no socket and
no AWS call, so the tests below need nothing stubbed to reach it.
`_collect` folds any async iterable of SDK messages, so the transcript-versus-verdict
rule is tested over a handful of hand-built dataclasses with no `claude` process and no
Bedrock call anywhere.

The prompt is the only role boundary that carries *words*, and `role_tools` is the only
one that carries *tools*. There is one permission mode and one plugin set for `plan`,
`replan`, `code` and `review`; what differs is the model, the turn budget, the words, and
-- since the AWS documentation server arrived for the planning and coding roles alone --
one entry in the tool list. That difference has exactly one owner, `role_tools(role)`,
which both `build_options`' `allowed_tools` and `tool_paragraph(role)` read: a second
list would be a second place for the boundary to drift.

Two MCP servers reach these sessions now, and they differ in shape as well as in reach.
The AWS documentation server is a *stdio* subprocess for `MCP_ROLES` alone. The `memory`
server (`memory_tools`) is IN-PROCESS and **every** role gets it -- but only when the task
has a memory resource, which is a property of the state rather than of the role: a
repository with no memory, and `python -m coding_agent.local_run`, get no server and no
`mcp__memory__*` entry at all rather than tools that fail on every call. `build_options`
is still pure with it: `memory_tools.build_server` constructs an object graph and nothing
else, because `TaskMemory` builds its boto3 client inside each call.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    TextBlock,
    query,
)

# `AgentDefinition` is re-exported at the top level; these two TypedDicts are not, so
# they come from the types module by name -- the same shape as `hooks.HookEvent`.
from claude_agent_sdk.types import McpStdioServerConfig, TaskBudget

from . import activity, config, hooks, memory_tools
from .state import TaskState

log = logging.getLogger(__name__)

# `"Task"` is spelled the old way deliberately: `claude-agent-sdk` 0.2.x still accepts
# it after the upstream `Task` -> `Agent` rename, and the narrow `>=0.2.0,<0.3.0` pin in
# `agent/pyproject.toml` exists for exactly this line -- widening it renames the tool out
# from under the string.
#
# `WebFetch` and `WebSearch` are ABSENT here, which is invariant 33 and the reason the
# no-network compliance claim is a claim at all. A "let the agent read the docs" change
# would undo it in a single line, so the omission is asserted by a test rather than
# trusted to a comment -- `tests/test_subagents.py::test_no_role_can_reach_the_network`,
# which checks both names against every surface a role's tools reach: this tuple,
# `role_tools`, the session options, the generated `tool_paragraph`, and the fan-out
# implementer's `AgentDefinition`.
#
# `TodoWrite` and `Agent` are absent too, and deliberately: probe (c) ran a session with
# `--tools "Read,Write,Edit,Grep,Glob,Bash,Skill,Task,Agent,TodoWrite"` and the
# `system:init` message reported exactly
# `['Bash','Edit','Glob','Grep','Read','Skill','Task','Write']` -- neither name is in the
# session's tool list even when passed. Unknown names in `--tools` are silently
# tolerated, so adding one fails as an absent tool and never loudly, and this tuple is
# what `tool_paragraph` is generated from: a name here that the session does not expose
# is a prompt promising a tool the role does not have, which is the exact failure the
# generation exists to prevent. Nothing is lost -- `agents=` (below) delivers subtask
# breakdown through a primitive the session really does expose.
ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Write",
    "Edit",
    "Grep",
    "Glob",
    "Bash",
    "Skill",
    "Task",
)

# The belt to `ALLOWED_TOOLS`' braces. Exactly four, each for its own reason:
DENIED_TOOLS: tuple[str, ...] = (
    "WebFetch",  # invariant 33, said a second way: an absent tool and a denied tool
    "WebSearch",  # fail differently, so no-network is stated in both mechanisms
    "NotebookEdit",  # surface no role here needs; `Edit` covers every file we touch
    "ExitPlanMode",  # only means anything in the "plan" mode this code never uses
)


# The AWS documentation server: `plan`, `replan` and `code` only. Three rules hold it in
# place, and each of them cost something to learn:
#
# DOCUMENTATION TOOLS ONLY (invariants 10b and 34). This server reads
# `docs.aws.amazon.com` and nothing else. Anything that could call an AWS API would run
# under the container's execution role and hand a prompt-injected agent an AWS client, so
# a server that offered `call_aws` would not be added here with a narrower allow-list --
# it would not be added at all.
#
# PINNED, AND INSTALLED AT BUILD TIME. The version is an exact `==`, never
# `uvx ...@latest`: a runtime fetch is a moving reference executing inside the container
# and it makes every task depend on a package registry being up. `uvx` runs the version
# the image already has; it must not be the thing that goes and gets one.
#
# `agent/Dockerfile` holds the build-time end of that: it runs
#   RUN uv tool install "awslabs.aws-documentation-mcp-server==<AWS_DOCS_VERSION>"
# with `uvx` on `PATH`, so the `AWS_DOCS_SERVER` command below resolves what the image
# already cached. The image's `ARG AWS_DOCS_MCP_VERSION` and `AWS_DOCS_VERSION` here must
# be the same version, and
# `tests/test_repo_contracts.py::test_the_agent_image_installs_the_pinned_aws_documentation_server`
# is what asserts the pair: a drift makes `uvx` fetch from a registry at task time, or --
# with no network -- makes the tools silently not appear.
#
# STILL OWED, and not this phase's: `docs.aws.amazon.com` on the VPC DNS allowlist, which
# the Route 53 Resolver DNS Firewall owns. Until that lands the tools may simply not appear
# -- an absent tool, never an error -- so it is named in this card's pull request
# description as well as here.
AWS_DOCS_PACKAGE = "awslabs.aws-documentation-mcp-server"
AWS_DOCS_VERSION = "1.1.30"
AWS_DOCS_REQUIREMENT = f"{AWS_DOCS_PACKAGE}=={AWS_DOCS_VERSION}"
# No `env` key, so no credential can be handed to the server even by accident.
AWS_DOCS_SERVER: McpStdioServerConfig = {"command": "uvx", "args": [AWS_DOCS_REQUIREMENT]}
AWS_DOCS_TOOLS = "mcp__aws-docs__*"

# The in-process memory server's two names, re-exported so `subagents.MEMORY_TOOLS` -- which
# `role_tools` is where the wildcard is used -- resolves without a caller importing
# `memory_tools` itself. They live in `memory_tools` next to the tools they describe; this is
# an alias and never a second spelling.
MEMORY_SERVER_NAME = memory_tools.MEMORY_SERVER_NAME
MEMORY_TOOLS = memory_tools.MEMORY_TOOLS

# `replan` is in the set and `review` is not. The brief says "`plan` and `code` only",
# which means the plan node's two roles -- `replan` is the same planning job on a harder
# input, and a re-planner that could not look something up would be the one role guessing
# -- and the code node's one. `review` judges a diff it can read, and `verify` makes no
# model call at all.
MCP_ROLES: frozenset[str] = frozenset({"plan", "replan", "code"})


# Fan-out, for the coding role and no other. Four bounds, and they are not equivalent:
#
# `FANOUT_MAX_TURNS` is the implementer's own turn budget -- 80 against the coding role's
# 200, because a subagent implements one unit of work and not the whole specification.
#
# `FANOUT_MAX_BUDGET_USD` is the REAL runaway cap. It has a defined failure the caller can
# see: the session ends with `subtype == "error_max_budget_usd"`, which is a `ResultMessage`
# a node can read rather than a silence it has to infer.
#
# `FANOUT_TASK_BUDGET` is a pacing HINT and never a guarantee. It rides the
# `task-budgets-2026-03-13` beta header, which is an Anthropic-API mechanism -- `SdkBeta`
# in the pinned 0.2.141 is `Literal["context-1m-2025-08-07"]` and cannot even name it --
# so on Bedrock it may be silently inert. Nothing may describe this as a cap.
#
# `FANOUT_ENV` bounds the blast radius: depth 1 means a subagent cannot spawn subagents of
# its own, and five concurrent is the width. Both variable names are present in the CLI
# binary bundled with `claude-agent-sdk` 0.2.141.
#
# And the honesty clause, which belongs here and in the README: FAN-OUT HAS NEVER BEEN
# MEASURED HERE. It is a demonstration of a Claude Code capability, not a claim that it
# makes the agent better, and the budget above is a runaway backstop rather than an
# observation of what a fan-out run costs.
FANOUT_MAX_TURNS = 80
FANOUT_MAX_BUDGET_USD = 5.0
FANOUT_TASK_BUDGET: TaskBudget = {"total": 2_000_000}
FANOUT_ENV = {
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "5",
}

IMPLEMENTER_PROMPT = """You implement ONE unit of work from a specification another role
wrote, in the working tree you have been given. The role that dispatched you owns the rest
of the specification and the closing statement; you own your unit and nothing else.

- Implement the part of the specification you were handed, and only that part. Do not
  reach into a file another unit owns: two writers on one file lose an edit with no error.
- Write code that reads like the code already in this tree: match its naming, its idioms,
  its error handling and its comment density.
- Do not commit, do not push, and do not touch a git remote or a git config. A later step
  commits the whole tree at once; a commit of your own empties the diff it reads.
- Nothing you write under `.agent/` counts as implementing the task. A change confined to
  that directory is treated as a run that did nothing.
- Append anything durable you learn to `.agent/memory/code-<unit>.md`, where `<unit>` is
  the unit you were given -- your OWN file, and NEVER `code.md`. Appending is a
  read-modify-write of a whole file, so two subagents appending to one file silently lose
  an entry.
- Finish by stating what you changed, in two or three sentences.
"""


class RoleError(RuntimeError):
    """A role session that produced nothing a node can use."""


def role_tools(role: str, *, memory: bool = False) -> tuple[str, ...]:
    """What `role` may call: the single owner of the per-role tool list.

    Both `build_options`' `allowed_tools` and `tool_paragraph(role)` read this and
    nothing else, so the session's real toolset and the sentence the prompt makes about
    it cannot disagree. `ALLOWED_TOOLS` for everyone, plus the AWS documentation lookup
    for the roles in `MCP_ROLES`.

    The memory lookup is **every** role's and not `MCP_ROLES`' -- a review that cannot
    record what it found is the role with the most to say and no way to say it -- but it is
    present only when the task really has a memory resource, which is what `memory` says.
    A `bool` rather than the state deliberately: it keeps this function a pure function of
    its arguments, and it stops `build_options` building the server twice per session.
    """
    tools = (*ALLOWED_TOOLS, AWS_DOCS_TOOLS) if role in MCP_ROLES else ALLOWED_TOOLS
    if memory:
        return (*tools, MEMORY_TOOLS)
    return tools


def tool_paragraph(role: str, *, memory: bool = False) -> str:
    """The prompt's sentence about tools, generated from `role_tools(role)`.

    Generated rather than written out, so a prompt cannot promise a tool the role does
    not have: a role told it may `WebFetch` spends turns discovering that it may not,
    and a role never told about `Skill` never invokes one. `role` is REQUIRED -- the
    roles no longer share one toolset, so a call site that could omit it would be a call
    site quietly keeping the old fleet-wide sentence.

    `nodes/**` does not pass `memory=` yet: the prompt half of the memory work is the next
    card's, and nothing is broken in the meantime because MCP tools are advertised to the
    model by the server itself -- they are discoverable and callable without this sentence.
    This keyword is the seam that card needs.
    """
    tools = role_tools(role, memory=memory)
    # The MCP wildcard is not a name a model types, so sentence one lists the plain tool
    # names exactly as it always has and the server gets a sentence of its own.
    plain = tuple(name for name in tools if not name.startswith("mcp__"))
    paragraph = (
        f"Your tools are {', '.join(plain[:-1])} and {plain[-1]}. "
        "Anything outside that set is unavailable to you and there is no point asking "
        "for it. You have a full shell and write access to the repository in your "
        "working directory."
    )
    if role in MCP_ROLES:
        paragraph += (
            f" You also have `{AWS_DOCS_TOOLS}`: a read-only lookup against the AWS "
            "documentation, for checking what a service or API actually does rather "
            "than guessing at it. It reads documentation and reaches no AWS account, so "
            "it can tell you what an API is called and never what is deployed."
        )
    if memory:
        paragraph += (
            f" You also have `{MEMORY_TOOLS}`: this repository's AgentCore memory, in two "
            "scopes. `episodic` is what this repository has learned across every task it "
            "has ever run; `session` is what this task has recorded during this run. "
            "Search it before guessing at how this codebase works, and record a durable "
            "finding a future task would otherwise rediscover -- never narration or "
            "progress."
        )
    return paragraph


def implementer_definition(
    role_cfg: config.RoleConfig, cfg: config.AgentConfig, *, memory: bool = False
) -> AgentDefinition:
    """The one subagent the coding role may dispatch, bounded and denied what it must be.

    The field names are the SDK's own and they are camelCase inside a Python dataclass --
    `disallowedTools`, `maxTurns`, `permissionMode`. Easy to get wrong and silent when it
    is wrong (an unknown keyword would raise, but a *plausible* snake_case name simply is
    not one of these fields), so they are written out here once and asserted by a test.

    `disallowedTools` REPEATS `DENIED_TOOLS` on purpose. A subagent inherits nothing it is
    not given, and a subagent that could push would bypass `submit` -- which is the only
    reason that list exists at all.

    `memory` is threaded straight into `role_tools("code", ...)` and nowhere else. The
    fan-out implementer runs inside the coding session's own process, so the in-process
    memory server really is reachable from it; `role_tools` being the single owner of the
    tool list is what makes that fall out rather than need wiring here.
    """
    return AgentDefinition(
        description=(
            "Implements one independent unit of work from the specification, in the files "
            "that unit owns. Dispatch several in parallel when the specification marks "
            "units as independent of each other."
        ),
        prompt=IMPLEMENTER_PROMPT,
        # `"Skill"` in `tools` is deprecated by the SDK; `skills=` is the supported
        # channel, and it takes an explicit list -- `skills="all"` is valid at the top
        # level but not inside an `AgentDefinition`, so the skills are named.
        tools=[name for name in role_tools("code", memory=memory) if name != "Skill"],
        skills=list(config.EMBEDDED_SKILLS),
        disallowedTools=list(DENIED_TOOLS),
        model=role_cfg.model or cfg.model,
        # Inherited from the coding role deliberately: a subagent on a weaker model or a
        # lower effort would be an unannounced second routing table (invariant 8).
        effort=role_cfg.effort,
        maxTurns=FANOUT_MAX_TURNS,
        # Invariant 9, for the subagent too: never `"plan"`.
        permissionMode="acceptEdits",
    )


@dataclass(frozen=True)
class RoleResult:
    """What one role session produced.

    Invariant 7: **read `final_text`.** It is the last assistant message that carried
    text -- the role's closing statement, which is where a verdict and an artefact path
    live. `text` is the whole transcript, narration included, and exists for logs and
    the activity feed.

    Both halves of that have already cost a run. "I'll start by exploring the
    repository" was once persisted as the spec, because the first message was read
    instead of the last; and a mid-run "the tests are failing" line once vetoed a run
    that ended green, because the verdict was matched against the transcript.
    """

    role: str
    text: str
    final_text: str
    num_turns: int
    total_cost_usd: float | None
    session_id: str | None
    is_error: bool
    # The rest of the `ResultMessage`'s accounting, all four verified present on the
    # pinned 0.2.141. They default to `None` because a stream that ended without a
    # terminator has none of them, which `_collect` already treats as abnormal-not-fatal.
    usage: dict[str, Any] | None = None
    model_usage: dict[str, Any] | None = None
    subtype: str | None = None
    terminal_reason: str | None = None


def cost_row(result: RoleResult, *, node: str) -> dict[str, Any]:
    """One cost row for one role session, tagged with the node that ran it.

    Two caveats belong exactly here, where a reader meets the number rather than three
    modules away from it.

    `total_cost_usd` is a **client-side estimate**. The CLI computes it from a price table
    bundled in the SDK, so it drifts from the bill whenever pricing changes or the SDK is
    bumped, and it is for operational budgeting -- which node spends the money -- and
    **never for invoicing**. Probe (d) confirmed it does arrive populated on Bedrock
    (`0.02556625` on a one-turn call, `0.2059445` on a `/security-review`), with
    `model_usage[...]["provider"] == "bedrock"`.

    `usage` covers the **main loop only** while `model_usage` includes **subagents**. That
    difference is invisible today and stops being invisible the moment fan-out is on, when
    the two disagree by whatever the implementer spent -- so the per-model breakdown is
    what rides on the row, and `usage` stays on the `RoleResult` for a caller that knows
    which of the two it is asking for.

    `costUSD` inside `model_usage` is a float, which is why `store.record_cost` runs the
    rows through `_ddb_safe`: the DynamoDB resource API refuses a float at any depth.
    """
    return {
        "node": node,
        "role": result.role,
        "totalCostUsd": result.total_cost_usd,
        "numTurns": result.num_turns,
        "modelUsage": dict(result.model_usage or {}),
        "sessionId": result.session_id,
        # `subtype` is how a runaway is told from a finish: `error_max_budget_usd` is the
        # defined failure `FANOUT_MAX_BUDGET_USD` produces, and it is a field rather than
        # a silence.
        "subtype": result.subtype,
        "terminalReason": result.terminal_reason,
        "at": activity.utcnow_iso(),
    }


def build_options(
    role: str,
    *,
    cwd: Path,
    cfg: config.AgentConfig,
    system_prompt: str | None = None,
    state: TaskState | None = None,
    node: str | None = None,
) -> ClaudeAgentOptions:
    """The complete option surface for one role session. No session, and no network.

    `cwd` is the clone and nothing above it (invariant 11). The caller resolves it;
    this function never derives a path of its own.

    Nothing here touches a filesystem or reads an environment variable, so the whole option
    surface is decided by this function's arguments.

    `state` and `node` exist only for the hooks: the compaction and subagent callbacks
    need somewhere to put an activity row, and `hooks.build_hooks` closes over both.
    Building a closure is not I/O, so this function is still pure with them.

    `state` is also where the memory id is, which is why the in-process memory server can be
    built here without reaching AWS: `memory_tools.build_server` reads `memoryId` off the
    state and closes over it, and the boto3 client is built inside a tool call and never
    before one. It is `None` when the task has no memory, which is the memoryless path.
    """
    role_cfg = cfg.roles.get(role) or config.ROLES.get(role)
    if role_cfg is None:
        raise KeyError(
            f"{role!r} is not a role this fleet runs: expected one of "
            f"{', '.join(sorted(config.ROLES))}"
        )

    # Fan-out is the coding role's alone: `plan` and `review` each produce one artefact and
    # have nothing to parallelise, and a subagent they could dispatch would be a second
    # writer on that artefact.
    fanout = role == "code"

    # Every role, not just `MCP_ROLES`: a review that cannot record what it found is the
    # role with the most to say and no way to say it. `verify` makes no model call at all
    # and needs nothing. `None` when the task has no memory, which is how a memoryless run
    # ends up with no server rather than with tools that always fail.
    #
    # This keeps `build_options` pure. `build_server` constructs an object graph and
    # nothing else -- `TaskMemory` builds its boto3 client inside `record`/`search`, per
    # call, so no AWS client exists until a model actually invokes a tool.
    memory_server = memory_tools.build_server(state)
    has_memory = memory_server is not None

    return ClaudeAgentOptions(
        cwd=str(cwd),
        # One owner for the per-role tool list, read here and in `tool_paragraph(role)`.
        allowed_tools=list(role_tools(role, memory=has_memory)),
        disallowed_tools=list(DENIED_TOOLS),
        # Invariant 9: `acceptEdits`, never `"plan"`. `"plan"` writes to a host-side
        # plan file and returns only narration, so the real spec never reaches the
        # coding subagent -- the session looks busy and the tree is untouched.
        permission_mode="acceptEdits",
        # Invariant 10: the clone is untrusted input, so a checked-in `.claude/` must
        # not be able to widen this session's permissions. No setting source is read.
        #
        # `agent/Dockerfile` ships NO CLI config file -- no `~/.claude.json`, no
        # `/root/.claude/settings.json`, and nothing copied to either; its own closing comment
        # says so, and `tests/test_repo_contracts.py`
        # `::test_the_agent_image_bakes_the_skills_and_sets_no_work_root` holds it. One would load
        # REGARDLESS of this `setting_sources=[]`, so it is the one file that can widen this
        # session from inside the image.
        setting_sources=[],
        # Invariant 10, the other half. Two mechanisms because they fail differently:
        # `setting_sources=[]` is a consequence, this flag is a statement -- only the
        # servers passed programmatically below are used. Nothing arrives from the clone's
        # `.mcp.json`, from user or global settings, or from a PLUGIN-PROVIDED server --
        # and that last one is not hypothetical here, because skills reach these sessions
        # as plugin roots (`plugins=` below).
        strict_mcp_config=True,
        # The AWS documentation lookup, for the planning and coding roles only: `review`
        # judges a diff it can read and has no reason to hold a network-backed tool. The
        # memory server joins it for every role when the task has one -- MERGED rather than
        # assigned, because a second `mcp_servers=` that overwrote the first would silently
        # take the documentation server away from `plan`, `replan` and `code`. `{}` when
        # neither applies, which is also the SDK's own default.
        #
        # `strict_mcp_config=True` above is untouched and needs no exception: both servers
        # arrive PROGRAMMATICALLY, which is exactly the channel that flag permits, and
        # nothing about either can be widened from the clone.
        mcp_servers={
            **({"aws-docs": AWS_DOCS_SERVER} if role in MCP_ROLES else {}),
            # Tested on the value and not on `has_memory`: only the value test narrows
            # `memory_server` away from `| None`, and a `None` server in this map is what a
            # type checker is right to reject.
            **({MEMORY_SERVER_NAME: memory_server} if memory_server is not None else {}),
        },
        max_turns=role_cfg.turns,
        effort=role_cfg.effort,
        model=role_cfg.model or cfg.model,
        # Every role, one fleet value. With per-role models a momentarily unavailable model
        # should degrade the run rather than kill it, and both values are Bedrock ones --
        # so the worst a misconfiguration here can do is run a role on the wrong model,
        # never send a request to a vendor endpoint.
        fallback_model=cfg.fallback_model,
        # The only channel by which a skill reaches a session. Never a setting source:
        # invariant 10 has switched those off, and a skill root is not a permission.
        plugins=[{"type": "local", "path": str(path)} for path in cfg.skill_paths],
        # Every role gets every skill in every root -- the skill's own frontmatter
        # decides when it triggers, so there is no per-role wiring here to get wrong.
        skills="all",
        # `CLAUDE_CODE_DISABLE_AUTO_MEMORY` is the one input that loads regardless of
        # `setting_sources`, so it is switched off explicitly rather than assumed absent
        # (invariant 10's third mechanism). It is a literal in the dict rather than a
        # merged-in constant, so the fan-out merge below cannot drop it.
        #
        # It is one of TEN CLI suppressions, and the only one that appears here.
        # `provisioner._fleet_environment` owns the other nine because they belong to the
        # container's PROCESS environment: the SDK spawns the CLI with `os.environ`
        # overlaid by `options.env`, so a variable on the runtime reaches the CLI without
        # this module naming it, and forwarding it as fleet policy is what makes the set
        # auditable in a `GetAgentRuntime` response. This one is duplicated here on
        # purpose -- it is the CROSS-TENANT suppression, so it must also hold on a local
        # `python -m coding_agent.local_run` and on a runtime whose environment has not
        # converged yet. Nothing else joins it: a second copy would be a second owner
        # that can drift, and the values are pinned in
        # `tests/test_env_contract.CLI_SUPPRESSIONS`.
        #
        # `CLAUDE_CODE_ENABLE_TELEMETRY=0` joins it for a related but narrower reason: it
        # is the ONLY variable that turns the CLI's OTLP exporter on, this container ships
        # nothing for it to export to, and there is no code path here that would ever set
        # it to `"1"`. It is a fleet variable too (`provisioner._fleet_environment`), and
        # the duplicate is what holds on a local `python -m coding_agent.local_run` and on
        # a runtime whose environment has not converged. Hard-coded rather than probed: the
        # collector this once configured is gone, so the answer no longer depends on which
        # image is running. Restoring an exporter here means restoring an egress
        # destination, which is invariant 33's business and not a one-line default.
        env={
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
            **(FANOUT_ENV if fanout else {}),
        },
        # The coding role's fan-out, and its three bounds. `None` everywhere else, which
        # is the SDK's own default: an `agents=` entry a role never dispatches is still a
        # subagent definition a prompt-injected role could find.
        agents=(
            {"implementer": implementer_definition(role_cfg, cfg, memory=has_memory)}
            if fanout
            else None
        ),
        max_budget_usd=FANOUT_MAX_BUDGET_USD if fanout else None,
        task_budget=FANOUT_TASK_BUDGET if fanout else None,
        # Invariant 10c, mechanised. Hooks are programmatic, so `setting_sources=[]`
        # stays as it is, and they fire inside subagents too -- so this one registration
        # is also what denies a commit to the fan-out implementer. Every role gets it:
        # the rule is universal, and a per-role registration would be a second place for
        # it to drift.
        hooks=hooks.build_hooks(state, node=node or role),
        # `None` is the SDK's own default, so "when given" needs no branch here.
        system_prompt=system_prompt,
    )


async def _collect(
    messages: AsyncIterable[Message],
    *,
    role: str,
    node: str | None = None,
    on_row: Callable[[list[dict[str, Any]]], None] | None = None,
) -> RoleResult:
    """Fold an SDK message stream into a `RoleResult`.

    This is the offline test seam: it takes any async iterable, so the transcript rule
    is driven over hand-built `AssistantMessage`/`ResultMessage` dataclasses with no CLI
    process and no Bedrock call. Nothing that needs a live session belongs in here --
    one such line takes the whole offline surface down with it.

    `node` exists because one node can run a differently-named role: the `plan` node
    runs `replan` on every iteration after the first, and the activity feed is read by
    node name while `RoleResult.role` names the role that ran.
    """
    chunks: list[str] = []
    final_text = ""
    result: ResultMessage | None = None
    row_node = node or role

    async for message in messages:
        if isinstance(message, AssistantMessage):
            joined = "\n".join(
                block.text for block in message.content if isinstance(block, TextBlock)
            )
            if joined:
                chunks.append(joined)
            if joined.strip():
                # The LAST non-empty join wins, which is the whole of invariant 7.
                final_text = joined
        elif isinstance(message, ResultMessage):
            result = message
            if message.result:
                chunks.append(message.result)

        if on_row is not None:
            # Observability is best-effort by construction: a row that cannot be built
            # or written must never be able to fail a role whose work is already done.
            try:
                rows = activity.rows_from_message(message, node=row_node)
                if rows:
                    on_row(rows)
            except Exception:
                log.warning("dropped the activity rows for a %s message", role, exc_info=True)

    if result is None:
        # Abnormal but not fatal: the caller still gets whatever text arrived, and the
        # missing terminator is worth a line in the log rather than an exception.
        log.warning("the %s session ended without a result message", role)
    elif not final_text:
        # No assistant message carried text -- a session that only ran tools, or one the
        # CLI summarised itself. `ResultMessage.result` is the only other place the
        # closing statement can be.
        final_text = result.result or ""

    return RoleResult(
        role=role,
        text="\n".join(chunks),
        final_text=final_text,
        num_turns=result.num_turns if result is not None else 0,
        total_cost_usd=result.total_cost_usd if result is not None else None,
        session_id=result.session_id if result is not None else None,
        is_error=result.is_error if result is not None else False,
        usage=result.usage if result is not None else None,
        model_usage=dict(result.model_usage) if result is not None and result.model_usage else None,
        subtype=result.subtype if result is not None else None,
        terminal_reason=result.terminal_reason if result is not None else None,
    )


async def run_role(
    role: str,
    prompt: str,
    *,
    cwd: Path,
    cfg: config.AgentConfig | None = None,
    state: TaskState | None = None,
    node: str | None = None,
    system_prompt: str | None = None,
) -> RoleResult:
    """Run one role session to completion in `cwd` and return what it produced.

    The prompt is the only role boundary, so everything that distinguishes this session
    from the other three is either in `prompt` or in the routing table `build_options`
    reads. Nothing in `state` is passed to the role: `cloneSource` is a filesystem path
    on a local run and a role handed one followed it out of its working tree
    (invariant 11).

    `system_prompt` is a plain string, and a plain string **replaces** the CLI's system
    prompt rather than appending to it: the SDK passes `--system-prompt <str>` for a
    string and `--system-prompt ""` for `None`. Replacing is safe here precisely because
    the current value is empty -- every role runs with `--system-prompt ""` today, so a
    string moves it from `""` to whatever the caller gives and changes nothing else. The
    `{"type": "preset", "preset": "claude_code", "append": ...}` dict form, which the SDK
    turns into `--append-system-prompt`, is deliberately NOT used: it would newly switch
    the entire Claude Code preset system prompt on for that role, which is a far larger
    change than any caller here is asking for.
    """
    cfg = cfg or config.load_config()

    def _record(rows: list[dict[str, Any]]) -> None:
        """Rows only when there is a task item to put them on, as `hooks.build_hooks` does.

        Both guards are load-bearing: this one makes the closure safe to call with any
        state, and the `on_row=` conditional below stops `_collect` building rows nobody
        will keep.
        """
        if state is not None:
            activity.record(state, rows)

    # Rows are only built when there is somewhere to put them, so `local_run` and the
    # node tests pay nothing for the feed.
    result = await _collect(
        query(
            prompt=prompt,
            options=build_options(
                role,
                cwd=cwd,
                cfg=cfg,
                state=state,
                node=node or role,
                system_prompt=system_prompt,
            ),
        ),
        role=role,
        node=node or role,
        on_row=_record if state is not None else None,
    )

    log.info(
        "the %s role finished in %d turns (error=%s, cost=%s)",
        role,
        result.num_turns,
        result.is_error,
        result.total_cost_usd,
    )
    # Emptiness alone is the failure signal, and it is the only reliable one. A CLI
    # command that aborts before any model call reports `subtype: "success"`,
    # `is_error: false`, `num_turns: 0`, `total_cost_usd: 0` and `result: ""` -- so
    # `is_error` does not see it, and it used to return normally and let a downstream
    # guard invent a different explanation. `num_turns` is worthless as a did-it-run
    # signal in the other direction too: `/code-review` returned `num_turns: 0` while
    # producing 1234 characters of real findings and costing $0.157. A non-empty `result`
    # is what is left, and `_collect` already falls back to `ResultMessage.result` when no
    # assistant message carried text, so an empty `final_text` after the fold really does
    # mean no output at all.
    #
    # An errored run that still produced its artefact is therefore still not a failure: a
    # role that hits its turn limit after writing the spec has done the job, and the node
    # that owns the artefact reads it back and decides.
    if not result.final_text.strip():
        raise RoleError(
            f"the {role} role produced no text at all, so the session did not run: "
            f"num_turns={result.num_turns}, total_cost_usd={result.total_cost_usd}, "
            f"subtype={result.subtype!r}, terminal_reason={result.terminal_reason!r}; "
            "there is nothing for the node to read back"
        )
    return result
