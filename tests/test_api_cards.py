"""`cards.py`: the board, its single write path to a run, and the derived half.

Configuration items are seeded straight into the `Configurations` table rather than
through `handlers.configurations`: that module is a different unit of the same wave, and
a card test that imports it fails on its neighbour's half-written file instead of on
`cards.py`. `_load` imports inside the test for the reason in
`test_api_fakes.load_handlers` -- a module-level import is a collection error.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import api_event
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


def _create(
    cards: Any, description: str = "Add a retry to the uploader", **body: Any
) -> dict[str, Any]:
    event = api_event(
        "POST", "/cards", body={"configId": _CONFIG_ID, "description": description, **body}
    )
    out = cards.create(event, None)
    assert out["statusCode"] == 201, out["body"]
    return _body(out)


def _seed_card(common: Any, card_id: str, **overrides: Any) -> dict[str, Any]:
    """A card written directly, for the ordering assertions: `createdAt` is the GSI sort
    key and two cards created in the same millisecond share it."""
    item: dict[str, Any] = {
        "cardId": card_id,
        "configId": _CONFIG_ID,
        "title": card_id,
        "description": card_id,
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


def _activate(common: Any, card: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """The state a won claim leaves behind, written directly so a test can start from a
    live run without going through `move`. `#c` because `column` is a DynamoDB reserved
    word."""
    return common.cards_table.update_item(
        Key={"cardId": card["cardId"]},
        UpdateExpression="SET #c = :active, taskId = :taskId, runCount = :one",
        ExpressionAttributeNames={"#c": "column"},
        ExpressionAttributeValues={":active": "ACTIVE", ":taskId": task["taskId"], ":one": 1},
        ReturnValues="ALL_NEW",
    )["Attributes"]


def _stored(common: Any, card_id: str) -> dict[str, Any]:
    return common.cards_table.get_item(Key={"cardId": card_id})["Item"]


def _get(cards: Any, card_id: str) -> dict[str, Any]:
    out = cards.get(
        api_event("GET", f"/cards/{card_id}", path_parameters={"cardId": card_id}), None
    )
    assert out["statusCode"] == 200, out["body"]
    return _body(out)


def _move(cards: Any, card_id: str, column: str) -> dict[str, Any]:
    event = api_event(
        "POST",
        f"/cards/{card_id}/move",
        body={"column": column},
        path_parameters={"cardId": card_id},
    )
    return cards.move(event, None)


def _queued(aws: Any) -> list[dict[str, Any]]:
    """The dispatch messages, read without consuming them: `VisibilityTimeout=0` puts
    them straight back so a later assertion still sees the queue."""
    received = aws.sqs.receive_message(
        QueueUrl=aws.task_queue_url, MaxNumberOfMessages=10, VisibilityTimeout=0
    )
    return [json.loads(message["Body"]) for message in received.get("Messages", [])]


def _queue_depth(aws: Any) -> int:
    attributes = aws.sqs.get_queue_attributes(
        QueueUrl=aws.task_queue_url, AttributeNames=["ApproximateNumberOfMessages"]
    )["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"])


# -- create --------------------------------------------------------------------------


def test_create_defaults_the_title_to_the_first_line_of_the_description(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    long_line = "x" * 130
    card = _create(cards, description=f"\n   \n{long_line}\nand mention it in the changelog")

    assert card["title"] == "x" * 120, "the first non-empty line, truncated -- no model call"
    assert card["cardId"].startswith("crd_")
    assert card["column"] == "BACKLOG"
    assert card["runCount"] == 0
    assert card["createdAt"] == card["updatedAt"]

    short = _create(cards, description="\n\n  Add a retry  \nand a test\n")
    assert short["title"] == "Add a retry"


def test_create_takes_an_explicit_title_over_the_derived_one(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards, description="a long description", title="  Short title  ")
    assert card["title"] == "Short title"


def test_create_requires_a_config_id_and_a_description(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    out = cards.create(api_event("POST", "/cards", body={"configId": _CONFIG_ID}), None)
    assert out["statusCode"] == 400
    assert _body(out)["field"] == "description"

    bare = cards.create(api_event("POST", "/cards", body={"description": "x"}), None)
    assert bare["statusCode"] == 400
    assert _body(bare)["field"] == "configId"


def test_create_404s_on_an_unknown_configuration_but_does_not_require_ready(aws: Any) -> None:
    """A card is a backlog note, so it can be written against a configuration that is
    still provisioning. `READY` is `move`'s business, and `runs.queue_run` owns it."""
    common, cards = _load("cards")
    out = cards.create(
        api_event("POST", "/cards", body={"configId": "cfg_nope", "description": "x"}), None
    )
    assert out["statusCode"] == 404
    assert _body(out) == {
        "error": "NotFound",
        "message": "no such configuration",
        "configId": "cfg_nope",
    }

    _seed_config(common, status="CREATING")
    assert _create(cards, description="written while the runtime is still building")


