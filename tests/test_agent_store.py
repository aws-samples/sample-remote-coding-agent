"""`store`, plus the two cheap modules that ride on it: `activity` and `memory`.

Every test here runs against moto through the `aws` fixture -- the Tasks table keyed
`configId` + `taskId` and the `Configurations` table keyed `configId` -- so nothing in
this file can reach a real endpoint. What is being protected is a short list of
properties that all fail *quietly* when they break: an activity write that bumps
`updatedAt` blinds the watchdog, a float anywhere in a row raises a `TypeError` from
boto3's serialiser rather than from anything that looks like the caller's fault, a write
that raises kills a task whose PR was already open, and a `Scan` against the
configuration table is a container reading somebody else's repository.
"""

from __future__ import annotations

import inspect
import itertools
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import pytest
from agent_helpers import no_aws, write
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from coding_agent import activity, memory, store
from coding_agent.state import TaskState, VerifyResult
from conftest import AwsFixture

# One task, named once. `configId` + `taskId` is the Tasks table's whole key schema, and
# a state missing either is `store`'s "skip the write" branch rather than a test subject.
_STATE: TaskState = {"configId": "cfg-1", "taskId": "task-1"}


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `store._now()` strictly increasing, one second per call.

    Load-bearing rather than tidiness: the real `_now()` has *seconds* resolution, so two
    writes in the same second produce byte-identical timestamps and a test that asserted
    `updatedAt` was untouched would pass whether or not the write touched it.
    """
    ticks = itertools.count()
    monkeypatch.setattr(store, "_now", lambda: f"2026-01-01T00:00:{next(ticks):02d}+00:00")


def _task_item(aws: AwsFixture) -> dict[str, Any]:
    """The stored Tasks item, read through the resource API so numbers arrive as `Decimal`."""
    table = boto3.resource("dynamodb", region_name=aws.region).Table(aws.tasks_table)
    return table.get_item(Key=dict(_STATE))["Item"]


def test_a_status_write_lands_on_the_task_item(
    aws: AwsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`set_status` writes status and `updatedAt`, and `steps` appends rather than replaces."""
    _fake_clock(monkeypatch)

    store.set_status(_STATE, "CODING", step="code")
    first = _task_item(aws)
    assert first["status"] == "CODING"
    assert first["steps"] == ["code"]
    assert first["updatedAt"]

    store.set_status(_STATE, "VERIFYING", step="verify")
    second = _task_item(aws)
    assert second["status"] == "VERIFYING"
    assert second["steps"] == ["code", "verify"], (
        "`steps` is append-only because `status` is overwritten: a run that ends FAILED "
        "has otherwise lost every trace of where it got to, and 'failed in verify after "
        "three iterations' and 'failed in checkout' are different bugs"
    )
    assert second["updatedAt"] > first["updatedAt"]


