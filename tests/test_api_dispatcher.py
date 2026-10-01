"""`dispatcher.py`: the task queue's consumer and its dead-letter handler.

Every test here is a real incident. SQS is at-least-once, so a redelivery that invokes a
second container gives one task two branches and two pull requests; a dispatch that fails
after claiming strands the task at `DISPATCHED`; a *resume* that releases its claim turns
a crash into an unbounded loop; and a dead-letter handler that raises redrives the
dead-letter queue itself, so the failure of a run is recorded nowhere.

AgentCore is exercised through `FakeAgentCore`, never moto: moto's `bedrock-agentcore`
backend answers "Not yet implemented" for `stop_runtime_session` and does not validate
parameters, so it would happily accept the raw 24-character task id as a
`runtimeSessionId` that the real botocore rejects client-side. moto still owns DynamoDB
and SQS here. Nothing in this file imports a handler module at import time --
`boto3.resource("dynamodb")` raises `NoRegionError` at construction, which would be a
collection error rather than a test failure.
"""

from __future__ import annotations

import json
from typing import Any

import invoke_payload
import pytest
from botocore.exceptions import ClientError
from test_api_fakes import FakeAgentCore, client_error, install
from test_api_fakes import load_handlers as _load

CONFIG_ID = "cfg_0123456789ab"
TASK_ID = "tsk_1700000000000_abcdef"
RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/rca_cfg_0123456789ab-ABC123"
)
# A stored timestamp no code path under test would ever write, so "the item was not
# touched" is an assertion rather than a hope.
SEEDED_AT = "2026-01-01T00:00:00.000Z"


def _seed_config(common: Any) -> dict[str, Any]:
    """A configuration written straight into the table: this file must not import
    `configurations.py`, which another unit owns."""
    config = {
        "configId": CONFIG_ID,
        "repository": "owner/repo",
        "repositoryUrl": "https://github.com/owner/repo",
        "status": "READY",
        "defaultBranch": "main",
        "verifyCommand": "make test",
        "setupCommand": "make setup",
        # The container's only route to a GitHub token: it is read by `nodes/checkout.py`
        # and `nodes/submit.py`, so the payload test asserts it is forwarded.
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeId": "rca_cfg_0123456789ab-ABC123",
        "createdAt": SEEDED_AT,
        "updatedAt": SEEDED_AT,
    }
    common.config_table.put_item(Item=config)
    return config


def _seed_task(
    common: Any, *, task_id: str = TASK_ID, status: str = "QUEUED", **overrides: Any
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "configId": CONFIG_ID,
        "taskId": task_id,
        "prompt": "add a healthcheck",
        "status": status,
        "sessionId": f"{task_id}-seeded",
        "agentRuntimeArn": RUNTIME_ARN,
        "resumeCount": 0,
        "activity": [],
        "steps": [],
        "source": "api",
        "createdAt": SEEDED_AT,
        "updatedAt": SEEDED_AT,
    }
    task.update(overrides)
    common.tasks_table.put_item(Item=task)
    return task


def _item(common: Any, task_id: str = TASK_ID) -> dict[str, Any]:
    return common.tasks_table.get_item(Key={"configId": CONFIG_ID, "taskId": task_id})["Item"]


def _record(
    *,
    task_id: str = TASK_ID,
    config_id: str = CONFIG_ID,
    resume: bool = False,
    raw: str | None = None,
) -> dict[str, Any]:
    return {
        "messageId": "00000000-0000-0000-0000-000000000000",
        "eventSource": "aws:sqs",
        "body": json.dumps({"taskId": task_id, "configId": config_id, "resume": resume})
        if raw is None
        else raw,
    }


def _event(**kwargs: Any) -> dict[str, Any]:
    return {"Records": [_record(**kwargs)]}


def _sent(data: FakeAgentCore) -> dict[str, Any]:
    """The payload of the last `InvokeAgentRuntime`, decoded."""
    return json.loads(data.last("InvokeAgentRuntime")["payload"])


