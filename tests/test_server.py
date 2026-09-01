"""The one thing this file pins is invariant 1: the graph never runs on the invocation thread.

AgentCore serves its health ping on the thread the entrypoint returns to. An entrypoint
that awaited a multi-hour graph blocks that ping, the runtime stops seeing `HealthyBusy`,
and the session is reaped at fifteen minutes -- silently, mid-task, with a half-finished
branch left behind. So the tests below assert a *real* second thread: `threading.Thread` is
never patched away, only `graph.run_task` and the two `app` bookkeeping calls are.

Nothing here binds a port. Importing `server` constructs a `BedrockAgentCoreApp` at module
level, and that is inert by inspection of the installed SDK: `__init__` builds three
Starlette routes, attaches a log handler and registers an OTel span processor. The worker
event loop is created lazily by `_ensure_worker_loop`, which only the ASGI request path
reaches, and the socket is opened by `run()`, which only `__main__` calls.
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass, field
from typing import Any

import invoke_payload
import pytest
from agent_helpers import no_aws
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from coding_agent import server

# Every wait in this file is bounded. A test that hangs on a graph thread hangs the whole
# xdist worker, which reads as a stuck CI job rather than as a failed assertion.
_TIMEOUT = 10.0


def _payload(task_id: str) -> dict[str, Any]:
    """A complete invocation payload: the four required fields and nothing optional.

    Built from `invoke_payload`, which is the one definition of the shape the dispatcher
    writes -- so a field renamed on either side of the boundary fails a test here rather
    than a deployment (invariant 13b).
    """
    return invoke_payload.required_only(taskId=task_id)


def _new_threads(before: set[threading.Thread]) -> list[threading.Thread]:
    """Threads alive now that were not alive when `before` was taken.

    Compared by identity rather than by name so these tests do not depend on how
    `server` names its thread -- a thread that exists at all is the assertion.
    """
    return [thread for thread in threading.enumerate() if thread not in before]


def _drain(before: set[threading.Thread]) -> None:
    """Join every thread the entrypoint started, with a bound. Always called in `finally`."""
    for pending in _new_threads(before):
        pending.join(timeout=_TIMEOUT)


@dataclass
class _AppRecorder:
    """Stand-in for the two `app` calls invariant 1 turns on, recording their order.

    Patched onto the module-level `app` instance rather than the class, so the real
    runtime's task bookkeeping is never touched and one test cannot see another's tasks.
    """

    ASYNC_TASK_ID = 4242

    events: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    metadata: list[dict[str, Any]] = field(default_factory=list)
    completed: list[int] = field(default_factory=list)
    finished: threading.Event = field(default_factory=threading.Event)

    def add_async_task(self, name: str, metadata: dict[str, Any] | None = None) -> int:
        self.events.append("add_async_task")
        self.names.append(name)
        self.metadata.append(dict(metadata or {}))
        return self.ASYNC_TASK_ID

    def complete_async_task(self, async_task_id: int) -> bool:
        self.events.append("complete_async_task")
        self.completed.append(async_task_id)
        # Set last: a test waiting on this event is waiting for the whole of `_run`'s
        # `finally` to have happened.
        self.finished.set()
        return True

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server.app, "add_async_task", self.add_async_task)
        monkeypatch.setattr(server.app, "complete_async_task", self.complete_async_task)


@dataclass
class _StoreRecorder:
    """Stand-in for `server.store`: records the status write instead of reaching DynamoDB."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def set_status(
        self,
        state: dict[str, Any],
        status: str,
        *,
        step: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append(
            {"state": dict(state), "status": status, "step": step, "extra": dict(extra or {})}
        )


def test_the_entrypoint_returns_before_the_graph_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant 1: `invoke` returns while the graph is still running, on a thread of its own."""
    no_aws(monkeypatch)
    task_id = "task-handoff"
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    async def blocked_run_task(payload: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        assert release.wait(timeout=_TIMEOUT), "the test never released the graph"
        finished.set()
        return {}

    monkeypatch.setattr(server.graph, "run_task", blocked_run_task)
    before = set(threading.enumerate())
    started: list[threading.Thread] = []

    try:
        accepted = server.invoke(_payload(task_id))
        assert accepted == {"status": "accepted", "taskId": task_id}
        assert entered.wait(timeout=_TIMEOUT)
        # `release` is still clear and only the graph sets `finished`, after waiting on it.
        # So the entrypoint returned with the graph demonstrably still mid-run.
        assert not finished.is_set()

        started = _new_threads(before)
        assert len(started) == 1, f"expected exactly one graph thread, got {started}"
        # Daemon, so a reaped container's interpreter is never held open by a graph that
        # will lose its session storage anyway.
        assert started[0].daemon
        assert started[0].is_alive()
        # And the runtime is being told the container is busy for as long as it runs.
        assert server.app.get_async_task_info()["active_count"] >= 1
    finally:
        release.set()
        _drain(before)

    assert finished.is_set()
    assert not started[0].is_alive()


def test_the_async_task_is_registered_before_the_thread_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`add_async_task` happens before the graph's first instruction: no window where the graph runs and the runtime believes the container is idle."""
    no_aws(monkeypatch)
    task_id = "task-order"
    recorder = _AppRecorder()
    recorder.install(monkeypatch)

    async def recorded_run_task(payload: dict[str, Any]) -> dict[str, Any]:
        recorder.events.append("graph")
        return {}

    monkeypatch.setattr(server.graph, "run_task", recorded_run_task)
    before = set(threading.enumerate())

    try:
        accepted = server.invoke(_payload(task_id))
        assert accepted["status"] == "accepted"
        assert recorder.finished.wait(timeout=_TIMEOUT)
    finally:
        _drain(before)

    assert recorder.events == ["add_async_task", "graph", "complete_async_task"]
    assert recorder.names == ["coding-task"]
    # The metadata is how an operator reading `/ping` debug output learns *which* task is
    # holding the container busy.
    assert recorder.metadata == [{"taskId": task_id}]
    assert recorder.completed == [_AppRecorder.ASYNC_TASK_ID]


