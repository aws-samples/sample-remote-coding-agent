"""`observability.py`: the session-id and log-location derivations.

No AWS call in either arm -- the `Stubber` test constructs a client and validates
parameters without a request leaving the process, which is the whole point of note 30.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.exceptions import ParamValidationError
from botocore.stub import Stubber
from test_api_fakes import load_handlers as _load


def test_a_session_id_clears_botocores_minimum_for_any_task_id(aws: Any) -> None:
    _common, observability = _load("observability")
    for task_id in ("t", "tsk_1763036400000_a1b2c3"):
        session = observability.session_id(task_id)
        assert len(session) >= observability.SESSION_ID_MIN_LENGTH
        assert session.startswith(task_id)


def test_a_session_id_is_deterministic_and_distinct_per_task(aws: Any) -> None:
    _common, observability = _load("observability")
    first = observability.session_id("tsk_1763036400000_a1b2c3")
    assert first == observability.session_id("tsk_1763036400000_a1b2c3")
    assert first != observability.session_id("tsk_1763036400000_a1b2c4")


def test_an_empty_task_id_is_a_value_error_not_a_short_session(aws: Any) -> None:
    _common, observability = _load("observability")
    with pytest.raises(ValueError, match="task_id"):
        observability.session_id("")


def test_botocore_rejects_a_raw_task_id_as_a_session_id(aws: Any) -> None:
    """Invariant 18's mechanical half: the 33-character minimum is enforced
    client-side, so a raw 20-char task id never reaches the service at all."""
    _common, observability = _load("observability")
    task_id = "tsk_1763036400000_a1b2c3"
    arn = "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/rca_cfg_abc-XYZ"
    # No credentials passed: `conftest._aws_env` is autouse and has already put fake ones in
    # the environment, which is where boto3 looks.
    client = boto3.client("bedrock-agentcore", region_name="ap-southeast-1")
    with Stubber(client) as stub:
        # Two queued responses, not one: the Stubber asserts call *order* on
        # `before-parameter-build`, which fires before validation, so an unqueued
        # second call would report a missing stub instead of the short session id.
        stub.add_response("stop_runtime_session", {}, None)
        stub.add_response("stop_runtime_session", {}, None)
        client.stop_runtime_session(
            agentRuntimeArn=arn, runtimeSessionId=observability.session_id(task_id)
        )
        with pytest.raises(ParamValidationError, match="min length: 33"):
            client.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=task_id)


def test_the_log_group_and_stream_are_derived_from_the_runtime_id(aws: Any) -> None:
    _common, observability = _load("observability")
    assert observability.ENDPOINT_NAME == "DEFAULT"
    assert (
        observability.runtime_log_group("rca_cfg_abc-XYZ")
        == "/aws/bedrock-agentcore/runtimes/rca_cfg_abc-XYZ-DEFAULT"
    )
    assert observability.runtime_log_stream("session-1") == "[runtime-logs]session-1"


def test_the_console_url_uses_the_consoles_own_double_escaping(aws: Any) -> None:
    _common, observability = _load("observability")
    group = observability.runtime_log_group("rca_cfg_abc-XYZ")
    stream = observability.runtime_log_stream("sess-1")
    url = observability.console_url("ap-southeast-1", group, stream)
    assert url.startswith(
        "https://ap-southeast-1.console.aws.amazon.com/cloudwatch/home?region=ap-southeast-1"
    )
    assert "$252Faws$252Fbedrock-agentcore$252Fruntimes" in url
    assert "$255Bruntime-logs$255D" in url
    assert "/log-events/" in url
    assert "/log-events/" not in observability.console_url("ap-southeast-1", group)


def test_log_locations_are_empty_for_a_configuration_that_has_no_runtime_yet(aws: Any) -> None:
    _common, observability = _load("observability")
    assert observability.log_locations({"status": "CREATING"}, {"taskId": "tsk_1"}) == {}


def test_log_locations_fall_back_to_the_derived_session_id(aws: Any) -> None:
    _common, observability = _load("observability")
    config = {"agentRuntimeId": "rca_cfg_abc-XYZ", "region": "ap-southeast-1"}
    stored = observability.log_locations(
        config, {"taskId": "tsk_1", "sessionId": "stored-session-id"}
    )
    assert stored["sessionId"] == "stored-session-id"
    derived = observability.log_locations(config, {"taskId": "tsk_1763036400000_a1b2c3"})
    assert derived["sessionId"] == observability.session_id("tsk_1763036400000_a1b2c3")
    assert set(derived) == {"logGroup", "logStream", "consoleUrl", "sessionId"}


@pytest.mark.parametrize("path", ["/mnt/workspace", "/mnt/workspace/", "/mnt/work_space-1.0"])
def test_a_mount_path_one_level_under_mnt_is_valid(aws: Any, path: str) -> None:
    _common, observability = _load("observability")
    assert observability.is_valid_mount_path(path)


@pytest.mark.parametrize(
    "path", ["/mnt/a/b", "/mnt", "/mnt/", "/workspace", "/mnt/workspace/work", ""]
)
def test_anything_else_is_refused_before_the_service_refuses_it(aws: Any, path: str) -> None:
    """botocore does not validate `pattern` client-side, so without this check
    `/mnt/workspace/work` reaches `CreateAgentRuntime` as a bare `ValidationException`
    that reads like a service problem."""
    _common, observability = _load("observability")
    assert not observability.is_valid_mount_path(path)
