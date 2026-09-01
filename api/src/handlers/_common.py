"""Shared plumbing for every control-plane Lambda: the `@handler` decorator, the
error/response envelope, log scrubbing, and the one place boto3 clients are built.

Every other module does `from . import _common` and reaches the clients **through the
module object at call time** (`_common.tasks_table.get_item(...)`, never
`from ._common import tasks_table`). That is the whole reload contract: a test reloads
`_common` inside `moto`'s mock and every other module immediately sees the rebound
table handles, so no test has to reload modules in a particular order.

Route functions take the event alone; the decorator's wrapper is what Lambda calls
with `(event, context)`. An unused Lambda `context` is spelled `_context` throughout
`api/src` so ruff's ARG rules stay switched on.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast

import boto3
from botocore.exceptions import ClientError

LOG = logging.getLogger("handlers")
# A bad LOG_LEVEL must not take a Lambda out at cold start, so an unknown name falls
# back to INFO rather than raising out of `setLevel`.
LOG.setLevel(
    logging.getLevelNamesMapping().get(os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
)

_SESSION = boto3.session.Session()
dynamodb = _SESSION.resource("dynamodb")
sqs = _SESSION.client("sqs")
secrets = _SESSION.client("secretsmanager")
iam = _SESSION.client("iam")

# Every environment lookup carries a default: an import with nothing set must not
# raise, or a missing variable reads as a broken bundle rather than a missing grant.
config_table = dynamodb.Table(os.environ.get("CONFIG_TABLE", ""))
tasks_table = dynamodb.Table(os.environ.get("TASKS_TABLE", ""))
cards_table = dynamodb.Table(os.environ.get("CARDS_TABLE", ""))

TASK_QUEUE_URL = os.environ.get("TASK_QUEUE_URL", "")
PROVISION_QUEUE_URL = os.environ.get("PROVISION_QUEUE_URL", "")


@functools.lru_cache(maxsize=1)
def agentcore_control() -> Any:
    """The AgentCore *control* plane: Create/Get/Update/DeleteAgentRuntime.

    Built lazily rather than at import: `bedrock-agentcore-control` needs the boto3
    this bundle vendors (invariant 20), and a stale botocore must raise
    `UnknownServiceError` inside the provisioner rather than at the cold start of
    every read route, where it would look like an API outage.
    """
    return _SESSION.client("bedrock-agentcore-control")


@functools.lru_cache(maxsize=1)
def agentcore_data() -> Any:
    """The AgentCore *data* plane: InvokeAgentRuntime and StopRuntimeSession. Lazy for
    the same reason as `agentcore_control`."""
    return _SESSION.client("bedrock-agentcore")


class ApiError(Exception):
    """A response an HTTP handler chose, as opposed to one it suffered."""

    # The three leading parameters are positional-only so that a detail may itself be
    # called `status` or `code` -- `ApiError(409, "ConfigurationNotReady", "...",
    # status=config["status"])` is the natural way to report the status that was
    # wrong, and with ordinary parameters it is a `TypeError` instead.
    def __init__(self, status: int, code: str, message: str, /, **details: Any) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    @property
    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.details}


def _json_default(value: Any) -> Any:
    """Serialise what DynamoDB hands back. Deliberately no `str()` fallback: that is
    how a `Binary` attribute reaches a client as `b'...'` garbage nobody notices."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"cannot serialise {type(value).__name__} into a response body")


def response(status: int, body: Any, *, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": dict(headers) if headers else {"content-type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def parse_body(event: Mapping[str, Any]) -> dict[str, Any]:
    """The request body as a dict. An absent or empty body is `{}`, not an error --
    `DELETE` and the two `POST`s with no body go through here too."""
    raw = event.get("body")
    if raw is None or raw == "":
        return {}
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError) as err:
            raise ApiError(400, "InvalidJSON", "body is not valid base64") from err
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as err:
        raise ApiError(400, "InvalidJSON", "body is not valid JSON") from err
    if not isinstance(parsed, dict):
        raise ApiError(400, "InvalidBody", "body must be a JSON object")
    return parsed


def require(body: Mapping[str, Any], *names: str) -> tuple[Any, ...]:
    """Return the named values, in order, or raise naming the *first* missing one.

    Naming one field rather than listing all of them is deliberate: a client fixes
    them one at a time anyway, and the first name is the one the message can be
    specific about.
    """
    values: list[Any] = []
    for name in names:
        value = body.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ApiError(
                400,
                "MissingField",
                f"{name} is required and must be a non-empty string",
                field=name,
            )
        values.append(value)
    return tuple(values)


def path_param(event: Mapping[str, Any], name: str) -> str:
    """A path parameter. `pathParameters` is *absent* from a real HTTP API event when
    the route has none, so this tolerates the key missing entirely."""
    value = (event.get("pathParameters") or {}).get(name)
    if not value:
        raise ApiError(400, "MissingPathParameter", f"{name} is required in the path", field=name)
    return value


def query_param(event: Mapping[str, Any], name: str, default: str | None = None) -> str | None:
    """A query-string parameter, tolerating `queryStringParameters` being absent."""
    value = (event.get("queryStringParameters") or {}).get(name)
    return default if value in (None, "") else value


