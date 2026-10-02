"""Shared fixtures: fake AWS credentials, the moto data plane, and API Gateway events.

`tests` is on `pytest.ini`'s `pythonpath`, so test modules may `from conftest import
api_event, REPO_ROOT` as well as taking the fixtures by name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

# The documented default region. Hard-coded rather than read from the environment so a developer
# with AWS_REGION set to something else gets the same table and queue ARNs.
_REGION = "ap-southeast-1"

# Exported so the layout tests can assert against real paths without each recomputing
# the number of `.parent`s.
REPO_ROOT = Path(__file__).resolve().parents[1]

_CONFIG_TABLE = "Configurations"
_TASKS_TABLE = "Tasks"
_CARDS_TABLE = "Cards"

# The fake PATs, in one place. Every one of these is a string whose *job* is to be asserted
# absent from a log line, an exception message or a URL, so a suite that spells its own is a
# suite where the next author invents a fifteenth. One owner also means a static scanner reads
# them once here instead of flagging a hardcoded credential in nine test modules -- which is why
# the names avoid `TOKEN`, `SECRET` and `PASSWORD`: those are the words bandit's `B105` looks for
# beside a literal, and a fake credential filed under a real-sounding name costs a triage every
# time it is scanned.
#
# `FAKE_PAT` is the classic shape `POST /configurations` REFUSES (`ClassicTokenRejected`) and the
# agent container carries; the two `FAKE_FINE_GRAINED_*` are the fine-grained shape registration
# accepts, and they differ from each other because a rotation test has to tell the new value from
# the old one.
FAKE_PAT = "ghp-t0ken-that-must-not-leak"
FAKE_FINE_GRAINED_PAT = "github_pat_11ABCDEFGHIJKLMNOP0123456789"
FAKE_FINE_GRAINED_REPLACEMENT_PAT = "github_pat_22ZYXWVUTSRQPONMLKJI9876543210"
# Shorter, and it says what it is where the assertion is about *echoing* rather than about a
# token's shape -- `mcp/` logs a method, a path and a status, never a body.
FAKE_UNLOGGED_PAT = "github_pat_11ABCDEFG_never_echoed"

# `FAKE_FINE_GRAINED_PAT` is here because `configurations.create` must refuse a body
# carrying a token, and the refusal test needs something shaped like what an operator
# would try to paste. Token-shaped literals with no reader are not kept: a constant
# nothing imports is exactly what the next artifact scan flags for no reason anyone can
# act on.

# `_aws_env` below puts the same pair in the environment for every test, which is where boto3
# finds them. This mapping is for the one client built at *module import*, before any fixture has
# run: `tests/test_api_agentcore_figures.py` needs a service model, not a session. Spelled through
# `_FAKE_CREDENTIAL` rather than twice inline, because a `str` under a key containing `secret` is
# the other half of what `B105` matches.
_FAKE_CREDENTIAL = "testing"
FAKE_AWS_CREDENTIALS = {
    "aws_access_key_id": _FAKE_CREDENTIAL,
    "aws_secret_access_key": _FAKE_CREDENTIAL,
}


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject fake credentials so a missed mock fails loudly instead of billing.

    This is the whole mechanism behind "no test may reach a real AWS endpoint": a
    test that slips out of `mock_aws` gets a credential error against a bogus key,
    never a real call signed with the developer's own profile. `AWS_PROFILE` and
    `AWS_ENDPOINT_URL` are removed for the same reason -- either one would send the
    call somewhere real.
    """
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
    ):
        monkeypatch.setenv(name, _FAKE_CREDENTIAL)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_REGION", _REGION)
    # Forwarded to the provisioner Lambda by `api_stack._provisioner_environment`, and read by
    # `provisioner._account_and_region` to build every ARN in an execution role's inline policy
    # and the trust policy's `aws:SourceAccount`. Set here rather than per test because without
    # it that function falls back to `*`, every policy assertion compares against a wildcard
    # account, and the boundary-vs-grant resource check fails on a mismatch that is an artefact
    # of the fixture rather than of the policy. Matches moto's own account id.
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)