def test_create_claimed_is_a_card_born_in_the_column_a_won_claim_leaves_behind(aws: Any) -> None:
    """The card a `POST /tasks` run gets. `ACTIVE` in one `put_item` and `runCount` at 1:
    the id is freshly minted so there is no concurrent drop to fence out, and the counter is
    a claim token whose one claim is already spent."""
    common, cards = _load("cards")
    _seed_config(common)
    long_line = "y" * 130
    card = cards.create_claimed(_CONFIG_ID, description=f"\n \n{long_line}\nand a test")

    assert card["column"] == "ACTIVE"
    assert card["runCount"] == 1
    assert card["taskId"] == "", "the run does not exist yet; `store_task_id` records it"
    assert card["cardId"].startswith("crd_")
    assert card["createdAt"] == card["updatedAt"]
    assert card["title"] == "y" * cards._TITLE_MAX
    assert common.cards_table.get_item(Key={"cardId": card["cardId"]})["Item"]["column"] == "ACTIVE"


def test_both_kinds_of_card_are_the_same_item_shape(aws: Any) -> None:
    """The cards-side drift test, and the reason `_card_item` exists rather than two
    literals: a card dragged into `Backlog` and a card born claimed by `POST /tasks` differ
    in the claim and in nothing else."""
    common, cards = _load("cards")
    _seed_config(common)
    description = "Add a --version flag"
    backlog = _create(cards, description=description)
    claimed = cards.create_claimed(_CONFIG_ID, description=description)

    # `_with_column` merges the derived view over the stored item on the way out of
    # `create`, so compare against what is actually in the table on both sides.
    stored = common.cards_table.get_item(Key={"cardId": backlog["cardId"]})["Item"]
    assert set(stored) == set(claimed), "one builder, so one set of attributes"

    volatile = {"cardId", "createdAt", "updatedAt"}
    differ = {key for key in stored if key not in volatile and stored[key] != claimed[key]}
    assert differ == {"column", "runCount"}, (
        "the claim is the only intentional difference between the two kinds of card"
    )
    assert (stored["column"], stored["runCount"]) == ("BACKLOG", 0)
    assert (claimed["column"], claimed["runCount"]) == ("ACTIVE", 1)


# -- list and get --------------------------------------------------------------------


