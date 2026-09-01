"""The kanban board's *contract*: its column vocabulary, its legal moves and its refusals.

Deliberately a second file next to `tests/test_api_cards.py`, and the split is worth not
"tidying" away:

* `tests/test_api_cards.py` is phase 5's **route-level** file -- what each of the six
  `cards` routes does with a well-formed event, field by field. It is not edited here, and
  nothing in phase 6 needed it to be: the three-tier failed-step reader is
  backwards-compatible with the bare-string `steps` those tests write.
* this file is phase 6's **board** file -- the five columns as one closed vocabulary, every
  legal move and every refusal as a table of `(status, error code)`, the run-count equality
  condition the claim is pinned on, the rules that place a *failed* run in the column it
  died in, and the decision that a card is **not** cascade-deleted with its configuration.

`_load` imports the handler inside each test for the reason spelled out in
`test_api_fakes.load_handlers`: `boto3.resource("dynamodb")` raises `NoRegionError` at
construction, and a module-level handler import therefore fails at *collection* time,
before `conftest._aws_env` has set a region -- which reads as a broken suite rather than as
a failing test. The `_seed_*` helpers below are duplicated rather than imported out of
`tests/test_api_cards.py` so this file does not depend on a neighbour's private names.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import REPO_ROOT, api_event
from test_api_fakes import load_handlers as _load

_CONFIG_ID = "cfg_0123456789ab"
_TASK_ID = "tsk_1763036400000_a1b2c3"
_STAMP = "2026-01-01T00:00:00.000Z"


def _body(out: dict[str, Any]) -> dict[str, Any]:
    return json.loads(out["body"])


def _seed_config(common: Any, *, status: str = "READY", **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "configId": _CONFIG_ID,
        "repository": "owner/repo",
        "repositoryUrl": "https://github.com/owner/repo",
        "status": status,
        "agentRuntimeArn": (
            f"arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/rca_{_CONFIG_ID}-XYZ"
        ),
        "agentRuntimeId": f"rca_{_CONFIG_ID}-XYZ",
        "defaultBranch": "main",
        "createdAt": _STAMP,
        "updatedAt": _STAMP,
    }
    item.update(overrides)
    common.config_table.put_item(Item=item)
    return item


def _seed_card(common: Any, card_id: str = "crd_board", **overrides: Any) -> dict[str, Any]:
    """A card written straight into the table, so a test can start from any stored column
    without going through `move` -- which is the thing under test in half of this file."""
    item: dict[str, Any] = {
        "cardId": card_id,
        "configId": _CONFIG_ID,
        "title": card_id,
        "description": f"work described by {card_id}",
        "column": "BACKLOG",
        "taskId": "",
        "runCount": 0,
        "createdAt": _STAMP,
        "updatedAt": _STAMP,
    }
    item.update(overrides)
    common.cards_table.put_item(Item=item)
    return item


def _seed_task(
    common: Any, card: dict[str, Any], *, status: str, **overrides: Any
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "configId": card["configId"],
        "taskId": _TASK_ID,
        "cardId": card["cardId"],
        "prompt": card["description"],
        "status": status,
        "steps": [],
        "activity": [],
        "resumeCount": 0,
        "source": "card",
        "createdAt": _STAMP,
        "updatedAt": _STAMP,
    }
    item.update(overrides)
    common.tasks_table.put_item(Item=item)
    return item


def _stored(common: Any, card_id: str) -> dict[str, Any]:
    return common.cards_table.get_item(Key={"cardId": card_id})["Item"]


def _get(cards: Any, card_id: str) -> dict[str, Any]:
    out = cards.get(
        api_event("GET", f"/cards/{card_id}", path_parameters={"cardId": card_id}), None
    )
    assert out["statusCode"] == 200, out["body"]
    return _body(out)


def _move(cards: Any, card_id: str, column: Any) -> dict[str, Any]:
    body = {} if column is None else {"column": column}
    event = api_event(
        "POST", f"/cards/{card_id}/move", body=body, path_parameters={"cardId": card_id}
    )
    return cards.move(event, None)


def _queue_depth(aws: Any) -> int:
    attributes = aws.sqs.get_queue_attributes(
        QueueUrl=aws.task_queue_url, AttributeNames=["ApproximateNumberOfMessages"]
    )["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"])


def _failed_view(cards: Any, common: Any, steps: Any) -> str:
    """The column a `FAILED` run with these `steps` lands in, read through the real read
    path (`cards.get` -> `_claimed_run` -> `derived_view`) rather than through the helper,
    so the projection is exercised the way a board poll exercises it."""
    card = _seed_card(common, "crd_failed", column="ACTIVE", taskId=_TASK_ID, runCount=1)
    _seed_task(common, card, status="FAILED", steps=steps)
    return _get(cards, card["cardId"])["column"]


# -- the vocabulary -------------------------------------------------------------------


def test_the_board_has_exactly_five_columns(aws: Any) -> None:
    """Five columns, left to right, in one tuple.

    Written as a literal rather than derived from the two halves, so reordering the board
    or adding a sixth column is a deliberate edit to this line and not a side effect.
    """
    _common, cards = _load("cards")
    assert cards.BOARD_COLUMNS == ("BACKLOG", "PLANNING", "CODING", "VERIFYING", "DONE")
    assert cards.STORED_COLUMNS == ("BACKLOG", "ACTIVE", "DONE")
    assert cards.DERIVED_COLUMNS == ("PLANNING", "CODING", "VERIFYING")

    # Six names between the two halves, five columns: `ACTIVE` is the difference.
    assert set(cards.STORED_COLUMNS) | set(cards.DERIVED_COLUMNS) == set(cards.BOARD_COLUMNS) | {
        "ACTIVE"
    }
    assert "ACTIVE" not in cards.BOARD_COLUMNS, (
        "ACTIVE is stored and never drawn: it is what a won PLANNING claim leaves behind, and "
        "the projection of the claimed run is what the board draws in its place"
    )
    # `REVIEWING` maps onto `VERIFYING` instead of getting a column: running the tests and
    # judging them are one phase to an operator, and a sixth column for a step that lasts a
    # minute costs board width for no information.
    assert "REVIEWING" not in cards.BOARD_COLUMNS
    assert "REVIEWING" not in cards.STORED_COLUMNS + cards.DERIVED_COLUMNS


def test_every_mapping_lands_in_the_board_vocabulary(aws: Any) -> None:
    """Both projection maps are closed over the five drawn columns, and both are total over
    the vocabulary `runs` owns from the other side of the api/agent boundary."""
    _common, cards, runs = _load("cards", "runs")
    board = set(cards.BOARD_COLUMNS)
    assert set(cards._STATUS_COLUMN.values()) <= board, "a status projected onto no column"
    assert set(cards._STEP_COLUMN.values()) <= board, "a step projected onto no column"

    invariant_13 = (
        "invariant 13: this vocabulary is duplicated across api/ and agent/ on purpose, so a "
        "renamed status or node is four edits across three units -- state.py, runs.py, "
        "cards.py and the console -- and the map that fell behind must fail here"
    )
    # `FAILED` is the one status with no entry: it says the run stopped, never where it
    # was, so `steps` answers it instead.
    assert set(cards._STATUS_COLUMN) | {"FAILED"} == set(runs.TASK_STATUSES), invariant_13
    assert set(cards._STEP_COLUMN) == set(runs.STEPS), invariant_13
    assert cards._STATUS_COLUMN["REVIEWING"] == "VERIFYING"


# -- the legal moves, and every refusal ------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "run_status", "target", "status", "error"),
    [
        # The one move that starts a run.
        ("BACKLOG", None, "PLANNING", 202, None),
        ("BACKLOG", None, "planning", 202, None),  # the target is upper-cased
        # Parks and operator judgements, from every stored column they are legal in.
        ("BACKLOG", None, "BACKLOG", 200, None),
        ("BACKLOG", None, "DONE", 200, None),
        ("ACTIVE", "SUCCEEDED", "BACKLOG", 200, None),
        ("ACTIVE", "FAILED", "DONE", 200, None),
        # Both were `409 RunInFlight` until `runs.TERMINAL_STATUSES` grew the container's
        # last two names: an operator could not clear a finished-and-vetoed card off the
        # board, nor a run they had cancelled themselves.
        ("ACTIVE", "FAILED_VERIFICATION", "DONE", 200, None),
        ("ACTIVE", "CANCELLED", "BACKLOG", 200, None),
        ("DONE", None, "BACKLOG", 200, None),
        # A run starts only from BACKLOG, and only once per drop.
        ("DONE", None, "PLANNING", 409, "CardMoved"),
        ("ACTIVE", "CODING", "PLANNING", 409, "CardMoved"),
        # Neither a park nor a DONE stops the container, so neither is allowed to pretend.
        ("ACTIVE", "CODING", "BACKLOG", 409, "RunInFlight"),
        ("ACTIVE", "VERIFYING", "DONE", 409, "RunInFlight"),
        # Real column names that are not places a card can be put -- 409, not 400.
        ("BACKLOG", None, "CODING", 409, "DerivedColumn"),
        ("BACKLOG", None, "VERIFYING", 409, "DerivedColumn"),
        ("BACKLOG", None, "ACTIVE", 409, "NotAnOperatorColumn"),
        # A name that is not a column at all.
        ("BACKLOG", None, "SOMEDAY", 400, "UnknownColumn"),
    ],
)
def test_the_legal_moves_and_every_refusal(
    aws: Any, stored: str, run_status: str | None, target: str, status: int, error: str | None
) -> None:
    """The whole move table in one place, enforced server-side and nowhere else.

    Every refused row also asserts that nothing was written: a refusal that moves the
    stored column or queues a run is the failure this table exists to catch, and it is
    invisible in a response body that already said 409.
    """
    common, cards = _load("cards")
    _seed_config(common)
    card = _seed_card(
        common,
        column=stored,
        **({"taskId": _TASK_ID, "runCount": 1} if run_status else {}),
    )
    if run_status:
        _seed_task(common, card, status=run_status, steps=["checkout", "plan", "code"])

    out = _move(cards, card["cardId"], target)
    assert out["statusCode"] == status, out["body"]
    if error is None:
        assert "error" not in _body(out)
    else:
        assert _body(out)["error"] == error

    after = _stored(common, card["cardId"])
    if status == 202:
        assert after["column"] == "ACTIVE", "a won claim leaves the card ACTIVE"
        assert after["runCount"] == card["runCount"] + 1
    elif status == 200:
        assert after["column"] == target.upper()
        assert after["runCount"] == card["runCount"], "only a claim spends the counter"
    else:
        assert after["column"] == stored, "a refusal wrote a column"
        assert after["runCount"] == card["runCount"], "a refusal spent a claim"

    # One dispatch for the one move that dispatches, and none for anything else. The
    # seeded run is written directly, so it never went near the queue.
    expected_tasks = (1 if run_status else 0) + (1 if status == 202 else 0)
    assert len(common.tasks_table.scan()["Items"]) == expected_tasks
    assert _queue_depth(aws) == (1 if status == 202 else 0)


def test_a_missing_column_and_an_unknown_card_are_refused_before_anything_else(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    card = _seed_card(common)

    bare = _move(cards, card["cardId"], None)
    assert bare["statusCode"] == 400
    assert _body(bare)["error"] == "MissingField"
    assert _body(bare)["field"] == "column"

    missing = _move(cards, "crd_nope", "PLANNING")
    assert missing["statusCode"] == 404
    assert _body(missing)["error"] == "NotFound"

    assert _stored(common, card["cardId"])["column"] == "BACKLOG"
    assert common.tasks_table.scan()["Items"] == []


def test_a_derived_target_names_every_projection_it_has(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `DerivedColumn` sentence is built *from* `DERIVED_COLUMNS`, so a fourth
    projection cannot leave the message naming three.

    A hand-listed sentence passes phase 5's route test (which only checks the three names
    that exist today) and fails here, which is the whole point of the extra column.
    """
    common, cards = _load("cards")
    _seed_config(common)
    card = _seed_card(common)
    monkeypatch.setattr(cards, "DERIVED_COLUMNS", (*cards.DERIVED_COLUMNS, "DEPLOYING"))

    for target in ("DEPLOYING", "CODING"):
        out = _move(cards, card["cardId"], target)
        assert out["statusCode"] == 409, target
        body = _body(out)
        assert body["error"] == "DerivedColumn"
        assert "DEPLOYING" in body["message"], "the sentence was hand-listed, not joined"
        for name in cards.DERIVED_COLUMNS:
            assert name in body["message"]
        assert body["derivedColumns"] == list(cards.DERIVED_COLUMNS)

    assert _stored(common, card["cardId"])["column"] == "BACKLOG"
    assert common.tasks_table.scan()["Items"] == []


