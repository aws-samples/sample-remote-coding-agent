"""The container's only AWS surface: the two DynamoDB tables and one secret.

Two rules shape every function here, and both exist to keep the rest of the package
testable and runnable off AWS entirely:

1. **Clients are built per call, never at import.** That is what lets the `aws`
   fixture's `monkeypatch.setenv` take effect with no `importlib.reload`, and what
   lets a developer `python -m coding_agent.local_run` on a machine with no tables.
2. **Every entry point returns early when its table is not configured** --
   `TASKS_TABLE` for the task writes, `CONFIG_TABLE` for the configuration read. The
   variable is read *inside* the function, because it may be set or deleted between
   two calls in one process (it is, in the tests).

The writes are also best-effort: a `ClientError` from a status or activity write is a
`log.warning` and a return, never an exception. That is not spelled out in the spec
and is worth naming -- a task whose code, verification and PR all succeeded must not
die because the progress bar could not be updated. The one exception is
`get_configuration`, which the caller genuinely needs: see its docstring.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from botocore.exceptions import BotoCoreError, ClientError

from .state import TaskState, TaskStatus, VerifyResult

log = logging.getLogger(__name__)

# The activity feed is a bounded tail, not a log: the console renders it in one
# request and an item has a 400 KB ceiling. 400 rows is the cap; a long task loses
# its oldest narration, which is the cheapest thing it has.
_MAX_ROWS = 400


def _now() -> str:
    """The one timestamp format the table uses.

    Duplicated from `activity.utcnow_iso()` rather than imported: `activity` imports
    `store`, so the arrow only points one way.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ddb_safe(value: Any) -> Any:
    """Walk a row and replace every `float` with a `Decimal`.

    This detail is in neither the spec nor the plan, and it *will* bite: the DynamoDB
    resource API raises `TypeError: Float types are not supported, use Decimal types
    instead` for a float anywhere in an item, however deeply nested -- and a cost in
    dollars or a duration in seconds is exactly the shape an activity row or a
    `verifyResult` grows later. `Decimal(str(v))` and not `Decimal(v)`, because the
    latter expands the binary float to ~50 significant digits and DynamoDB rejects
    anything over 38.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {key: _ddb_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_ddb_safe(item) for item in value]
    return value


def _table(env_var: str) -> Any | None:
    """The `Table` resource named by `env_var`, or `None` when it is unset.

    The resource API rather than `client("dynamodb")` plus a `TypeSerializer`: the
    rows written here are nested dicts and lists, and hand-serialising them is a
    second place for the shapes to go wrong. `_ddb_safe` is the price of that choice.
    """
    name = os.environ.get(env_var)
    if not name:
        return None
    return boto3.resource("dynamodb").Table(name)


def _key(state: TaskState) -> dict[str, str] | None:
    """The Tasks item key, or `None` when the state cannot name one.

    Both halves are required by the table's schema (`configId` HASH + `taskId`
    RANGE), and a state missing either is a caller bug -- but a status write is
    best-effort, so it is a warning rather than the exception that kills the run.
    """
    config_id = state.get("configId")
    task_id = state.get("taskId")
    if not config_id or not task_id:
        log.warning(
            "no Tasks key on the state (configId=%r taskId=%r); skipping the write",
            config_id,
            task_id,
        )
        return None
    return {"configId": config_id, "taskId": task_id}


def get_configuration(config_id: str) -> dict[str, Any] | None:
    """Read this task's own configuration item, or `None` when there is no table.

    `GetItem` on `CONFIG_TABLE` and nothing else -- no `PutItem`, no `UpdateItem`,
    no `Scan` (invariant 31). The container reads only its own configuration and
    writes none, because a compromised container that could edit another
    repository's `verifyCommand` has remote code execution in that repository's
    container. The execution role's `LeadingKeys` condition is the enforcement; this
    module not having the call is the belt.

    Unlike the writes below this is *not* best-effort: the caller wants the
    repository's commands, and silently returning `None` on a throttle would look
    exactly like an unconfigured repository. A real error propagates.
    """
    table = _table("CONFIG_TABLE")
    if table is None:
        return None
    item = table.get_item(Key={"configId": config_id}).get("Item")
    return dict(item) if item else None


def github_token(credential_provider_name: str) -> str:
    """Fetch the repository's PAT from AgentCore Identity.

    The return value is *passed as an argument* -- to `git_ops.push`, to
    `github.create_pull_request` -- and is never written to `os.environ` and never
    put on a command line (invariant 4). Every coding subagent has `Bash` and
    inherits this process's environment, so a token in the environment is a token the
    model can print. `git_ops` reaches git through a `GIT_ASKPASS` script for the
    same reason.

    The credential is named rather than located: this takes a provider *name*, and
    Identity decides which credential that name resolves to for the workload asking.
    The container never holds a secret ARN, and the execution role's grant is
    `bedrock-agentcore:GetResourceApiKey` on one provider ARN (invariant 31).

    **The workload access token comes from the request, not from an API call.** AgentCore
    mints it for the `runtimeUserId` the dispatcher sends and delivers it as a request
    header, which the SDK puts in a `ContextVar`; `server.invoke` copies that context onto
    the worker thread so it is readable here. A missing token means the dispatcher sent no
    `runtimeUserId`, and the message says so -- calling `GetWorkloadAccessToken` instead
    would not help, because a runtime's own identity is service-linked and refuses a
    caller-initiated exchange.

    `.strip()` because a PAT pasted into the console picks up a trailing newline and
    an HTTP header carrying one fails as an unrelated-looking 400.
    """
    if not credential_provider_name:
        return ""
    workload_token = BedrockAgentCoreContext.get_workload_access_token()
    if not workload_token:
        raise RuntimeError(
            "no workload access token reached this task, so the GitHub credential cannot "
            "be fetched. AgentCore supplies one only when the invocation carries a "
            "runtimeUserId (`api/src/handlers/dispatcher.py`), and it is readable on this "
            "thread only because `server.invoke` copies the request context onto it."
        )
    identity = boto3.client("bedrock-agentcore")
    answer = identity.get_resource_api_key(
        workloadIdentityToken=workload_token,
        resourceCredentialProviderName=credential_provider_name,
    )
    return (answer.get("apiKey") or "").strip()


def set_status(
    state: TaskState,
    status: TaskStatus,
    *,
    step: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Write the task's current status, and optionally append one step to the trail.

    `steps` is append-only, via `list_append(if_not_exists(steps, :empty), :step)`,
    because `status` is overwritten: a run that ends `FAILED` has lost every trace of
    where it actually got to, and "failed in `verify` after three iterations" and
    "failed in `checkout`" are different bugs. The step is whatever string the caller
    passes (`graph._instrument` passes the node name).

    `extra` merges extra attribute name -> value into the same `SET`, so a status and
    the attribute that explains it land in one atomic write rather than as a pair a
    reader can catch half of.
    """
    table = _table("TASKS_TABLE")
    if table is None:
        return
    key = _key(state)
    if key is None:
        return

    now = _now()
    names = {"#status": "status"}  # `status` is a DynamoDB reserved word.
    values: dict[str, Any] = {":s": status, ":now": now}
    sets = ["#status = :s", "updatedAt = :now"]
    if step:
        sets.append("steps = list_append(if_not_exists(steps, :empty), :step)")
        values[":empty"] = []
        values[":step"] = [step]
    for index, (name, value) in enumerate(sorted((extra or {}).items())):
        # Aliased rather than inlined: an attribute name is caller-supplied and
        # `status`/`timestamp`/`name` are all reserved words.
        sets.append(f"#e{index} = :e{index}")
        names[f"#e{index}"] = name
        values[f":e{index}"] = _ddb_safe(value)

    try:
        table.update_item(
            Key=key,
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except (ClientError, BotoCoreError) as exc:
        log.warning("could not write status %s for task %s: %s", status, key["taskId"], exc)


def record_result(
    state: TaskState,
    *,
    status: TaskStatus,
    pr_url: str | None = None,
    verify_result: VerifyResult | None = None,
) -> None:
    """The terminal write: the final status, the PR url and the verdict behind it.

    Delegates to `set_status` so `updatedAt` and the `#status` alias have exactly one
    owner. A falsy `pr_url` is omitted rather than written empty: a resumed task that
    already opened a PR must not have the url erased by a later failure.
    """
    extra: dict[str, Any] = {}
    if pr_url:
        extra["prUrl"] = pr_url
    if verify_result is not None:
        extra["verifyResult"] = dict(verify_result)
    set_status(state, status, extra=extra)


def record_cost(state: TaskState, rows: list[dict[str, Any]]) -> None:
    """Append per-node cost rows to the task item and add to the task's running total.

    Best-effort like the other writes here: cost is observability, and a task whose code,
    verification and pull request all succeeded must not die because the accounting could
    not be filed.

    **One atomic, append-only `update_item`.** `list_append` for the rows and `ADD` for
    the total, so there is no read-modify-write anywhere in this function -- a nested map
    keyed by node would have needed one, and two nodes finishing close together would
    then lose each other's row. `costs` grows unbounded on purpose: it is one small row
    per role session and the worst case is a handful per iteration, so it does not need
    `append_activity`'s tail.

    **This write must not touch `updatedAt`** (invariant 13), for the same reason
    `append_activity` must not. `_instrument` writes the node's status either side of the
    node, so this write carries no liveness information at all; bumping the watchdog's
    only clock from a bookkeeping write is exactly the mistake documented there.

    `_ddb_safe` is mandatory rather than defensive here. `totalCostUsd` is a float and so
    is `costUSD` inside every `modelUsage` entry, and the resource API raises
    `TypeError: Float types are not supported` for a float anywhere in the item.
    """
    if not rows:
        return
    table = _table("TASKS_TABLE")
    if table is None:
        return
    key = _key(state)
    if key is None:
        return

    # The rows already carry their own per-session figure; this is the one number a
    # console can show without reading the list back.
    total = sum(float(row.get("totalCostUsd") or 0.0) for row in rows)
    try:
        table.update_item(
            Key=key,
            UpdateExpression=(
                "SET costs = list_append(if_not_exists(costs, :empty), :rows) "
                "ADD totalCostUsd :total"
            ),
            ExpressionAttributeValues={
                ":empty": [],
                ":rows": _ddb_safe(list(rows)),
                ":total": _ddb_safe(total),
            },
        )
    except (ClientError, BotoCoreError) as exc:
        log.warning("dropping %d cost rows for task %s: %s", len(rows), key["taskId"], exc)


def append_activity(state: TaskState, rows: list[dict[str, Any]]) -> None:
    """Append activity rows to the task item, bounded and best-effort.

    **This write must not touch `updatedAt`** (invariant 13). That attribute is the
    `lastFinished` index's sort key *and* the watchdog's only liveness clock, so an
    activity row that bumped it would make a task hung inside a 30-minute `make test`
    look alive for as long as its subagent kept narrating -- the exact failure the
    watchdog exists to catch.

    Trimming uses the list `ReturnValues="UPDATED_NEW"` hands back, so the cap costs
    no extra read. The second write clobbers whatever `activity` holds at that
    moment; that is safe because one container is the only writer of its own task
    item, and the feed is advisory anyway.
    """
    if not rows:
        return
    table = _table("TASKS_TABLE")
    if table is None:
        return
    key = _key(state)
    if key is None:
        return

    try:
        updated = table.update_item(
            Key=key,
            UpdateExpression="SET activity = list_append(if_not_exists(activity, :empty), :rows)",
            ExpressionAttributeValues={":empty": [], ":rows": _ddb_safe(list(rows))},
            ReturnValues="UPDATED_NEW",
        )
        current = updated.get("Attributes", {}).get("activity") or []
        if len(current) > _MAX_ROWS:
            table.update_item(
                Key=key,
                UpdateExpression="SET activity = :trimmed",
                ExpressionAttributeValues={":trimmed": current[-_MAX_ROWS:]},
            )
    except (ClientError, BotoCoreError) as exc:
        # Dropped, not retried and never raised: the activity feed is narration, and
        # a task that dies because its progress could not be described is worse than
        # a task with a gap in its feed.
        log.warning("dropping %d activity rows for task %s: %s", len(rows), key["taskId"], exc)