def test_an_activity_write_never_touches_updated_at(
    aws: AwsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 13: `append_activity` grows `activity` and leaves `updatedAt` untouched."""
    _fake_clock(monkeypatch)

    store.set_status(_STATE, "CODING", step="code")
    before = _task_item(aws)["updatedAt"]

    store.append_activity(
        _STATE,
        [
            {"at": "2026-01-01T00:00:10+00:00", "node": "code", "kind": "tool", "tool": "Bash"},
            {"at": "2026-01-01T00:00:11+00:00", "node": "code", "kind": "text", "detail": "hello"},
        ],
    )
    after = _task_item(aws)

    assert after["updatedAt"] == before, (
        "`updatedAt` is the watchdog's only liveness clock, so an activity row that "
        "bumped it would make a task hung inside a 30-minute `make test` look alive for "
        "as long as its subagent kept narrating -- the exact failure the watchdog exists to catch"
    )
    assert len(after["activity"]) == 2
    assert after["activity"][0]["tool"] == "Bash"


def test_the_activity_feed_is_a_bounded_tail(aws: AwsFixture) -> None:
    """The feed is capped at `store._MAX_ROWS` and keeps the newest rows, not the oldest."""
    overflow = store._MAX_ROWS + 50
    store.append_activity(_STATE, [{"i": index} for index in range(overflow)])

    stored = _task_item(aws)["activity"]
    assert len(stored) == store._MAX_ROWS, (
        "the console renders the feed in one request and an item has a 400 KB ceiling, "
        f"so the tail is bounded; got {len(stored)} rows from {overflow}"
    )
    assert [int(row["i"]) for row in stored] == list(range(50, overflow)), (
        "a long task loses its OLDEST narration, which is the cheapest thing it has -- "
        "dropping the newest would hide exactly the rows a human is watching for"
    )


def test_a_float_anywhere_in_a_row_does_not_break_the_write(aws: AwsFixture) -> None:
    """`_ddb_safe` converts every nested float to `Decimal`, at any depth."""
    table = boto3.resource("dynamodb", region_name=aws.region).Table(aws.tasks_table)

    # The control, so the conversion is shown to be load-bearing rather than decorative:
    # the resource API refuses a raw float outright, and a cost in dollars or a duration
    # in seconds is exactly the shape these rows grow.
    with pytest.raises(TypeError, match="Float types are not supported"):
        table.update_item(
            Key=dict(_STATE),
            UpdateExpression="SET probe = :probe",
            ExpressionAttributeValues={":probe": [{"usage": {"total_cost_usd": 0.0125}}]},
        )

    store.append_activity(
        _STATE,
        [{"kind": "result", "seconds": 1.5, "usage": [{"total_cost_usd": 0.0125}]}],
    )
    row = _task_item(aws)["activity"][0]
    assert row["seconds"] == Decimal("1.5")
    assert isinstance(row["usage"][0]["total_cost_usd"], Decimal)
    # `Decimal(str(v))` and not `Decimal(v)`: the latter expands the binary float to ~50
    # significant digits and DynamoDB rejects anything over 38.
    assert row["usage"][0]["total_cost_usd"] == Decimal("0.0125")


def _cost_row(*, node: str, total: float, cost_usd: float) -> dict[str, Any]:
    """A `subagents.cost_row` in shape, with a float where the write would fall over.

    `costUSD` inside `modelUsage` is the one that bites: it is nested two levels down, so
    a `_ddb_safe` that only walked the top level of a row would pass every other test here.
    """
    return {
        "node": node,
        "role": node,
        "totalCostUsd": total,
        "numTurns": 3,
        "modelUsage": {"anthropic.claude-opus-4-5": {"costUSD": cost_usd, "provider": "bedrock"}},
        "sessionId": "s-1",
        "subtype": "success",
        "terminalReason": None,
        "at": "2026-01-01T00:00:00+00:00",
    }


def test_a_cost_row_with_a_float_survives_the_write(aws: AwsFixture) -> None:
    """Every float in a cost row lands as a `Decimal`, and `totalCostUsd` accumulates."""
    store.record_cost(_STATE, [_cost_row(node="plan", total=0.25, cost_usd=0.125)])
    first = _task_item(aws)
    assert [row["node"] for row in first["costs"]] == ["plan"]
    assert first["costs"][0]["totalCostUsd"] == Decimal("0.25")
    nested = first["costs"][0]["modelUsage"]["anthropic.claude-opus-4-5"]["costUSD"]
    assert isinstance(nested, Decimal), (
        "`costUSD` is a float two levels inside `model_usage`, and the resource API raises "
        "`TypeError: Float types are not supported` for a float anywhere in the item"
    )
    assert nested == Decimal("0.125")
    assert first["totalCostUsd"] == Decimal("0.25")

    # Two nodes, two calls, and the total is an `ADD` rather than a read-modify-write --
    # which is what makes the second call safe without reading the first one back.
    store.record_cost(
        _STATE,
        [
            _cost_row(node="code", total=1.5, cost_usd=1.25),
            _cost_row(node="code", total=0.25, cost_usd=0.2),
        ],
    )
    second = _task_item(aws)
    assert [row["node"] for row in second["costs"]] == ["plan", "code", "code"], (
        "`costs` is append-only, so the order a run spent its money in survives"
    )
    assert second["totalCostUsd"] == Decimal("2.0")


def test_a_cost_write_does_not_touch_updated_at(
    aws: AwsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 13 again: cost is bookkeeping and carries no liveness information."""
    _fake_clock(monkeypatch)

    store.set_status(_STATE, "REVIEWING", step="review")
    before = _task_item(aws)["updatedAt"]

    store.record_cost(_STATE, [_cost_row(node="review", total=0.2, cost_usd=0.1)])
    after = _task_item(aws)

    assert after["updatedAt"] == before, (
        "`_instrument` writes the node's status either side of the node, so this write "
        "adds no liveness information -- and bumping the watchdog's only clock from a "
        "bookkeeping write is exactly the mistake `append_activity` documents"
    )
    assert after["costs"]


def test_a_cost_write_with_no_table_is_a_no_op(
    aws: AwsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no `TASKS_TABLE`, and with no rows, `record_cost` builds no client at all."""
    no_aws(monkeypatch)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("record_cost built an AWS client with no table configured")

    monkeypatch.setattr(store.boto3, "resource", _explode)
    store.record_cost(_STATE, [_cost_row(node="plan", total=0.25, cost_usd=0.125)])

    # And the empty list returns before the table is even looked up, which is the case
    # `graph._instrument` hits on every node that runs no role session.
    monkeypatch.setenv("TASKS_TABLE", aws.tasks_table)
    store.record_cost(_STATE, [])


def test_the_container_reads_only_its_own_configuration(aws: AwsFixture) -> None:
    """Invariant 31: `get_configuration` is a `GetItem`, and the module has no write path."""
    boto3.resource("dynamodb", region_name=aws.region).Table(aws.config_table).put_item(
        Item={"configId": "cfg-1", "repository": "acme/widget", "verifyCommand": "make test"}
    )

    item = store.get_configuration("cfg-1")
    assert item is not None
    assert item["verifyCommand"] == "make test"
    assert store.get_configuration("cfg-absent") is None

    # A source-level assertion earns its place here because the property is the *absence*
    # of a call, and absence cannot be exercised: no test can catch a `put_item` that
    # nobody happened to hit. The execution role's `LeadingKeys` condition is the
    # enforcement; this is the belt. A compromised container that could edit another
    # repository's `verifyCommand` has remote code execution in that repository's container.
    source = Path(inspect.getsourcefile(store) or "").read_text(encoding="utf-8")
    for call in (".put_item(", ".delete_item(", ".scan(", ".batch_write_item("):
        assert call not in source, f"store.py must not call {call.strip('.(')} anywhere"

    read = inspect.getsource(store.get_configuration)
    assert "get_item" in read
    assert "update_item" not in read, "the only write in this module is against the Tasks table"


def test_every_write_is_a_no_op_with_no_table_configured(
    aws: AwsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no `TASKS_TABLE`, every write returns without raising and without an AWS call."""
    no_aws(monkeypatch)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("store built an AWS client with no table configured")

    monkeypatch.setattr(store.boto3, "resource", _explode)

    store.set_status(_STATE, "CODING", step="code")
    store.record_result(_STATE, status="SUCCEEDED", pr_url="https://example.invalid/pr/1")
    store.append_activity(_STATE, [{"kind": "text", "detail": "nobody is listening"}])
    assert store.get_configuration("cfg-1") is None, (
        "this is what makes `python -m coding_agent.local_run` work on a machine with "
        "no tables: the table name is read inside every function, and its absence is a return"
    )


def test_a_client_error_is_logged_not_raised(
    aws: AwsFixture, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A status write against a missing table warns and returns; the task survives it."""
    monkeypatch.setenv("TASKS_TABLE", "Tasks-that-was-never-created")

    with caplog.at_level(logging.WARNING, logger="coding_agent.store"):
        store.set_status(_STATE, "SUCCEEDED", step="submit")

    assert "could not write status" in caplog.text, (
        "the writes are best-effort by design: a task whose code, verification and PR "
        "all succeeded must not die because the progress bar could not be updated"
    )


def test_the_terminal_write_records_the_verdict(
    aws: AwsFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`record_result` writes status, `prUrl` and `verifyResult` at once, and never blanks a url."""
    _fake_clock(monkeypatch)
    verdict: VerifyResult = {
        "passed": True,
        "verified": True,
        "warning": None,
        "command": "make test",
        "exitCode": 0,
    }

    store.record_result(
        _STATE,
        status="SUCCEEDED",
        pr_url="https://github.com/acme/widget/pull/7",
        verify_result=verdict,
    )
    item = _task_item(aws)
    assert item["status"] == "SUCCEEDED"
    assert item["prUrl"] == "https://github.com/acme/widget/pull/7"
    assert item["verifyResult"]["verified"] is True
    assert item["verifyResult"]["command"] == "make test"
    assert item["verifyResult"]["exitCode"] == 0

    # A later failure with no url of its own must not erase the one already recorded: a
    # resumed task that already opened a PR would otherwise lose the only link to it.
    store.record_result(_STATE, status="FAILED", pr_url=None, verify_result={"passed": False})
    reread = _task_item(aws)
    assert reread["status"] == "FAILED"
    assert reread["prUrl"] == "https://github.com/acme/widget/pull/7"
    assert reread["verifyResult"] == {"passed": False}


def test_activity_rows_are_bounded_and_prefer_the_identifying_field() -> None:
    """`rows_from_message` caps a burst, picks the field that identifies the call, and trims."""
    message = AssistantMessage(
        content=[
            ToolUseBlock(
                id="t1",
                name="Bash",
                # `description` comes later in `_DETAIL_FIELDS` than `command`: a Bash row
                # without its command is a row that says nothing.
                input={"description": "run the suite", "command": "make test"},
            ),
            ToolUseBlock(id="t2", name="Frobnicate", input={"unanticipated": "still renders"}),
            TextBlock(text="a  b\n\nc " + "x" * 500),
        ],
        model="sonnet",
    )

    rows = activity.rows_from_message(message, node="code")
    assert [row["kind"] for row in rows] == ["tool", "tool", "text"]
    assert {row["node"] for row in rows} == {"code"}
    assert rows[0]["tool"] == "Bash"
    assert rows[0]["detail"] == "make test"
    # Any named tool block becomes a row, off `block.name` and not off a whitelist, so a
    # tool a later phase turns on renders with no edit to `activity.py`.
    assert rows[1]["tool"] == "Frobnicate"
    assert rows[1]["detail"] == "still renders"
    assert rows[2]["detail"].startswith("a b c ")
    assert len(rows[2]["detail"]) == activity._MAX_DETAIL
    assert rows[2]["detail"].endswith("…")

    burst = AssistantMessage(
        content=[
            ToolUseBlock(id=f"t{i}", name="Read", input={"file_path": f"f{i}"}) for i in range(20)
        ],
        model="sonnet",
    )
    assert len(activity.rows_from_message(burst, node="code")) == activity._MAX_ROWS_PER_MESSAGE

    result = activity.rows_from_message(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=3,
            session_id="s-1",
        ),
        node="review",
    )
    assert len(result) == 1
    assert result[0]["kind"] == "result"
    assert result[0]["num_turns"] == 3
    assert result[0]["is_error"] is False
    assert result[0]["node"] == "review"
    assert result[0]["at"]


def test_the_memory_digest_is_ordered_and_truncates_with_its_marker(tmp_path: Path) -> None:
    """`read_digest` is sorted by filename, skips empty notes, and marks a truncation."""
    assert memory.read_digest(tmp_path) == "", (
        "a first iteration has no notes, and that is not an error"
    )

    write(memory.note_path(tmp_path, "plan"), "the suite needs `make setup` first\n")
    write(memory.note_path(tmp_path, "code"), "the checkout is shallow\n")
    write(memory.note_path(tmp_path, "blank"), "\n")

    digest = memory.read_digest(tmp_path)
    # Sorted, so the same notes produce the same digest twice: a prompt that reshuffles
    # between iterations defeats prompt caching for no benefit.
    assert digest.index("## code.md") < digest.index("## plan.md")
    assert "the suite needs `make setup` first" in digest
    assert "blank.md" not in digest

    truncated = memory.read_digest(tmp_path, max_chars=20)
    assert truncated.endswith("…(truncated)"), (
        "a role has to be able to tell 'there was more' from 'that is all there is': a "
        "silent cut reads as a complete history and invites the wrong conclusion"
    )
    assert len(truncated) < len(digest)


# -- the GitHub credential, fetched from AgentCore Identity ---------------------------


def test_the_credential_is_exchanged_for_the_request_s_workload_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`store.github_token` presents the invocation's workload access token and asks for a
    provider by *name*; it never names a secret.

    The token is the one AgentCore delivered as a request header, which the SDK put in a
    `ContextVar`. Asserted as the value actually sent, because the failure this guards is a
    call that succeeds against the wrong workload -- Identity resolves a provider name per
    workload, so a stale or invented token would fetch somebody else's credential or none.
    """
    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

    calls: list[dict[str, Any]] = []

    class _Identity:
        def get_resource_api_key(self, **kwargs: Any) -> dict[str, str]:
            calls.append(kwargs)
            return {"apiKey": "github_pat_from_identity\n"}

    monkeypatch.setattr(store.boto3, "client", lambda _service: _Identity())
    BedrockAgentCoreContext.set_workload_access_token("wat-for-this-invocation")

    token = store.github_token("rca_cfg_0123456789ab")

    assert token == "github_pat_from_identity", "the trailing newline is stripped"
    assert calls == [
        {
            "workloadIdentityToken": "wat-for-this-invocation",
            "resourceCredentialProviderName": "rca_cfg_0123456789ab",
        }
    ]


def test_no_provider_name_means_no_credential_and_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configuration registered before the credential moved to Identity carries no provider
    name, and a local run has none either. Both must return `""` rather than reaching AWS --
    `nodes/submit.py` falls back to `GITHUB_TOKEN` in exactly that case."""

    def forbidden(service: str) -> Any:
        raise AssertionError(f"built a {service} client with no provider name to ask about")

    monkeypatch.setattr(store.boto3, "client", forbidden)
    assert store.github_token("") == ""


def test_a_missing_workload_token_names_the_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure an operator will actually hit if the dispatcher stops sending
    `runtimeUserId`, or if `server.invoke` stops copying the request context onto the worker
    thread.

    It must raise rather than return `""`: an empty token makes `git_ops` clone anonymously,
    which succeeds for a public repository and then fails at the push -- far from the cause.
    The message names both halves, because either one produces this state.
    """
    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

    monkeypatch.setattr(
        BedrockAgentCoreContext, "get_workload_access_token", classmethod(lambda _cls: None)
    )
    monkeypatch.setattr(
        store.boto3,
        "client",
        lambda _service: pytest.fail("reached AWS with no workload token"),
    )

    with pytest.raises(RuntimeError) as raised:
        store.github_token("rca_cfg_0123456789ab")

    message = str(raised.value)
    assert "runtimeUserId" in message
    assert "server.invoke" in message
