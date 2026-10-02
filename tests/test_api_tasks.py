"""`tasks.py`: creating a run, listing runs, one run's view, and the activity feed.

Configurations and tasks are seeded straight into the tables rather than through
`configurations.create`/`cards.move`: this file is about the task routes, and going
through another module's handler would make its bugs read as this one's.

Handler modules build their table handles at import, so `_load` reloads them *inside*
`mock_aws` -- see `test_api_fakes.load_handlers`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from boto3.dynamodb.conditions import Attr, Key
from conftest import api_event
from test_api_fakes import FakeAgentCore, client_error, install
from test_api_fakes import load_handlers as _load

_CONFIG_ID = "cfg_1"
_RUNTIME_ID = "rca_cfg_1-ABC123"


def _seed_config(common: Any, **overrides: Any) -> dict[str, Any]:
    """A `READY` configuration. An override of `None` *drops* the key, which is how a
    configuration that has not been provisioned yet is spelled."""
    config = {
        "configId": _CONFIG_ID,
        "repository": "owner/repo",
        "repositoryUrl": "https://github.com/owner/repo",
        "status": "READY",
        "region": "ap-southeast-1",
        "agentRuntimeId": _RUNTIME_ID,
        "agentRuntimeArn": (
            f"arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/{_RUNTIME_ID}"
        ),
        "createdAt": "2026-01-01T00:00:00.000Z",
    }
    config.update(overrides)
    config = {key: value for key, value in config.items() if value is not None}
    common.config_table.put_item(Item=config)
    return config


def _seed_task(common: Any, task_id: str, **overrides: Any) -> dict[str, Any]:
    task = {
        "configId": _CONFIG_ID,
        "taskId": task_id,
        "prompt": "make the thing work",
        "status": "QUEUED",
        "sessionId": f"{task_id}-{'0' * 32}",
        "activity": [],
        "steps": [],
        "resumeCount": 0,
        "source": "api",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:00.000Z",
    }
    task.update(overrides)
    common.tasks_table.put_item(Item=task)
    return task


def _seed_live_task(common: Any, task_id: str, **overrides: Any) -> dict[str, Any]:
    """A task that is actually running, i.e. one there is a session to stop.

    `_seed_task` writes `sessionId` but no `agentRuntimeArn`, and without the arn
    `watchdog.stop_session` takes its "nothing to stop" branch and never calls
    `StopRuntimeSession` -- so a cancel test written against it asserts nothing about the
    stop while looking green. Seeded here rather than in `_seed_task` so the existing tests
    keep the shape they were written against.
    """
    live = {
        "status": "CODING",
        "agentRuntimeArn": (
            f"arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/{_RUNTIME_ID}"
        ),
    }
    return _seed_task(common, task_id, **{**live, **overrides})


def _cancel_event(task_id: str) -> dict[str, Any]:
    """`_task_event` hard-codes GET, and this is the one route here that is a POST."""
    return api_event(
        "POST",
        "/tasks/{taskId}/cancel",
        path_parameters={"taskId": task_id},
        query={"configId": _CONFIG_ID},
    )


def _stored(common: Any, task_id: str) -> dict[str, Any]:
    return (
        common.tasks_table.get_item(Key={"configId": _CONFIG_ID, "taskId": task_id}).get("Item")
        or {}
    )


def _rows(count: int) -> list[dict[str, str]]:
    return [
        {"at": f"2026-01-01T00:{index:02d}:00.000Z", "kind": "tool", "text": f"row {index}"}
        for index in range(count)
    ]


def _body(out: dict[str, Any]) -> dict[str, Any]:
    return json.loads(out["body"])


def _task_event(task_id: str, path: str = "", **query: str) -> dict[str, Any]:
    return api_event(
        "GET",
        f"/tasks/{{taskId}}{path}",
        path_parameters={"taskId": task_id},
        query={"configId": _CONFIG_ID, **query},
    )


class _PagingTable:
    """A `Tasks` handle that always has another page.

    Real 1 MB paging needs megabytes of seed data and moto's page boundaries are not
    what is under test -- the internal page budget is. Installed over
    `_common.tasks_table`, which only works because `tasks.py` reaches the handle
    through the module at call time rather than aliasing it at import.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "Items": [],
            "LastEvaluatedKey": {"configId": _CONFIG_ID, "taskId": f"tsk_page_{len(self.calls)}"},
        }


