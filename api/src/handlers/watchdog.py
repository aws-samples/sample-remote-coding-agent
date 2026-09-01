"""The liveness sweep: one EventBridge tick every 15 minutes, no HTTP surface.

Three jobs, in this order per task: *resolve* a cancellation, *stop* the session of a
task that has finished, and *resume* a task that has gone quiet. The sweep is the only
thing in the system that can unstick a run whose container died mid-node, so it is
written to converge -- every branch either writes an attribute the next scan filters on
(`stoppedAt`, a terminal `status`) or claims a bounded resume. A branch that only logged
would log the same task every 15 minutes forever.

`lambda_handler` is undecorated and ignores its event: there is nothing in a scheduled
EventBridge payload this needs, and `@handler` is for the HTTP surface. This module
declares no `ROUTES` for the same reason.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Attr, ConditionBase
from botocore.exceptions import ClientError

from . import _common, observability, runs
from ._common import LOG

# 2700, not 1800: `verify` makes no model call at all, so a *healthy* verification emits
# no activity rows for as long as the suite runs -- the agent's own `VERIFY_TIMEOUT` is
# 1800 -- and a threshold at or below that re-invokes a task that is working perfectly
# well. Invariant 27: this bound has to clear `VERIFY_TIMEOUT`, and it is `verify`'s
# alone (a `REVIEWING` subagent streams activity rows, and setup writes one when it
# finishes, so the timeouts never add up).
QUIET_THRESHOLD_SECONDS = 2700

# Three attempts, then FAILED naming the reason. A crash that reproduces is a bug in the
# task, not something more compute fixes, and an unbounded resume is a bill.
MAX_RESUMES = 3

# FOLLOW-UP (phase 2 integration): pinned duplication of the non-terminal half of
# `runs.TASK_STATUSES` -- `tests/test_api_agentcore_figures.py` asserts this equals
# `set(runs.TASK_STATUSES) - runs.TERMINAL_STATUSES`, so the two vocabularies cannot
# drift silently. Invariant 13: a new node needs a line here as well as in the graph.
RESUMABLE_STATUSES = frozenset(
    {
        "QUEUED",
        "DISPATCHED",
        "CHECKING_OUT",
        "PLANNING",
        "CODING",
        "VERIFYING",
        "REVIEWING",
        "SUBMITTING",
    }
)

# `cancelledAt` and `stoppedAt` are projected because their branches read them: drop
# either name and the branch below becomes dead code that never fires (invariant 28).
# `prompt` and `steps` are deliberately absent -- a sweep reads every live task on every
# tick and the prompt is the largest attribute on the item.
_SCAN_PROJECTION = (
    "configId, taskId, #st, activity, resumeCount, lastResumeAt, "
    "cancelledAt, stoppedAt, sessionId, agentRuntimeArn, "
    "dispatchedAt, createdAt"
)

_STOP_ATTEMPTS = 3

# `runs.py` names only the two statuses it writes itself; this is the one the watchdog
# writes, and it is the same string `runs.TERMINAL_STATUSES` carries.
_FAILED = "FAILED"


def _status_is_one_of(statuses: Iterable[str]) -> ConditionBase:
    """`status = a OR status = b OR ...`.

    An OR of equalities rather than `IN` or a negated `IN`: this expression is evaluated
    by DynamoDB against every item in the table, and equality/OR/`attribute_exists` is
    the subset every engine (including the one the tests run against) agrees on. It also
    means "non-terminal" is spelled as `RESUMABLE_STATUSES` -- the set a figures test
    pins equal to the non-terminal statuses -- rather than as a negation that would
    silently include a status this code has never heard of.
    """
    clauses = [Attr("status").eq(status) for status in sorted(statuses)]
    condition = clauses[0]
    for clause in clauses[1:]:
        condition = condition | clause
    return condition


def _scan() -> list[dict[str, Any]]:
    """Every task this sweep could act on, paged to the end of the table.

    Two halves: a task that has not finished (it may be quiet, or cancelled), and a task
    that *has* finished but still holds a session nobody stopped. The second half is what
    stops idle compute being billed, and writing `stoppedAt` is what takes the task back
    out of this filter on the next tick.
    """
    live = _status_is_one_of(RESUMABLE_STATUSES)
    finished_with_a_session = (
        _status_is_one_of(runs.TERMINAL_STATUSES)
        & Attr("sessionId").exists()
        & Attr("stoppedAt").not_exists()
    )

    items: list[dict[str, Any]] = []
    start_key: dict[str, Any] | None = None
    while True:
        # A fresh argument dict per page: boto3's condition transform injects its
        # generated placeholders into whatever `ExpressionAttributeNames` it is handed.
        kwargs: dict[str, Any] = {
            "ProjectionExpression": _SCAN_PROJECTION,
            "ExpressionAttributeNames": {"#st": "status"},
            "FilterExpression": live | finished_with_a_session,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        page = _common.tasks_table.scan(**kwargs)
        items.extend(page.get("Items", []))
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            return items


def _last_seen(task: Mapping[str, Any]) -> datetime | None:
    """When this task last proved it was alive.

    The maximum `at` timestamp on the activity feed, folded with `lastResumeAt`, falling
    back to `dispatchedAt` and then `createdAt`. It **never reads `updatedAt`** (invariant
    13): through `updatedAt` a 40-minute coding node and a dead container are
    indistinguishable, which is exactly why activity writes skip that field -- it is
    `lastFinished`'s sort key, not a heartbeat.

    A resume is itself a liveness signal: the sweep just acted, and the container is owed
    a whole quiet window to answer before another tick acts again.

    FOLLOW-UP (phase 2 integration): `agent/src/coding_agent/activity.py` is the other
    half of the row shape. This reader is deliberately tolerant -- an unparseable row is
    skipped rather than raised -- so a mismatch degrades the clock to `dispatchedAt`
    instead of taking out the sweep.
    """
    stamps = [
        _common.parse_iso(row.get("at"))
        for row in task.get("activity") or []
        if isinstance(row, Mapping)
    ]
    stamps.append(_common.parse_iso(task.get("lastResumeAt")))
    seen = [stamp for stamp in stamps if stamp is not None]
    if seen:
        return max(seen)
    return _common.parse_iso(task.get("dispatchedAt")) or _common.parse_iso(task.get("createdAt"))


def _quiet_seconds(task: Mapping[str, Any], now: datetime) -> float | None:
    """How long the task has been silent, or `None` when nothing on the item parses --
    which is a task the sweep declines to judge rather than one it resumes blind."""
    seen = _last_seen(task)
    return None if seen is None else (now - seen).total_seconds()


def _resume_count(task: Mapping[str, Any]) -> int:
    """DynamoDB hands numbers back as `Decimal`, and a task written before the attribute
    existed has none at all."""
    try:
        return int(task.get("resumeCount") or 0)
    except (TypeError, ValueError):
        return 0


def _fail(task: Mapping[str, Any], reason: str) -> bool:
    """Resolve the task to `FAILED` with a reason an operator can read. Returns whether
    this sweep won the write."""
    stamp = _common.now_iso()
    try:
        _common.tasks_table.update_item(
            Key={"configId": task["configId"], "taskId": task["taskId"]},
            UpdateExpression="SET #st = :failed, statusReason = :reason, finishedAt = :now, updatedAt = :now",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":failed": _FAILED, ":reason": reason, ":now": stamp},
            # `attribute_exists(taskId)` because `update_item` *creates* an absent item
            # (invariant 19), and the status pin is the optimistic half: a container that
            # moved on since the scan has just proved it is alive, so this sweep's verdict
            # is stale and must be refused rather than overwrite a live run.
            ConditionExpression=Attr("taskId").exists() & Attr("status").eq(task.get("status")),
        )
    except ClientError as err:
        if not _common.is_condition_failure(err):
            raise
        LOG.info("task=%s moved underneath the sweep; not resolved to FAILED", task["taskId"])
        return False
    LOG.warning("task=%s resolved to FAILED: %s", task["taskId"], reason)
    return True


def _claim_resume(task: Mapping[str, Any]) -> bool:
    """Claim the right to re-dispatch this task. Returns whether this sweep won.

    Pinning `resumeCount` to the value the scan read is what makes exactly one of two
    concurrent ticks win: both see the same count, both try to set `count + 1`, and the
    second one's condition fails. The claim is **never released** -- unlike a dispatch
    claim, which is released so SQS can redeliver -- because releasing it would make a
    container that crashes on startup resume forever.

    `lastResumeAt` and not `updatedAt` on purpose: see `_last_seen`.
    """
    previous = _resume_count(task)
    # A task written before `resumeCount` existed is pinned on its absence, which is the
    # same claim: exactly one writer can turn "absent" into 1.
    pin = (
        Attr("resumeCount").eq(previous)
        if "resumeCount" in task
        else Attr("resumeCount").not_exists()
    )
    try:
        _common.tasks_table.update_item(
            Key={"configId": task["configId"], "taskId": task["taskId"]},
            UpdateExpression="SET resumeCount = :next, lastResumeAt = :now",
            ExpressionAttributeValues={":next": previous + 1, ":now": _common.now_iso()},
            ConditionExpression=Attr("taskId").exists() & pin,
        )
    except ClientError as err:
        if not _common.is_condition_failure(err):
            raise
        LOG.info("task=%s was already claimed by another tick", task["taskId"])
        return False
    return True


def _mark_stopped(task: Mapping[str, Any]) -> None:
    """Write `stoppedAt`. This is the attribute `_scan`'s filter excludes on, so the mark
    is what makes the sweep converge as well as the record that the session is gone."""
    try:
        _common.tasks_table.update_item(
            Key={"configId": task["configId"], "taskId": task["taskId"]},
            UpdateExpression="SET stoppedAt = :now",
            ExpressionAttributeValues={":now": _common.now_iso()},
            ConditionExpression=Attr("taskId").exists() & Attr("stoppedAt").not_exists(),
        )
    except ClientError as err:
        if not _common.is_condition_failure(err):
            raise
        LOG.info("task=%s was already marked stopped", task["taskId"])


def stop_session(task: Mapping[str, Any]) -> None:
    """Stop the task's AgentCore session once, then mark it stopped.

    A **409 `RetryableConflictException` is expected**, not exceptional: it is what a
    session answers while it is still provisioning or already tearing down. So it is
    retried with bounded backoff, and if it is still conflicting after `_STOP_ATTEMPTS`
    the mark is deliberately *not* written -- the next tick picks the task up again.
    `ResourceNotFoundException` is success: the session is already gone, which is the
    state this call was asking for.

    Public, and called by `tasks.cancel` as well as by the sweep: this is deliberately the
    only caller of `StopRuntimeSession` in the control plane, so an operator's cancellation
    and a tick's cleanup cannot drift about what stopping means.
    """
    task_id, session, arn = task["taskId"], task.get("sessionId"), task.get("agentRuntimeArn")
    if not session or not arn:
        # Nothing to stop, but the mark still has to be written or this task is listed
        # and logged on every tick forever -- the same reasoning as resolving a
        # cancellation rather than skipping it.
        LOG.warning("task=%s is finished with no session to stop", task_id)
        _mark_stopped(task)
        return

    for attempt in range(_STOP_ATTEMPTS):
        try:
            _common.agentcore_data().stop_runtime_session(
                agentRuntimeArn=str(arn),
                runtimeSessionId=str(session),
                qualifier=observability.ENDPOINT_NAME,
            )
            break
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            http_status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == "ResourceNotFoundException":
                LOG.info("task=%s session was already gone", task_id)
                break
            if code == "RetryableConflictException" or http_status == 409:
                if attempt + 1 == _STOP_ATTEMPTS:
                    LOG.warning(
                        "task=%s still conflicting after %d stop attempts; next tick retries",
                        task_id,
                        _STOP_ATTEMPTS,
                    )
                    return
                time.sleep(2**attempt)
                continue
            raise
    _mark_stopped(task)


def _sweep_task(task: Mapping[str, Any], now: datetime) -> str:  # noqa: PLR0911 -- every return is a guard clause naming one outcome for the tick's summary, and early exit is the readable form
    """What this sweep did to one task, as a word for the tick's summary."""
    task_id, status = task["taskId"], task.get("status")

    # 1. A cancellation is *resolved*, never skipped (invariant 28): an operator wrote
    #    `cancelledAt`, so the run is over and the item has to say so. Skipping it would
    #    re-list and re-log the same task every 15 minutes for as long as the table lives.
    if task.get("cancelledAt") and not runs.is_terminal(status):
        if not _fail(task, "cancelled by operator"):
            return "raced"
        stop_session(task)
        return "cancelled"

    # 2. A finished task only needs its session stopped -- nothing here re-reads a
    #    verdict some other writer already reached.
    if runs.is_terminal(status):
        if task.get("sessionId") and not task.get("stoppedAt"):
            stop_session(task)
            return "stopped"
        return "idle"

    quiet = _quiet_seconds(task, now)
    if quiet is None:
        LOG.warning("task=%s carries no parseable timestamp; left alone", task_id)
        return "idle"
    if quiet <= QUIET_THRESHOLD_SECONDS:
        return "idle"
    if status not in RESUMABLE_STATUSES:
        # A status this deployment unit has never heard of (the agent renamed a node)
        # degrades to "leave it alone and say so", never to a crash or a blind resume.
        LOG.warning(
            "task=%s quiet for %.0fs in unknown status %s; left alone", task_id, quiet, status
        )
        return "idle"

    # 3. Quiet and resumable. Exhaustion becomes FAILED *naming the reason*, never a
    #    silent give-up: the reason is the only place an operator can read why a run that
    #    was moving stopped moving.
    previous = _resume_count(task)
    if previous >= MAX_RESUMES:
        reason = (
            f"no activity for {QUIET_THRESHOLD_SECONDS}s; {previous} of {MAX_RESUMES} resumes used"
        )
        return "failed" if _fail(task, reason) else "raced"

    if not _claim_resume(task):
        return "raced"
    # Re-dispatch goes through `runs.enqueue_dispatch` and nothing else, so the resume
    # message and the first dispatch cannot drift; the dispatcher does not re-claim a
    # resume, because the claim above is the one that counts.
    runs.enqueue_dispatch(task, resume=True)
    LOG.info("resumed task=%s quiet=%.0fs resume=%d/%d", task_id, quiet, previous + 1, MAX_RESUMES)
    return "resumed"


def lambda_handler(_event: Any, _context: Any = None) -> dict[str, Any]:
    """The 15-minute tick. Schedule-driven, so the event carries nothing this reads."""
    now = datetime.now(tz=UTC)
    tasks = _scan()
    outcomes: Counter[str] = Counter()
    for task in tasks:
        try:
            outcomes[_sweep_task(task, now)] += 1
        except Exception:
            # One malformed or unlucky task must not end the sweep: the sweep is what
            # unsticks every *other* task, and it only runs four times an hour.
            LOG.exception("watchdog failed on task=%s", task.get("taskId"))
            outcomes["error"] += 1
    LOG.info("watchdog swept %d task(s): %s", len(tasks), dict(outcomes))
    return {"scanned": len(tasks), **outcomes}
