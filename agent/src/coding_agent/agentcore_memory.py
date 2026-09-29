"""The container's AgentCore Memory surface: two operations, keyed two ways.

**The keying rule is the whole design, and it is one sentence.** An *episodic* record
belongs to the **registered repository** -- `/rca/<configId>/episodic`, ONE namespace
shared by every task that repository ever runs -- and a *session* record belongs to the
**task** -- `/rca/<configId>/session/<taskId>`, one namespace per run. That is what makes
the episodic memory a repository's memory rather than a task's, so a lesson learned in one
task is readable by the next one, and it falls out of a single pairing on the write:
`actorId` is the **configuration id** and `sessionId` is the **task id** (deliverable D6).
Get that pairing backwards and both namespace templates resolve to something no reader
looks at.

**The two namespace templates below are half of a contract.** The provisioner writes the
same two strings down when it creates the memory resource. A mismatch
does not fail: `retrieve_memory_records` simply returns nothing, forever, with no error and
no warning -- an agent that never remembers. They are module constants, and the resolvers
`.format()` them rather than re-spelling the path, so at least the two halves can be diffed
by eye.

`store.py`'s two rules shape this module too, and for the same reasons:

1. **The client is built per call, never at import** -- that is what lets the `aws`
   fixture's `monkeypatch.setenv` take effect with no `importlib.reload`, and what lets a
   developer `python -m coding_agent.local_run` on a machine with no memory resource at
   all.
2. **Every entry point returns early when the thing it needs is absent.** No memory id, no
   configuration id, no task id, an empty query or an empty text: a no-op, not a call.

**Failure is degradation, never an exception** (D11). Every AWS call here is wrapped in
`except (ClientError, BotoCoreError)` -> `log.warning` -> empty result, exactly as
`store.py`'s writes are. Memory is an optional feature reached from inside a model's tool
call, and a run whose code, verification and pull request all succeeded must not die
because a memory write failed.

No environment variable is read here. The memory id arrives on the state, resolved once in
`graph.run_task` off the configuration item (`resolve_memory_id` below).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import store
from .state import TaskState

log = logging.getLogger(__name__)

# The two namespace templates, in the provisioner's spelling. See the module docstring:
# these same two strings are configured on the memory resource by the provisioner, and a
# mismatch makes retrieval silently return nothing.
EPISODIC_NAMESPACE = "/rca/{actorId}/episodic"
SESSION_NAMESPACE = "/rca/{actorId}/session/{sessionId}"

# The default retrieval width, shared with `memory_tools`' schema so the tool's advertised
# default and the client's are one number.
DEFAULT_TOP_K = 5

# What the formatter says when nothing matched. A *stated* sentence, never `""`: a model
# handed an empty string cannot tell "nothing matched" from "the tool broke".
_NO_RECORDS = "No memory records matched."


def episodic_namespace(config_id: str) -> str:
    """`/rca/<configId>/episodic` -- ONE namespace per registered repository, shared by
    every session it ever runs. This is what makes the episodic memory a repository's
    memory and not a task's."""
    return EPISODIC_NAMESPACE.format(actorId=config_id)


def session_namespace(config_id: str, task_id: str) -> str:
    """`/rca/<configId>/session/<taskId>` -- one namespace per task."""
    return SESSION_NAMESPACE.format(actorId=config_id, sessionId=task_id)


def format_records(records: list[dict[str, Any]]) -> str:
    """Render retrieved records as plain text a model can read.

    The one formatter, so the search tool and any prompt that pastes recalled memory in
    cannot spell a record two ways. Reads `content.text` defensively -- the response shape
    is AWS's, not ours -- and skips a record whose text is empty. Never returns `""`.
    """
    lines = []
    for record in records:
        text = ((record.get("content") or {}).get("text") or "").strip()
        if not text:
            continue
        lines.append(f"- {text}")
    if not lines:
        return _NO_RECORDS
    return "\n".join(lines)


# The cap on what `recall` sends as a search query. `RetrieveMemoryRecords` is a *semantic*
# search, not a grep, so a whole specification pasted in as the query embeds to noise -- and
# the coding role's natural query is exactly that: the description plus every heading and file
# list in `specText`. One owner of the number, here rather than sliced at three call sites, so
# three nodes cannot each pick a different few-hundred and so the cap is testable in one place.
MAX_QUERY_CHARS = 400


def recall(
    state: TaskState | None, query: str, *, scope: str = "episodic", top_k: int = DEFAULT_TOP_K
) -> str:
    """What this repository already knows about `query`, as prompt-ready text. `""` when nothing.

    The one entry point a node uses to paste recalled memory into a role prompt, and the two
    things about it that are not obvious from the code:

    1. **`""`, never `format_records`' nothing-matched sentence.** The caller here is a
       *prompt*, and a prompt chooses its own placeholder in its own voice -- a tool result is
       read back by the model that just called it, while a prompt slot is read cold. So the
       emptiness test at a call site is `recall(...) or "(nothing recorded yet)"` and nothing
       outside `format_records` has to know what `_NO_RECORDS` says.
       `format_records` stays the one renderer of a record.
    2. **No `try` here.** Every AWS failure is already degradation inside `.search`, which
       swallows `ClientError`/`BotoCoreError` into `[]` with a `log.warning`; a second
       `except` around it would be a second swallow of the same error.

    The `not memory.enabled` return comes *before* anything that could build a client, which is
    what keeps a memoryless task off AWS entirely rather than merely off a useful answer.
    """
    memory = TaskMemory.for_state(state)
    if not memory.enabled:
        return ""
    # `.search` would return `[]` for a blank query too, but the early return is what makes
    # "a blank query costs nothing" true of this function on its own.
    if not query.strip():
        return ""
    records = memory.search(query.strip()[:MAX_QUERY_CHARS], scope=scope, top_k=top_k)
    if not records:
        return ""
    return format_records(records)


