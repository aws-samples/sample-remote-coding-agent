"""The kanban board: five columns, of which only three are ever written.

PLAN's `## The kanban board` keeps one idea from the system this replaces: the derived
half of a card is computed on **read** and never stored. `STORED_COLUMNS` is the
operator's own value and the only thing this module writes; `DERIVED_COLUMNS` are
projections of the claimed run's own status, so the board cannot show a card in a state
no run is in -- there is no second copy of that truth to drift. `BOARD_COLUMNS` names the
five columns actually drawn, left to right, in one place: `STORED_COLUMNS` and
`DERIVED_COLUMNS` are six names between them and `ACTIVE` is not one a board renders. And
a card is deliberately **not** cascade-deleted with its configuration: cascading would put
a `Cards` read+write grant on the delete handler and make configuration teardown depend on
board state, so instead a card simply cannot outlive its configuration usefully -- `list_`
needs a `configId`, a `move` to `PLANNING` 404s through `_configuration`, and no card
handler writes to the `Configurations` table at all.

Three consequences shape everything below.

* `derived_view` is pure, so `get` and `list_` reach `get_item`/`query` and nothing
  else (`tests/test_api_grant_contract.py` asserts it). A read route therefore has no
  write grant to lose, and the status vocabulary this module duplicates across the
  api/agent boundary degrades to a wrong-but-cheap label instead of turning a whole
  board into a 500.
* `move` is the only door to a run and it goes through `runs.queue_run`. It never
  writes a task item or sends a dispatch itself: `tests/test_api_runs.py` compares
  `tasks.create` and `cards.move` key for key, and that comparison only means anything
  while there is exactly one definition of "queue a run".
* the mirror of that rule holds for cards: `tasks.create` does not write one of its own
  either, it calls `create_claimed` here. Every run has a card (invariant 38), so a card
  born already claimed by `POST /tasks` and a card dragged onto `PLANNING` are the same
  item built by one `_card_item`, and `create_claimed`/`store_task_id`/`release` are
  public because the other entry point composes exactly the writes `_dispatch` does.

`column` is a **DynamoDB reserved word**, so every expression naming it goes through
`#column`. Without the alias the call fails on the expression rather than on the
attribute, which reads as a broken SDK call instead of a badly chosen field name.
"""

from __future__ import annotations

import secrets as secrets_module  # `_common.secrets` is the Secrets Manager client
import time
from collections.abc import Mapping
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from . import _common, runs
from ._common import LOG, ApiError, handler

# The operator's value. `ACTIVE` is not something an operator asks for -- it is what a
# won `PLANNING` claim leaves behind -- but it is stored, and `move` says so rather than
# calling it unknown.
STORED_COLUMNS = ("BACKLOG", "ACTIVE", "DONE")
# Projections of the run's status. `REVIEWING` deliberately gets no column of its own:
# running the tests and judging them are one phase to an operator, and a sixth column
# for a step that lasts a minute costs board width for no information.
DERIVED_COLUMNS = ("PLANNING", "CODING", "VERIFYING")
EDITABLE_FIELDS = ("title", "description")

BACKLOG, ACTIVE, DONE = STORED_COLUMNS
PLANNING, CODING, VERIFYING = DERIVED_COLUMNS

# The five columns the board draws, left to right. Not `STORED_COLUMNS + DERIVED_COLUMNS`:
# `ACTIVE` is stored and is *not* a column -- it is what a won `PLANNING` claim leaves
# behind, and the projection is what gets drawn in its place. One tuple, in board order,
# because a projection that lands outside it renders nowhere and the card silently
# disappears from the board rather than failing.
#
# `web/js/transitions.js` mirrors this tuple's order, and
# `tests/test_web_status_vocabulary.py` holds the two equal: JavaScript cannot import a
# Python tuple, so a test is what keeps a column added here from being forgotten there.
BOARD_COLUMNS = (BACKLOG, PLANNING, CODING, VERIFYING, DONE)

# The only targets `move` accepts: the stored columns an operator owns, plus the one
# derived column that is a *request* rather than a projection.
MOVE_TARGETS = (PLANNING, BACKLOG, DONE)