def test_list_requires_a_config_id_and_merges_the_derived_view(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    parked = _create(cards, description="not started yet")
    running = _create(cards, description="in flight")
    _activate(common, running, _seed_task(common, running, status="CODING"))

    bare = cards.list_(api_event("GET", "/cards"), None)
    assert bare["statusCode"] == 400
    assert _body(bare)["field"] == "configId"

    out = cards.list_(api_event("GET", "/cards", query={"configId": _CONFIG_ID}), None)
    assert out["statusCode"] == 200
    board = {card["cardId"]: card for card in _body(out)["cards"]}
    assert _body(out)["nextCursor"] is None
    assert board[parked["cardId"]]["column"] == "BACKLOG"
    assert board[parked["cardId"]]["status"] is None
    assert board[running["cardId"]]["column"] == "CODING", "derived on read from the run's status"
    assert board[running["cardId"]]["storedColumn"] == "ACTIVE", "the operator's own value survives"
    assert board[running["cardId"]]["taskId"] == _TASK_ID
    # The derivation is computed, never stored: the item itself still says ACTIVE.
    assert _stored(common, running["cardId"])["column"] == "ACTIVE"


def test_list_pages_newest_first_through_a_cursor(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    for index in (1, 2, 3):
        _seed_card(common, f"crd_{index}", createdAt=f"2026-01-0{index}T00:00:00.000Z")

    first = _body(
        cards.list_(api_event("GET", "/cards", query={"configId": _CONFIG_ID, "limit": "2"}), None)
    )
    assert [card["cardId"] for card in first["cards"]] == ["crd_3", "crd_2"]
    assert first["nextCursor"]

    query = {"configId": _CONFIG_ID, "limit": "2", "cursor": first["nextCursor"]}
    second = _body(cards.list_(api_event("GET", "/cards", query=query), None))
    assert [card["cardId"] for card in second["cards"]] == ["crd_1"]

    forged = {"configId": _CONFIG_ID, "cursor": "!!!not-a-cursor"}
    out = cards.list_(api_event("GET", "/cards", query=forged), None)
    assert out["statusCode"] == 400
    assert _body(out)["error"] == "InvalidCursor"


def test_get_404s_on_an_unknown_card_and_otherwise_draws_the_claimed_run(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    out = cards.get(
        api_event("GET", "/cards/crd_nope", path_parameters={"cardId": "crd_nope"}), None
    )
    assert out["statusCode"] == 404
    assert _body(out)["error"] == "NotFound"

    card = _create(cards)
    task = _seed_task(
        common,
        card,
        status="SUCCEEDED",
        steps=["checkout", "plan", "code", "verify", "review", "submit"],
        prUrl="https://github.com/owner/repo/pull/7",
        verifyResult={"ok": True},
    )
    _activate(common, card, task)

    view = _get(cards, card["cardId"])
    assert view["column"] == "DONE"
    assert view["status"] == "SUCCEEDED"
    assert view["prUrl"] == "https://github.com/owner/repo/pull/7"
    assert view["verified"] is True
    assert "activity" not in view, "the feed is the activity route's, not the board's"


# -- move: the one door to a run -----------------------------------------------------


def test_move_backlog_to_planning_queues_exactly_one_run(aws: Any) -> None:
    """Two operators dropping the same card produce one run.

    The second call is the race: nothing is checked before the claim, so the loser is
    refused by the `column = BACKLOG AND runCount = :prev` condition itself rather than
    by a read that could go stale between the read and the write.
    """
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)

    first = _move(cards, card["cardId"], "PLANNING")
    second = _move(cards, card["cardId"], "PLANNING")

    assert first["statusCode"] == 202, first["body"]
    assert second["statusCode"] == 409
    assert _body(second)["error"] == "CardMoved"
    assert "moved underneath you" in _body(second)["message"]

    tasks = common.tasks_table.scan()["Items"]
    assert len(tasks) == 1, "one run, not two containers on the same branch"
    assert _queue_depth(aws) == 1
    assert _queued(aws) == [{"taskId": tasks[0]["taskId"], "configId": _CONFIG_ID, "resume": False}]

    # The run knows the card it came from, and the card knows the run it claimed.
    assert tasks[0]["cardId"] == card["cardId"]
    assert tasks[0]["source"] == "card"
    assert tasks[0]["prompt"] == card["description"]
    stored = _stored(common, card["cardId"])
    assert stored["column"] == "ACTIVE"
    assert stored["runCount"] == 1
    assert stored["taskId"] == tasks[0]["taskId"]
    # The board reads PLANNING off the QUEUED run without anything storing that word.
    assert _body(first)["column"] == "PLANNING"
    assert _get(cards, card["cardId"])["column"] == "PLANNING"


def test_move_upper_cases_the_requested_column_and_requires_one(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)

    bare = cards.move(
        api_event("POST", "/cards/x/move", body={}, path_parameters={"cardId": card["cardId"]}),
        None,
    )
    assert bare["statusCode"] == 400
    assert _body(bare)["field"] == "column"

    assert _move(cards, card["cardId"], "planning")["statusCode"] == 202


def test_a_derived_column_is_a_409_and_an_unknown_column_is_a_400(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)

    for column in ("CODING", "VERIFYING"):
        out = _move(cards, card["cardId"], column)
        assert out["statusCode"] == 409, column
        body = _body(out)
        assert body["error"] == "DerivedColumn"
        # The sentence is built by joining DERIVED_COLUMNS, so it cannot go stale.
        for name in cards.DERIVED_COLUMNS:
            assert name in body["message"]
        assert body["derivedColumns"] == list(cards.DERIVED_COLUMNS)

    unknown = _move(cards, card["cardId"], "SOMEDAY")
    assert unknown["statusCode"] == 400
    assert _body(unknown)["error"] == "UnknownColumn"
    assert _body(unknown)["columns"] == ["PLANNING", "BACKLOG", "DONE"]

    # ACTIVE is real and stored, so calling it unknown would be a lie -- but it is what
    # a won claim leaves behind rather than a move an operator makes.
    active = _move(cards, card["cardId"], "ACTIVE")
    assert active["statusCode"] == 409
    assert _body(active)["error"] == "NotAnOperatorColumn"

    assert _stored(common, card["cardId"])["column"] == "BACKLOG", "no refusal wrote a column"
    assert common.tasks_table.scan()["Items"] == []


def test_park_is_refused_while_the_run_is_live(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)
    task = _seed_task(common, card, status="CODING", steps=["checkout", "plan"])
    _activate(common, card, task)

    refused = _move(cards, card["cardId"], "BACKLOG")
    assert refused["statusCode"] == 409
    assert _body(refused)["error"] == "RunInFlight"
    assert _body(refused)["status"] == "CODING"
    assert _stored(common, card["cardId"])["column"] == "ACTIVE"

    _seed_task(common, card, status="SUCCEEDED", steps=["checkout", "plan", "code", "verify"])
    parked = _move(cards, card["cardId"], "BACKLOG")
    assert parked["statusCode"] == 200
    assert _stored(common, card["cardId"])["column"] == "BACKLOG"
    # The park has to be visible: a parked card whose last run succeeded must not be
    # dragged back into DONE by the derivation.
    assert _body(parked)["column"] == "BACKLOG"
    assert _get(cards, card["cardId"])["column"] == "BACKLOG"


def test_done_is_an_operator_act_and_also_waits_for_the_run(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)
    _activate(common, card, _seed_task(common, card, status="VERIFYING"))

    refused = _move(cards, card["cardId"], "DONE")
    assert refused["statusCode"] == 409
    assert _body(refused)["error"] == "RunInFlight"

    _seed_task(common, card, status="FAILED", steps=["checkout", "plan", "code"])
    done = _move(cards, card["cardId"], "DONE")
    assert done["statusCode"] == 200
    assert _body(done)["column"] == "DONE", "the operator outranks the run that died"
    assert _stored(common, card["cardId"])["column"] == "DONE"


def test_a_second_run_is_a_second_claim_after_a_park(aws: Any) -> None:
    """`runCount` is a claim token, not a run count: parking and dropping again is a new
    claim, and the pin is what keeps a duplicate of the first drop from winning."""
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)
    assert _move(cards, card["cardId"], "PLANNING")["statusCode"] == 202
    common.tasks_table.update_item(
        Key={"configId": _CONFIG_ID, "taskId": _stored(common, card["cardId"])["taskId"]},
        UpdateExpression="SET #s = :failed",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":failed": "FAILED"},
    )
    assert _move(cards, card["cardId"], "BACKLOG")["statusCode"] == 200

    assert _move(cards, card["cardId"], "PLANNING")["statusCode"] == 202
    assert _stored(common, card["cardId"])["runCount"] == 2
    assert len(common.tasks_table.scan()["Items"]) == 2