def test_the_claim_is_pinned_on_the_run_count_as_well_as_the_column(aws: Any) -> None:
    """`column = :expected` alone cannot catch a replayed drop.

    A card that was claimed and then parked is legitimately back in `BACKLOG`, so the
    column condition of a *stale* second request is trivially true again. Without
    `runCount = :prev` two concurrent dispatch requests both win and two containers race
    the same branch. The counter is a claim token, not a count of runs, which is why
    `release` leaves it spent.
    """
    common, cards = _load("cards")
    _seed_config(common)
    card = _seed_card(common)
    assert card["runCount"] == 0

    claimed = cards._claim(card)
    assert claimed["column"] == "ACTIVE"
    assert claimed["runCount"] == 1

    # The park `release` performs: back to BACKLOG, `runCount` left at 1.
    common.cards_table.update_item(
        Key={"cardId": card["cardId"]},
        UpdateExpression="SET #column = :backlog",
        ExpressionAttributeNames={"#column": "column"},
        ExpressionAttributeValues={":backlog": "BACKLOG"},
    )
    assert _stored(common, card["cardId"])["column"] == "BACKLOG"

    with pytest.raises(common.ApiError) as replayed:
        cards._claim(card)  # the same stale dict, still saying runCount == 0
    assert replayed.value.status == 409
    assert replayed.value.code == "CardMoved"
    assert _stored(common, card["cardId"])["runCount"] == 1, "the replay did not claim again"

    # A card with no `runCount` at all fails the condition too: refusing a dispatch is the
    # safe direction, and a card without the token cannot prove it has not been claimed.
    bare = _seed_card(common, "crd_no_counter")
    common.cards_table.update_item(
        Key={"cardId": bare["cardId"]}, UpdateExpression="REMOVE runCount"
    )
    with pytest.raises(common.ApiError) as refused:
        cards._claim({"cardId": bare["cardId"], "column": "BACKLOG"})
    assert refused.value.status == 409
    assert refused.value.code == "CardMoved"
    assert _stored(common, bare["cardId"])["column"] == "BACKLOG"


