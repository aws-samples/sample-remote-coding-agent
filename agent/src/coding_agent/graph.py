"""The `checkout -> plan -> code -> verify -> review -> submit` graph and its wiring.

Two things live here and nowhere else: the routing decision after `review`
(`_after_review`), and the per-node instrumentation that turns a node visit into a
DynamoDB status write and a pair of activity rows (`_instrument`).

`_after_review` is also the **only** reader of `verifyResult["passed"]` in the tree.
`nodes/submit.py`, the PR body and `state.terminal_status` all key on `verified`
instead. That split is what makes the veto one-way (invariant 6): the node holding
the exit code has no verdict to apply, the node holding the verdict cannot re-run
the command, and the router reading `passed` cannot write it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from . import activity, agentcore_memory, checkpoint, config, store
from .nodes.checkout import checkout
from .nodes.code import code
from .nodes.plan import plan
from .nodes.review import review
from .nodes.submit import submit
from .nodes.verify import verify
from .state import MAX_ITERATIONS, TaskState, TaskStatus

log = logging.getLogger(__name__)


class NodeFn(Protocol):
    """What a node is: a coroutine function taking the state and returning an update.

    A `Protocol` rather than a `Callable[[TaskState], ...]` alias because LangGraph's own
    node protocol declares `__call__(self, state: ...)` with the parameter **named**, so it
    may pass `state` as a KEYWORD -- and a `Callable` alias describes a positional-only
    parameter, which is why every `add_node` call matched no overload. Every node in
    `nodes/**` already names its parameter `state`.

    The return stays `dict[str, Any]` and deliberately not `TaskState`: an update may carry
    `roleCosts`, which `_instrument` pops and which is not a `TaskState` key, so a node's
    return really is wider than the state.
    """

    def __call__(self, state: TaskState) -> Awaitable[dict[str, Any]]: ...


# Every observable transition is written to DynamoDB (invariant 13). Adding a node is
# FOUR edits across THREE deployment units: this table, a `state.TaskStatus` literal,
# `watchdog.RESUMABLE_STATUSES` + `cards._STEP_COLUMN` in `api/`, and `web/js/status.js`
# in the console -- and the console one is parsed line by line by its own test, so its
# formatting is load-bearing. `review`/`REVIEWING` is the worked example.
# FOLLOW-UP (phases 5/6): the `api/` and `web/` halves of that list. `api/` belongs to
# another card this wave, so the statuses this phase introduces are named in the PR
# description instead of edited in.
_NODE_STATUS: Mapping[str, TaskStatus] = {
    "checkout": "CHECKING_OUT",
    "plan": "PLANNING",
    "code": "CODING",
    "verify": "VERIFYING",
    "review": "REVIEWING",
    "submit": "SUBMITTING",
}

# The container's half of the invocation-payload contract, and the only place it is
# written down. Wire name -> `TaskState` key. `prompt` becomes `description` because that
# is the graph's own name for it -- it reaches three role prompts and the commit subject --
# while `prompt` is the public field of `POST /tasks` and the stored Tasks attribute. The
# dispatcher sent `prompt` while `server._REQUIRED` demanded `description` for a whole
# phase and both suites stayed green (invariant 13b);
# `tests/test_invoke_payload_contract.py` is what stops that recurring.
PAYLOAD_TO_STATE: Mapping[str, str] = {
    "taskId": "taskId",
    "configId": "configId",
    "repository": "repository",
    "prompt": "description",
}

# Read when present. `cloneSource`/`baseBranch` are an override channel nothing sends
# today -- see the declaration tables in tests/test_invoke_payload_contract.py.
OPTIONAL_PAYLOAD_FIELDS: tuple[str, ...] = (
    "secretArn",
    "cloneSource",
    "baseBranch",
    "verifyCommand",
    "setupCommand",
)

DEFAULT_NODES: Mapping[str, NodeFn] = {
    "checkout": checkout,
    "plan": plan,
    "code": code,
    "verify": verify,
    "review": review,
    "submit": submit,
}

# LangGraph counts supersteps, and the worst-case *iteration* is six node visits, not
# four: plan, code, verify, review, plus the one permitted verify/review re-run that
# `iteration` deliberately does not count (invariant 6c). So:
#
#   checkout (1) + MAX_ITERATIONS * 6 + submit (1) + margin (2) == 22
#
# The margin is not decoration. LangGraph's own recursion guard firing first would
# replace a draft PR -- the whole point of the unverified path -- with an opaque
# `GraphRecursionError` that says nothing about the task, so the limit must never be
# what stops the loop. `tests/test_graph.py` recomputes this arithmetic independently.
_VISITS_PER_ITERATION = 6
_MARGIN = 2
RECURSION_LIMIT = 1 + MAX_ITERATIONS * _VISITS_PER_ITERATION + 1 + _MARGIN

# One memory event per node visit is the whole budget, and `RECURSION_LIMIT` above is
# already what bounds it: 22 visits in the worst run there is, therefore at most 22
# `CreateEvent` calls. That is why there is no counter here -- the ceiling is derived from
# `MAX_ITERATIONS` two lines up and `tests/test_graph.py` recomputes the arithmetic.
#
# Bounded at 500 rather than the 2000 the `FAILED` status write allows, and for a second
# reason on top of DynamoDB's 400 KB item cap: a memory record is *prompt surface*. A
# future task on this repository retrieves these through `memory_tools`' search tool and
# pastes them into a role's context, so a node that pasted a command-output tail in here
# would spend that run's tokens re-reading this run's noise.
_MEMORY_TEXT_LIMIT = 500


def _memory_note(
    name: str,
    elapsed: float,
    *,
    state: TaskState,
    update: Mapping[str, Any] | None = None,
    error: str = "",
) -> str:
    """One line saying what a node visit did, for this repository's memory.

    `.get(...)` throughout, on the update and on the state, and never subscription: this
    line is observability, and a `KeyError` raised while composing it would be a node that
    did its whole job and then died describing it. Every branch degrades to the base line,
    which already names the node and its elapsed time.
    """
    if error:
        # The failure path has no update to read. The node name and the exception text are
        # the whole signal, and they are the same two things the `FAILED` status carries.
        return f"{name} failed after {elapsed:.1f}s: {error}"[:_MEMORY_TEXT_LIMIT]

    produced = update or {}
    note = f"{name} finished in {elapsed:.1f}s"
    if name == "verify":
        # `commandResult`, NOT `verifyResult`. `nodes/review.py` is the only writer of the
        # verdict (invariant 6), so the `verifyResult` on the state during a `verify` visit
        # still describes the *previous* measurement -- the same staleness `_after_review`
        # checks `reviewRerun` before `passed` to avoid. What this node knows is the command
        # it ran and the exit code it got back, and `exitCode: None` means nothing ran.
        cr = produced.get("commandResult") or {}
        note += (
            f": ran {cr.get('command') or '(no command configured)'} -> exit {cr.get('exitCode')}"
        )
        warning = cr.get("warning")
        if warning:
            note += f" ({warning})"
    elif name == "review":
        # The verdict, and both halves of it: `passed` is what `_after_review` reads and
        # `verified` is what decides a draft pull request. A line carrying only one of them
        # would be unreadable on exactly the runs worth remembering -- a run with no command
        # configured is `passed` and *not* `verified` (invariant 6).
        vr = produced.get("verifyResult") or {}
        note += (
            f": verdict passed={bool(vr.get('passed'))} verified={bool(vr.get('verified'))}"
            f" on {vr.get('command') or '(no command)'}"
        )
        summary = (vr.get("summary") or "").strip()
        if summary:
            note += f" -- {summary}"
    elif name == "submit":
        # `verified` off the *state*, because `submit` reads `verifyResult` and does not
        # write it; `prUrl` off the update, because that is the one thing this node made.
        vr = state.get("verifyResult") or {}
        note += f": verified={bool(vr.get('verified'))}, pr {produced.get('prUrl') or '(none)'}"
    return note[:_MEMORY_TEXT_LIMIT]


def _record_memory(
    name: str,
    elapsed: float,
    state: TaskState,
    *,
    update: Mapping[str, Any] | None = None,
    error: str = "",
) -> None:
    """Write one memory event for this node visit, or reach nothing at all.

    The `.enabled` guard comes before the composition and it is the whole point of the
    function. `TaskMemory.record` logs a warning and returns `False` when the task has no
    memory, so calling it unconditionally would put one warning per node visit into every
    local run and every repository registered before its memory resource existed -- up to
    22 lines of noise per run, hiding the one warning that means something. A task with no
    `memoryId` makes no call, builds no client, and says nothing.

    No `scope` and no `namespace`: `record` keys the event `actorId=configId` and
    `sessionId=taskId` (D6) and the memory resource's two strategies are what fan that one
    event out to the repository-shared and the per-session namespace. And no `try`/`except`:
    `agentcore_memory` already degrades every AWS failure to a `log.warning` and `False`
    (D11), exactly as `store.set_status` and `activity.record` do on these same two paths,
    and a second swallow here would only hide which layer dropped the write.
    """
    memory = agentcore_memory.TaskMemory.for_state(state)
    if not memory.enabled:
        return
    memory.record(_memory_note(name, elapsed, state=state, update=update, error=error))


def _instrument(name: str, fn: NodeFn) -> NodeFn:
    """Wrap a node so its visit is observable, and so a crash says which node crashed.

    It writes the node's status and appends the step through `store.set_status`, emits
    a start and a finish activity row, and times the node. It never writes `status`
    into the *graph state*: `submit` owns the terminal status, and a wrapper that also
    wrote one would be a second writer of the field the console shows. It also records one
    memory event per visit through `agentcore_memory.TaskMemory`, so a node's outcome
    reaches this repository's memory whether or not a role thought to write it -- and, like
    the status and the activity rows, that write never reaches the returned update.
    """

    async def instrumented(state: TaskState) -> dict[str, Any]:
        status = _NODE_STATUS[name]
        store.set_status(state, status, step=name)
        activity.record(state, activity.node_started(state, name))
        started = time.monotonic()
        try:
            update = await fn(state)
        except Exception as exc:  # re-raised below; this is only the log-and-record site
            elapsed = time.monotonic() - started
            log.exception("node %s failed after %.1fs", name, elapsed)
            # Bounded: an exception message can carry a whole command output tail, and
            # DynamoDB items are capped at 400 KB.
            store.set_status(state, "FAILED", extra={"error": f"{name}: {exc}"[:2000]})
            activity.record(state, activity.node_finished(state, name, elapsed))
            _record_memory(name, elapsed, state, error=str(exc))
            raise
        elapsed = time.monotonic() - started
        activity.record(state, activity.node_finished(state, name, elapsed))

        # The node's `ResultMessage` accounting, already tagged with this node's name by
        # `subagents.cost_row`. **Popped**, so it never reaches the graph state and never
        # reaches the checkpoint: cost is observability, not state, and leaving it in
        # would put a growing list into every checkpoint a resume has to read back.
        #
        # Success path only. The failure path above has already written `FAILED` with the
        # error, and a node that raised has no update to pop anything off.
        merged = dict(update or {})
        rows = merged.pop("roleCosts", None)
        if rows:
            store.record_cost(state, rows)
            activity.record(state, activity.node_cost(name, rows))
        # Observability of the same kind as the two writes above, and held to the same rule:
        # it reaches DynamoDB and AgentCore Memory and it does NOT reach the returned update,
        # so nothing here lands in the graph state or in the checkpoint a resume reads back.
        _record_memory(name, elapsed, state, update=merged)
        return {**merged, "currentNode": name}

    return instrumented


def _after_review(state: TaskState) -> str:
    """Where to go after `review`. The order of these three checks is the whole design.

    `reviewRerun` is checked **first** because on the hop `verifyResult` still
    describes the *previous* measurement -- the review role edited the tree after it,
    so reading `passed` there would route on a stale exit code.
    """
    if state.get("reviewRerun"):
        return "verify"
    if (state.get("verifyResult") or {}).get("passed"):
        return "submit"
    if state.get("iteration", 0) < MAX_ITERATIONS:
        # Back to `plan`, never to `code`: a failed attempt needs a new spec, and
        # re-running the coder against the spec that just failed is how a loop burns
        # its whole budget producing the same diff three times.
        return "plan"
    # Out of iterations. Still `submit` -- as a draft, because `verified` is false.
    return "submit"


def build_graph(
    *,
    nodes: Mapping[str, NodeFn] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    instrument: bool = True,
) -> CompiledStateGraph:
    """Compile the graph, optionally with some nodes replaced.

    `nodes` merges over `DEFAULT_NODES`, which is the whole offline test seam: every
    routing test builds the real graph out of stub coroutines that append to a visit
    list, so it needs neither Bedrock, nor git, nor a filesystem.
    """
    resolved: dict[str, NodeFn] = {**DEFAULT_NODES, **(nodes or {})}
    builder: StateGraph = StateGraph(TaskState)
    for name, fn in resolved.items():
        builder.add_node(name, _instrument(name, fn) if instrument else fn)

    builder.set_entry_point("checkout")
    builder.add_edge("checkout", "plan")
    builder.add_edge("plan", "code")
    builder.add_edge("code", "verify")
    builder.add_edge("verify", "review")
    builder.add_conditional_edges(
        "review",
        _after_review,
        {"verify": "verify", "plan": "plan", "submit": "submit"},
    )
    builder.add_edge("submit", END)
    return builder.compile(checkpointer=checkpointer)


def graph_config(task_id: str) -> RunnableConfig:
    """The per-task runnable config. `thread_id` is the task id, so the checkpoint a
    resume reads back is the one this task wrote."""
    return {"configurable": {"thread_id": task_id}, "recursion_limit": RECURSION_LIMIT}


def _initial_state(payload: dict[str, Any]) -> TaskState:
    """The cold-start state. Only what the dispatcher knows; every other field is a
    node's output.

    The wire-name-to-state-name translation happens here and nowhere else, off
    `PAYLOAD_TO_STATE`. Subscripted rather than `.get()`, so a payload that somehow got
    past `server._validate` raises `KeyError` here rather than putting `""` into a role
    prompt.
    """
    state: TaskState = {
        **{key: payload[wire] for wire, key in PAYLOAD_TO_STATE.items()},  # type: ignore[typeddict-item]
        "iteration": 0,
        "reviewRerun": False,
        # Never read off the payload: `run_task` re-asserts `resumed` from the checkpoint
        # it finds, so a redelivered payload has no say either way -- see the comment at
        # tests/test_checkpoint.py:355.
        "resumed": False,
    }
    for optional in OPTIONAL_PAYLOAD_FIELDS:
        if payload.get(optional):
            state[optional] = payload[optional]  # type: ignore[literal-required]
    return state


async def run_task(payload: dict[str, Any]) -> TaskState:
    """Run one task to completion, cold or resumed.

    The checkpoint path is derived from `WORK_ROOT` and `taskId` *before* `checkout`
    runs, because the checkpointer has to exist before the first node does.
    """
    cfg = config.load_config()
    task_id = payload["taskId"]
    work_dir = cfg.work_root / task_id
    saver = checkpoint.FileCheckpointer(checkpoint.checkpoint_path({"workDir": str(work_dir)}))
    graph = build_graph(checkpointer=saver)
    run_config = graph_config(task_id)

    # The one place the memory id is resolved. Off the configuration item this container
    # is already allowed to read -- `GetItem` on `CONFIG_TABLE`, invariant 31's only
    # permitted read -- and never in a node (that would be one `GetItem` per node visit
    # for a value that cannot change mid-task) and never in `build_options`, which is
    # pure. `resolve_memory_id` returns "" when there is no table, when the repository has
    # no memory resource, and when the read itself fails (D11).
    memory_id = agentcore_memory.resolve_memory_id(payload["configId"])

    try:
        if saver.get_tuple(run_config) is not None:
            # A resume. `resumed` has to be re-asserted on the resuming run: a
            # `checkout` that re-runs would otherwise quarantine the `.agent/`
            # directory *we ourselves wrote* on the first attempt, destroying the spec
            # and the memory the resume exists to reuse.
            log.info("task %s has a checkpoint; resuming rather than starting cold", task_id)
            # The memory id is re-asserted here too, and deliberately: a task whose first
            # attempt predates the repository's memory resource picks it up on the resume.
            # Only when truthy -- absence is the signal `TaskMemory.for_state` reads, and a
            # falsy `memoryId` in the checkpoint would be a second spelling of the same thing.
            update: dict[str, Any] = {"resumed": True}
            if memory_id:
                update["memoryId"] = memory_id
            await graph.aupdate_state(run_config, update)
            # `ainvoke` is typed `dict[str, Any] | Any`, because a compiled graph's output
            # schema is decided at runtime. This one was compiled from
            # `StateGraph(TaskState)`, so the mapping it returns *is* the task state.
            return cast("TaskState", await graph.ainvoke(None, run_config))
        state = _initial_state(payload)
        if memory_id:
            state["memoryId"] = memory_id
        return cast("TaskState", await graph.ainvoke(state, run_config))
    except GraphRecursionError:
        # Invariant 12: this must never be how a run ends. If it is, the derivation of
        # RECURSION_LIMIT no longer matches the graph's shape -- most likely a new node
        # or a new edge back into the loop -- and the symptom is a lost draft PR.
        log.error(
            "task %s hit LangGraph's recursion limit of %d (invariant 12): the limit is "
            "derived from MAX_ITERATIONS=%d at %d visits per iteration, so the graph's "
            "shape has changed and the derivation has not",
            task_id,
            RECURSION_LIMIT,
            MAX_ITERATIONS,
            _VISITS_PER_ITERATION,
        )
        raise


def resume_path(work_root: Path, task_id: str) -> Path:
    """Where a task's checkpoint lives. Exposed so an operator tool can find it without
    reconstructing the layout."""
    return checkpoint.checkpoint_path({"workDir": str(work_root / task_id)})
