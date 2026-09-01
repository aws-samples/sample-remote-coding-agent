"""`watchdog.py`: the 15-minute liveness sweep.

Everything here is seeded straight into the tables and read back from them, because the
watchdog's whole contract is which attribute it reads (never `updatedAt`) and which it
writes (`resumeCount`, `lastResumeAt`, `stoppedAt`, a terminal `status`). AgentCore is
the fake, not moto: moto's `stop_runtime_session` answers "Not yet implemented", so a
test written against it would prove the opposite of what it claims.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest
from test_api_fakes import FakeAgentCore, client_error, install
from test_api_fakes import load_handlers as _load

_CONFIG_ID = "cfg_0123456789ab"
_TASK_ID = "tsk_1700000000_abc"
# `observability.session_id(_TASK_ID)`'s shape, written out: >= 33 characters, because
# botocore refuses a shorter `runtimeSessionId` client-side and the fake refuses it too.
_SESSION_ID = "tsk_1700000000_abc-0123456789abcdef0123456789abcdef"
_RUNTIME_ARN = "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/rca_cfg_abc-XYZ"


def _ago(seconds: float) -> str:
    """A timestamp `seconds` in the past, in `_common.now_iso`'s format."""
    stamp = datetime.now(tz=UTC) - timedelta(seconds=seconds)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _seed(common: ModuleType, **overrides: Any) -> dict[str, Any]:
    """A task item as `runs.queue_run` writes it, three hours old, straight into the
    table -- `runs.py` and its tests are another unit's, and this file must not depend on
    the route that created the run."""
    task: dict[str, Any] = {
        "configId": _CONFIG_ID,
        "taskId": _TASK_ID,
        "prompt": "add a regression test",
        "status": "CODING",
        "sessionId": _SESSION_ID,
        "agentRuntimeArn": _RUNTIME_ARN,
        "resumeCount": 0,
        "activity": [],
        "steps": [],
        "source": "api",
        "createdAt": _ago(3 * 3600),
        "updatedAt": _ago(3 * 3600),
        "dispatchedAt": _ago(3 * 3600),
    }
    task.update(overrides)
    common.tasks_table.put_item(Item=task)
    return task


def _stored(common: ModuleType) -> dict[str, Any]:
    return common.tasks_table.get_item(Key={"configId": _CONFIG_ID, "taskId": _TASK_ID})["Item"]


def _dispatches(aws: Any) -> list[dict[str, Any]]:
    """Every message sitting on the task queue, decoded."""
    received = aws.sqs.receive_message(QueueUrl=aws.task_queue_url, MaxNumberOfMessages=10)
    return [json.loads(message["Body"]) for message in received.get("Messages", [])]


def _data(monkeypatch: pytest.MonkeyPatch, common: ModuleType, **kwargs: Any) -> FakeAgentCore:
    fake = FakeAgentCore("bedrock-agentcore", **kwargs)
    install(monkeypatch, common, data=fake)
    return fake


def test_the_quiet_threshold_clears_the_verify_timeout(aws: Any) -> None:
    """Invariant 27. `verify` makes no model call, so a healthy verification is silent
    for up to `VERIFY_TIMEOUT` (1800s); a threshold at or below that re-invokes a task
    that is working."""
    _, watchdog = _load("watchdog")
    assert watchdog.QUIET_THRESHOLD_SECONDS == 2700
    assert watchdog.QUIET_THRESHOLD_SECONDS > 1800
    assert watchdog.MAX_RESUMES == 3
    assert isinstance(watchdog.RESUMABLE_STATUSES, frozenset)


def test_cancelled_at_is_in_the_scan_projection(aws: Any) -> None:
    """Invariant 28: drop `cancelledAt` or `stoppedAt` from the projection and the branch
    that reads it can never fire, so the sweep silently stops resolving cancellations."""
    _, watchdog = _load("watchdog")
    for name in (
        "cancelledAt",
        "stoppedAt",
        "activity",
        "resumeCount",
        "lastResumeAt",
        "sessionId",
        "agentRuntimeArn",
        "dispatchedAt",
        "createdAt",
    ):
        assert name in watchdog._SCAN_PROJECTION, f"{name}'s branch would be dead code"
    assert "#st" in watchdog._SCAN_PROJECTION, "`status` is a DynamoDB reserved word"
    assert "updatedAt" not in watchdog._SCAN_PROJECTION, "invariant 13: not the liveness clock"


