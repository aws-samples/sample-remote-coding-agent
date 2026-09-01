"""`_common.py`: the response envelope, body parsing, scrubbing and the decorator.

Every handler module builds its clients at import, so `_load` reloads them *inside*
`mock_aws` -- see `test_api_fakes.load_handlers`.
"""

from __future__ import annotations

import base64
import json
import logging
from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import api_event
from test_api_fakes import load_handlers as _load


def test_response_carries_the_json_content_type_and_a_serialised_body(aws: Any) -> None:
    (common,) = _load()
    out = common.response(201, {"ok": True})
    assert out["statusCode"] == 201
    assert out["headers"] == {"content-type": "application/json"}
    assert json.loads(out["body"]) == {"ok": True}


def test_a_dynamodb_decimal_serialises_as_a_number_not_a_string(aws: Any) -> None:
    (common,) = _load()
    body = json.loads(
        common.response(200, {"runCount": Decimal(2), "ratio": Decimal("2.5")})["body"]
    )
    assert body == {"runCount": 2, "ratio": 2.5}
    assert isinstance(body["runCount"], int)


def test_an_unserialisable_attribute_raises_rather_than_reaching_a_client_as_garbage(
    aws: Any,
) -> None:
    (common,) = _load()
    with pytest.raises(TypeError):
        common.response(200, {"blob": object()})


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({}, {}),
        ({"body": ""}, {}),
        ({"body": '{"a": 1}'}, {"a": 1}),
        ({"body": base64.b64encode(b'{"a": 1}').decode(), "isBase64Encoded": True}, {"a": 1}),
    ],
)
def test_parse_body_handles_absent_empty_and_base64_bodies(
    aws: Any, event: dict, expected: dict
) -> None:
    (common,) = _load()
    assert common.parse_body(event) == expected


@pytest.mark.parametrize(
    ("body", "code"), [("{not json", "InvalidJSON"), ("[1, 2]", "InvalidBody")]
)
def test_parse_body_refuses_invalid_json_and_a_non_object(aws: Any, body: str, code: str) -> None:
    (common,) = _load()
    with pytest.raises(common.ApiError) as raised:
        common.parse_body({"body": body})
    assert raised.value.status == 400
    assert raised.value.code == code


def test_require_names_the_first_missing_field(aws: Any) -> None:
    (common,) = _load()
    assert common.require({"a": "1", "b": "2"}, "a", "b") == ("1", "2")
    with pytest.raises(common.ApiError) as raised:
        common.require({"b": "2"}, "a", "b")
    assert raised.value.body["field"] == "a"
    assert "a" in raised.value.message


@pytest.mark.parametrize("value", ["", "   ", None, 7, {"nested": 1}])
def test_require_refuses_an_empty_or_non_string_value(aws: Any, value: Any) -> None:
    (common,) = _load()
    with pytest.raises(common.ApiError):
        common.require({"a": value}, "a")


def test_path_and_query_params_tolerate_the_keys_being_absent(aws: Any) -> None:
    """The shape `conftest.api_event` actually produces: a real HTTP API event omits
    `pathParameters`/`queryStringParameters` entirely rather than sending `{}`."""
    (common,) = _load()
    bare = api_event("GET", "/configurations")
    assert "pathParameters" not in bare and "queryStringParameters" not in bare
    assert common.query_param(bare, "configId") is None
    assert common.query_param(bare, "limit", "25") == "25"
    with pytest.raises(common.ApiError) as raised:
        common.path_param(bare, "configId")
    assert raised.value.code == "MissingPathParameter"

    full = api_event(
        "GET", "/c/{configId}", path_parameters={"configId": "cfg_1"}, query={"limit": "5"}
    )
    assert common.path_param(full, "configId") == "cfg_1"
    assert common.query_param(full, "limit") == "5"


def test_int_query_param_clamps_and_refuses_a_non_integer(aws: Any) -> None:
    (common,) = _load()
    event = api_event("GET", "/tasks", query={"limit": "500", "cursor": "abc"})
    assert common.int_query_param(event, "limit", 25, maximum=100) == 100
    assert common.int_query_param(event, "missing", 25, maximum=100) == 25
    with pytest.raises(common.ApiError):
        common.int_query_param(event, "cursor", 0, maximum=100)


def test_a_cursor_round_trips_and_a_forged_one_is_a_400(aws: Any) -> None:
    (common,) = _load()
    assert common.encode_cursor(None) is None
    cursor = common.encode_cursor({"configId": "cfg_1", "taskId": "tsk_1"})
    assert common.decode_cursor(cursor) == {"configId": "cfg_1", "taskId": "tsk_1"}
    with pytest.raises(common.ApiError) as raised:
        common.decode_cursor("!!!not-a-cursor")
    assert raised.value.code == "InvalidCursor"