@dataclass(frozen=True)
class AwsFixture:
    """What `aws` hands back, so tests read `aws.task_queue_url` rather than rebuilding it."""

    region: str
    dynamodb: Any
    sqs: Any
    config_table: str
    tasks_table: str
    cards_table: str
    task_queue_url: str
    task_dlq_url: str
    provision_queue_url: str
    provision_dlq_url: str


def _queue_with_dlq(sqs: Any, name: str) -> tuple[str, str]:
    """Create `name` and `name-dlq`, wired by a RedrivePolicy. One helper so the
    task and provision queues cannot drift apart."""
    dlq_url = sqs.create_queue(QueueName=f"{name}-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    queue_url = sqs.create_queue(
        QueueName=name,
        Attributes={
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"}),
        },
    )["QueueUrl"]
    return queue_url, dlq_url


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The moto data plane: the three tables, the two queues with their DLQs, and the
    environment variables the handlers bind at import time."""
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=_REGION)
        sqs = boto3.client("sqs", region_name=_REGION)

        dynamodb.create_table(
            TableName=_CONFIG_TABLE,
            KeySchema=[{"AttributeName": "configId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "configId", "AttributeType": "S"},
                {"AttributeName": "repository", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "byRepository",
                    "KeySchema": [{"AttributeName": "repository", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        # No `byConfig` GSI on Tasks: the base table is already ordered by
        # configuration, which is exactly what lets a `dynamodb:LeadingKeys`
        # condition fence a container to its own repository.
        dynamodb.create_table(
            TableName=_TASKS_TABLE,
            KeySchema=[
                {"AttributeName": "configId", "KeyType": "HASH"},
                {"AttributeName": "taskId", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "configId", "AttributeType": "S"},
                {"AttributeName": "taskId", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName=_CARDS_TABLE,
            KeySchema=[{"AttributeName": "cardId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "cardId", "AttributeType": "S"},
                {"AttributeName": "configId", "AttributeType": "S"},
                {"AttributeName": "createdAt", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "byConfig",
                    "KeySchema": [
                        {"AttributeName": "configId", "KeyType": "HASH"},
                        {"AttributeName": "createdAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        task_queue_url, task_dlq_url = _queue_with_dlq(sqs, "rca-tasks")
        provision_queue_url, provision_dlq_url = _queue_with_dlq(sqs, "rca-provision")

        # Handler modules bind these at import time, so they must be set before a
        # test's `importlib.reload`.
        monkeypatch.setenv("CONFIG_TABLE", _CONFIG_TABLE)
        monkeypatch.setenv("TASKS_TABLE", _TASKS_TABLE)
        monkeypatch.setenv("CARDS_TABLE", _CARDS_TABLE)
        monkeypatch.setenv("TASK_QUEUE_URL", task_queue_url)
        monkeypatch.setenv("PROVISION_QUEUE_URL", provision_queue_url)

        yield AwsFixture(
            region=_REGION,
            dynamodb=dynamodb,
            sqs=sqs,
            config_table=_CONFIG_TABLE,
            tasks_table=_TASKS_TABLE,
            cards_table=_CARDS_TABLE,
            task_queue_url=task_queue_url,
            task_dlq_url=task_dlq_url,
            provision_queue_url=provision_queue_url,
            provision_dlq_url=provision_dlq_url,
        )


def api_event(
    method: str,
    path: str,
    *,
    body: Any = None,
    path_parameters: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build an API Gateway **v2** (HTTP API) proxy event.

    A plain function rather than a fixture because it takes arguments.
    `pathParameters` and `queryStringParameters` are omitted entirely when not given,
    which is what a real event does -- a handler that assumes they are always present
    should fail here too.
    """
    event: dict[str, Any] = {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "&".join(f"{k}={v}" for k, v in (query or {}).items()),
        "headers": headers if headers is not None else {"content-type": "application/json"},
        "requestContext": {
            "http": {"method": method, "path": path, "sourceIp": "203.0.113.1"},
            "requestId": "test-request-id",
            "stage": "$default",
        },
        "isBase64Encoded": False,
    }
    if path_parameters:
        event["pathParameters"] = path_parameters
    if query:
        event["queryStringParameters"] = dict(query)
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event
