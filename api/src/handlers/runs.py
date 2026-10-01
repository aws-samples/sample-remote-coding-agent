"""The one definition of "queue a run", and the owner of the task vocabulary.

`tasks.create` and `cards.move` both call `queue_run` and nothing else, so the two
entry points cannot drift about what a run is -- which item attributes exist, what a
dispatch message looks like, or whether a configuration was allowed to run at all.
"""

from __future__ import annotations

import json
import secrets as secrets_module
import time
from collections.abc import Mapping
from typing import Any

from boto3.dynamodb.conditions import Attr

from . import _common, github_app, observability
from ._common import LOG, ApiError

QUEUED, DISPATCHED = "QUEUED", "DISPATCHED"

# Pinned duplication of `agent/src/coding_agent/state.py`'s `TaskStatus` and
# `TERMINAL_STATUSES`, in that file's own order. The two deployment units must not import
# each other -- `api/` is a Lambda bundle and the agent is a container image -- so the
# vocabulary is written twice on purpose, and
# `tests/test_web_status_vocabulary.py::test_the_api_status_tuple_is_the_console_vocabulary`
# is what holds the two copies equal (order included) rather than review.
# `tests/test_api_agentcore_figures.py::test_the_watchdog_and_the_task_vocabulary_cannot_drift`
# holds the other half: `watchdog.RESUMABLE_STATUSES` is exactly the non-terminal names here.
# If the agent lands a name this tuple has never heard of, the board degrades to the stored
# column (see `cards._STATUS_COLUMN`) and the watchdog treats the status as neither resumable
# nor terminal and leaves it alone; neither raises.
TASK_STATUSES = (
    "QUEUED",
    "DISPATCHED",
    "CHECKING_OUT",
    "PLANNING",
    "CODING",
    "VERIFYING",
    "REVIEWING",
    "SUBMITTING",
    "SUCCEEDED",
    "SUCCEEDED_UNVERIFIED",
    "FAILED_VERIFICATION",
    "FAILED",
    "CANCELLED",
)
# The container's own `TERMINAL_STATUSES`: what the watchdog must never resume and what the
# console renders as finished. `FAILED_VERIFICATION` and `CANCELLED` were missing, and a task
# in either was in *neither* the watchdog's live half nor its finished half -- never listed,
# so its AgentCore session billed until `MAX_LIFETIME_SECONDS`, and `done` never went true.
TERMINAL_STATUSES = frozenset(
    {"SUCCEEDED", "SUCCEEDED_UNVERIFIED", "FAILED_VERIFICATION", "FAILED", "CANCELLED"}
)

# The other half of the same agreement -- the graph's node names, appended to the task item's `steps` as each node finishes. `cards`
# reads them to place a failed run in the column it died in.
STEPS = ("checkout", "plan", "code", "verify", "review", "submit")


def is_terminal(status: str | None) -> bool:
    return status in TERMINAL_STATUSES


def new_task_id() -> str:
    """`tsk_<epoch-ms>_<6 hex>`: time-sortable, so a `query` with
    `ScanIndexForward=False` is newest-first with no GSI at all (PLAN's `### Data`
    removed the `byConfig` index), and still opaque enough not to be guessable."""
    return f"tsk_{int(time.time() * 1000)}_{secrets_module.token_hex(3)}"


def queue_run(
    config: Mapping[str, Any],
    *,
    prompt: str,
    card: Mapping[str, Any] | None = None,
    source: str,
) -> dict[str, Any]:
    """Write the task item and enqueue its dispatch. Returns the stored item."""
    status = config.get("status")
    if status != "READY":
        # A run queued against a `CREATING` configuration dies in the dispatcher with a
        # `ResourceNotFound` nobody can read, so it is refused where the operator is.
        raise ApiError(
            409,
            "ConfigurationNotReady",
            "the configuration is not READY, so a run cannot be dispatched against it",
            configId=config.get("configId"),
            status=status,
        )

    # The second gate, and it fails the same way the first does rather than 20 minutes
    # later. A repository can be perfectly `READY` and still have no GitHub credential: the
    # authorisation is a separate operator action (`github_app.py`), and without it the run
    # clones, plans, codes, verifies and then dies at `submit` having spent a full task's
    # tokens. Checked here rather than in `tasks.create`, because `runs.queue_run` is the one
    # definition of "queue a run" and `cards.move` must refuse for the same reason.
    if not github_app.resolve_token(str(config["configId"]))["accessToken"]:
        raise ApiError(
            409,
            "GitHubNotAuthorized",
            "this repository has no GitHub credential yet. Authorise it in the console "
            "(Repository settings -> Authorise GitHub); if it is already authorised, the "
            "deployment's GitHub App is not installed on this repository.",
            configId=config.get("configId"),
            repository=config.get("repository"),
        )

    task_id = new_task_id()
    stamp = _common.now_iso()
    task: dict[str, Any] = {
        "configId": config["configId"],
        "taskId": task_id,
        "prompt": prompt,
        "status": QUEUED,
        "sessionId": observability.session_id(task_id),
        # Copied rather than resolved at dispatch time, so a `DELETE` of the
        # configuration cannot orphan a dispatch that is already in flight.
        "agentRuntimeArn": config.get("agentRuntimeArn", ""),
        "repository": config.get("repository", ""),
        "resumeCount": 0,
        "activity": [],
        "steps": [],
        "source": source,
        "createdAt": stamp,
        "updatedAt": stamp,
    }
    if card is not None:
        task["cardId"] = card["cardId"]

    _common.tasks_table.put_item(Item=task, ConditionExpression=Attr("taskId").not_exists())
    enqueue_dispatch(task)
    return task


def enqueue_dispatch(task: Mapping[str, Any], *, resume: bool = False) -> None:
    """The single owner of the dispatch message shape. The watchdog sends the same
    message with `resume=True`, so the dispatcher has one body to parse."""
    task_id, config_id = task["taskId"], task["configId"]
    _common.sqs.send_message(
        QueueUrl=_common.TASK_QUEUE_URL,
        MessageBody=json.dumps({"taskId": task_id, "configId": config_id, "resume": resume}),
    )
    LOG.info("queued dispatch task=%s config=%s resume=%s", task_id, config_id, resume)