def test_scrub_redacts_by_key_by_pattern_and_at_depth(aws: Any) -> None:
    (common,) = _load()
    body = {
        "githubToken": "github_pat_11ABCDEFG0123456789",
        "GITHUB_TOKEN": "x",
        "notes": "the token is ghp_abcdef0123456789 and it leaked",
        "nested": {"credentials": "s3cret", "keep": "fine"},
        "list": [{"password": "hunter2"}],
    }
    scrubbed = common.scrub(body)
    assert scrubbed["githubToken"] == "***"
    assert scrubbed["GITHUB_TOKEN"] == "***"
    assert "ghp_" not in scrubbed["notes"] and "***" in scrubbed["notes"]
    assert scrubbed["nested"] == {"credentials": "***", "keep": "fine"}
    assert scrubbed["list"] == [{"password": "***"}]
    assert body["githubToken"] == "github_pat_11ABCDEFG0123456789", (
        "scrub must not mutate its argument"
    )


def test_scrub_truncates_and_bottoms_out_rather_than_hanging_a_log_line(aws: Any) -> None:
    (common,) = _load()
    long_value = common.scrub({"diff": "x" * 600})["diff"]
    assert long_value.startswith("x" * 512)
    assert long_value.endswith("(88 more)")

    deep: dict[str, Any] = {"leaf": "value"}
    for _ in range(12):
        deep = {"down": deep}
    assert "…" in json.dumps(common.scrub(deep), ensure_ascii=False)


def test_the_decorator_turns_an_api_error_into_its_own_status(aws: Any) -> None:
    (common,) = _load()

    @common.handler
    def route(event: dict) -> tuple[int, dict]:
        raise common.ApiError(404, "NotFound", "no such configuration", configId="cfg_1")

    out = route(api_event("GET", "/configurations/cfg_1"), None)
    assert out["statusCode"] == 404
    assert json.loads(out["body"]) == {
        "error": "NotFound",
        "message": "no such configuration",
        "configId": "cfg_1",
    }
    assert route.__api_handler__ is True


def test_the_decorator_turns_a_lost_conditional_write_into_a_409(aws: Any) -> None:
    (common,) = _load()

    @common.handler
    def route(event: dict) -> tuple[int, dict]:
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, "UpdateItem"
        )

    out = route(api_event("POST", "/cards/crd_1/move"), None)
    assert out["statusCode"] == 409
    assert json.loads(out["body"])["error"] == "Conflict"


def test_an_unexpected_exception_is_a_500_carrying_no_exception_text(aws: Any) -> None:
    (common,) = _load()

    @common.handler
    def route(event: dict) -> tuple[int, dict]:
        raise RuntimeError("ghp_abcdef0123456789 was rejected by GitHub")

    out = route(api_event("POST", "/configurations"), None)
    assert out["statusCode"] == 500
    assert json.loads(out["body"]) == {"error": "InternalError", "message": "unexpected error"}
    assert "ghp_" not in out["body"]


def test_a_token_in_a_body_never_reaches_a_log_line(
    aws: Any, caplog: pytest.LogCaptureFixture
) -> None:
    (common,) = _load()

    @common.handler
    def route(event: dict) -> tuple[int, dict]:
        body = common.parse_body(event)
        common.LOG.info("registering %s", common.scrub(body))
        return 201, {"ok": True}

    caplog.set_level(logging.DEBUG, logger="handlers")
    event = api_event(
        "POST",
        "/configurations",
        body={
            "repositoryUrl": "https://github.com/o/r",
            "githubToken": "github_pat_11ABC0123456789",
        },
    )
    assert route(event, None)["statusCode"] == 201
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "github_pat_" not in logged
    assert "***" in logged
    assert "https://github.com/o/r" in logged, "the non-credential half of the body is still useful"


def test_now_iso_round_trips_through_parse_iso(aws: Any) -> None:
    (common,) = _load()
    stamp = common.now_iso()
    assert stamp.endswith("Z") and "T" in stamp
    parsed = common.parse_iso(stamp)
    assert parsed is not None and parsed.tzinfo is not None


@pytest.mark.parametrize("value", ["", None, "not a timestamp", 17, "2026-13-45T99:99:99Z"])
def test_parse_iso_returns_none_on_anything_a_container_might_have_written(
    aws: Any, value: Any
) -> None:
    (common,) = _load()
    assert common.parse_iso(value) is None
