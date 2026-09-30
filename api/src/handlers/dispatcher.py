"""The dispatch Lambda: the task queue's consumer, and the DLQ handler that is the one
place a run's failure stops.

Both entry points are **undecorated** -- they answer a queue, not an HTTP request, and
an SQS event has no `requestContext` for `@handler` to log. `tests/test_api_route_contract.py`
asserts that mechanically, along with this module declaring no `ROUTES`.

Three things here are the expensive lessons and are commented as such:

* SQS is **at-least-once**, so a dispatch is a conditional `QUEUED -> DISPATCHED` claim
  (`_claim`) and a lost condition is a logged no-op. Without the claim a redelivered
  message invokes a second container against the same session, which means two branches
  and two pull requests for one task.
* Any exception out of the invoke **releases** a dispatch claim (`_release`, conditional
  `DISPATCHED -> QUEUED`) and re-raises so SQS redelivers; without the release the task
  strands at `DISPATCHED` until the watchdog notices it 45 minutes later. A **resume**
  claim is never released: the watchdog already claimed it and pinned `resumeCount`, and
  releasing it would make a crash loop unbounded, so a failed resume just re-raises and
  rides the redrive policy to the DLQ.
* `dlq_handler` is the terminal owner that sets `FAILED`, and it **never raises**: an
  exception out of a dead-letter consumer redrives the dead-letter queue itself, and the
  one job of this function is to be the place a run's failure stops.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from boto3.dynamodb.conditions import Attr, ConditionBase
from botocore.exceptions import ClientError

from . import _common, observability, runs
from ._common import LOG

# `runs` owns the task vocabulary; there is no single-status constant to import, so the
# one status this module writes is spelled here and `runs.TERMINAL_STATUSES` is what
# decides whether writing it is still allowed.
FAILED = "FAILED"
DISPATCH_FAILED_REASON = "dispatch failed after retries"

# Only what a dispatch is actually made of. Projected rather than read whole because
# `activity` grows to hundreds of rows on a long run and none of it is dispatch input.
_TASK_PROJECTION = "configId, taskId, prompt, #st, agentRuntimeArn"

# `status` is a DynamoDB reserved word, so every expression that names it goes through a
# placeholder. Always passed as `dict(_STATUS)`: boto3 builds the placeholders for a
# `ConditionExpression` object by *mutating* the `ExpressionAttributeNames` it is given,
# and a shared dict would accumulate a `#n0` that the next call's expressions never use --
# which DynamoDB rejects as a `ValidationException`.
_STATUS = {"#st": "status"}


def _message(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The parsed message body, or `None` for one that is not a JSON object.

    Dropped rather than raised: a redelivery cannot make a malformed body parse, and the
    exception would take the rest of the batch with it.
    """
    try:
        parsed = json.loads(record.get("body") or "{}")
    except ValueError:
        LOG.exception("a dispatch message was not JSON; dropping it")
        return None
    if not isinstance(parsed, dict):
        LOG.error("a dispatch message was not a JSON object; dropping it")
        return None
    return parsed


