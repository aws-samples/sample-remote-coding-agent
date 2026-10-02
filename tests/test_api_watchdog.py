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

# A tick that found nothing to do, in both halves. Spelled once because the shape is the
# whole point of asserting it: `lambda_handler` returns a *flat* count per half rather than
# only what it acted on, so "the sweep ran and this item is no longer listed" is
# distinguishable from "the sweep did not look".
_SWEPT_NOTHING = {"scanned": 0, "configurationsScanned": 0, "configurations": {}}


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

    assert summary == _SWEPT_NOTHING
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
    assert watchdog.lambda_handler({}, None) == _SWEPT_NOTHING
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

    assert watchdog.lambda_handler({}, None) == _SWEPT_NOTHING
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

    assert watchdog.lambda_handler({}, None) == _SWEPT_NOTHING
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
    already stopped nor a finished one that never had a session. `eee` and `fff` cover the
    two terminal statuses that carry sessions: a vetoed run belongs in the finished half,
    and a cancelled run already marked stopped belongs in neither."""
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


# -- the configuration half: a provisioning chain whose next message was lost ---------


def _seed_config(common: ModuleType, **overrides: Any) -> dict[str, Any]:
    """A configuration item stalled in `CREATING` for an hour, written straight into the
    table. An hour clears `CONFIG_STUCK_THRESHOLD_SECONDS` (2700s) without being so old
    that a test reads as being about age."""
    config: dict[str, Any] = {
        "configId": _CONFIG_ID,
        "repository": "owner/repo",
        "status": "CREATING",
        "agentRuntimeId": "rca_cfg_abc-XYZ",
        "createdAt": _ago(3600),
        "updatedAt": _ago(3600),
    }
    config.update(overrides)
    common.config_table.put_item(Item=config)
    return config


def _stored_config(common: ModuleType) -> dict[str, Any]:
    return common.config_table.get_item(Key={"configId": _CONFIG_ID})["Item"]


def _provisions(aws: Any) -> list[dict[str, Any]]:
    """Every message sitting on the *provision* queue, decoded. A nudge landing on the task
    queue instead would be an AccessDenied in production and silence here, so the two queues
    are read separately and never folded together."""
    received = aws.sqs.receive_message(
        QueueUrl=aws.provision_queue_url, MaxNumberOfMessages=10, VisibilityTimeout=0
    )
    return [json.loads(message["Body"]) for message in received.get("Messages", [])]


def test_the_config_threshold_is_derived_from_the_provisioners_own_patience(aws: Any) -> None:
    """The number that must not be guessed. `_check` polls `MAX_CHECK_ATTEMPTS` times
    `CHECK_DELAY_SECONDS` apart and then writes `FAILED` itself, so a *healthy* chain is
    never non-terminal for longer than that -- and a threshold below it would nudge a
    configuration that is merely slow, which is a duplicate message for no reason.

    Asserted as the relation and not as the literal, because the failure this prevents is
    somebody lowering `MAX_CHECK_ATTEMPTS` and leaving the threshold where it was.
    """
    _, watchdog = _load("watchdog")
    _, provisioner = _load("provisioner")

    ceiling = provisioner.MAX_CHECK_ATTEMPTS * provisioner.CHECK_DELAY_SECONDS
    assert ceiling < watchdog.CONFIG_STUCK_THRESHOLD_SECONDS, (
        f"the sweep must wait out the provisioner's own patience ({ceiling}s) before touching "
        f"a configuration, or it nudges chains that are working"
    )
    assert watchdog.CONFIG_STUCK_THRESHOLD_SECONDS - ceiling >= 300, (
        "at least one tick of slack on top, so a chain that is one poll from finishing is not "
        "raced by the sweep"
    )


def test_a_stranded_creating_configuration_is_re_driven_with_a_check(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this half exists for, end to end.

    The provisioner's chain is self-perpetuating, so a lost `CHECK` leaves a configuration in
    `CREATING` with nothing to redeliver -- observed live as twelve minutes of `CREATING` with
    an empty DLQ and no Lambda invocation, cleared by injecting one `CHECK` by hand. This is
    that hand, and the message body has to be the one the chain sends itself.
    """
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed_config(common)

    summary = watchdog.lambda_handler({}, None)

    assert summary["configurations"] == {"nudged": 1}
    assert _provisions(aws) == [{"phase": "CHECK", "configId": _CONFIG_ID, "attempt": 0}], (
        "the nudge goes through `provisioner.enqueue`, the single owner of the provision "
        "message shape, so it cannot drift from the message the chain sends itself"
    )
    stored = _stored_config(common)
    assert int(stored["provisionNudges"]) == 1
    assert stored["lastNudgeAt"]
    assert stored["status"] == "CREATING", "a nudge re-drives the chain; it does not judge it"