# A pinned copy of the agent's `TaskStatus` and `graph._NODE_STATUS` names, the same
# agreement `runs.TASK_STATUSES` and `runs.STEPS` hold up from the other side. The two deployment units must not import each other, so
# both maps `.get(..., fallback)` rather than indexing.
_STATUS_COLUMN = {
    "QUEUED": PLANNING,
    "DISPATCHED": PLANNING,
    "CHECKING_OUT": PLANNING,
    "PLANNING": PLANNING,
    "CODING": CODING,
    "VERIFYING": VERIFYING,
    "REVIEWING": VERIFYING,
    "SUBMITTING": VERIFYING,
    "SUCCEEDED": DONE,
    "SUCCEEDED_UNVERIFIED": DONE,
    # `DONE`: the loop finished and review vetoed it, so there is a draft PR to read and
    # nothing left to run -- unlike `FAILED` there is no column it died in to recover.
    "FAILED_VERIFICATION": DONE,
    # `DONE`: the operator stopped it themselves, so the run is over wherever it had got to.
    "CANCELLED": DONE,
}
# `FAILED` is deliberately absent: it says the run stopped, never where it was, so it is
# answered from `steps` instead.
_STEP_COLUMN = {
    "checkout": PLANNING,
    "plan": PLANNING,
    "code": CODING,
    "verify": VERIFYING,
    "review": VERIFYING,
    "submit": VERIFYING,
}
# The step-entry outcomes this reader keys on. `RUNNING` is deliberately absent as a
# constant: it is simply "neither of these two", which is what a container killed mid-node
# leaves behind, and the rule below has to accept it rather than enumerate it.
_STEP_FAILED, _STEP_SKIPPED = "FAILED", "SKIPPED"

_TITLE_MAX = 120
_LIST_LIMIT, _LIST_LIMIT_MAX = 25, 100

# What the board draws of a run, and nothing more: a 40-minute run's activity feed is
# the activity route's business, and paying for it once per card per board read is how a
# board poll gets expensive without anyone noticing.
_TASK_PROJECTION = "taskId, configId, #st, steps, prUrl, verifyResult, updatedAt"
_TASK_NAMES = {"#st": "status"}


def new_card_id() -> str:
    """`crd_<epoch-ms>_<6 hex>`, the same shape as `runs.new_task_id`. The board's order
    comes from `createdAt` (the `byConfig` sort key), so the timestamp here is for
    reading a log line, not for a query."""
    return f"crd_{int(time.time() * 1000)}_{secrets_module.token_hex(3)}"


# -- the derived half: pure, no boto3, never written ---------------------------------


def _verified(task: Mapping[str, Any]) -> bool | None:
    """Whether the run's own verification passed, or `None` for "not known".

    `verifyResult`'s shape is phase 2's and is not settled here, so a bool and a mapping
    carrying `ok`/`passed` are both read, and anything else falls back to the status
    pair. Unknown is never reported as `False`: a board claiming a run failed
    verification when it simply has not run it yet is worse than one that says nothing.
    """
    result = task.get("verifyResult")
    if isinstance(result, bool):
        return result
    if isinstance(result, Mapping):
        for key in ("ok", "passed", "verified"):
            if isinstance(result.get(key), bool):
                return bool(result[key])
    status = task.get("status")
    if status == "SUCCEEDED":
        return True
    if status == "SUCCEEDED_UNVERIFIED":
        return False
    return None


def _step_entry(entry: Any) -> tuple[str, str]:
    """One `steps` row as `(step name, outcome)`.

    Two shapes are accepted on purpose. `store.set_status(state, status, step=name)`
    appends the bare node name, so a plain string is the shape the container writes and it
    carries no outcome; a mapping (`{"step": ..., "status": ...}`) is the shape a per-step
    outcome takes. `api/` and the container are separate deployment units and must not
    import each other, so this reads both rather than assuming one.
    """
    if isinstance(entry, Mapping):
        return str(entry.get("step") or ""), str(entry.get("status") or "").strip().upper()
    # No outcome, therefore "not SKIPPED", therefore eligible as the last-entry fallback --
    # which is exactly what today's bare-string list means.
    return str(entry or ""), ""


