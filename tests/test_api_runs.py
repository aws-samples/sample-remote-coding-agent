"""`runs.py`: the one definition of "queue a run".

The last test in this file is the reason the module exists -- it compares the run
`tasks.create` produces with the run `cards.move` produces, key for key. Two entry
points that each built their own task item would drift, and the drift would show up as
a task the dispatcher cannot invoke or a board column that never fills in.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import api_event
from test_api_fakes import load_handlers as _load

CONFIG_ID = "cfg_0123456789ab"


def _seed_config(common: Any, *, status: str = "READY") -> dict[str, Any]:
    config = {
        "configId": CONFIG_ID,
        "repository": "owner/repo",
        "repositoryUrl": "https://github.com/owner/repo",
        "status": status,
        "defaultBranch": "main",
        "verifyCommand": "make test",
        "setupCommand": "make setup",
        "secretArn": f"arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:rca/github/{CONFIG_ID}-AbCdEf",
        "agentRuntimeArn": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/rca_cfg_0123456789ab-ABC123",
        "agentRuntimeId": "rca_cfg_0123456789ab-ABC123",
        "createdAt": common.now_iso(),
        "updatedAt": common.now_iso(),
    }
    common.config_table.put_item(Item=config)
    return config


def _messages(aws: Any) -> list[dict[str, Any]]:
    received = aws.sqs.receive_message(QueueUrl=aws.task_queue_url, MaxNumberOfMessages=10)
    return [json.loads(message["Body"]) for message in received.get("Messages", [])]


def test_a_new_task_id_is_time_sortable_and_opaque(aws: Any) -> None:
    _common, runs = _load("runs")
    first = runs.new_task_id()
    second = runs.new_task_id()
    # `tsk_` + 13 epoch-millisecond digits + `_` + 6 hex. The spec says "20-21 chars",
    # which is one epoch length out of date; what matters is that it is time-sortable
    # (so `ScanIndexForward=False` is newest-first with no GSI) and not guessable.
    assert first.startswith("tsk_") and len(first) == 24
    assert first != second
    assert first.split("_")[1] <= second.split("_")[1]


def test_queue_run_refuses_a_configuration_that_is_not_ready(aws: Any) -> None:
    """A run queued against a CREATING configuration dies in the dispatcher with a
    `ResourceNotFound` nobody can read, so it is refused where the operator is."""
    common, runs = _load("runs")
    config = _seed_config(common, status="CREATING")
    with pytest.raises(common.ApiError) as raised:
        runs.queue_run(config, prompt="add a healthcheck", source="api")
    assert raised.value.status == 409
    assert raised.value.code == "ConfigurationNotReady"
    assert raised.value.body["status"] == "CREATING"
    assert _messages(aws) == [], "nothing was queued"


def test_queue_run_writes_the_task_and_queues_exactly_one_dispatch(aws: Any) -> None:
    common, runs = _load("runs")
    config = _seed_config(common)
    task = runs.queue_run(config, prompt="add a healthcheck", source="api")

    stored = common.tasks_table.get_item(Key={"configId": CONFIG_ID, "taskId": task["taskId"]})[
        "Item"
    ]
    assert stored["status"] == runs.QUEUED
    assert stored["resumeCount"] == 0
    assert stored["activity"] == [] and stored["steps"] == []
    assert stored["sessionId"] == task["sessionId"]
    # Copied from the configuration, so a later DELETE cannot orphan the dispatch.
    assert stored["agentRuntimeArn"] == config["agentRuntimeArn"]
    assert _messages(aws) == [{"taskId": task["taskId"], "configId": CONFIG_ID, "resume": False}]


def test_the_stored_session_id_is_the_one_owner_derivation(aws: Any) -> None:
    common, runs, observability = _load("runs", "observability")
    task = runs.queue_run(_seed_config(common), prompt="p", source="api")
    assert task["sessionId"] == observability.session_id(task["taskId"])
    assert len(task["sessionId"]) >= observability.SESSION_ID_MIN_LENGTH


def test_a_resume_dispatch_carries_the_resume_flag(aws: Any) -> None:
    common, runs = _load("runs")
    task = runs.queue_run(_seed_config(common), prompt="p", source="api")
    aws.sqs.purge_queue(QueueUrl=aws.task_queue_url)
    runs.enqueue_dispatch(task, resume=True)
    assert _messages(aws) == [{"taskId": task["taskId"], "configId": CONFIG_ID, "resume": True}]


def test_every_task_status_the_board_and_the_watchdog_know_is_declared_here(aws: Any) -> None:
    _common, runs = _load("runs")
    assert set(runs.TASK_STATUSES) >= runs.TERMINAL_STATUSES
    assert runs.is_terminal("SUCCEEDED") and runs.is_terminal("FAILED")
    assert not runs.is_terminal("CODING")
    # A status phase 2 renamed is non-terminal by default: the watchdog then leaves it
    # alone rather than resolving a live task to FAILED.
    assert not runs.is_terminal("CLONING") and not runs.is_terminal(None)


def test_both_entry_points_produce_the_same_run_shape(aws: Any) -> None:
    """The drift test that is the whole reason `runs.py` exists: `tasks.create` and
    `cards.move` are compared key for key, minus the ids and timestamps that must
    differ."""
    common, cards, tasks = _load("cards", "tasks")
    _seed_config(common)

    from_api = json.loads(
        tasks.create(
            api_event(
                "POST", "/tasks", body={"configId": CONFIG_ID, "prompt": "add a healthcheck"}
            ),
            None,
        )["body"]
    )

    card = json.loads(
        cards.create(
            api_event(
                "POST", "/cards", body={"configId": CONFIG_ID, "description": "add a healthcheck"}
            ),
            None,
        )["body"]
    )
    move = cards.move(
        api_event(
            "POST",
            "/cards/{cardId}/move",
            path_parameters={"cardId": card["cardId"]},
            body={"column": "PLANNING"},
        ),
        None,
    )
    assert move["statusCode"] in (200, 202), move["body"]

    tasks_stored = common.tasks_table.query(
        KeyConditionExpression="configId = :c",
        ExpressionAttributeValues={":c": CONFIG_ID},
    )["Items"]
    assert len(tasks_stored) == 2
    from_card = next(item for item in tasks_stored if item["taskId"] != from_api["taskId"])
    api_item = next(item for item in tasks_stored if item["taskId"] == from_api["taskId"])

    volatile = {"taskId", "sessionId", "createdAt", "updatedAt", "source", "cardId"}
    assert set(api_item) == set(from_card), (
        "the two entry points must produce the same attributes -- including `cardId`, now "
        "that every run has a card. They agree on the card link's presence and differ only "
        "in its value and in `source`"
    )
    for key in set(api_item) - volatile:
        assert api_item[key] == from_card[key], f"{key} drifted between the two entry points"
    assert api_item["source"] == "api" and from_card["source"] == "card"
    assert from_card["cardId"] == card["cardId"]
    assert api_item["cardId"].startswith("crd_") and from_card["cardId"].startswith("crd_")
    assert api_item["cardId"] != from_card["cardId"], "two runs, two cards"
