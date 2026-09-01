"""The fixtures every later phase builds on: the moto data plane, the API Gateway v2
event helper, and the credential guard that keeps a missed mock from billing."""

from __future__ import annotations

import json
import os

from conftest import AwsFixture, api_event


def test_the_aws_fixture_creates_the_three_tables(aws: AwsFixture) -> None:
    assert set(aws.dynamodb.list_tables()["TableNames"]) == {"Configurations", "Tasks", "Cards"}

    tasks = aws.dynamodb.describe_table(TableName="Tasks")["Table"]
    assert [key["AttributeName"] for key in tasks["KeySchema"]] == ["configId", "taskId"]
    # No byConfig GSI on Tasks: the base table is already partitioned by
    # configuration, which is what lets `dynamodb:LeadingKeys` fence a container.
    assert tasks.get("GlobalSecondaryIndexes", []) == []

    cards = aws.dynamodb.describe_table(TableName="Cards")["Table"]
    assert [index["IndexName"] for index in cards["GlobalSecondaryIndexes"]] == ["byConfig"]


def test_each_queue_has_a_dead_letter_queue(aws: AwsFixture) -> None:
    urls = aws.sqs.list_queues()["QueueUrls"]
    assert len(urls) == 4

    for queue_url, dlq_url in (
        (aws.task_queue_url, aws.task_dlq_url),
        (aws.provision_queue_url, aws.provision_dlq_url),
    ):
        policy = json.loads(
            aws.sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["RedrivePolicy"])[
                "Attributes"
            ]["RedrivePolicy"]
        )
        dlq_arn = aws.sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        assert policy["deadLetterTargetArn"] == dlq_arn
        # The fixture writes `"3"` because real SQS rejects a non-string here, but
        # moto hands it back as an int -- compare as text either way.
        assert str(policy["maxReceiveCount"]) == "3"


def test_api_event_builds_an_api_gateway_v2_event() -> None:
    event = api_event("POST", "/tasks", body={"prompt": "add a --version flag"})
    assert event["version"] == "2.0"
    assert event["routeKey"] == "POST /tasks"
    assert event["rawPath"] == "/tasks"
    assert event["requestContext"]["http"]["method"] == "POST"
    assert json.loads(event["body"]) == {"prompt": "add a --version flag"}
    # A real event omits these keys entirely rather than passing null.
    assert "pathParameters" not in event
    assert "queryStringParameters" not in event

    with_params = api_event(
        "GET", "/tasks/{taskId}", path_parameters={"taskId": "t-1"}, query={"limit": "5"}
    )
    assert with_params["pathParameters"] == {"taskId": "t-1"}
    assert with_params["queryStringParameters"] == {"limit": "5"}
    assert with_params["rawQueryString"] == "limit=5"
    assert "body" not in with_params


def test_fake_credentials_are_injected_so_a_missed_mock_cannot_bill() -> None:
    assert os.environ["AWS_ACCESS_KEY_ID"] == "testing"
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "testing"
    assert os.environ["AWS_REGION"] == "ap-southeast-1"
    assert "AWS_PROFILE" not in os.environ, (
        "a real AWS_PROFILE would let a call that escaped mock_aws reach a real "
        "endpoint with real credentials"
    )