def test_liveness_comes_from_the_feed_not_updated_at(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 13. `updatedAt` is *now* and the feed is an hour old: through `updatedAt`
    a 40-minute coding node and a dead container are indistinguishable, which is exactly
    why activity writes skip that field."""
    common, watchdog = _load("watchdog")
    data = _data(monkeypatch, common)
    _seed(
        common,
        status="CODING",
        updatedAt=_ago(0),
        activity=[{"at": _ago(60 * 60), "kind": "tool", "text": "Edit api/src/handlers/runs.py"}],
    )

    assert watchdog.lambda_handler({}, None)["resumed"] == 1

    stored = _stored(common)
    assert int(stored["resumeCount"]) == 1
    assert stored["lastResumeAt"], "the resume is recorded where the next tick will read it"
    assert _dispatches(aws) == [{"taskId": _TASK_ID, "configId": _CONFIG_ID, "resume": True}]
    # A resume remounts the *same* session, so the session is not stopped on the way.
    assert data.operations() == []


def test_a_healthy_silent_verification_is_not_resumed(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """25 minutes of silence is what a passing test suite looks like from outside."""
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed(
        common,
        status="VERIFYING",
        activity=[{"at": _ago(25 * 60), "kind": "node", "text": "verify"}],
    )

    summary = watchdog.lambda_handler({}, None)

    assert summary["scanned"] == 1
    assert summary.get("resumed", 0) == 0
    assert int(_stored(common)["resumeCount"]) == 0
    assert _dispatches(aws) == []


def test_two_concurrent_ticks_produce_one_resume(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both ticks read the same `resumeCount`, so the pin in the claim's condition is
    what makes exactly one of them win."""
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed(common, activity=[{"at": _ago(90 * 60)}])

    stale = watchdog._scan()
    assert len(stale) == 1
    # Two ticks overlapping: the second one is working from the scan the first one took,
    # which is exactly the state a 15-minute schedule with a slow sweep produces.
    monkeypatch.setattr(watchdog, "_scan", lambda: [dict(item) for item in stale])

    first = watchdog.lambda_handler({}, None)
    second = watchdog.lambda_handler({}, None)

    assert first["resumed"] == 1
    assert second.get("resumed", 0) == 0
    assert second["raced"] == 1
    assert int(_stored(common)["resumeCount"]) == 1
    assert len(_dispatches(aws)) == 1, "one resume, one dispatch message"


def test_a_resume_is_itself_a_liveness_signal(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The feed is an hour old but the sweep resumed this task five minutes ago: the
    container is owed a whole quiet window to answer before another tick acts."""
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    task = _seed(common, activity=[{"at": _ago(60 * 60)}], resumeCount=1, lastResumeAt=_ago(5 * 60))

    assert watchdog.lambda_handler({}, None).get("resumed", 0) == 0
    assert int(_stored(common)["resumeCount"]) == 1
    assert _dispatches(aws) == []
    assert watchdog._last_seen(task) == common.parse_iso(task["lastResumeAt"])


def test_the_liveness_clock_falls_back_to_dispatched_at_then_created_at(aws: Any) -> None:
    """A run that has not written a row yet is timed from its dispatch, and one that
    never reached the dispatcher from its creation. An unparseable row is skipped, not
    raised -- the feed is written by a container and is best-effort by design."""
    common, watchdog = _load("watchdog")
    dispatched, created = _ago(600), _ago(1200)

    assert watchdog._last_seen(
        {"activity": [], "dispatchedAt": dispatched, "createdAt": created}
    ) == (common.parse_iso(dispatched))
    assert watchdog._last_seen({"createdAt": created}) == common.parse_iso(created)
    assert watchdog._last_seen({"activity": [{"at": "not a timestamp"}, "junk"]}) is None


def test_resume_exhaustion_fails_the_task_naming_the_reason(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhaustion is a FAILED task with a reason an operator can read, never a silent
    give-up that leaves a run sitting at `CODING` forever."""
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed(common, resumeCount=3, activity=[{"at": _ago(2 * 3600)}])

    assert watchdog.lambda_handler({}, None)["failed"] == 1

    stored = _stored(common)
    assert stored["status"] == "FAILED"
    assert "2700" in stored["statusReason"]
    assert "3 of 3" in stored["statusReason"]
    assert stored["finishedAt"]
    assert _dispatches(aws) == [], "an exhausted task is not dispatched a fourth time"


def test_an_unknown_status_is_left_alone_rather_than_resumed(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status vocabulary is pinned duplication across the api/agent boundary, so a
    node the agent renamed must degrade -- not crash, and not resume blind.

    Two lines of defence, and this asserts both: the scan's live half *is*
    `RESUMABLE_STATUSES`, so an unknown status is never listed, and `_sweep_task` leaves it
    alone if it is handed one anyway.
    """
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    task = _seed(common, status="CLONING", activity=[{"at": _ago(2 * 3600)}])

    summary = watchdog.lambda_handler({}, None)

    assert summary == {"scanned": 0}
    assert _stored(common)["status"] == "CLONING"
    assert _dispatches(aws) == []
    assert watchdog._sweep_task(task, datetime.now(tz=UTC)) == "idle"
    assert int(_stored(common)["resumeCount"]) == 0


def test_a_cancelled_task_is_resolved_to_failed_not_skipped(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 28. The task is live (it wrote a row a minute ago), so only the operator's
    `cancelledAt` resolves it -- and resolving is what stops it being re-listed and
    re-logged on every tick forever."""
    common, watchdog = _load("watchdog")
    data = _data(monkeypatch, common)
    _seed(common, status="CODING", cancelledAt=_ago(30), activity=[{"at": _ago(60)}])

    assert watchdog.lambda_handler({}, None)["cancelled"] == 1

    stored = _stored(common)
    assert stored["status"] == "FAILED"
    assert stored["statusReason"] == "cancelled by operator"
    assert stored["stoppedAt"], "the session goes with the cancellation"
    assert data.operations() == ["StopRuntimeSession"]

    # Resolved, not skipped: the next sweep has nothing left to say about this task.
    assert watchdog._scan() == []
    assert watchdog.lambda_handler({}, None) == {"scanned": 0}
    assert len(data.calls_to("StopRuntimeSession")) == 1
    assert _dispatches(aws) == []


def test_a_cancelled_run_is_stopped_rather_than_re_failed(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CANCELLED` is one of the container's terminal statuses, so an item that already
    carries it takes step 2 of the sweep and not step 1: the `cancelledAt` branch is guarded
    by `not runs.is_terminal(status)`, so the sweep resolves this task by stopping its
    session and writing `stoppedAt` and leaves the status the agent itself wrote alone --
    it does not overwrite it with `FAILED` or compose a reason of its own."""
    common, watchdog = _load("watchdog")
    data = _data(monkeypatch, common)
    _seed(common, status="CANCELLED", cancelledAt=_ago(30), activity=[{"at": _ago(10)}])

    summary = watchdog.lambda_handler({}, None)

    assert summary["stopped"] == 1
    assert summary.get("cancelled", 0) == 0, "already cancelled: nothing left to resolve"

    stored = _stored(common)
    assert stored["status"] == "CANCELLED", "the container's own spelling survives the sweep"
    assert "statusReason" not in stored, "the sweep composed no verdict of its own"
    assert stored["stoppedAt"], "resolved by the mark the next scan filters on"
    assert data.operations() == ["StopRuntimeSession"]

    # Resolved, not re-listed: `stoppedAt` takes it out of the finished half of the filter.
    assert watchdog._scan() == []


def test_a_vetoed_run_has_its_session_stopped(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The billing regression this whole change exists for. Before `FAILED_VERIFICATION`
    was in `runs.TERMINAL_STATUSES` this task matched *neither* half of `_scan`'s filter --
    not the live half (it is not in `RESUMABLE_STATUSES`) and not the finished half -- so it
    was never listed, nobody ever called `StopRuntimeSession` on it, and its AgentCore
    session billed idle compute until the runtime's own `MAX_LIFETIME_SECONDS` expired."""
    common, watchdog = _load("watchdog")
    data = _data(monkeypatch, common)
    _seed(common, status="FAILED_VERIFICATION", activity=[{"at": _ago(10)}])

    assert watchdog.lambda_handler({}, None)["stopped"] == 1

    stop = data.last("StopRuntimeSession")
    assert stop["runtimeSessionId"] == _SESSION_ID
    assert stop["agentRuntimeArn"] == _RUNTIME_ARN
    assert stop["qualifier"] == "DEFAULT", "invariant 14: there is never a custom endpoint"
    assert _stored(common)["stoppedAt"]

    assert watchdog.lambda_handler({}, None) == {"scanned": 0}
    assert len(data.calls_to("StopRuntimeSession")) == 1


def test_a_quiet_vetoed_run_is_stopped_and_never_resumed(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog must never resume a terminal status, asserted through behaviour: three
    hours of silence is far past `QUIET_THRESHOLD_SECONDS`, and the sweep still only stops
    the session. A vetoed run is *finished*, so re-invoking the container would start a
    second run on a verdict some other writer already reached (invariants 6 and 6b)."""
    common, watchdog, runs = _load("watchdog", "runs")
    _data(monkeypatch, common)
    _seed(common, status="FAILED_VERIFICATION", activity=[{"at": _ago(3 * 3600)}])

    summary = watchdog.lambda_handler({}, None)

    assert summary["stopped"] == 1
    assert summary.get("resumed", 0) == 0
    assert _dispatches(aws) == []

    stored = _stored(common)
    assert int(stored["resumeCount"]) == 0
    assert stored["status"] == "FAILED_VERIFICATION", "the sweep composed no verdict of its own"
    # The structural half of the same invariant: the two sets cannot overlap, so no status
    # is ever both terminal and a candidate for re-invocation.
    assert watchdog.RESUMABLE_STATUSES.isdisjoint(runs.TERMINAL_STATUSES)


def test_a_terminal_task_is_stopped_once_and_then_stops_being_listed(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop is what stops idle compute being billed, and the `stoppedAt` it writes is
    what makes the sweep converge: two sweeps, exactly one `StopRuntimeSession`."""
    common, watchdog = _load("watchdog")
    data = _data(monkeypatch, common)
    _seed(common, status="SUCCEEDED", activity=[{"at": _ago(10)}])

    assert watchdog.lambda_handler({}, None)["stopped"] == 1

    stop = data.last("StopRuntimeSession")
    assert stop["runtimeSessionId"] == _SESSION_ID
    assert len(stop["runtimeSessionId"]) >= 33
    assert stop["agentRuntimeArn"] == _RUNTIME_ARN
    assert stop["qualifier"] == "DEFAULT", "invariant 14: there is never a custom endpoint"
    assert _stored(common)["stoppedAt"]

    assert watchdog.lambda_handler({}, None) == {"scanned": 0}
    assert len(data.calls_to("StopRuntimeSession")) == 1


def test_a_409_retryable_conflict_is_retried(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 409 `RetryableConflictException` is *expected*: it is what a session answers
    while it provisions or tears down. Treating it as fatal leaves a session running."""
    common, watchdog = _load("watchdog")
    data = _data(
        monkeypatch,
        common,
        errors={
            "StopRuntimeSession": [
                client_error(
                    "RetryableConflictException",
                    "the session is still provisioning",
                    status=409,
                    operation="StopRuntimeSession",
                )
            ]
        },
    )
    slept: list[float] = []
    monkeypatch.setattr(watchdog.time, "sleep", slept.append)
    _seed(common, status="FAILED", activity=[{"at": _ago(10)}])

    assert watchdog.lambda_handler({}, None)["stopped"] == 1

    assert len(data.calls_to("StopRuntimeSession")) == 2
    assert slept == [1], "bounded backoff, and the test never actually waits"
    assert _stored(common)["stoppedAt"]


def test_a_session_that_is_already_gone_is_success_and_still_converges(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ResourceNotFoundException` is the state the call was asking for, so `stoppedAt` is
    still written -- otherwise the task is re-listed and re-stopped every 15 minutes."""
    common, watchdog = _load("watchdog")
    data = _data(
        monkeypatch,
        common,
        errors={
            "StopRuntimeSession": [
                client_error(
                    "ResourceNotFoundException",
                    "no such session",
                    status=404,
                    operation="StopRuntimeSession",
                )
            ]
        },
    )
    _seed(common, status="SUCCEEDED_UNVERIFIED", activity=[{"at": _ago(10)}])

    assert watchdog.lambda_handler({}, None)["stopped"] == 1
    assert _stored(common)["stoppedAt"]
    assert len(data.calls_to("StopRuntimeSession")) == 1
    assert watchdog._scan() == []


def test_the_scan_lists_the_two_halves_and_nothing_else(aws: Any) -> None:
    """A live task, a finished one still holding a session, and neither a finished one
    already stopped nor a finished one that never had a session. `eee` and `fff` are the two
    statuses the api's vocabulary used to be missing: a vetoed run holding a session belongs
    in the finished half, and a cancelled one already marked stopped belongs in neither."""
    common, watchdog = _load("watchdog")
    for task_id, item in {
        "tsk_1700000000_aaa": {"status": "CODING", "sessionId": _SESSION_ID},
        "tsk_1700000000_bbb": {"status": "SUCCEEDED", "sessionId": _SESSION_ID},
        "tsk_1700000000_ccc": {
            "status": "SUCCEEDED",
            "sessionId": _SESSION_ID,
            "stoppedAt": _ago(60),
        },
        "tsk_1700000000_ddd": {"status": "FAILED"},
        "tsk_1700000000_eee": {"status": "FAILED_VERIFICATION", "sessionId": _SESSION_ID},
        "tsk_1700000000_fff": {
            "status": "CANCELLED",
            "sessionId": _SESSION_ID,
            "stoppedAt": _ago(60),
        },
    }.items():
        common.tasks_table.put_item(
            Item={"configId": _CONFIG_ID, "taskId": task_id, "createdAt": _ago(3600), **item}
        )

    assert sorted(task["taskId"] for task in watchdog._scan()) == [
        "tsk_1700000000_aaa",
        "tsk_1700000000_bbb",
        "tsk_1700000000_eee",
    ]