def test_a_run_that_cannot_be_queued_hands_the_card_back_to_backlog(aws: Any) -> None:
    """`runs.queue_run` is the only judge of whether a configuration may run, so the
    claim is written before it and undone when it refuses. `runCount` stays spent: the
    concurrent drop that already lost must not be able to win with the value it read."""
    common, cards = _load("cards")
    _seed_config(common, status="CREATING")
    card = _create(cards)

    out = _move(cards, card["cardId"], "PLANNING")
    assert out["statusCode"] == 409
    assert _body(out)["error"] == "ConfigurationNotReady"

    stored = _stored(common, card["cardId"])
    assert stored["column"] == "BACKLOG"
    assert stored["runCount"] == 1
    assert common.tasks_table.scan()["Items"] == []
    assert _queue_depth(aws) == 0


# -- patch and delete ----------------------------------------------------------------


def test_patch_merges_the_allow_list_and_leaves_everything_else_alone(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)
    event = api_event(
        "PATCH",
        f"/cards/{card['cardId']}",
        body={"title": "Retry the uploader", "description": "with a backoff"},
        path_parameters={"cardId": card["cardId"]},
    )
    out = cards.patch(event, None)
    assert out["statusCode"] == 200
    body = _body(out)
    assert body["title"] == "Retry the uploader"
    assert body["description"] == "with a backoff"
    assert body["column"] == "BACKLOG"
    assert body["updatedAt"] >= card["updatedAt"]