def _failed_step(task: Mapping[str, Any]) -> str | None:
    """The step a `FAILED` run died in, or `None` when `steps` says nothing.

    Three tiers, in this order, and each one is a distinct incident:

    * the **last `FAILED`** entry -- a resumed run can fail twice and the later failure is
      the one the board is about;
    * failing that the **last entry that is not `SKIPPED`** -- a container killed mid-node
      leaves a `RUNNING` entry and a timed-out run leaves no `FAILED` entry at all, and in
      both cases the last entry entered still names where the run was. `SKIPPED` is passed
      over because it names a step the run never entered and would report the wrong column;
    * failing both `None` -- a dispatch that never started a step at all.

    Never `status`: `FAILED` has already overwritten it, which is why `steps` is
    append-only in the first place.
    """
    steps = task.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    entries = [_step_entry(row) for row in steps]
    # The name goes back even when it is empty or unrecognised: degrading it is
    # `_STEP_COLUMN`'s `PLANNING` fallback's job, not this function's.
    for name, outcome in reversed(entries):
        if outcome == _STEP_FAILED:
            return name
    for name, outcome in reversed(entries):
        if outcome != _STEP_SKIPPED:
            return name
    return None


def _failed_column(task: Mapping[str, Any]) -> str | None:
    """`_failed_step` mapped onto a column. `None` propagates: "no step information" and
    "a step name this bundle has never heard of" are different, and only the second one is
    an unknown-name degradation to `PLANNING`."""
    step = _failed_step(task)
    return None if step is None else _STEP_COLUMN.get(step, PLANNING)


def derived_view(card: Mapping[str, Any], task: Mapping[str, Any] | None) -> dict[str, Any]:
    """The derived half of a card: `{"column", "status", "taskId", "prUrl", "verified"}`.

    Pure -- no boto3, no clock, a fresh dict every call, and neither argument is
    mutated. Only an `ACTIVE` card is projected: `BACKLOG` and `DONE` are the operator's
    own statement about the card and outrank the run's status, or parking a card whose
    last run succeeded would leave it sitting in `DONE` and the park would be invisible.
    """
    stored = str(card.get("column") or BACKLOG)
    view: dict[str, Any] = {
        "column": stored,
        "status": None,
        "taskId": card.get("taskId") or None,
        "prUrl": None,
        "verified": None,
    }
    if not task:
        return view

    status = task.get("status")
    view["status"] = status
    view["taskId"] = task.get("taskId") or view["taskId"]
    view["prUrl"] = task.get("prUrl") or None
    view["verified"] = _verified(task)
    if stored == ACTIVE:
        # An unrecognised status degrades to the stored column for the same reason an
        # unrecognised step degrades to `PLANNING`: the board keeps drawing.
        view["column"] = (
            # `PLANNING` for a `None`: `steps` said nothing at all, so the run never
            # started a step, and `PLANNING` is the leftmost live column -- a dispatch that
            # never got going has not got past planning.
            (_failed_column(task) or PLANNING)
            if status == "FAILED"
            # An unrecognised status degrades to the stored column, which for a projected
            # card is `ACTIVE` -- the one value here that is not a `BOARD_COLUMNS` member.
            else _STATUS_COLUMN.get(str(status), stored)
        )
    return view


def _with_column(card: Mapping[str, Any], task: Mapping[str, Any] | None) -> dict[str, Any]:
    """A card as a client sees it: the stored item with the derived view merged over it.

    `storedColumn` survives the merge because the console needs the operator's own value
    as well as the projection -- without it a card whose run has not started yet is
    indistinguishable from one that was parked.
    """
    return {**card, "storedColumn": card.get("column") or BACKLOG, **derived_view(card, task)}


# -- reads ---------------------------------------------------------------------------


def _card(card_id: str) -> dict[str, Any]:
    found = _common.cards_table.get_item(Key={"cardId": card_id}).get("Item")
    if not found:
        raise ApiError(404, "NotFound", "no such card", cardId=card_id)
    return found


def _claimed_run(card: Mapping[str, Any]) -> dict[str, Any] | None:
    """The run a card claims, projected down to what the board draws. A card that claims
    nothing costs no read at all, which is most of a backlog."""
    task_id, config_id = card.get("taskId"), card.get("configId")
    if not task_id or not config_id:
        return None
    return _common.tasks_table.get_item(
        Key={"configId": config_id, "taskId": task_id},
        ProjectionExpression=_TASK_PROJECTION,
        ExpressionAttributeNames=_TASK_NAMES,
    ).get("Item")