# -- where a failed run is drawn -------------------------------------------------------


def test_a_failed_run_is_placed_by_the_last_failed_step(aws: Any) -> None:
    """The last `FAILED` entry wins, and it wins over later entries too: a resumed run can
    fail twice, and the second failure is the one the board is about. Never from `status`,
    which `FAILED` has already overwritten."""
    common, cards = _load("cards")
    _seed_config(common)
    assert (
        _failed_view(
            cards,
            common,
            [
                {"step": "plan", "status": "OK"},
                {"step": "code", "status": "FAILED"},
                {"step": "verify", "status": "SKIPPED"},
            ],
        )
        == "CODING"
    )
    assert (
        _failed_view(
            cards,
            common,
            [
                {"step": "code", "status": "FAILED"},
                {"step": "code", "status": "OK"},
                {"step": "verify", "status": "FAILED"},
            ],
        )
        == "VERIFYING"
    )


def test_a_run_killed_mid_node_is_placed_by_its_last_entered_step(aws: Any) -> None:
    """No `FAILED` entry at all is the common case, not the exotic one: a container killed
    mid-node leaves a `RUNNING` entry and a timed-out run leaves nothing marked failed. The
    last entry the run actually *entered* still names where it was -- and a `SKIPPED` entry
    names a step it never entered, so it is passed over rather than reported."""
    common, cards = _load("cards")
    _seed_config(common)
    assert (
        _failed_view(
            cards,
            common,
            [{"step": "code", "status": "OK"}, {"step": "verify", "status": "RUNNING"}],
        )
        == "VERIFYING"
    )
    assert (
        _failed_view(
            cards,
            common,
            [
                {"step": "code", "status": "OK"},
                {"step": "verify", "status": "SKIPPED"},
                {"step": "submit", "status": "SKIPPED"},
            ],
        )
        == "CODING"
    )