def test_patch_refuses_a_column_write_and_points_at_the_move_route(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)
    event = api_event(
        "PATCH",
        f"/cards/{card['cardId']}",
        body={"column": "ACTIVE"},
        path_parameters={"cardId": card["cardId"]},
    )
    out = cards.patch(event, None)
    assert out["statusCode"] == 400
    body = _body(out)
    assert body["error"] == "NotEditable"
    assert "POST /cards/{cardId}/move" in body["message"]
    assert _stored(common, card["cardId"])["column"] == "BACKLOG"


def test_patch_refuses_an_unknown_field_an_empty_body_and_an_unknown_card(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)

    def _patch(card_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return cards.patch(
            api_event("PATCH", f"/cards/{card_id}", body=body, path_parameters={"cardId": card_id}),
            None,
        )

    unknown = _patch(card["cardId"], {"configId": "cfg_other"})
    assert unknown["statusCode"] == 400
    assert _body(unknown)["field"] == "configId"
    for field in cards.EDITABLE_FIELDS:
        assert field in _body(unknown)["message"]

    empty = _patch(card["cardId"], {})
    assert empty["statusCode"] == 400
    assert _body(empty)["error"] == "EmptyPatch"

    blank = _patch(card["cardId"], {"title": "   "})
    assert blank["statusCode"] == 400
    assert _body(blank)["error"] == "InvalidField"

    missing = _patch("crd_nope", {"title": "x"})
    assert missing["statusCode"] == 404
    assert _body(missing)["error"] == "NotFound"


def test_delete_409s_while_the_claimed_run_is_live(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)
    _activate(common, card, _seed_task(common, card, status="DISPATCHED"))

    def _delete() -> dict[str, Any]:
        return cards.delete(
            api_event(
                "DELETE",
                f"/cards/{card['cardId']}",
                path_parameters={"cardId": card["cardId"]},
            ),
            None,
        )

    refused = _delete()
    assert refused["statusCode"] == 409
    assert _body(refused)["error"] == "RunInFlight"
    assert _stored(common, card["cardId"])["cardId"] == card["cardId"]

    _seed_task(common, card, status="SUCCEEDED_UNVERIFIED", steps=["checkout", "plan", "code"])
    deleted = _delete()
    assert deleted["statusCode"] == 200
    assert common.cards_table.get_item(Key={"cardId": card["cardId"]}).get("Item") is None
    # The run itself is the audit trail and outlives the note about it.
    assert len(common.tasks_table.scan()["Items"]) == 1


@pytest.mark.parametrize("status", ["FAILED_VERIFICATION", "CANCELLED"])
def test_a_vetoed_or_cancelled_run_is_not_refused_as_in_flight(aws: Any, status: str) -> None:
    """`_refuse_while_live` asks `runs.is_terminal`. A finished-and-vetoed or cancelled run
    is terminal, so a card in either state may be moved and deleted rather than blocked by
    a 409 from a guard that treats it as still in flight."""
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)
    _activate(
        common, card, _seed_task(common, card, status=status, steps=["checkout", "plan", "code"])
    )

    moved = _move(cards, card["cardId"], "DONE")
    assert moved["statusCode"] == 200, moved["body"]
    assert _body(moved)["column"] == "DONE"

    out = cards.delete(
        api_event("DELETE", f"/cards/{card['cardId']}", path_parameters={"cardId": card["cardId"]}),
        None,
    )
    assert out["statusCode"] == 200, out["body"]
    assert common.cards_table.get_item(Key={"cardId": card["cardId"]}).get("Item") is None
    # The run is the audit trail: deleting the card neither stops nor rewrites it.
    assert [task["status"] for task in common.tasks_table.scan()["Items"]] == [status]


# -- the derived half, directly -------------------------------------------------------