def int_query_param(
    event: Mapping[str, Any], name: str, default: int, *, minimum: int = 0, maximum: int
) -> int:
    """A bounded integer query parameter. Out of range clamps rather than 400s -- a
    console asking for 1000 rows wants as many as it can have -- but a non-integer is
    a client bug and says so."""
    raw = query_param(event, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as err:
        raise ApiError(
            400, "InvalidQueryParameter", f"{name} must be an integer", field=name
        ) from err
    return max(minimum, min(maximum, value))


def encode_cursor(key: Mapping[str, Any] | None) -> str | None:
    """A `LastEvaluatedKey` as an opaque string. One owner for all three list routes so
    a cursor from one cannot be decoded differently by another."""
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key, default=_json_default).encode()).decode()


def decode_cursor(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value.encode()))
    except (binascii.Error, UnicodeDecodeError, ValueError) as err:
        raise ApiError(400, "InvalidCursor", "cursor is not a cursor this API issued") from err
    if not isinstance(decoded, dict):
        raise ApiError(400, "InvalidCursor", "cursor is not a cursor this API issued")
    return decoded


# Keys whose value is a credential wherever it appears. Compared case-folded, so
# `githubToken`, `GitHubToken` and `github_token` are one entry each.
_TOKEN_KEYS = frozenset(
    {
        "token",
        "githubtoken",
        "github_token",
        "secret",
        "secretstring",
        "password",
        "authorization",
        "pat",
        "credentials",
    }
)
# The shapes GitHub's own tokens take, for the case where one arrives under a key
# nobody thought of (`notes`, a stack trace, a commit message).
_TOKEN_PATTERN = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{6,}|github_pat_[A-Za-z0-9_]{6,})\b")
_MAX_STRING = 512
_MAX_DEPTH = 6


def _scrub_string(value: str) -> str:
    value = _TOKEN_PATTERN.sub("***", value)
    if len(value) > _MAX_STRING:
        return f"{value[:_MAX_STRING]}…({len(value) - _MAX_STRING} more)"
    return value


def scrub(value: Any, _depth: int = 0) -> Any:
    """A copy of `value` safe to log: credentials by key, credentials by shape, and
    nothing unbounded. Never mutates its argument -- the caller usually goes on to
    store it. Recursion past `_MAX_DEPTH` becomes `"…"`, so a deeply nested or
    cyclic-looking structure cannot hang a log line."""
    if _depth > _MAX_DEPTH:
        return "…"
    if isinstance(value, Mapping):
        return {
            key: "***" if str(key).casefold() in _TOKEN_KEYS else scrub(item, _depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, list | tuple):
        return [scrub(item, _depth + 1) for item in value]
    return value


def is_condition_failure(err: ClientError) -> bool:
    """Whether a `ClientError` is DynamoDB refusing a conditional write. One reader, so
    the string is spelled once."""
    return err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class ApiHandler(Protocol):
    """A route function after `@handler`: the Lambda entry point, plus the marker."""

    __api_handler__: bool

    def __call__(self, event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]: ...


def handler(fn: Callable[[Mapping[str, Any]], tuple[int, Any]]) -> ApiHandler:
    """Wrap a route function `(event) -> (status, body)` into a Lambda entry point.

    The log line carries the method, the path and the request id and **never the
    event**: a request body can hold a GitHub token, and CloudWatch is not where a
    credential should be recoverable from. `tests/test_api_route_contract.py` enforces
    that mechanically rather than trusting this comment.
    """

    @functools.wraps(fn)
    def wrapper(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
        request = event.get("requestContext", {}) if isinstance(event, Mapping) else {}
        http = request.get("http", {})
        method, path, request_id = http.get("method"), http.get("path"), request.get("requestId")
        LOG.info("%s %s request=%s", method, path, request_id)
        try:
            status, body = fn(event)
            return response(status, body)
        except ApiError as err:
            LOG.warning("%s %s refused: %s", method, path, scrub(err.body))
            return response(err.status, err.body)
        except ClientError as err:
            if is_condition_failure(err):
                LOG.warning("%s %s lost a conditional write", method, path)
                return response(
                    409,
                    {
                        "error": "Conflict",
                        "message": "the item changed underneath this request; re-read it and retry",
                    },
                )
            # A `ResourceNotFoundException` from DynamoDB is deliberately *not* a 404:
            # a missing table is a deploy bug and a 404 would hide it behind an empty
            # board.
            LOG.exception("%s %s failed against AWS", method, path)
            return response(500, {"error": "InternalError", "message": "unexpected error"})
        except Exception:
            # No `str(exc)` in the body: an exception argument is one of the places a
            # token turns up, and this is the one response nobody inspects.
            LOG.exception("%s %s raised", method, path)
            return response(500, {"error": "InternalError", "message": "unexpected error"})

    marked = cast("ApiHandler", wrapper)
    marked.__api_handler__ = True
    return marked


def now_iso() -> str:
    """The one timestamp format in the system: `2026-01-01T00:00:00.000Z`. The watchdog
    parses these back with `parse_iso`, so a second format would silently degrade its
    liveness clock to the fallback."""
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    """Parse a timestamp a container wrote, tolerantly: `Z` or `+00:00`, with or
    without microseconds -- `fromisoformat` has read a `Z` suffix itself since 3.11, and
    these handlers run on 3.12. Returns `None` rather than raising -- the reader is the
    watchdog, and one malformed activity row must not take out a sweep."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