def _configuration(config_id: str) -> dict[str, Any]:
    """The configuration a card belongs to. A card against a configuration that is not
    there is a card whose run can never be dispatched, so it is refused at the door."""
    found = _common.config_table.get_item(Key={"configId": config_id}).get("Item")
    if not found:
        raise ApiError(404, "NotFound", "no such configuration", configId=config_id)
    return found


# -- writes --------------------------------------------------------------------------


def _editable() -> str:
    """The allow-list as a sentence. One reader, so `patch`'s two 400s cannot end up
    describing different sets of fields."""
    return ", ".join(EDITABLE_FIELDS)


def _default_title(description: str) -> str:
    """The first non-empty line of the description, truncated. PLAN cut `summarize.py`,
    so there is no model call here: a title is a slice of what the operator typed."""
    lines = (line.strip() for line in description.splitlines())
    first = next((line for line in lines if line), description.strip())
    return first[:_TITLE_MAX]


def _card_item(
    config_id: str,
    *,
    description: str,
    title: str | None = None,
    column: str = BACKLOG,
    run_count: int = 0,
) -> dict[str, Any]:
    """The whole of what a stored card is, in one place.

    `create` and `create_claimed` write cards that differ in two values and nothing else,
    so both build the item here -- the same argument `runs.queue_run` makes about task
    items, applied to cards, and what makes the drift assertion in
    `tests/test_api_cards.py` mean something rather than restate one function. One
    `now_iso()` call, so a card that has never been touched has `createdAt == updatedAt`
    exactly.
    """
    stamp = _common.now_iso()
    return {
        "cardId": new_card_id(),
        "configId": config_id,
        "title": title or _default_title(description),
        "description": description,
        "column": column,
        "taskId": "",
        "runCount": run_count,
        "createdAt": stamp,
        "updatedAt": stamp,
    }


def _refuse_while_live(card: Mapping[str, Any], action: str) -> dict[str, Any] | None:
    """Refuse `action` while the claimed run is still going, and hand the run back so
    the caller does not read it twice. A card whose run is mid-flight is a container
    with a checkout and a branch; moving or deleting the card would not stop it, it
    would only lose the operator's only handle on it."""
    task = _claimed_run(card)
    if task is not None and not runs.is_terminal(task.get("status")):
        status = task.get("status")
        raise ApiError(
            409,
            "RunInFlight",
            f"the card claims a run that is still {status}, so it cannot be {action}; "
            "wait for it to finish or cancel it first",
            cardId=card.get("cardId"),
            taskId=task.get("taskId"),
            status=status,
        )
    return task


