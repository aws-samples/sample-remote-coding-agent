"""The AgentCore Runtime entrypoint. Its whole job is to hand off in milliseconds.

**Never run the graph on the invocation thread** (invariant 1). AgentCore's runtime pings
the container to ask whether it is healthy; that ping is served on the same thread the
entrypoint returns to. A graph run takes minutes to hours, so an entrypoint that awaited
it blocks the ping thread, the ping thread stops reporting `HealthyBusy`, and AgentCore
reaps the session at fifteen minutes -- silently, mid-task, with no error anywhere and a
half-finished branch left behind. `add_async_task` is what tells the runtime the container
is deliberately busy; the `threading.Thread` is what keeps the entrypoint free to return.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from . import graph, store
from .state import TaskState

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

# *Wire* names, not graph-state names. The graph calls `prompt` `description` -- it is the
# name three role prompts and the commit subject interpolate -- and
# `graph._initial_state` is the one place the two spellings meet. The agreement with
# `api/src/handlers/dispatcher.py::_payload` is held by
# `tests/test_invoke_payload_contract.py` (invariant 13b): this constant demanded
# `description` while the dispatcher sent `prompt` for a whole phase, both suites stayed
# green, and every invocation that reached a container answered 500.
_REQUIRED = ("taskId", "configId", "repository", "prompt")


def _validate(payload: dict[str, Any]) -> None:
    missing = [field for field in _REQUIRED if not payload.get(field)]
    if missing:
        raise ValueError(f"the invocation payload is missing {', '.join(missing)}")


def _run(payload: dict[str, Any], async_task_id: int) -> None:
    """The graph, on its own thread. Every exit path completes the async task."""
    try:
        asyncio.run(graph.run_task(payload))
    except Exception as exc:  # the top of a thread: nothing above this frame would log it
        log.exception("task %s failed", payload.get("taskId"))
        # The status write is the only trace a human will find, so it happens even though
        # the graph's own instrumentation already tried: by here, that attempt is what
        # failed.
        #
        # Narrowed to the two attributes `store._key` reads rather than handing the whole
        # payload over: a raw payload is not a `TaskState`, because the wire calls the
        # description `prompt` and `graph._initial_state` is the one place that translation
        # happens (invariant 13b). Behaviour is unchanged -- `set_status` only ever read
        # these two keys off its first argument.
        #
        # `.get` rather than `[...]`: this is the one line that leaves a trace of a failed
        # run, so a `KeyError` raised inside this handler would lose it. An empty value
        # degrades to `store._key`'s own warn-and-skip instead.
        key: TaskState = {
            "taskId": str(payload.get("taskId") or ""),
            "configId": str(payload.get("configId") or ""),
        }
        store.set_status(key, "FAILED", extra={"error": str(exc)[:2000]})
    finally:
        # In `finally` and not after the try: an un-completed async task leaves the runtime
        # reporting HealthyBusy forever, which blocks the next dispatch to this container.
        app.complete_async_task(async_task_id)


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept a task and return. Returns in milliseconds; the work happens on a thread."""
    _validate(payload)
    # Registered *before* the thread starts, so there is no window in which the graph is
    # running and the runtime believes the container is idle.
    async_task_id = app.add_async_task("coding-task", {"taskId": payload["taskId"]})
    threading.Thread(
        target=_run,
        args=(payload, async_task_id),
        daemon=True,
        name=f"coding-task-{payload['taskId']}",
    ).start()
    return {"status": "accepted", "taskId": payload["taskId"]}


if __name__ == "__main__":
    app.run()