def _task_and_config(task_id: str, config_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The projected task and its configuration, or `None` when either is gone.

    A dispatch for a task or a configuration that no longer exists is finished work:
    there is nothing to invoke, the runtime and role went with the configuration, and
    redelivering cannot make either reappear. The task item -- if it is the
    configuration that vanished -- is left where the watchdog will resolve it, rather
    than failed from here: `dlq_handler` is the only writer of `FAILED`.
    """
    found = _common.tasks_table.get_item(
        Key={"configId": config_id, "taskId": task_id},
        ProjectionExpression=_TASK_PROJECTION,
        ExpressionAttributeNames=dict(_STATUS),
    ).get("Item")
    if found is None:
        LOG.warning("dispatch for task %s config %s: no such task, dropped", task_id, config_id)
        return None
    config = _common.config_table.get_item(Key={"configId": config_id}).get("Item")
    if config is None:
        LOG.warning("dispatch for task %s: config %s is gone, dropped", task_id, config_id)
        return None
    return found, config


def _claim(task: Mapping[str, Any]) -> bool:
    """Take the task from `QUEUED` to `DISPATCHED`, or report that something else did.

    This is the whole at-least-once defence: two deliveries of one message race here and
    exactly one of them wins the condition. A `ConditionalCheckFailedException` is
    therefore expected traffic, not an error -- it means the task is already dispatched
    (or already finished), and the right response is to invoke nothing.
    """
    task_id = str(task["taskId"])
    try:
        _common.tasks_table.update_item(
            Key={"configId": task["configId"], "taskId": task_id},
            UpdateExpression="SET #st = :dispatched, dispatchedAt = :now, updatedAt = :now",
            ExpressionAttributeNames=dict(_STATUS),
            ExpressionAttributeValues={":dispatched": runs.DISPATCHED, ":now": _common.now_iso()},
            ConditionExpression=Attr("taskId").exists() & Attr("status").eq(runs.QUEUED),
        )
    except ClientError as err:
        if not _common.is_condition_failure(err):
            raise
        LOG.info("task %s was not QUEUED any more; this delivery invokes nothing", task_id)
        return False
    return True


def _release(task: Mapping[str, Any]) -> None:
    """Hand a claimed task back for redelivery: conditional `DISPATCHED -> QUEUED`.

    Called from the invoke's `except` branch, so it swallows everything it can: raising
    here would replace the exception that says *why* the dispatch failed, which is the
    one worth reading in CloudWatch. A release that cannot be written leaves the task
    `DISPATCHED` and quiet, which is exactly the state the watchdog resumes.
    """
    task_id = str(task["taskId"])
    try:
        _common.tasks_table.update_item(
            Key={"configId": task["configId"], "taskId": task_id},
            UpdateExpression="SET #st = :queued, updatedAt = :now",
            ExpressionAttributeNames=dict(_STATUS),
            ExpressionAttributeValues={":queued": runs.QUEUED, ":now": _common.now_iso()},
            ConditionExpression=Attr("taskId").exists() & Attr("status").eq(runs.DISPATCHED),
        )
        LOG.warning("released the dispatch claim on task %s for redelivery", task_id)
    except ClientError as err:
        if not _common.is_condition_failure(err):
            LOG.exception("could not release the dispatch claim on task %s", task_id)
            return
        LOG.info("task %s was no longer DISPATCHED; left as it is", task_id)


def _payload(task: Mapping[str, Any], config: Mapping[str, Any], resume: bool) -> dict[str, Any]:
    """The invoke body, and the single place its shape is written.

    Every field comes from a stored attribute: the container is handed what it needs
    rather than a table name and the grants to go looking. `sessionId` is derived, never
    read back and never minted, so a resume remounts the same workspace (invariant 18).
    """
    # The other half of this contract is `agent/src/coding_agent/server.py::_REQUIRED`
    # plus `graph.PAYLOAD_TO_STATE`/`graph.OPTIONAL_PAYLOAD_FIELDS`, and the agreement
    # between the two is held by `tests/test_invoke_payload_contract.py` against the one
    # definition of the shape in `tests/invoke_payload.py`. A field added here has to be
    # classified in that test -- mapped onto a `TaskState` key or declared as sent and
    # deliberately unread, with a reason (invariant 13b).
    return {
        "taskId": str(task["taskId"]),
        "configId": str(task["configId"]),
        "prompt": task.get("prompt", ""),
        "repository": config.get("repository", ""),
        "repositoryUrl": config.get("repositoryUrl", ""),
        "defaultBranch": config.get("defaultBranch", ""),
        # Read by `nodes/checkout.py` and `nodes/submit.py` through `store.github_token`,
        # and sent by nothing at all until the contract test asked who consumed it: a
        # container without it clones no private repository and opens no pull request.
        "credentialProviderName": config.get("credentialProviderName", ""),
        "verifyCommand": config.get("verifyCommand", ""),
        "setupCommand": config.get("setupCommand", ""),
        "resume": resume,
        "sessionId": observability.session_id(str(task["taskId"])),
    }


def _invoke(task: Mapping[str, Any], config: Mapping[str, Any], *, resume: bool) -> None:
    """One `InvokeAgentRuntime`, at the DEFAULT endpoint, on the task's own session."""
    # Never a fresh id and never the raw task id: `runtimeSessionId`'s minimum is 33
    # characters and botocore enforces it *client-side*, so a bare 24-character task id
    # is a `ParamValidationError` before anything leaves the Lambda.
    session = observability.session_id(str(task["taskId"]))
    _common.agentcore_data().invoke_agent_runtime(
        # Copied onto the task when the run was queued, so a `DELETE` of the
        # configuration cannot orphan a dispatch that is already in flight.
        agentRuntimeArn=str(task.get("agentRuntimeArn") or config.get("agentRuntimeArn") or ""),
        # Invariant 14: `create_agent_runtime` makes DEFAULT itself and nothing in this
        # repository creates another endpoint, so this is the only qualifier there is.
        qualifier=observability.ENDPOINT_NAME,
        runtimeSessionId=session,
        # The workload the credential belongs to, and the reason the container can fetch a
        # GitHub token at all. AgentCore mints a workload access token for this id and
        # delivers it to the container as a request header; without it
        # `store.github_token` has nothing to exchange, because a runtime's own identity is
        # service-linked and refuses a caller-initiated `GetWorkloadAccessToken`.
        #
        # The **configuration id**, so the identity is the repository rather than the run:
        # one credential provider serves every task against that repository, and a task id
        # here would mint a fresh identity per run for a credential that does not vary.
        runtimeUserId=str(task["configId"]),
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(_payload(task, config, resume)).encode(),
    )


def _dispatch(task_id: str, config_id: str, *, resume: bool) -> None:
    loaded = _task_and_config(task_id, config_id)
    if loaded is None:
        return
    task, config = loaded

    if runs.is_terminal(str(task.get("status", ""))):
        # The claim below covers a redelivered *dispatch*, but a resume carries no claim,
        # so this is the only thing standing between an old resume message and a container
        # started on a task that has already finished.
        LOG.info("task %s already finished as %s; invoking nothing", task_id, task.get("status"))
        return

    # A resume carries **no** claim: the watchdog already claimed the task and pinned
    # `resumeCount`, so re-claiming here would either fail (the status is not `QUEUED`)
    # or re-open the door the watchdog just closed.
    if not resume and not _claim(task):
        return

    try:
        _invoke(task, config, resume=resume)
    except Exception:
        if not resume:
            # Re-raised either way, so SQS redelivers and the redrive policy eventually
            # parks the message on the DLQ. The release is what lets that redelivery
            # claim the task again; a resume keeps its claim, because releasing it would
            # make a crash loop unbounded.
            _release(task)
        raise
    LOG.info("invoked task %s config %s resume=%s", task_id, config_id, resume)


def _not_already_finished() -> ConditionBase:
    """`attribute_exists(taskId)` and a `<>` against every terminal status.

    Derived from `runs.TERMINAL_STATUSES` rather than spelled out, so a status phase 2
    adds cannot be quietly overwritten here. Written as a chain of comparators rather
    than `NOT status IN (...)`: the two are equivalent for every item this table holds --
    a task item always carries a status -- and `<>` is the form there is no doubt about.
    """
    condition = Attr("taskId").exists()
    for status in sorted(runs.TERMINAL_STATUSES):
        condition &= Attr("status").ne(status)
    return condition


def _fail(task_id: str, config_id: str) -> None:
    """The terminal write: `FAILED`, conditional on the task existing and not already
    being finished.

    `attribute_exists(taskId)` is invariant 23 in miniature -- `update_item` *creates* an
    absent item, so a dead-lettered message for a deleted task would otherwise resurrect
    it as a `FAILED` ghost. The non-terminal condition is what keeps a run that actually
    succeeded (and whose dispatch message was dead-lettered for an unrelated reason)
    from being overwritten with a failure.
    """
    try:
        _common.tasks_table.update_item(
            Key={"configId": config_id, "taskId": task_id},
            UpdateExpression=(
                "SET #st = :failed, statusReason = :reason, finishedAt = :now, updatedAt = :now"
            ),
            ExpressionAttributeNames=dict(_STATUS),
            ExpressionAttributeValues={
                ":failed": FAILED,
                ":reason": DISPATCH_FAILED_REASON,
                ":now": _common.now_iso(),
            },
            ConditionExpression=_not_already_finished(),
        )
        LOG.error("task %s failed: %s", task_id, DISPATCH_FAILED_REASON)
    except ClientError as err:
        if not _common.is_condition_failure(err):
            raise
        LOG.info("dead-lettered task %s is gone or already finished; left as it is", task_id)


def _identity(message: Mapping[str, Any]) -> tuple[str, str, bool] | None:
    """`(taskId, configId, resume)` off a dispatch message, or `None` when it names no
    task. `runs.enqueue_dispatch` is the only writer of this shape."""
    task_id, config_id = str(message.get("taskId") or ""), str(message.get("configId") or "")
    if not task_id or not config_id:
        LOG.error("a dispatch message names no task and no configuration; dropped")
        return None
    return task_id, config_id, bool(message.get("resume", False))


def lambda_handler(event: Mapping[str, Any], _context: Any = None) -> None:
    """The task queue's consumer. Undecorated: there is no HTTP route here.

    An unexpected exception is deliberately *not* caught -- SQS redelivers, and the
    redrive policy is what eventually parks a genuinely broken dispatch on the DLQ, where
    `dlq_handler` turns it into a `FAILED` task somebody can read. Only a message this
    function cannot even read is dropped, because redelivering it cannot make it parse.
    """
    for record in event.get("Records", []):
        message = _message(record)
        if message is None:
            continue
        identity = _identity(message)
        if identity is None:
            continue
        task_id, config_id, resume = identity
        LOG.info("dispatch task=%s config=%s resume=%s", task_id, config_id, resume)
        _dispatch(task_id, config_id, resume=resume)


def dlq_handler(event: Mapping[str, Any], _context: Any = None) -> None:
    """The dead-letter consumer, and the terminal owner of `FAILED`.

    Every record is handled inside its own `try`, and nothing is re-raised: an exception
    out of here returns the message to the *dead-letter* queue, which has no redrive of
    its own, so a run whose dispatch failed would keep failing to be recorded as failed.
    One unreadable record must not stop the rest of the batch being resolved either,
    which is why the `try` is per record rather than around the loop.
    """
    for record in event.get("Records", []):
        try:
            message = _message(record)
            if message is None:
                continue
            identity = _identity(message)
            if identity is None:
                continue
            task_id, config_id, _resume = identity
            _fail(task_id, config_id)
        except Exception:
            # No `continue` needed, but the log line is the point: this is the last place
            # the failure of a run is visible at all.
            LOG.exception("could not record a dead-lettered dispatch as FAILED")