class _RecordingTable:
    """A `Tasks` handle that answers one item and remembers how it was asked."""

    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.kwargs: dict[str, Any] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return {"Item": self.item}


class _ServiceOrderTable:
    """A `Tasks` handle whose `query` follows the **service's** order of operations:
    `Limit` bounds the rows *read* from the partition and a `FilterExpression` runs
    afterwards, over that page only.

    Deliberately not moto: the ordering is the whole subject of the test that uses this,
    and a backend that filters before it limits would pass that test for the naive
    implementation too -- the same class of trap as moto's AgentCore backend
    (`tests/test_api_fakes.py`).
    """

    def __init__(self, items: list[dict[str, Any]]) -> None:
        # Newest-first on the time-sortable `taskId`, the only direction this module
        # queries in.
        self.items = sorted(items, key=lambda item: item["taskId"], reverse=True)
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _matches(condition: Any, item: dict[str, Any]) -> bool:
        if condition is None:
            return True
        # boto3's own accessor for a built condition: `values` is `(Attr, value)`.
        attribute, value = condition.get_expression()["values"]
        return item.get(attribute.name) == value

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        offset = 0
        start = kwargs.get("ExclusiveStartKey")
        if start:
            offset = (
                next(
                    index
                    for index, item in enumerate(self.items)
                    if item["taskId"] == start["taskId"]
                )
                + 1
            )
        read = (
            self.items[offset : offset + kwargs["Limit"]]
            if "Limit" in kwargs
            else self.items[offset:]
        )
        answer: dict[str, Any] = {
            "Items": [item for item in read if self._matches(kwargs.get("FilterExpression"), item)]
        }
        if read and offset + len(read) < len(self.items):
            answer["LastEvaluatedKey"] = {"configId": _CONFIG_ID, "taskId": read[-1]["taskId"]}
        return answer


# -- create -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [({"prompt": "ship it"}, "configId"), ({"configId": _CONFIG_ID}, "prompt")],
)
def test_create_requires_a_config_id_and_a_prompt(aws: Any, body: dict, field: str) -> None:
    common, tasks = _load("tasks")
    _seed_config(common)
    out = tasks.create(api_event("POST", "/tasks", body=body), None)
    assert out["statusCode"] == 400
    assert _body(out) == {
        "error": "MissingField",
        "message": f"{field} is required and must be a non-empty string",
        "field": field,
    }


def test_create_404s_on_a_configuration_that_does_not_exist(aws: Any) -> None:
    common, tasks = _load("tasks")
    event = api_event("POST", "/tasks", body={"configId": "cfg_missing", "prompt": "ship it"})
    out = tasks.create(event, None)
    assert out["statusCode"] == 404
    assert _body(out)["error"] == "NotFound"
    assert common.tasks_table.scan()["Count"] == 0
    # The configuration read comes first, so a 404 leaves no card behind either.
    assert common.cards_table.scan()["Count"] == 0


def test_create_returns_the_queued_task_item_and_one_dispatch(aws: Any) -> None:
    common, tasks = _load("tasks")
    _seed_config(common)
    out = tasks.create(
        api_event("POST", "/tasks", body={"configId": _CONFIG_ID, "prompt": "ship it"}), None
    )
    assert out["statusCode"] == 202

    task = _body(out)
    assert task["taskId"].startswith("tsk_")
    assert task["status"] == "QUEUED"
    assert task["source"] == "api"
    assert task["prompt"] == "ship it"
    assert task["activity"] == [] and task["steps"] == []

    stored = common.tasks_table.get_item(Key={"configId": _CONFIG_ID, "taskId": task["taskId"]})[
        "Item"
    ]
    assert stored["prompt"] == "ship it"

    # Every run has a card, and this one is born claimed: `runCount` is a claim token and
    # the one claim is spent, so the board draws it the moment the run is queued.
    assert task["cardId"].startswith("crd_")
    card = common.cards_table.get_item(Key={"cardId": task["cardId"]})["Item"]
    assert card["column"] == "ACTIVE"
    assert card["runCount"] == 1
    assert card["taskId"] == task["taskId"]
    assert card["description"] == "ship it" and card["title"] == "ship it"

    messages = aws.sqs.receive_message(QueueUrl=aws.task_queue_url, MaxNumberOfMessages=10)[
        "Messages"
    ]
    assert [json.loads(message["Body"]) for message in messages] == [
        {"taskId": task["taskId"], "configId": _CONFIG_ID, "resume": False}
    ]