def test_a_failed_run_keeps_the_column_it_died_in(aws: Any) -> None:
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)
    _activate(
        common, card, _seed_task(common, card, status="FAILED", steps=["checkout", "plan", "code"])
    )

    view = _get(cards, card["cardId"])
    assert view["column"] == "CODING", "read from steps, never from FAILED"
    assert view["status"] == "FAILED"
    assert view["verified"] is None

    _seed_task(common, card, status="FAILED", steps=["checkout", "plan", "code", "verify"])
    assert _get(cards, card["cardId"])["column"] == "VERIFYING"

    # FAILED is not itself a column, which is exactly why `steps` has to answer this.
    assert "FAILED" not in cards.STORED_COLUMNS + cards.DERIVED_COLUMNS


def test_an_unknown_step_degrades_to_planning_rather_than_500(aws: Any) -> None:
    """Phase 2 owns the graph's node names and is being written in parallel, so a name
    this map has never heard of has to cost a wrong label, not a whole board."""
    common, cards = _load("cards")
    _seed_config(common)
    card = _create(cards)
    _activate(
        common, card, _seed_task(common, card, status="FAILED", steps=["checkout", "frobnicate"])
    )

    assert _get(cards, card["cardId"])["column"] == "PLANNING"
    assert (
        cards.derived_view({"column": "ACTIVE"}, {"status": "FAILED", "steps": []})["column"]
        == "PLANNING"
    )
    assert cards.derived_view({"column": "ACTIVE"}, {"status": "FAILED"})["column"] == "PLANNING"


def test_an_unknown_status_degrades_to_the_stored_column(aws: Any) -> None:
    _common, cards = _load("cards")
    view = cards.derived_view({"column": "ACTIVE", "taskId": "tsk_1"}, {"status": "CLONING"})
    assert view["column"] == "ACTIVE"
    assert view["status"] == "CLONING", "the raw status is still reported, just not mapped"


@pytest.mark.parametrize(
    ("status", "column"),
    [
        ("QUEUED", "PLANNING"),
        ("DISPATCHED", "PLANNING"),
        ("CHECKING_OUT", "PLANNING"),
        ("PLANNING", "PLANNING"),
        ("CODING", "CODING"),
        ("VERIFYING", "VERIFYING"),
        ("REVIEWING", "VERIFYING"),
        ("SUBMITTING", "VERIFYING"),
        ("SUCCEEDED", "DONE"),
        ("SUCCEEDED_UNVERIFIED", "DONE"),
        ("FAILED_VERIFICATION", "DONE"),
        ("CANCELLED", "DONE"),
    ],
)
def test_every_task_status_maps_onto_a_column(aws: Any, status: str, column: str) -> None:
    """REVIEWING deliberately has no column of its own: judging the tests and running
    them are one phase to an operator."""
    _common, cards, runs = _load("cards", "runs")
    assert status in runs.TASK_STATUSES
    assert cards.derived_view({"column": "ACTIVE"}, {"status": status})["column"] == column


def test_derived_view_is_pure_and_returns_a_fresh_dict(aws: Any) -> None:
    common, cards = _load("cards")
    card = {"cardId": "crd_1", "column": "ACTIVE", "taskId": "tsk_1"}
    task = {"taskId": "tsk_1", "status": "REVIEWING", "steps": ["checkout", "plan", "code"]}
    before = (dict(card), dict(task))

    first = cards.derived_view(card, task)
    second = cards.derived_view(card, task)
    assert first == second
    assert first is not second
    assert set(first) == {"column", "status", "taskId", "prUrl", "verified"}
    assert (card, task) == before, "neither argument is mutated"
    assert cards.derived_view(card, None) == {
        "column": "ACTIVE",
        "status": None,
        "taskId": "tsk_1",
        "prUrl": None,
        "verified": None,
    }
    # No item was read and none was written: the whole derivation is arithmetic.
    assert common.cards_table.scan()["Items"] == []


def test_the_route_table_matches_the_decorated_functions(aws: Any) -> None:
    """The local half of `tests/test_api_route_contract.py`, so this file fails on a
    route added without a `ROUTES` entry even before that test exists."""
    _common, cards = _load("cards")
    declared = {name for _, _, name in cards.ROUTES}
    assert declared == {"create", "list_", "get", "patch", "delete", "move"}
    for method, path, name in cards.ROUTES:
        assert method in {"GET", "POST", "PATCH", "DELETE"}
        assert path.startswith("/cards")
        assert getattr(cards, name).__api_handler__ is True
    assert not hasattr(cards, "lambda_handler"), "cards is HTTP-only; a queue Lambda is not a route"