def _store_column(card_id: str, column: str) -> dict[str, Any]:
    """Write a stored column. Conditional on the card still existing because
    `update_item` *creates* an absent item (invariant 19's other half), so a park racing
    a delete would otherwise resurrect the card as a column and nothing else."""
    try:
        return _common.cards_table.update_item(
            Key={"cardId": card_id},
            UpdateExpression="SET #column = :column, #updatedAt = :now",
            ExpressionAttributeNames={"#column": "column", "#updatedAt": "updatedAt"},
            ExpressionAttributeValues={":column": column, ":now": _common.now_iso()},
            ConditionExpression=Attr("cardId").exists(),
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as err:
        if _common.is_condition_failure(err):
            raise ApiError(404, "NotFound", "no such card", cardId=card_id) from err
        raise


def _claim(card: Mapping[str, Any]) -> dict[str, Any]:
    """Win the right to dispatch a run for this card, or 409.

    There is no read-then-check in front of this: the condition *is* the check, so two
    operators dropping the same card at the same moment cannot both pass it. `runCount`
    is pinned as well as the column because a card that was parked and dropped again is
    legitimately back in `BACKLOG` -- only the counter can tell that second drop apart
    from a duplicate of the first (PLAN's `## The kanban board`, invariant 19). A card
    with no `runCount` at all fails the condition, which is the safe direction.
    """
    card_id, previous = card["cardId"], card.get("runCount", 0)
    try:
        return _common.cards_table.update_item(
            Key={"cardId": card_id},
            UpdateExpression="SET #column = :active, #runCount = :next, #updatedAt = :now",
            ExpressionAttributeNames={
                "#column": "column",
                "#runCount": "runCount",
                "#updatedAt": "updatedAt",
            },
            ExpressionAttributeValues={
                ":active": ACTIVE,
                ":next": previous + 1,
                ":now": _common.now_iso(),
            },
            ConditionExpression=Attr("column").eq(BACKLOG) & Attr("runCount").eq(previous),
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as err:
        if _common.is_condition_failure(err):
            raise ApiError(
                409,
                "CardMoved",
                f"the card moved underneath you: a run starts only from {BACKLOG} and only once "
                "per drop, and this card is in another column or a concurrent drop has already "
                "claimed it. Re-read it and retry.",
                cardId=card_id,
                column=card.get("column"),
            ) from err
        raise


def release(claimed: Mapping[str, Any]) -> None:
    """Hand a claimed card back to `BACKLOG` after a run that never queued.

    Best effort, and `runCount` stays incremented on purpose: the counter is a claim
    token rather than a count of runs, so the concurrent drop that already lost cannot
    come back and win with the value it read. Swallows its own failures -- the caller is
    already raising the error the operator needs to see, and a second one on the way out
    would replace it with a 500.
    """
    card_id = claimed["cardId"]
    try:
        _common.cards_table.update_item(
            Key={"cardId": card_id},
            UpdateExpression="SET #column = :backlog, #updatedAt = :now",
            ExpressionAttributeNames={"#column": "column", "#updatedAt": "updatedAt"},
            ExpressionAttributeValues={":backlog": BACKLOG, ":now": _common.now_iso()},
            ConditionExpression=Attr("column").eq(ACTIVE)
            & Attr("runCount").eq(claimed["runCount"]),
        )
    except ClientError:
        LOG.warning("card=%s stayed ACTIVE after a run that never queued", card_id)


def store_task_id(card_id: str, task_id: str) -> dict[str, Any]:
    """The second, small update: the card records the run it claimed. Separate from the
    claim because the task id does not exist until the claim has been won, and a card
    naming a run that a lost claim never queued is worse than one naming none.

    Public, together with `release` and `create_claimed`, because `tasks.create` composes
    the same three writes for a card that is born claimed."""
    return _common.cards_table.update_item(
        Key={"cardId": card_id},
        UpdateExpression="SET #taskId = :taskId, #updatedAt = :now",
        ExpressionAttributeNames={"#taskId": "taskId", "#updatedAt": "updatedAt"},
        ExpressionAttributeValues={":taskId": task_id, ":now": _common.now_iso()},
        ConditionExpression=Attr("cardId").exists(),
        ReturnValues="ALL_NEW",
    )["Attributes"]


def create_claimed(config_id: str, *, description: str) -> dict[str, Any]:
    """The card a `POST /tasks` run gets: born `ACTIVE` in one `put_item`, and returned.

    Not `create` + `_claim`. A card dropped on `PLANNING` has to *win* its claim because
    a second operator may be dropping the same card at the same moment; here the id has
    just been minted and nothing else can have seen it, so there is no concurrent drop to
    fence out and the conditional-update dance would be ceremony. The card is simply born
    in the column a won claim leaves behind.

    `runCount` starts at **1** for the same reason `_claim` increments it: the counter is
    a claim token rather than a count of runs, and this card's one claim is already spent
    -- which is also what lets `release` pin it if the run never queues.

    `taskId` stays empty: the run does not exist yet, and `store_task_id` records it once
    it does, exactly as `_dispatch` does from the other side.
    """
    card = _card_item(config_id, description=description, column=ACTIVE, run_count=1)
    _common.cards_table.put_item(Item=card, ConditionExpression=Attr("cardId").not_exists())
    LOG.info("created claimed card=%s config=%s", card["cardId"], config_id)
    return card


def _dispatch(card: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """`BACKLOG -> PLANNING`: claim the card, queue the run, record the run.

    In that order. Queueing first cannot be undone when the claim then loses, whereas a
    claim that wins and fails to queue is repaired by `release`.
    """
    card_id = str(card["cardId"])
    config = _configuration(str(card["configId"]))
    claimed = _claim(card)
    try:
        task = runs.queue_run(
            config,
            prompt=str(card.get("description") or card.get("title") or ""),
            card=card,
            source="card",
        )
    except Exception:
        release(claimed)
        raise
    stored = store_task_id(card_id, task["taskId"])
    LOG.info("card=%s dispatched task=%s config=%s", card_id, task["taskId"], config["configId"])
    return 202, _with_column(stored, task)


# -- routes --------------------------------------------------------------------------


@handler
def create(event: Mapping[str, Any]) -> tuple[int, dict]:
    body = _common.parse_body(event)
    config_id, description = _common.require(body, "configId", "description")
    # Existence only. A card is a backlog note, so a configuration that is still
    # `CREATING` can hold cards -- `move` is where `READY` starts to matter, and
    # `runs.queue_run` is the one place that decides it.
    _configuration(config_id)

    title = body.get("title")
    title = title.strip() if isinstance(title, str) and title.strip() else None
    _screen_card_text(title, description)
    card = _card_item(config_id, description=description, title=title)
    _common.cards_table.put_item(Item=card, ConditionExpression=Attr("cardId").not_exists())
    LOG.info("created card=%s config=%s", card["cardId"], config_id)
    return 201, _with_column(card, None)


def _screen_card_text(*parts: str | None) -> None:
    """Screen a card's text before it is stored, not only when it is dispatched.

    `move` screens a card's prompt on its way into Planning (invariant 43), but by then the
    text is already in the Cards table: a card holding a pasted access key was refused at
    dispatch, returned to the backlog, and kept the key -- in the item, and in the table's
    point-in-time recovery for as long as that retains it. So a write the guardrail refuses
    stores nothing, and `move` still screens, because a card written before the guardrail
    existed, or against an older guardrail version, was never screened by this one.
    """
    text = "\n\n".join(part for part in parts if part)
    if text:
        runs.screen_prompt(text, refused="the card was not saved")


@handler
def list_(event: Mapping[str, Any]) -> tuple[int, dict]:
    config_id = _common.query_param(event, "configId")
    if not config_id:
        # The board is always one repository's. Without the partition key this would be
        # a `scan` of every configuration's cards, which is a different feature.
        raise ApiError(
            400,
            "MissingQueryParameter",
            "configId is required: a board is one configuration's cards",
            field="configId",
        )
    limit = _common.int_query_param(event, "limit", _LIST_LIMIT, minimum=1, maximum=_LIST_LIMIT_MAX)
    cursor = _common.decode_cursor(_common.query_param(event, "cursor"))

    query: dict[str, Any] = {
        "IndexName": "byConfig",
        "KeyConditionExpression": Key("configId").eq(config_id),
        # `createdAt` descending: a board reads newest first, and the GSI sort key is
        # what makes that free.
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if cursor:
        query["ExclusiveStartKey"] = cursor
    page = _common.cards_table.query(**query)
    cards = [_with_column(card, _claimed_run(card)) for card in page.get("Items", [])]
    return 200, {
        "cards": cards,
        "nextCursor": _common.encode_cursor(page.get("LastEvaluatedKey")),
    }


@handler
def get(event: Mapping[str, Any]) -> tuple[int, dict]:
    card = _card(_common.path_param(event, "cardId"))
    return 200, _with_column(card, _claimed_run(card))


@handler
def patch(event: Mapping[str, Any]) -> tuple[int, dict]:
    card_id = _common.path_param(event, "cardId")
    body = _common.parse_body(event)
    if not body:
        raise ApiError(400, "EmptyPatch", f"nothing to patch; editable fields are {_editable()}")
    if "column" in body:
        # A stored column written straight through is how a card ends up `ACTIVE` with
        # no run: the claim, the dispatch and the run count all live in `move`.
        raise ApiError(
            400,
            "NotEditable",
            "column is not patchable; move a card with POST /cards/{cardId}/move, which is what "
            "claims it and dispatches the run",
            field="column",
        )
    unknown = [key for key in body if key not in EDITABLE_FIELDS]
    if unknown:
        raise ApiError(
            400,
            "NotEditable",
            f"{unknown[0]} is not editable; editable fields are {_editable()}",
            field=unknown[0],
        )

    names: dict[str, str] = {"#updatedAt": "updatedAt"}
    values: dict[str, Any] = {":now": _common.now_iso()}
    sets: list[str] = []
    for index, field in enumerate(name for name in EDITABLE_FIELDS if name in body):
        value = body[field]
        if not isinstance(value, str) or not value.strip():
            raise ApiError(400, "InvalidField", f"{field} must be a non-empty string", field=field)
        names[f"#f{index}"] = field
        values[f":v{index}"] = value
        sets.append(f"#f{index} = :v{index}")
    # After validation and before the write: an edit is how a secret gets onto a card that
    # was screened clean when it was created.
    _screen_card_text(*(body[field] for field in EDITABLE_FIELDS if field in body))

    try:
        updated = _common.cards_table.update_item(
            Key={"cardId": card_id},
            UpdateExpression="SET " + ", ".join([*sets, "#updatedAt = :now"]),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression=Attr("cardId").exists(),
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as err:
        if _common.is_condition_failure(err):
            raise ApiError(404, "NotFound", "no such card", cardId=card_id) from err
        raise
    return 200, _with_column(updated, _claimed_run(updated))


@handler
def delete(event: Mapping[str, Any]) -> tuple[int, dict]:
    card_id = _common.path_param(event, "cardId")
    card = _card(card_id)
    _refuse_while_live(card, "deleted")
    # The task item outlives the card on purpose: a run's history is the audit trail,
    # and a card is a note about work, not the record of it.
    _common.cards_table.delete_item(
        Key={"cardId": card_id}, ConditionExpression=Attr("cardId").exists()
    )
    LOG.info("deleted card=%s config=%s", card_id, card.get("configId"))
    return 200, {"deleted": True, "cardId": card_id}


@handler
def move(event: Mapping[str, Any]) -> tuple[int, dict]:
    """The only door to a run, and the only writer of a stored column.

    The legal moves are enforced here and nowhere else -- the console draws five columns
    but decides nothing, because a client that can pick its own column can put a card
    somewhere no run can follow it.
    """
    card_id = _common.path_param(event, "cardId")
    (requested,) = _common.require(_common.parse_body(event), "column")
    target = requested.strip().upper()
    card = _card(card_id)

    if target == PLANNING:
        return _dispatch(card)
    if target in DERIVED_COLUMNS:
        # Real columns, but projections of a run's status rather than places a card can
        # be put. The message is built from the tuple so it cannot go stale.
        raise ApiError(
            409,
            "DerivedColumn",
            f"{target} is derived: {', '.join(DERIVED_COLUMNS)} are projections of the run's own "
            f"status, not places a card can be put. Drop on {PLANNING} to start a run.",
            column=target,
            derivedColumns=list(DERIVED_COLUMNS),
        )
    if target == ACTIVE:
        raise ApiError(
            409,
            "NotAnOperatorColumn",
            f"{ACTIVE} is what a won {PLANNING} drop leaves behind, not a move; drop on "
            f"{PLANNING} to start a run",
            column=target,
        )
    if target not in STORED_COLUMNS:
        raise ApiError(
            400,
            "UnknownColumn",
            f"{target} is not a column; the legal targets are {', '.join(MOVE_TARGETS)}",
            column=target,
            columns=list(MOVE_TARGETS),
        )

    # `BACKLOG` is a park and `DONE` is an operator calling the work finished. Both are
    # refused while the run is live, for the same reason: neither stops the container.
    task = _refuse_while_live(card, "parked" if target == BACKLOG else f"moved to {target}")
    updated = _store_column(card_id, target)
    LOG.info("card=%s moved to %s", card_id, target)
    return 200, _with_column(updated, task)


# The one definition of this module's routes. `infra/stacks/api_stack.py` wires them up and
# `tests/test_api_route_contract.py` holds the two in agreement, so a route added here and
# not there fails a test rather than answering 404 the first time a console asks for it.
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/cards", "create"),
    ("GET", "/cards", "list_"),
    ("GET", "/cards/{cardId}", "get"),
    ("PATCH", "/cards/{cardId}", "patch"),
    ("DELETE", "/cards/{cardId}", "delete"),
    ("POST", "/cards/{cardId}/move", "move"),
)