def test_dispatch_claims_queued_and_a_redelivery_invokes_once(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The at-least-once defence. Without the conditional `QUEUED -> DISPATCHED` claim a
    redelivered message invokes a second container on the same session, which means two
    branches and two pull requests for one task."""
    common, dispatcher = _load("dispatcher")
    _seed_config(common)
    _seed_task(common)
    data = FakeAgentCore("bedrock-agentcore")
    install(monkeypatch, common, data=data)

    dispatcher.lambda_handler(_event(), None)

    claimed = _item(common)
    assert claimed["status"] == "DISPATCHED"
    assert claimed["dispatchedAt"] > SEEDED_AT
    assert len(data.calls_to("InvokeAgentRuntime")) == 1
    assert _sent(data)["prompt"] == "add a healthcheck"

    dispatcher.lambda_handler(_event(), None)  # the same message, delivered twice

    assert len(data.calls_to("InvokeAgentRuntime")) == 1, "the lost claim invoked nothing"
    assert _item(common)["dispatchedAt"] == claimed["dispatchedAt"], "and wrote nothing"


def test_a_transient_error_releases_the_claim_and_raises(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed task whose invoke fails must go back to `QUEUED` *and* the exception must
    escape: without the release the task strands at `DISPATCHED` with no container behind
    it, and without the raise SQS deletes the message and the run is lost silently."""
    common, dispatcher = _load("dispatcher")
    _seed_config(common)
    _seed_task(common)
    data = FakeAgentCore(
        "bedrock-agentcore",
        errors={
            "InvokeAgentRuntime": [
                client_error(
                    "ThrottlingException",
                    "rate exceeded",
                    status=429,
                    operation="InvokeAgentRuntime",
                )
            ]
        },
    )
    install(monkeypatch, common, data=data)

    with pytest.raises(ClientError):
        dispatcher.lambda_handler(_event(), None)

    assert _item(common)["status"] == "QUEUED"

    # The point of the release: the redelivery SQS now makes can claim the task again.
    dispatcher.lambda_handler(_event(), None)
    assert _item(common)["status"] == "DISPATCHED"
    assert len(data.calls_to("InvokeAgentRuntime")) == 2


def test_a_failed_resume_is_never_released(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume carries no claim of its own -- the watchdog already claimed the task and
    pinned `resumeCount` -- so a failed resume writes nothing at all and rides the redrive
    policy to the DLQ. Releasing it to `QUEUED` would make a crash loop unbounded."""
    common, dispatcher = _load("dispatcher")
    _seed_config(common)
    _seed_task(common, status="CODING", resumeCount=1, lastResumeAt=SEEDED_AT)
    data = FakeAgentCore(
        "bedrock-agentcore",
        errors={
            "InvokeAgentRuntime": [
                client_error(
                    "InternalServerException", "boom", status=500, operation="InvokeAgentRuntime"
                )
            ]
        },
    )
    install(monkeypatch, common, data=data)

    with pytest.raises(ClientError):
        dispatcher.lambda_handler(_event(resume=True), None)

    after = _item(common)
    assert after["status"] == "CODING", "a resume neither claims nor releases"
    assert after["resumeCount"] == 1, "the watchdog's pin is untouched"
    assert after["updatedAt"] == SEEDED_AT, "the item was not written at all"
    assert _sent(data)["resume"] is True


def test_a_resume_does_not_claim(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: a *successful* resume also leaves the status alone. Re-claiming
    would either fail (the status is not `QUEUED`) or re-open the door the watchdog
    closed."""
    common, dispatcher = _load("dispatcher")
    _seed_config(common)
    _seed_task(common, status="VERIFYING", resumeCount=2)
    data = FakeAgentCore("bedrock-agentcore")
    install(monkeypatch, common, data=data)

    dispatcher.lambda_handler(_event(resume=True), None)

    after = _item(common)
    assert after["status"] == "VERIFYING"
    assert after["updatedAt"] == SEEDED_AT
    assert len(data.calls_to("InvokeAgentRuntime")) == 1


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED_VERIFICATION", "CANCELLED"])
def test_a_late_resume_of_a_finished_task_invokes_nothing(
    aws: Any, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """The claim covers a redelivered dispatch; a resume has no claim, so the terminal
    check is the only thing between an old resume message and a container started on a run
    that already succeeded.

    `FAILED_VERIFICATION` and `CANCELLED` were missing from `runs.TERMINAL_STATUSES`, so a
    vetoed or cancelled run was non-terminal both to `_dispatch`'s `runs.is_terminal` guard
    and to `_not_already_finished()`'s chain of `<>` comparators: a redelivered message
    started a container on a run that was already over.
    """
    common, dispatcher = _load("dispatcher")
    _seed_config(common)
    _seed_task(common, status=status)
    data = FakeAgentCore("bedrock-agentcore")
    install(monkeypatch, common, data=data)

    dispatcher.lambda_handler(_event(resume=True), None)
    dispatcher.lambda_handler(_event(), None)

    assert data.calls == []
    assert _item(common)["status"] == status


def test_the_session_id_is_the_one_owner_derivation(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 18: one derivation, in `observability`. A fresh id would give a resumed
    task a second session -- a second workspace, a second branch, a second pull request --
    and the raw task id is 24 characters, under botocore's client-side 33-character
    minimum for `runtimeSessionId`."""
    common, dispatcher, observability = _load("dispatcher", "observability")
    _seed_config(common)
    _seed_task(common)
    data = FakeAgentCore("bedrock-agentcore")
    install(monkeypatch, common, data=data)

    dispatcher.lambda_handler(_event(), None)

    recorded = data.last("InvokeAgentRuntime")["runtimeSessionId"]
    assert recorded == observability.session_id(TASK_ID)
    assert len(recorded) >= observability.SESSION_ID_MIN_LENGTH >= 33
    assert recorded != TASK_ID
    assert _sent(data)["sessionId"] == recorded, "the container is told the same session"


def test_the_qualifier_is_the_default_endpoint(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant 14: `create_agent_runtime` creates DEFAULT itself and nothing in this
    repository creates another endpoint, so DEFAULT is the only qualifier there is."""
    common, dispatcher, observability = _load("dispatcher", "observability")
    _seed_config(common)
    _seed_task(common)
    data = FakeAgentCore("bedrock-agentcore")
    install(monkeypatch, common, data=data)

    dispatcher.lambda_handler(_event(), None)

    sent = data.last("InvokeAgentRuntime")
    assert sent["qualifier"] == observability.ENDPOINT_NAME == "DEFAULT"
    assert sent["agentRuntimeArn"] == RUNTIME_ARN
    assert sent["contentType"] == "application/json" and sent["accept"] == "application/json"
    assert data.operations() == ["InvokeAgentRuntime"], "the dispatcher reaches one operation"


def test_the_payload_is_the_contract_the_agent_reads(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatcher's half of the cross-boundary agreement: the container is handed
    everything it needs, so it never needs a grant to go looking.

    The key set is asserted against `invoke_payload.FIELDS`, the one definition of the
    shape, and `tests/test_invoke_payload_contract.py` holds the container's half to the
    same module -- so a field renamed here, or added here and read by nothing, fails a test
    rather than a deployment (invariant 13b)."""
    common, dispatcher = _load("dispatcher")
    config = _seed_config(common)
    task = _seed_task(common)
    data = FakeAgentCore("bedrock-agentcore")
    install(monkeypatch, common, data=data)

    dispatcher.lambda_handler(_event(), None)

    sent = _sent(data)
    assert set(sent) == set(invoke_payload.FIELDS)
    assert sent["taskId"] == TASK_ID and sent["configId"] == CONFIG_ID
    assert sent["prompt"] == task["prompt"]
    for key in (
        "repository",
        "repositoryUrl",
        "defaultBranch",
        "verifyCommand",
        "setupCommand",
    ):
        assert sent[key] == config[key]
    assert sent["resume"] is False


def test_a_message_with_nothing_to_dispatch_is_dropped_not_raised(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task or configuration that is gone is finished work: redelivering the message
    cannot make either reappear, so raising would only burn the redrive budget. A
    malformed body is dropped for the same reason."""
    common, dispatcher = _load("dispatcher")
    data = FakeAgentCore("bedrock-agentcore")
    install(monkeypatch, common, data=data)

    dispatcher.lambda_handler(_event(), None)  # no task, no config
    _seed_task(common)
    dispatcher.lambda_handler(_event(), None)  # a task whose config is gone
    dispatcher.lambda_handler({"Records": [_record(raw="{not json")]}, None)
    dispatcher.lambda_handler({"Records": [_record(raw=json.dumps({"resume": True}))]}, None)
    dispatcher.lambda_handler({}, None)

    assert data.calls == []
    assert _item(common)["status"] == "QUEUED", "the claim is never taken without a config"


def test_the_dlq_handler_sets_failed_and_never_raises(aws: Any) -> None:
    """The terminal owner of `FAILED`, and the place a run's failure stops. An exception
    out of here returns the message to the *dead-letter* queue, which has no redrive of
    its own, so the failure would be recorded nowhere at all."""
    common, dispatcher, runs = _load("dispatcher", "runs")
    _seed_config(common)
    _seed_task(common, status="DISPATCHED", dispatchedAt=SEEDED_AT)
    _seed_task(common, task_id="tsk_1700000000001_beefed", status="SUCCEEDED")

    dispatcher.dlq_handler(
        {
            "Records": [
                _record(raw="}not json{"),
                _record(raw=json.dumps({"configId": CONFIG_ID})),
                _record(raw=json.dumps("a string is not a message")),
                _record(task_id="tsk_1700000000002_c0ffee"),  # no such task
                _record(task_id="tsk_1700000000001_beefed"),  # already terminal
                _record(),
            ]
        },
        None,
    )

    failed = _item(common)
    assert failed["status"] == "FAILED"
    assert failed["statusReason"] == dispatcher.DISPATCH_FAILED_REASON
    assert failed["finishedAt"] > SEEDED_AT
    assert runs.is_terminal(failed["status"])

    # An already-terminal task keeps the status it earned: a run that actually succeeded
    # must not be rewritten as a failure because its message was dead-lettered.
    finished = _item(common, "tsk_1700000000001_beefed")
    assert finished["status"] == "SUCCEEDED"
    assert "statusReason" not in finished and "finishedAt" not in finished

    # `update_item` creates an absent item, so `attribute_exists(taskId)` is what stops a
    # dead-lettered message resurrecting a deleted task as a FAILED ghost.
    assert "Item" not in common.tasks_table.get_item(
        Key={"configId": CONFIG_ID, "taskId": "tsk_1700000000002_c0ffee"}
    )


def test_the_dlq_handler_survives_a_record_that_raises(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `try` is per record, not around the loop: one unreadable or unwritable record
    must not stop the rest of the batch being resolved."""
    common, dispatcher = _load("dispatcher")
    _seed_task(common)
    calls: list[str] = []
    real_update = common.tasks_table.update_item

    def flaky(**kwargs: Any) -> Any:
        calls.append(str(kwargs["Key"]["taskId"]))
        if len(calls) == 1:
            raise client_error(
                "ProvisionedThroughputExceededException", "slow down", operation="UpdateItem"
            )
        return real_update(**kwargs)

    monkeypatch.setattr(common.tasks_table, "update_item", flaky)
    dispatcher.dlq_handler(
        {"Records": [_record(task_id="tsk_1700000000003_dead"), _record()]}, None
    )

    assert len(calls) == 2, "the second record was still attempted"
    assert _item(common)["status"] == "FAILED"