def test_create_refuses_a_configuration_that_is_not_ready(aws: Any) -> None:
    """`runs.queue_run` owns the refusal; this asserts the route does not route around
    it -- a run dispatched against a `CREATING` config dies with a `ResourceNotFound`
    nobody can read.

    The card the refused run created stays, released to `BACKLOG`. That is the intended
    outcome rather than a leak: the operator's prompt survives as a card they can drag once
    the repository is `READY`, which is exactly what `cards._dispatch` leaves behind for the
    same failure. Deleting it would be a compensating write that can itself fail and that
    throws the prompt away."""
    common, tasks = _load("tasks")
    _seed_config(common, status="CREATING")
    out = tasks.create(
        api_event("POST", "/tasks", body={"configId": _CONFIG_ID, "prompt": "ship it"}), None
    )
    assert out["statusCode"] == 409
    assert _body(out)["error"] == "ConfigurationNotReady"
    assert common.tasks_table.scan()["Count"] == 0
    assert "Messages" not in aws.sqs.receive_message(QueueUrl=aws.task_queue_url)

    parked = common.cards_table.scan()["Items"]
    assert len(parked) == 1, "exactly one card, and no second attempt at one"
    assert parked[0]["column"] == "BACKLOG"
    assert parked[0]["taskId"] == ""
    assert parked[0]["description"] == "ship it"


def test_a_run_started_from_the_api_lands_on_the_board_in_planning(aws: Any) -> None:
    """Every run has a card (invariant 38): a `/sample-rca:remote` run started from Claude
    Code is a card in `Planning`, tracking the run from there.

    `POST /tasks` is what the MCP `start_task` tool calls. Nothing is stored to keep the
    column current -- `cards.derived_view` projects it off the run's own status on every
    read, so the column follows the run with no second copy of that truth to drift.
    """
    common, cards, tasks = _load("cards", "tasks")
    _seed_config(common)
    out = tasks.create(
        api_event(
            "POST", "/tasks", body={"configId": _CONFIG_ID, "prompt": "add a --version flag"}
        ),
        None,
    )
    assert out["statusCode"] == 202, out["body"]
    task = _body(out)

    def board() -> dict[str, Any]:
        answer = cards.list_(api_event("GET", "/cards", query={"configId": _CONFIG_ID}), None)
        assert answer["statusCode"] == 200, answer["body"]
        (one,) = _body(answer)["cards"]
        return one

    card = board()
    assert card["cardId"] == task["cardId"]
    assert card["column"] == "PLANNING", "a queued run draws in the leftmost live column"
    assert card["storedColumn"] == "ACTIVE"
    assert card["taskId"] == task["taskId"]
    assert card["status"] == "QUEUED"

    for status, column in (("CODING", "CODING"), ("SUCCEEDED", "DONE")):
        common.tasks_table.update_item(
            Key={"configId": _CONFIG_ID, "taskId": task["taskId"]},
            UpdateExpression="SET #st = :status",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":status": status},
        )
        assert board()["column"] == column, f"a {status} run draws in {column}"


# -- list -------------------------------------------------------------------------


def test_list_requires_a_config_id(aws: Any) -> None:
    _common, tasks = _load("tasks")
    out = tasks.list_(api_event("GET", "/tasks"), None)
    assert out["statusCode"] == 400
    body = _body(out)
    assert body["field"] == "configId"
    assert "configId" in body["message"]


def test_list_is_newest_first_and_pages_through_a_cursor(aws: Any) -> None:
    common, tasks = _load("tasks")
    _seed_config(common)
    for suffix in ("001", "002", "003"):
        _seed_task(common, f"tsk_1700000000{suffix}_aaa")

    first = _body(
        tasks.list_(api_event("GET", "/tasks", query={"configId": _CONFIG_ID, "limit": "2"}), None)
    )
    assert [task["taskId"] for task in first["tasks"]] == [
        "tsk_1700000000003_aaa",
        "tsk_1700000000002_aaa",
    ]
    assert first["count"] == 2
    assert first["nextCursor"] is not None

    query = {"configId": _CONFIG_ID, "limit": "2", "cursor": first["nextCursor"]}
    second = _body(tasks.list_(api_event("GET", "/tasks", query=query), None))
    assert [task["taskId"] for task in second["tasks"]] == ["tsk_1700000000001_aaa"]


