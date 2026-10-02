"""Task reads, the one HTTP way to start a run, and the one HTTP way to stop one.

`create` owns neither half of what it produces. The run is `runs.queue_run`'s alone -- no
task `put_item` and no `send_message` here -- and the card is `cards.create_claimed`'s
alone, so this entry point and `cards.move` cannot drift about what a run is or about what
a card is. What `create` does own is the order of the three writes and the repair when the
queue fails, which is the same order and the same repair as `cards._dispatch`.

`cancel` owns an order too, and the same way: the mark first and the session second, so a
stop that fails leaves an item the watchdog already knows how to resolve. It owns neither
half of what it does either -- stopping is `watchdog.stop_session`'s, the terminal status
is the sweep's -- which is why the one `bedrock-agentcore:*` grant in this module is
`cancel`'s alone.

The other four routes are read-only: a `get_item` or a `query` on
`Tasks`, one `get_item` on `Configurations`, and log locations *derived* from stored
attributes by `observability.log_locations` rather than looked up live. That is what
keeps them free of `logs:*` and `bedrock-agentcore:*`, and
`tests/test_api_grant_contract.py` is what keeps them that way.

`Tasks` is keyed `configId` + `taskId` after PLAN's Tier 1 #8 and has **no** GSI, so
every route here needs a configuration id. The two `/tasks/{taskId}` routes take it as
a required query parameter: a lookup by task id alone is not a question this table can
answer, and the alternative -- a `scan` per task view -- is the thing the re-key was
done to make impossible.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from boto3.dynamodb.conditions import Attr, Key

from . import _common, cards, observability, runs, watchdog
from ._common import LOG, ApiError, handler

# List bounds. A console asking for more gets the maximum rather than a 400 (see
# `_common.int_query_param`).
_DEFAULT_LIMIT, _MAX_LIMIT = 25, 100
_DEFAULT_ACTIVITY_LIMIT, _MAX_ACTIVITY_LIMIT = 50, 200

# How many DynamoDB pages one filtered list request may read. The filter runs *after*
# the page is read, so a filtered request costs pages rather than rows; ten bounds the
# work per request and the budget running out is reported as a `nextCursor`, never as a
# short answer that looks complete.
_MAX_PAGES = 10

# The activity cursor is a row count, and the feed is a bounded ring buffer, so any
# cursor this large is already past the end -- `activity_page` restarts it rather than
# refusing it, so clamping is enough.
_MAX_ACTIVITY_CURSOR = 1_000_000

# The same string `watchdog._sweep_task` writes as `statusReason` when it resolves the
# cancellation, so the activity row an operator reads and the status reason they read next
# tick say the same thing.
_CANCEL_REASON = "cancelled by operator"


def _required_config_id(event: Mapping[str, Any]) -> str:
    """`configId` from the query string, required on every route in this module.

    PLAN's Tier 1 #8 re-keyed `Tasks` to `configId` + `taskId` so a
    `dynamodb:LeadingKeys` condition can fence a container to its own repository, which
    removed the `byConfig` GSI. The path stays `/tasks/{taskId}` (the console's and the
    MCP tool's route list is authoritative for that) and the partition key arrives
    beside it.
    """
    config_id = _common.query_param(event, "configId")
    if not config_id:
        raise ApiError(
            400,
            "MissingQueryParameter",
            "configId is required: Tasks is keyed configId + taskId, so a task cannot be found by id alone",
            field="configId",
        )
    return config_id


def _config(config_id: str) -> dict[str, Any]:
    found = _common.config_table.get_item(Key={"configId": config_id}).get("Item")
    if not found:
        raise ApiError(404, "NotFound", "no configuration with that id", configId=config_id)
    return found


def _task(config_id: str, task_id: str) -> dict[str, Any]:
    found = _common.tasks_table.get_item(Key={"configId": config_id, "taskId": task_id}).get("Item")
    if not found:
        raise ApiError(
            404, "NotFound", "no task with that id under that configuration", taskId=task_id
        )
    return found


def _without_feed(task: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of a task item with its activity feed replaced by the feed's depth.

    The feed belongs to `GET /tasks/{taskId}/activity`. A 40-minute run's feed is
    hundreds of rows, and paying for it on every poll of a task view -- or once per row
    of a list -- is how a console gets slow while still looking correct.
    """
    trimmed = {key: value for key, value in task.items() if key != "activity"}
    trimmed["activityCount"] = len(task.get("activity") or [])
    return trimmed


def _filtered_pages(
    config_id: str, *, status: str, limit: int, start: Mapping[str, Any] | None
) -> tuple[list[dict[str, Any]], Mapping[str, Any] | None]:
    """Up to `limit` tasks in `status`, newest first, and the cursor to carry on from.

    `Limit` and `FilterExpression` are deliberately never sent in the same call.
    DynamoDB applies a filter **after** the limit, so `?status=CODING&limit=5` on a
    configuration whose five newest tasks have finished answers "nothing running" while
    a live task sits behind that page -- a wrong answer with a 200 on it, which is the
    reading half of the same problem the read-only grants address on the writing side.
    """
    collected: list[dict[str, Any]] = []
    for _ in range(_MAX_PAGES):
        answer = _common.tasks_table.query(
            KeyConditionExpression=Key("configId").eq(config_id),
            FilterExpression=Attr("status").eq(status),
            ScanIndexForward=False,
            **({"ExclusiveStartKey": dict(start)} if start else {}),
        )
        collected.extend(answer.get("Items", []))
        start = answer.get("LastEvaluatedKey")
        if not start or len(collected) >= limit:
            break

    rows = collected[:limit]
    if len(collected) > limit:
        # A page overshot the limit, so resume from the last row actually returned
        # rather than from the page boundary. The base table's key is the whole of a
        # row's identity, so a row is its own cursor.
        start = {"configId": config_id, "taskId": rows[-1]["taskId"]}
    return rows, start


def activity_page(
    rows: Sequence[Mapping[str, Any]], cursor: int, limit: int, *, status: str | None = None
) -> dict[str, Any]:
    """The slice of a feed after `cursor` rows, and whether polling can stop.

    Pure: no I/O, no mutation of `rows`, and every value is a function of the arguments.
    `cursor` is the number of rows the client has already seen and `nextCursor` is that
    plus the rows returned, so a poller only ever carries an integer.

    A `cursor` past the end of the feed means the ring buffer trimmed its head while the
    client was away. That resets to 0 rather than raising or answering empty: re-showing
    a row is better than skipping one, the feed is best-effort and bounded by design, and
    an empty page would look to a poller exactly like a finished run.

    `status` is the task's status, which the feed itself does not carry; it is keyword
    with a default so the helper stays callable as `activity_page(rows, cursor, limit)`
    against a feed alone.
    """
    total = len(rows)
    if cursor > total:
        LOG.warning(
            "activity cursor %s is past a feed of %s rows; restarting the feed", cursor, total
        )
        cursor = 0
    cursor = max(0, cursor)
    page = [dict(row) for row in rows[cursor : cursor + max(0, limit)]]
    return {
        "rows": page,
        "cursor": cursor,
        "nextCursor": cursor + len(page),
        "status": status,
        # A terminal task's feed cannot grow, so the console stops polling. It still
        # drains `nextCursor` to the end of the feed first -- the last rows of a run are
        # written in the same breath as its terminal status.
        "done": runs.is_terminal(status),
    }


@handler
def create(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """The one HTTP way to start a run, and every run it starts has a card.

    `_config` first, so an unknown `configId` 404s before anything is written; the card
    next, born claimed; then the run; then the card records it. Readiness stays
    `runs.queue_run`'s to refuse, which is why a `409 ConfigurationNotReady` is the one
    answer that leaves a card behind -- released to `BACKLOG`, where the operator's prompt
    is a card they can drag once the repository is `READY`. There is no request field that
    opts out of the card: an invariant with a flag on it is not an invariant.
    """
    body = _common.parse_body(event)
    config_id, prompt = _common.require(body, "configId", "prompt")
    config = _config(config_id)
    # Screened before the card exists (invariant 43): a refused prompt has to leave nothing
    # behind, and the repair below would otherwise keep it on the board as a `BACKLOG` card --
    # a pasted secret included. `queue_run` takes the result rather than screening twice.
    screened = runs.screen_prompt(prompt)
    card = cards.create_claimed(config_id, description=prompt)
    try:
        task = runs.queue_run(config, prompt=screened, card=card, source="api")
    except Exception:
        # The repair `cards._dispatch` performs for the identical situation: a claim that
        # is spent on a run that never queued is handed back rather than deleted.
        cards.release(card)
        raise
    cards.store_task_id(card["cardId"], task["taskId"])
    LOG.info(
        "created task=%s config=%s source=api card=%s", task["taskId"], config_id, card["cardId"]
    )
    return 202, task


@handler
def cancel(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Stop a run an operator started by mistake. **The mark first, then the session.**

    Two writes, and the order is not interchangeable:

    1. `cancelledAt`, plus one activity row, onto the task item -- conditionally;
    2. `StopRuntimeSession`, through `watchdog.stop_session`, which is the sweep's own
       caller so the two cannot drift about what stopping means.

    Marked first because the mark is what makes this durable: if the stop raises, or this
    Lambda dies between the two calls, the item already says cancelled and the watchdog
    resolves it to `FAILED` on its next tick (invariant 28). Stopping first would leave a
    stopped session behind an item that still looks live -- which is a task the watchdog
    tries to *resume*, the worst outcome available. That is also why the answer is `202`
    rather than `200`: this route accepts the cancellation, and the terminal status is the
    sweep's to write.

    `updatedAt` is deliberately not written (invariant 13): it is `lastFinished`'s sort key
    and the watchdog's only liveness clock, so a bookkeeping write that refreshed it would
    tell the sweep a dead container is alive.
    """
    task_id = _common.path_param(event, "taskId")
    config_id = _required_config_id(event)
    # Read first, so an unknown taskId 404s instead of being created: `update_item`
    # *creates* an absent item (invariant 19), and a conjured cancelled task is worse than
    # a refusal. The condition below is the second line of defence for the same thing.
    task = _task(config_id, task_id)
    status = str(task.get("status") or "")

    if task.get("cancelledAt"):
        raise ApiError(
            409,
            "AlreadyCancelled",
            "this task is already cancelled; the watchdog resolves it on its next tick",
            taskId=task_id,
            cancelledAt=str(task["cancelledAt"]),
        )
    if runs.is_terminal(status):
        raise ApiError(
            409,
            "TaskAlreadyFinished",
            f"this task is already {status}, so there is nothing left to stop",
            taskId=task_id,
            status=status,
        )

    now = _common.now_iso()
    _common.tasks_table.update_item(
        Key={"configId": config_id, "taskId": task_id},
        UpdateExpression=(
            "SET cancelledAt = :now, activity = list_append(if_not_exists(activity, :empty), :row)"
        ),
        ExpressionAttributeValues={
            ":now": now,
            ":empty": [],
            ":row": [{"at": now, "node": "api", "kind": "cancel", "detail": _CANCEL_REASON}],
        },
        # No `:status` value to go with the pin: the condition builder makes its own
        # placeholders, including the `#n0` name for the reserved word `status`, and an
        # `ExpressionAttributeValues` entry the update expression never uses is rejected.
        ConditionExpression=(
            Attr("taskId").exists() & Attr("cancelledAt").not_exists() & Attr("status").eq(status)
        ),
    )

    stopped = True
    try:
        watchdog.stop_session(task)
    except Exception:
        # The mark is already durable, so a stop that failed is the sweep's problem and not
        # the operator's: the next tick resolves the task and stops the session again.
        # Answering 500 here would report "nothing happened" about a run that is cancelled.
        LOG.exception("task=%s is marked cancelled but its session did not stop", task_id)
        stopped = False

    LOG.info("cancelled task=%s config=%s from=%s", task_id, config_id, status)
    return 202, {
        "taskId": task_id,
        "configId": config_id,
        # The status the run was in when it was cancelled. The terminal status is the
        # sweep's to write, so this route never reports one.
        "status": status,
        "cancelledAt": now,
        # Whether the stop call came back without raising -- not a promise the session is
        # gone. A 409 that outlasts `watchdog._STOP_ATTEMPTS` returns quietly and the next
        # tick retries it.
        "sessionStopped": stopped,
    }


@handler
def list_(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    config_id = _required_config_id(event)
    limit = _common.int_query_param(event, "limit", _DEFAULT_LIMIT, minimum=1, maximum=_MAX_LIMIT)
    cursor = _common.decode_cursor(_common.query_param(event, "cursor"))
    status = _common.query_param(event, "status")

    if status:
        rows, last = _filtered_pages(config_id, status=status, limit=limit, start=cursor)
    else:
        # `taskId` is `tsk_<epoch-ms>_<hex>`, so descending on the sort key is
        # newest-first with no GSI and no sort of our own.
        answer = _common.tasks_table.query(
            KeyConditionExpression=Key("configId").eq(config_id),
            ScanIndexForward=False,
            Limit=limit,
            **({"ExclusiveStartKey": cursor} if cursor else {}),
        )
        rows, last = answer.get("Items", []), answer.get("LastEvaluatedKey")

    LOG.info("listed %s task(s) config=%s status=%s", len(rows), config_id, status)
    return 200, {
        "tasks": [_without_feed(row) for row in rows],
        "count": len(rows),
        "nextCursor": _common.encode_cursor(last),
    }


@handler
def get(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    task_id = _common.path_param(event, "taskId")
    config_id = _required_config_id(event)
    config = _config(config_id)
    task = _task(config_id, task_id)

    view = _without_feed(task)
    # Derived, never looked up: `log_locations` is pure, so this route needs no
    # `logs:DescribeLogStreams`, and a configuration that is still provisioning answers
    # `{}` rather than 500ing.
    view["logs"] = observability.log_locations(config, task)
    return 200, view


@handler
def activity(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    task_id = _common.path_param(event, "taskId")
    config_id = _required_config_id(event)
    cursor = _common.int_query_param(event, "cursor", 0, maximum=_MAX_ACTIVITY_CURSOR)
    limit = _common.int_query_param(
        event, "limit", _DEFAULT_ACTIVITY_LIMIT, minimum=1, maximum=_MAX_ACTIVITY_LIMIT
    )

    # Projected rather than a whole-item read: this is the one route a console polls on
    # a timer, and `prompt`/`verifyResult` are not what it came for. `#st` because
    # `status` is a DynamoDB reserved word.
    found = _common.tasks_table.get_item(
        Key={"configId": config_id, "taskId": task_id},
        ProjectionExpression="taskId, #st, activity, resumeCount, updatedAt",
        ExpressionAttributeNames={"#st": "status"},
    ).get("Item")
    if not found:
        raise ApiError(
            404, "NotFound", "no task with that id under that configuration", taskId=task_id
        )

    page = activity_page(found.get("activity") or [], cursor, limit, status=found.get("status"))
    return 200, {
        **page,
        "taskId": task_id,
        "configId": config_id,
        "resumeCount": found.get("resumeCount", 0),
        "updatedAt": found.get("updatedAt", ""),
    }


# The one definition of this module's routes. `infra/stacks/api_stack.py` wires them up and
# `tests/test_api_route_contract.py` holds the two in agreement, so a route added here and
# not there fails a test rather than answering 404 the first time a console asks for it.
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/tasks", "create"),
    ("GET", "/tasks", "list_"),
    ("GET", "/tasks/{taskId}", "get"),
    ("GET", "/tasks/{taskId}/activity", "activity"),
    ("POST", "/tasks/{taskId}/cancel", "cancel"),
)