@pytest.mark.parametrize(
    "crashes", [False, True], ids=["a-graph-that-returns", "a-graph-that-raises"]
)
def test_every_exit_path_completes_the_async_task(
    monkeypatch: pytest.MonkeyPatch, crashes: bool
) -> None:
    """Both exit paths complete the async task: an un-completed one leaves the runtime HealthyBusy forever and blocks the next dispatch to this container."""
    no_aws(monkeypatch)
    task_id = f"task-exit-{crashes}"
    recorder = _AppRecorder()
    recorder.install(monkeypatch)
    monkeypatch.setattr(server, "store", _StoreRecorder())

    async def maybe_crashing_run_task(payload: dict[str, Any]) -> dict[str, Any]:
        if crashes:
            raise RuntimeError("the clone failed")
        return {"taskId": payload["taskId"]}

    monkeypatch.setattr(server.graph, "run_task", maybe_crashing_run_task)
    before = set(threading.enumerate())

    try:
        assert server.invoke(_payload(task_id))["taskId"] == task_id
        assert recorder.finished.wait(timeout=_TIMEOUT)
    finally:
        _drain(before)

    assert recorder.events[-1] == "complete_async_task"
    assert recorder.completed == [_AppRecorder.ASYNC_TASK_ID]


def test_a_crash_is_recorded_as_a_failed_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph that dies on the task thread still writes FAILED with a bounded error: that write is the only trace a human will find."""
    no_aws(monkeypatch)
    task_id = "task-crash"
    recorder = _AppRecorder()
    recorder.install(monkeypatch)
    store = _StoreRecorder()
    monkeypatch.setattr(server, "store", store)

    # Longer than the cap on purpose: an exception message can carry a whole command
    # output tail, and a DynamoDB item is capped at 400 KB.
    message = "the clone failed: " + "x" * 5000

    async def failing_run_task(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(message)

    monkeypatch.setattr(server.graph, "run_task", failing_run_task)
    before = set(threading.enumerate())

    try:
        server.invoke(_payload(task_id))
        assert recorder.finished.wait(timeout=_TIMEOUT)
    finally:
        _drain(before)

    assert [call["status"] for call in store.calls] == ["FAILED"]
    assert store.calls[0]["state"]["taskId"] == task_id
    error = store.calls[0]["extra"]["error"]
    assert error.startswith("the clone failed: ")
    assert len(message) > 2000
    assert len(error) <= 2000


@pytest.mark.parametrize("value", [None, ""], ids=["missing", "empty"])
@pytest.mark.parametrize("field_name", invoke_payload.REQUIRED)
def test_an_incomplete_payload_is_rejected_before_any_thread_starts(
    monkeypatch: pytest.MonkeyPatch, field_name: str, value: str | None
) -> None:
    """An incomplete payload raises `ValueError` naming the field, before a thread or an async task exists to leak."""
    no_aws(monkeypatch)
    recorder = _AppRecorder()
    recorder.install(monkeypatch)

    async def forbidden_run_task(payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("the graph must not run for an invalid payload")

    monkeypatch.setattr(server.graph, "run_task", forbidden_run_task)

    payload = _payload("task-invalid")
    if value is None:
        del payload[field_name]
    else:
        payload[field_name] = value

    before = set(threading.enumerate())
    try:
        with pytest.raises(ValueError, match=field_name):
            server.invoke(payload)
    finally:
        _drain(before)

    assert recorder.events == []
    assert _new_threads(before) == []


def test_the_entrypoint_is_registered_with_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """`invoke` is the app's `main` handler, and importing the module neither serves nor binds a port."""
    no_aws(monkeypatch)
    # `BedrockAgentCoreApp.entrypoint` stores the function in `handlers["main"]`, which is
    # what `_handle_invocation` looks up. Asserted here so a lost decorator fails as one
    # test rather than as a deployed container answering every invocation with
    # `{"error": "No entrypoint defined"}`.
    assert isinstance(server.app, BedrockAgentCoreApp)
    assert server.app.handlers["main"] is server.invoke

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("importing server must never start the HTTP server")

    monkeypatch.setattr(BedrockAgentCoreApp, "run", _explode)
    before = {thread.name for thread in threading.enumerate()}

    reloaded = importlib.reload(server)

    assert reloaded.app.handlers["main"] is reloaded.invoke
    # `app.run()` is guarded by `__main__`, and the SDK's own worker event loop is created
    # lazily by the request path -- so a plain import starts nothing.
    appeared = {thread.name for thread in threading.enumerate()} - before
    assert not [
        name
        for name in appeared
        if "agentcore" in name or "uvicorn" in name or name.startswith("coding-task")
    ], appeared