def test_a_dispatch_that_never_started_has_no_column_of_its_own(aws: Any) -> None:
    """`_failed_column` distinguishes "no step information" from "a step name this bundle
    has never heard of": only the second is the `PLANNING` degradation. The caller then
    collapses both to `PLANNING`, the leftmost live column -- a dispatch that never got
    going has not got past planning."""
    _common, cards = _load("cards")
    assert cards._failed_column({"steps": []}) is None
    assert cards._failed_column({}) is None
    assert cards._failed_column({"steps": [{"step": "plan", "status": "SKIPPED"}]}) is None
    # A non-list `steps` degrades rather than raising: this runs on every board read, so a
    # garbage attribute must cost a wrong label and not a 500 board.
    assert cards._failed_column({"steps": "checkout"}) is None

    for task in ({"status": "FAILED", "steps": []}, {"status": "FAILED"}):
        assert cards.derived_view({"column": "ACTIVE"}, task)["column"] == "PLANNING"


def test_the_bare_string_step_shape_still_places_a_failed_run(aws: Any) -> None:
    """The bare node name is what `store.set_status(state, status, step=name)` appends
    today, and `api/` and the container are separate deployment units that must not import
    each other -- so the reader takes both shapes and a mixed list as well, rather than
    assuming the one it happened to be written against. A string carries no outcome, which
    is "not SKIPPED", which makes it eligible as the last-entry fallback."""
    _common, cards = _load("cards")
    assert cards._failed_column({"steps": ["checkout", "plan", "code"]}) == "CODING"
    assert cards._failed_column({"steps": ["checkout", "frobnicate"]}) == "PLANNING"
    assert cards._failed_column(
        {"steps": ["checkout", {"step": "verify", "status": "FAILED"}]}
    ) == ("VERIFYING")