def test_list_leaves_the_activity_feed_to_the_activity_route(aws: Any) -> None:
    common, tasks = _load("tasks")
    _seed_config(common)
    _seed_task(common, "tsk_1700000000001_aaa", status="CODING", activity=_rows(4))
    listed = _body(tasks.list_(api_event("GET", "/tasks", query={"configId": _CONFIG_ID}), None))[
        "tasks"
    ][0]
    assert "activity" not in listed, "a board listing 25 tasks must not carry 25 feeds"
    assert listed["activityCount"] == 4


def test_a_status_filter_pages_instead_of_filtering_after_limit(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live task is the *oldest* of thirteen, which is what a long run looks like:
    twelve short tasks finished while it was still coding. A naive `Limit=5` plus a
    `FilterExpression` answers "nothing running", because `Limit` bounds the rows read
    and the filter then throws all five of them away.
    """
    common, tasks = _load("tasks")
    live_id = "tsk_1700000000000_live"
    items = [{"configId": _CONFIG_ID, "taskId": live_id, "status": "CODING", "activity": []}]
    items += [
        {"configId": _CONFIG_ID, "taskId": f"tsk_1700000001{index:03d}_done", "status": "SUCCEEDED"}
        for index in range(12)
    ]
    table = _ServiceOrderTable(items)
    monkeypatch.setattr(common, "tasks_table", table)

    naive = table.query(
        KeyConditionExpression=Key("configId").eq(_CONFIG_ID),
        FilterExpression=Attr("status").eq("CODING"),
        ScanIndexForward=False,
        Limit=5,
    )
    assert naive["Items"] == [], "the answer this route must not give"

    query = {"configId": _CONFIG_ID, "status": "CODING", "limit": "5"}
    body = _body(tasks.list_(api_event("GET", "/tasks", query=query), None))
    assert [task["taskId"] for task in body["tasks"]] == [live_id]
    assert body["nextCursor"] is None, (
        "the partition was read to its end, so there is nothing to resume"
    )


def test_a_status_filter_finds_a_live_task_behind_a_page_of_finished_ones(aws: Any) -> None:
    """The same shape end to end against real (moto) tables, so the paging helper is
    exercised over items DynamoDB itself stored and filtered."""
    common, tasks = _load("tasks")
    _seed_config(common)
    live_id = "tsk_1700000000000_live"
    _seed_task(common, live_id, status="CODING")
    for index in range(12):
        _seed_task(common, f"tsk_1700000001{index:03d}_done", status="SUCCEEDED")

    query = {"configId": _CONFIG_ID, "status": "CODING", "limit": "5"}
    body = _body(tasks.list_(api_event("GET", "/tasks", query=query), None))
    assert [task["taskId"] for task in body["tasks"]] == [live_id]

    finished = _body(
        tasks.list_(api_event("GET", "/tasks", query={**query, "status": "SUCCEEDED"}), None)
    )
    assert len(finished["tasks"]) == 5, "a filtered page is still bounded by the requested limit"
    assert finished["nextCursor"] is not None, "and the rest are behind a cursor"


def test_a_page_budget_that_runs_out_answers_with_a_cursor_not_a_short_page(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    common, tasks = _load("tasks")
    table = _PagingTable()
    monkeypatch.setattr(common, "tasks_table", table)

    query = {"configId": _CONFIG_ID, "status": "CODING", "limit": "5"}
    body = _body(tasks.list_(api_event("GET", "/tasks", query=query), None))
    assert body["tasks"] == []
    assert len(table.calls) == tasks._MAX_PAGES
    assert all("Limit" not in call for call in table.calls), (
        "a filtered query must never carry Limit"
    )
    assert body["nextCursor"] is not None, (
        "a budget that ran out must not look like an empty history"
    )
    assert common.decode_cursor(body["nextCursor"]) == {
        "configId": _CONFIG_ID,
        "taskId": f"tsk_page_{tasks._MAX_PAGES}",
    }


# -- get --------------------------------------------------------------------------


def test_get_400s_without_a_config_id_and_404s_on_a_missing_task(aws: Any) -> None:
    common, tasks = _load("tasks")
    _seed_config(common)

    bare = api_event("GET", "/tasks/{taskId}", path_parameters={"taskId": "tsk_1_a"})
    out = tasks.get(bare, None)
    assert out["statusCode"] == 400
    assert _body(out)["field"] == "configId"

    out = tasks.get(_task_event("tsk_missing"), None)
    assert out["statusCode"] == 404
    assert _body(out)["taskId"] == "tsk_missing"


def test_tasks_get_pops_the_activity_feed_out_of_its_own_response(aws: Any) -> None:
    common, tasks = _load("tasks")
    _seed_config(common)
    _seed_task(common, "tsk_1700000000001_aaa", status="CODING", activity=_rows(3))

    body = _body(tasks.get(_task_event("tsk_1700000000001_aaa"), None))
    assert "activity" not in body, "the feed belongs to GET /tasks/{taskId}/activity"
    assert body["activityCount"] == 3, "the depth still reaches the console, so it knows to poll"
    assert body["status"] == "CODING"
    assert body["logs"]["logGroup"].endswith(f"{_RUNTIME_ID}-DEFAULT")
    assert body["logs"]["sessionId"] == f"tsk_1700000000001_aaa-{'0' * 32}"
    assert "console.aws.amazon.com" in body["logs"]["consoleUrl"]

    stored = common.tasks_table.get_item(
        Key={"configId": _CONFIG_ID, "taskId": "tsk_1700000000001_aaa"}
    )["Item"]
    assert len(stored["activity"]) == 3, "popping the feed is a view of the item, not a write to it"


def test_get_answers_no_logs_for_a_configuration_with_no_runtime_yet(aws: Any) -> None:
    common, tasks = _load("tasks")
    _seed_config(common, status="CREATING", agentRuntimeId=None)
    _seed_task(common, "tsk_1700000000001_aaa")
    body = _body(tasks.get(_task_event("tsk_1700000000001_aaa"), None))
    assert body["logs"] == {}, (
        "a provisioning configuration has no log group, and that is not a 500"
    )


# -- activity ---------------------------------------------------------------------


def test_activity_400s_without_a_config_id_and_404s_on_a_missing_task(aws: Any) -> None:
    common, tasks = _load("tasks")
    _seed_config(common)

    bare = api_event("GET", "/tasks/{taskId}/activity", path_parameters={"taskId": "tsk_1_a"})
    out = tasks.activity(bare, None)
    assert out["statusCode"] == 400
    assert _body(out)["field"] == "configId"

    out = tasks.activity(_task_event("tsk_missing", "/activity"), None)
    assert out["statusCode"] == 404


def test_activity_polls_forward_from_a_cursor(aws: Any) -> None:
    common, tasks = _load("tasks")
    _seed_config(common)
    task_id = "tsk_1700000000001_aaa"
    _seed_task(common, task_id, status="CODING", activity=_rows(5))

    first = _body(tasks.activity(_task_event(task_id, "/activity", cursor="0", limit="2"), None))
    assert [row["text"] for row in first["rows"]] == ["row 0", "row 1"]
    assert (first["cursor"], first["nextCursor"]) == (0, 2)
    assert first["status"] == "CODING" and first["done"] is False
    assert first["taskId"] == task_id and first["configId"] == _CONFIG_ID

    second = _body(
        tasks.activity(
            _task_event(task_id, "/activity", cursor=str(first["nextCursor"]), limit="2"), None
        )
    )
    assert [row["text"] for row in second["rows"]] == ["row 2", "row 3"]
    assert second["nextCursor"] == 4

    third = _body(tasks.activity(_task_event(task_id, "/activity", cursor="4", limit="2"), None))
    assert [row["text"] for row in third["rows"]] == ["row 4"]
    assert third["nextCursor"] == 5

    caught_up = _body(
        tasks.activity(_task_event(task_id, "/activity", cursor="5", limit="2"), None)
    )
    assert caught_up["rows"] == []
    assert (caught_up["cursor"], caught_up["nextCursor"]) == (5, 5), (
        "caught up is not the same as reset"
    )

    _seed_task(common, task_id, status="SUCCEEDED", activity=_rows(5))
    finished = _body(tasks.activity(_task_event(task_id, "/activity", cursor="5"), None))
    assert finished["done"] is True, "the console needs one signal that says stop polling"


def test_activity_reads_a_projection_rather_than_the_whole_item(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    common, tasks = _load("tasks")
    table = _RecordingTable({"taskId": "tsk_1_a", "status": "CODING", "activity": _rows(1)})
    monkeypatch.setattr(common, "tasks_table", table)

    body = _body(tasks.activity(_task_event("tsk_1_a", "/activity"), None))
    assert body["rows"] == _rows(1)
    assert table.kwargs["ProjectionExpression"] == "taskId, #st, activity, resumeCount, updatedAt"
    assert table.kwargs["ExpressionAttributeNames"] == {"#st": "status"}
    assert "prompt" not in table.kwargs["ProjectionExpression"], (
        "the poll route does not re-read the prompt"
    )


def test_a_cursor_past_a_trimmed_feed_restarts_rather_than_skipping(
    aws: Any, caplog: pytest.LogCaptureFixture
) -> None:
    common, tasks = _load("tasks")
    _seed_config(common)
    task_id = "tsk_1700000000001_aaa"
    _seed_task(common, task_id, status="CODING", activity=_rows(3))

    caplog.set_level(logging.DEBUG, logger="handlers")
    body = _body(tasks.activity(_task_event(task_id, "/activity", cursor="99", limit="2"), None))
    assert body["cursor"] == 0
    assert [row["text"] for row in body["rows"]] == ["row 0", "row 1"]
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert any("past a feed" in message for message in warnings), (
        "re-showing a row is fine; doing it silently is not"
    )


@pytest.mark.parametrize("status", ["FAILED_VERIFICATION", "CANCELLED"])
def test_a_vetoed_or_cancelled_run_reports_done(aws: Any, status: str) -> None:
    """`done` is the only stop signal the console's run poller and the MCP `get_task` tool
    have, and it is served by `GET /tasks/{taskId}/activity` (`activity_page`), not by
    `tasks.get`, which carries no `done` key at all -- so do not go looking for it there.

    Before `runs.TERMINAL_STATUSES` gained these two names, `runs.is_terminal` answered
    `False` for both and this route reported `done: false` for ever: a finished run whose
    pollers never stopped.
    """
    common, tasks = _load("tasks")
    _seed_config(common)
    task_id = "tsk_1700000000001_aaa"
    _seed_task(common, task_id, status=status, activity=_rows(3))

    body = _body(tasks.activity(_task_event(task_id, "/activity"), None))
    assert body["status"] == status
    assert body["done"] is True


def test_activity_page_is_a_pure_function_of_its_arguments(aws: Any) -> None:
    _common, tasks = _load("tasks")
    rows = _rows(3)
    before = json.dumps(rows)

    page = tasks.activity_page(rows, 0, 2)
    assert set(page) == {"rows", "cursor", "nextCursor", "status", "done"}
    assert page["status"] is None and page["done"] is False

    page["rows"][0]["text"] = "mutated"
    assert json.dumps(rows) == before, "the page is a copy; the caller goes on to store the item"

    assert tasks.activity_page(rows, 0, 10, status="CODING")["done"] is False
    assert tasks.activity_page(rows, 0, 10, status="SUCCEEDED")["done"] is True
    assert tasks.activity_page(rows, 0, 10, status="SUCCEEDED_UNVERIFIED")["done"] is True
    assert tasks.activity_page(rows, 0, 10, status="FAILED_VERIFICATION")["done"] is True
    assert tasks.activity_page(rows, 0, 10, status="CANCELLED")["done"] is True
    assert tasks.activity_page(rows, 99, 2, status="FAILED")["cursor"] == 0


# -- cancel -----------------------------------------------------------------------


def test_cancel_marks_the_task_before_it_stops_the_session(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering, asserted mechanically rather than by reading the code.

    The mark has to be durable *before* `StopRuntimeSession` is called: a stop that lands
    with no mark leaves a stopped session behind an item that still looks live, which is a
    task the watchdog tries to **resume**. So the fake reads the stored item from inside the
    stop call, which is the one place that ordering is observable.
    """
    common, tasks = _load("tasks")
    _seed_config(common)
    task_id = "tsk_1700000000001_aaa"
    _seed_live_task(common, task_id)

    seen: list[Any] = []

    def stop(**kwargs: Any) -> dict[str, Any]:
        # What the item said at the moment the stop call was made.
        seen.append(_stored(common, task_id).get("cancelledAt"))
        return {"runtimeSessionId": kwargs["runtimeSessionId"]}

    fake = FakeAgentCore("bedrock-agentcore", responses={"StopRuntimeSession": stop})
    install(monkeypatch, common, data=fake)

    out = tasks.cancel(_cancel_event(task_id), None)
    assert out["statusCode"] == 202, out["body"]
    body = _body(out)
    assert body["taskId"] == task_id and body["configId"] == _CONFIG_ID
    assert body["status"] == "CODING", (
        "the status it was cancelled *from*; the sweep writes the terminal one"
    )
    assert body["sessionStopped"] is True

    stored = _stored(common, task_id)
    assert seen == [stored["cancelledAt"]], (
        "the mark was already durable when the session was stopped"
    )
    assert seen[0] is not None
    assert body["cancelledAt"] == stored["cancelledAt"]

    call = fake.last("StopRuntimeSession")
    assert call["runtimeSessionId"] == f"{task_id}-{'0' * 32}"
    assert call["agentRuntimeArn"].endswith(_RUNTIME_ID)
    assert call["qualifier"] == "DEFAULT"
    assert stored["stoppedAt"], (
        "`watchdog.stop_session` writes the mark that takes it out of the sweep"
    )


def test_a_failing_stop_still_leaves_the_task_cancelled(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the mark goes first. A `ValidationException` is not retryable, so
    `stop_session` re-raises on the first attempt -- and the route still answers 202,
    because the run *is* cancelled and the sweep resolves it on its next tick."""
    common, tasks = _load("tasks")
    _seed_config(common)
    task_id = "tsk_1700000000001_aaa"
    _seed_live_task(common, task_id)

    fake = FakeAgentCore(
        "bedrock-agentcore",
        errors={
            "StopRuntimeSession": [
                client_error(
                    "ValidationException", "no", status=400, operation="StopRuntimeSession"
                )
            ]
        },
    )
    install(monkeypatch, common, data=fake)

    out = tasks.cancel(_cancel_event(task_id), None)
    assert out["statusCode"] == 202, "a stop that failed is the sweep's problem, not the operator's"
    assert _body(out)["sessionStopped"] is False

    stored = _stored(common, task_id)
    assert stored["cancelledAt"], "the mark is what the 15-minute sweep resolves (invariant 28)"
    assert "stoppedAt" not in stored, "nothing was stopped, so nothing claims it was"
    assert stored["status"] == "CODING", "the terminal status is the sweep's to write"


def test_cancelling_a_task_that_does_not_exist_creates_nothing(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 19: `update_item` *creates* an absent item, so the read comes first. A
    conjured cancelled task is worse than a refusal -- it is a run nobody started that the
    sweep would go on to resolve to FAILED."""
    common, tasks = _load("tasks")
    _seed_config(common)
    fake = FakeAgentCore("bedrock-agentcore")
    install(monkeypatch, common, data=fake)

    out = tasks.cancel(_cancel_event("tsk_missing"), None)
    assert out["statusCode"] == 404
    assert _body(out)["error"] == "NotFound"
    assert "Item" not in common.tasks_table.get_item(
        Key={"configId": _CONFIG_ID, "taskId": "tsk_missing"}
    )
    assert fake.operations() == [], "nothing to stop, and nothing was asked to stop"


def test_cancelling_a_finished_task_is_refused_naming_its_status(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal task is not cancellable, and the refusal names the state it is actually
    in: "already SUCCEEDED" is something an operator can act on, where a 202 would report
    that a finished run had been stopped."""
    common, tasks = _load("tasks")
    _seed_config(common)
    task_id = "tsk_1700000000001_aaa"
    _seed_live_task(common, task_id, status="SUCCEEDED")
    fake = FakeAgentCore("bedrock-agentcore")
    install(monkeypatch, common, data=fake)

    out = tasks.cancel(_cancel_event(task_id), None)
    assert out["statusCode"] == 409
    body = _body(out)
    assert body["error"] == "TaskAlreadyFinished"
    assert "SUCCEEDED" in body["message"]
    assert body["status"] == "SUCCEEDED"
    assert "cancelledAt" not in _stored(common, task_id)
    assert fake.operations() == []


def test_cancelling_an_already_cancelled_task_is_refused(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second cancel must not stop a second session: the first one already marked the
    item, and the sweep resolves it whether or not anybody asks again."""
    common, tasks = _load("tasks")
    _seed_config(common)
    task_id = "tsk_1700000000001_aaa"
    _seed_live_task(common, task_id, cancelledAt="2026-01-01T00:05:00.000Z")
    fake = FakeAgentCore("bedrock-agentcore")
    install(monkeypatch, common, data=fake)

    out = tasks.cancel(_cancel_event(task_id), None)
    assert out["statusCode"] == 409
    body = _body(out)
    assert body["error"] == "AlreadyCancelled"
    assert body["cancelledAt"] == "2026-01-01T00:05:00.000Z"
    assert fake.calls_to("StopRuntimeSession") == []


def test_the_cancel_write_leaves_updated_at_alone_and_appends_one_row(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 13. `updatedAt` is `lastFinished`'s sort key *and* the watchdog's only
    liveness clock, so a bookkeeping write that refreshed it would tell the sweep a dead
    container is alive -- which is the one thing this route must not do."""
    common, tasks = _load("tasks")
    _seed_config(common)
    task_id = "tsk_1700000000001_aaa"
    seeded = _seed_live_task(common, task_id)
    install(monkeypatch, common, data=FakeAgentCore("bedrock-agentcore"))

    assert tasks.cancel(_cancel_event(task_id), None)["statusCode"] == 202
    stored = _stored(common, task_id)
    assert stored["updatedAt"] == seeded["updatedAt"], (
        "the liveness clock is not a bookkeeping field"
    )
    assert "finishedAt" not in stored, "the sweep writes the terminal status and its stamp"

    (row,) = stored["activity"]
    assert row["kind"] == "cancel"
    assert row["detail"] == tasks._CANCEL_REASON
    assert common.parse_iso(row["at"]) is not None, "an unparseable row degrades the sweep's clock"


def test_a_cancellation_the_route_wrote_is_resolved_by_the_sweep(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PLAN's verification item 7, end to end: write `cancelledAt`, then stop the session,
    and confirm the sweep resolves it to `FAILED` and stops re-listing it.

    Here rather than in `tests/test_api_watchdog.py` because that file deliberately seeds
    items directly and calls no route -- the half this asserts is that the shape the route
    writes is the shape the already-built sweep reads.
    """
    common, tasks, watchdog = _load("tasks", "watchdog")
    _seed_config(common)
    task_id = "tsk_1700000000001_aaa"
    _seed_live_task(common, task_id)
    fake = FakeAgentCore("bedrock-agentcore")
    install(monkeypatch, common, data=fake)

    assert tasks.cancel(_cancel_event(task_id), None)["statusCode"] == 202

    summary = watchdog.lambda_handler({}, None)
    assert summary["cancelled"] == 1, "a cancellation is resolved, never skipped (invariant 28)"
    stored = _stored(common, task_id)
    assert stored["status"] == "FAILED", (
        "the sweep resolves a cancelled task to FAILED, not to CANCELLED"
    )
    assert stored["statusReason"] == tasks._CANCEL_REASON
    assert stored["stoppedAt"]

    # The route stops the session and so does the sweep, which is idempotent by design
    # (`ResourceNotFoundException` is success), so the count is not what is asserted.
    assert len(fake.calls_to("StopRuntimeSession")) >= 1
    assert watchdog._scan() == [], "a resolved task is out of the filter, not logged forever"
    # The task half only: the sweep's configuration half is `tests/test_api_watchdog.py`'s,
    # and asserting its keys here would make this test fail for a reason about another table.
    assert watchdog.lambda_handler({}, None)["scanned"] == 0


# -- the route table --------------------------------------------------------------


def test_the_routes_are_declared_beside_the_functions_they_name(aws: Any) -> None:
    _common, tasks = _load("tasks")
    assert tasks.ROUTES == (
        ("POST", "/tasks", "create"),
        ("GET", "/tasks", "list_"),
        ("GET", "/tasks/{taskId}", "get"),
        ("GET", "/tasks/{taskId}/activity", "activity"),
        ("POST", "/tasks/{taskId}/cancel", "cancel"),
    )
    for _method, _path, name in tasks.ROUTES:
        assert getattr(getattr(tasks, name), "__api_handler__", False) is True
    assert not hasattr(tasks, "lambda_handler"), "every entry point here is an HTTP route"