def test_a_configuration_still_inside_the_window_is_left_alone(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provisioning takes minutes and the threshold is 2700s, so the common case is a chain
    that is simply working. Nudging it would be a duplicate `GetAgentRuntime` for
    every configuration being created, on every tick."""
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed_config(common, updatedAt=_ago(600))

    summary = watchdog.lambda_handler({}, None)

    assert summary["configurationsScanned"] == 1, "listed, so the filter is not what excused it"
    assert summary["configurations"] == {"working": 1}
    assert _provisions(aws) == []
    assert "provisionNudges" not in _stored_config(common)


def test_a_ready_configuration_is_never_scanned(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`READY` and `FAILED` are terminal: the chain finished, and an old `updatedAt` on a
    working repository must not look like a stall."""
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    for config_id, status in (("cfg_ready000000", "READY"), ("cfg_failed00000", "FAILED")):
        common.config_table.put_item(
            Item={"configId": config_id, "status": status, "updatedAt": _ago(30 * 24 * 3600)}
        )

    assert watchdog._scan_configurations() == []
    assert watchdog.lambda_handler({}, None)["configurationsScanned"] == 0


def test_a_stalled_deleting_configuration_is_re_driven_with_a_delete(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`DELETING`'s driver is `DELETE`, not `CHECK`, and sending the wrong one is silent:
    `provisioner._check` returns early for a `DELETING` configuration (invariant 23), so a
    `CHECK` here would be a message that does nothing forever.

    Re-sending `DELETE` is safe because `provisioner._delete` is idempotent by construction --
    every removal tolerates an already-gone resource, and it ends with the item itself.
    """
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed_config(common, status="DELETING")

    summary = watchdog.lambda_handler({}, None)

    assert summary["configurations"] == {"nudged": 1}
    assert _provisions(aws) == [{"phase": "DELETE", "configId": _CONFIG_ID, "attempt": 0}], (
        "a CHECK here would be a message `provisioner._check` returns early from, forever"
    )


def test_a_stalled_pending_configuration_is_failed_rather_than_re_created(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one arm that must NOT be re-driven, and the reason is a resource leak.

    `PENDING`'s driver is `CREATE`; `create_agent_runtime` takes no `clientToken`, so a
    `CREATE` re-sent after a runtime was created but its id never stored would mint a second
    AgentCore runtime that no configuration item names and nothing tears down -- it would
    just keep costing money. `FAILED` is the answer instead, because `POST /configurations`
    retires a `FAILED` item and registers a replacement (invariant 22), where a `PENDING` one
    has no route out at all.
    """
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed_config(common, status="PENDING", agentRuntimeId="")

    summary = watchdog.lambda_handler({}, None)

    assert summary["configurations"] == {"failed": 1}
    assert _provisions(aws) == [], "a CREATE here risks a second runtime nothing owns"
    stored = _stored_config(common)
    assert stored["status"] == "FAILED"
    assert "PENDING" in stored["statusReason"]
    assert "register" in stored["statusReason"].lower(), "the reason names the operator's way out"


def test_the_nudges_are_bounded_and_then_the_configuration_leaves_the_scan(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Convergence, which is the property invariant 28 names for the task half: a branch that
    only logged would log the same stuck configuration every five minutes forever.

    At the cap the sweep resolves it once, and the count is what takes it out of
    `_scan_configurations`' filter -- so the tick after that finds nothing, rather than
    finding it again and declining to act.
    """
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed_config(common, provisionNudges=watchdog.MAX_CONFIG_NUDGES)

    summary = watchdog.lambda_handler({}, None)

    assert summary["configurations"] == {"exhausted": 1}
    stored = _stored_config(common)
    assert stored["status"] == "FAILED"
    assert str(watchdog.MAX_CONFIG_NUDGES) in stored["statusReason"]
    assert _provisions(aws) == [], "no message at the cap: the chain has had its chances"

    assert watchdog._scan_configurations() == [], "out of the filter, not logged forever"
    assert watchdog.lambda_handler({}, None)["configurationsScanned"] == 0


def test_an_exhausted_deleting_configuration_keeps_its_status(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The asymmetry in `_fail_config`, and invariant 22 is why: a `FAILED` configuration is
    retired and replaced by the next registration, so flipping a `DELETING` one to `FAILED`
    would offer to retire a teardown that is already in flight. It gains a reason an operator
    can read and keeps its status; the nudge count at its cap is what stops the repetition.
    """
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed_config(common, status="DELETING", provisionNudges=watchdog.MAX_CONFIG_NUDGES)

    assert watchdog.lambda_handler({}, None)["configurations"] == {"exhausted": 1}

    stored = _stored_config(common)
    assert stored["status"] == "DELETING", "invariant 22: a teardown in flight is not retired"
    assert "DELETING" in stored["statusReason"]
    assert watchdog._scan_configurations() == []


def test_two_concurrent_ticks_produce_one_nudge(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same claim shape as `_claim_resume`: both ticks read the same count, both try to
    write count + 1, and the second one's condition fails -- so one lost message costs one
    replacement and never two."""
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed_config(common)

    stale = watchdog._scan_configurations()
    assert len(stale) == 1
    monkeypatch.setattr(watchdog, "_scan_configurations", lambda: [dict(item) for item in stale])

    first = watchdog.lambda_handler({}, None)
    second = watchdog.lambda_handler({}, None)

    assert first["configurations"] == {"nudged": 1}
    assert second["configurations"] == {"raced": 1}
    assert int(_stored_config(common)["provisionNudges"]) == 1
    assert len(_provisions(aws)) == 1, "one stall, one replacement message"


def test_a_configuration_that_moved_underneath_the_sweep_is_not_nudged(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status pin in the claim, which is not redundant with the count pin: a chain that
    reached `READY` between the scan and the write has just proved it is alive, so the nudge
    is stale and the duplicate message is exactly what the pin exists to avoid."""
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    stale = dict(_seed_config(common))
    common.config_table.put_item(Item={**stale, "status": "READY", "updatedAt": _ago(0)})

    assert watchdog._sweep_configuration(stale, datetime.now(tz=UTC)) == "raced"
    assert _provisions(aws) == []
    assert _stored_config(common)["status"] == "READY"


def test_a_configuration_with_no_readable_timestamp_is_not_judged(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declining to judge, rather than nudging blind: with no clock there is no way to tell a
    stalled chain from one that started a second ago, and the safe answer for a *mutating*
    re-drive is to do nothing and say so."""
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed_config(common, createdAt="not a timestamp", updatedAt="")

    assert watchdog.lambda_handler({}, None)["configurations"] == {"unreadable": 1}
    assert _provisions(aws) == []


def test_the_config_sweep_runs_even_when_the_task_sweep_raises(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranded configuration is what stops any task being started at all, so the half that
    unsticks it must not be gated on the health of the half that sweeps tasks."""
    common, watchdog = _load("watchdog")
    _data(monkeypatch, common)
    _seed(common, activity=[{"at": _ago(90 * 60)}])
    _seed_config(common)
    monkeypatch.setattr(watchdog, "_sweep_task", lambda *_: 1 / 0)

    summary = watchdog.lambda_handler({}, None)

    assert summary["error"] == 1, "the task half reported its own failure"
    assert summary["configurations"] == {"nudged": 1}, "and the configuration half still ran"
    assert len(_provisions(aws)) == 1