# -- the derivation is never written back ----------------------------------------------


def test_derived_view_writes_nothing_and_is_pure(aws: Any) -> None:
    """Purity is what keeps both board reads inside the read-only posture: `get` and
    `list_` reach `get_item`/`query` and nothing else, so neither needs a write grant on
    `Cards` (`tests/test_api_grant_contract.py` resolves that transitively)."""
    common, cards = _load("cards")
    card = {"cardId": "crd_1", "column": "ACTIVE", "taskId": _TASK_ID}
    task = {
        "taskId": _TASK_ID,
        "status": "FAILED",
        "steps": [{"step": "code", "status": "FAILED"}, {"step": "verify", "status": "SKIPPED"}],
    }
    before = (dict(card), [dict(row) for row in task["steps"]])

    first = cards.derived_view(card, task)
    second = cards.derived_view(card, task)
    assert first == second
    assert first is not second, "a fresh dict every call, so a caller cannot mutate a cache"
    assert set(first) == {"column", "status", "taskId", "prUrl", "verified"}
    assert first["column"] == "CODING"
    assert (card, [dict(row) for row in task["steps"]]) == before, "neither argument is mutated"
    assert common.cards_table.scan()["Items"] == []
    assert common.tasks_table.scan()["Items"] == []

    # And through the routes: the stored item still says ACTIVE after both reads, and the
    # operator's own value is reported alongside the projection.
    _seed_config(common)
    stored_card = _seed_card(common, column="ACTIVE", taskId=_TASK_ID, runCount=1)
    _seed_task(common, stored_card, status="CODING", steps=["checkout", "plan"])

    view = _get(cards, stored_card["cardId"])
    assert view["column"] == "CODING"
    assert view["storedColumn"] == "ACTIVE"
    assert _stored(common, stored_card["cardId"])["column"] == "ACTIVE"

    listed = cards.list_(api_event("GET", "/cards", query={"configId": _CONFIG_ID}), None)
    assert listed["statusCode"] == 200
    (drawn,) = _body(listed)["cards"]
    assert (drawn["column"], drawn["storedColumn"]) == ("CODING", "ACTIVE")
    assert _stored(common, stored_card["cardId"])["column"] == "ACTIVE"


# -- the non-cascade decision ----------------------------------------------------------


def test_a_card_is_not_cascade_deleted_with_its_configuration(aws: Any) -> None:
    """A card is deliberately **not** deleted with its configuration.

    Cascading would put a `Cards` read+write grant on the configuration delete handler and
    make configuration teardown depend on board state. Instead a card simply cannot outlive
    its configuration usefully: the list route needs a `configId`, a drop on `PLANNING`
    404s at the configuration read, and no card handler writes to `Configurations` at all.
    """
    common, cards = _load("cards")
    _seed_config(common)
    card = _seed_card(common)

    common.config_table.delete_item(Key={"configId": _CONFIG_ID})

    # (i) the read path never touches `Configurations`, so the card is still readable.
    assert _get(cards, card["cardId"])["cardId"] == card["cardId"]
    assert _stored(common, card["cardId"])["column"] == "BACKLOG"

    # (ii) a dispatch 404s at `_configuration`, before the claim -- so nothing was spent.
    out = _move(cards, card["cardId"], "PLANNING")
    assert out["statusCode"] == 404
    assert _body(out)["error"] == "NotFound"
    assert _body(out)["configId"] == _CONFIG_ID
    after = _stored(common, card["cardId"])
    assert (after["column"], after["runCount"]) == ("BACKLOG", 0)
    assert common.tasks_table.scan()["Items"] == []
    assert _queue_depth(aws) == 0

    # (iii) and the board itself is unreachable without a configuration to draw it for.
    bare = cards.list_(api_event("GET", "/cards"), None)
    assert bare["statusCode"] == 400
    assert _body(bare)["error"] == "MissingQueryParameter"

    # (iv) the decision, asserted where it would be broken: the configuration teardown
    # path does not so much as name the Cards table.
    reason = (
        "cascading would put a Cards read+write grant on the configuration delete handler and "
        "make configuration teardown depend on board state"
    )
    for module in ("configurations.py", "provisioner.py"):
        source = (REPO_ROOT / "api/src/handlers" / module).read_text(encoding="utf-8")
        assert "cards_table" not in source, f"{module} reaches the Cards table: {reason}"