def resolve_memory_id(config_id: str) -> str:
    """This repository's memory id, or `""` when it has none.

    `store.get_configuration` is a `GetItem` on `CONFIG_TABLE` and is invariant 31's only
    permitted read -- the container reads its own configuration item and nothing else. It
    returns `None` when there is no table at all (a local run), which this treats as "no
    memory", the same as a configuration item that names none.

    The swallow lives **here and not in `store.py`**: that function deliberately propagates
    a real error, because its other caller wants the repository's commands and a `None` on a
    throttle would look exactly like an unconfigured repository. A memory id is not that --
    it is an optional feature, and a throttled configuration read must not become a new way
    for a task to die (D11). So the degradation is the new caller's, and `store.py`'s
    contract is left intact.
    """
    try:
        item = store.get_configuration(config_id)
    except (ClientError, BotoCoreError) as exc:
        log.warning("could not read the configuration item for %s; no memory: %s", config_id, exc)
        return ""
    return str((item or {}).get("memoryId") or "")


@dataclass(frozen=True)
class TaskMemory:
    """One task's view of its repository's memory: `record` and `search`, and nothing else.

    Not `MemoryClient`: `bedrock_agentcore` ships a class by that name and `server.py`
    already imports from that package, so the two would be one grep apart.
    """

    memory_id: str
    config_id: str
    task_id: str

    @classmethod
    def for_state(cls, state: TaskState | None) -> TaskMemory:
        """The memory for this task, disabled when the state names none.

        `.get(...) or ""` for all three and not subscription, and `state or {}` because
        `build_options` really does pass `state=None`. This is not defensiveness: a state
        carrying a `taskId` and no `configId` is a shape the suite already builds and hands
        to `build_options`, so a `KeyError` here would fail a currently-green test and, in
        production, kill a role session over an absent optional feature.
        """
        state = state or {}
        return cls(
            memory_id=state.get("memoryId") or "",
            config_id=state.get("configId") or "",
            task_id=state.get("taskId") or "",
        )

    @property
    def enabled(self) -> bool:
        """Whether this task can reach memory at all.

        All three ids, not just the memory id: `CreateEvent` requires `actorId` and both
        namespace templates need both ids, so a state missing either is as unusable as one
        naming no memory.
        """
        return bool(self.memory_id and self.config_id and self.task_id)

    def _client(self) -> Any:
        """The data-plane client, built per call and never at import (`store.py`'s rule 1)."""
        return boto3.client("bedrock-agentcore")

    def _namespace(self, scope: str) -> str:
        """The namespace one of the two scopes resolves to.

        A *model* supplies this value, so an unrecognised one must be cheap: a warning and
        the repository's episodic namespace, never an exception out of a tool call.
        """
        if scope == "session":
            return session_namespace(self.config_id, self.task_id)
        if scope != "episodic":
            log.warning("unknown memory scope %r; searching the episodic namespace", scope)
        return episodic_namespace(self.config_id)

    def record(self, text: str, *, role: str = "ASSISTANT") -> bool:
        """Write one event to this repository's memory. `True` when it landed.

        `actorId` is the configuration id and `sessionId` is the task id. That pairing is
        deliverable D6 and it is the whole of the keying rule: it is what makes
        `/rca/<configId>/episodic` and `/rca/<configId>/session/<taskId>` resolve the way
        the module docstring says they do.
        """
        if not self.enabled:
            log.warning("no memory configured for task %r; dropping the record", self.task_id)
            return False
        body = text.strip()
        if not body:
            return False
        try:
            self._client().create_event(
                memoryId=self.memory_id,
                actorId=self.config_id,  # the CONFIGURATION id -- deliverable D6
                sessionId=self.task_id,  # the TASK id -- deliverable D6
                # An *aware* datetime: botocore serialises a naive one as if it were UTC,
                # and `datetime.now(UTC)` is the spelling `store.py` uses too.
                eventTimestamp=datetime.now(UTC),
                payload=[{"conversational": {"role": role, "content": {"text": body}}}],
            )
        except (ClientError, BotoCoreError) as exc:
            log.warning("could not record a memory event for task %s: %s", self.task_id, exc)
            return False
        return True

    def search(
        self, query: str, *, scope: str = "episodic", top_k: int = DEFAULT_TOP_K
    ) -> list[dict[str, Any]]:
        """Retrieve records from one of the two namespaces. `[]` when there is nothing to say.

        `[]` on a failure as well as on a miss, deliberately: the caller is a model reading
        the formatter's output, and the two are the same thing to it.
        """
        if not self.enabled:
            return []
        if not query.strip():
            return []
        try:
            response = self._client().retrieve_memory_records(
                memoryId=self.memory_id,
                namespace=self._namespace(scope),
                searchCriteria={"searchQuery": query, "topK": top_k},
            )
        except (ClientError, BotoCoreError) as exc:
            log.warning("could not search memory for task %s: %s", self.task_id, exc)
            return []
        return list(response.get("memoryRecordSummaries") or [])
