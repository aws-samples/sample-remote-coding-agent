"""The one HTTP boundary: endpoint discovery, the cache, the method allow-list, SigV4.

Two mechanisms stub the two halves, and they are not interchangeable: `moto.mock_aws`
patches botocore, so it covers `describe_stacks` and sees nothing of the API call, while
`httpx.MockTransport` covers the API call and sees nothing of CloudFormation. A test that
needs both -- the stale-cache recovery -- uses both at once.

`HOME` is redirected to `tmp_path` wherever the cache is in play. `client.cache_path()`
is `Path.home() / ".rca" / "endpoint.json"` rather than an invented `RCA_HOME` precisely
so that redirect is the whole of the isolation, and so the path a test writes is the same
one `/sample-rca:setup` writes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
from conftest import FAKE_UNLOGGED_PAT
from moto import mock_aws
from rca_mcp.client import (
    DEFAULT_STACK_NAME,
    ApiClient,
    ApiError,
    cache_path,
    resolve_endpoint,
)

# conftest's `_aws_env` pins the region, and the cache is keyed on the region actually in
# force -- so this is the region every assertion below expects to see written.
_REGION = "ap-southeast-1"
_API_URL = "https://abc123.execute-api.ap-southeast-1.amazonaws.com"
_STALE_URL = "https://stale456.execute-api.ap-southeast-1.amazonaws.com"
# A second region, for the deploy-somewhere-else cases: the cache names one region while
# this machine's ambient default is the other.
_OTHER_REGION = "us-east-1"
_OTHER_URL = "https://other789.execute-api.us-east-1.amazonaws.com"


def _template(url: str) -> str:
    """A stack whose only interesting feature is an `ApiUrl` output.

    The marker resource is an SQS queue rather than a `WaitConditionHandle`: moto
    supports the queue silently and warns on the handle, and a `UserWarning` in a
    fixture is noise every reader of this file has to re-diagnose.
    """
    return json.dumps(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": {
                "Marker": {"Type": "AWS::SQS::Queue", "Properties": {"QueueName": "rca-marker"}}
            },
            "Outputs": {"ApiUrl": {"Value": url}},
        }
    )


def _create_stack(
    *, url: str = _API_URL, name: str = DEFAULT_STACK_NAME, region: str = _REGION
) -> None:
    boto3.client("cloudformation", region_name=region).create_stack(
        StackName=name, TemplateBody=_template(url)
    )


def _write_cache(url: str, *, stack: str = DEFAULT_STACK_NAME, region: str = _REGION) -> Path:
    """Write the cache file by hand, in the shape `_write_cache` in the client writes."""
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "apiUrl": url,
                "stackName": stack,
                "region": region,
                "stackLastUpdated": "2026-08-19T00:00:00+00:00",
                "writtenBy": "rca-mcp",
            }
        )
    )
    return path


def _refuse(request: httpx.Request) -> httpx.Response:
    """A transport that fails the test if anything reaches it."""
    raise AssertionError(f"{request.method} {request.url} reached the transport")


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty home, and no overrides: the state a fresh install is in."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("RCA_API_URL", raising=False)
    monkeypatch.delenv("RCA_STACK_NAME", raising=False)
    return tmp_path


def test_the_endpoint_override_wins_over_everything(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RCA_API_URL` beats a populated cache, and beats it *before* any AWS session."""
    _write_cache("https://cached.example.com")
    monkeypatch.setenv("RCA_API_URL", "https://override.example.com/")

    def _no_session(**kwargs: Any) -> Any:
        raise AssertionError(
            "resolve_endpoint built an AWS session even though RCA_API_URL was set; the "
            "override exists so a hand-run server needs no cloudformation:DescribeStacks"
        )

    monkeypatch.setattr(boto3.session, "Session", _no_session)

    # The trailing slash is stripped: every path the tools send starts with one.
    assert resolve_endpoint() == ("https://override.example.com", "env", _REGION)


def test_a_blank_override_is_not_an_endpoint(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`RCA_API_URL=""` falls through, because that is what the plugin passes.

    `plugins/sample-rca/.mcp.json` interpolates `${user_config.api_url}` into `env`, and an
    override the operator never set arrives as the empty string. Treated as a value it would
    become the endpoint and every request would be sent against a relative URL -- a total
    failure caused by leaving a field blank.
    """
    _write_cache(_API_URL)

    for blank in ("", "   "):
        monkeypatch.setenv("RCA_API_URL", blank)
        assert resolve_endpoint() == (_API_URL, "cache", _REGION), (
            f"RCA_API_URL={blank!r} is an unset override, not an endpoint"
        )


def test_the_cache_is_read_and_written(home: Path) -> None:
    """One `describe_stacks` per machine, and a cache that refuses to serve the wrong
    deployment or a corrupt file."""
    with mock_aws():
        _create_stack()

        assert resolve_endpoint() == (_API_URL, "cloudformation", _REGION)
        cached = json.loads(cache_path().read_text())
        assert cached["apiUrl"] == _API_URL
        assert cached["stackName"] == DEFAULT_STACK_NAME
        assert cached["region"] == _REGION
        assert cached["stackLastUpdated"], (
            "moto's describe_stacks returns no LastUpdatedTime, and neither does a real "
            "stack that has never been updated, so the CreationTime fallback is required"
        )
        assert cached["writtenBy"] == "rca-mcp"

        # The second resolution is free.
        assert resolve_endpoint() == (_API_URL, "cache", _REGION)

        # A cache written for another deployment must not be served: someone running two
        # of these would otherwise dispatch a task into the wrong account.
        _write_cache("https://other.example.com", stack="OtherApi")
        assert resolve_endpoint() == (_API_URL, "cloudformation", _REGION)
        # Specifically the *pinned* case: conftest's `_aws_env` sets AWS_REGION, which is
        # what keeps the region half of the guard live here. Unpinned, the cache's own
        # region would be honoured instead -- see the cross-region tests below.
        _write_cache("https://other.example.com", region=_OTHER_REGION)
        assert resolve_endpoint() == (_API_URL, "cloudformation", _REGION)

        # A corrupt cache is a `describe_stacks`, never an exception.
        cache_path().write_text("{ not json at all")
        assert resolve_endpoint() == (_API_URL, "cloudformation", _REGION)


def test_an_unpinned_cache_from_another_region_is_served_and_signed_for_it(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`make deploy` into a region that is not this machine's default, as one test.

    README step 2 promises a running server needs no restart. Nothing is pinned here --
    no `region=`, no `AWS_REGION`, no `RCA_STACK_NAME` -- so the cache's own region is a
    better answer than the ambient default, and the document written seconds ago must be
    served rather than discarded as "not deployed".

    No `mock_aws`: a cache hit makes no AWS call at all, so a `describe_stacks` escaping
    this test would fail loudly on conftest's fake credentials.
    """
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _OTHER_REGION)
    _write_cache(_API_URL, region=_REGION)

    assert resolve_endpoint() == (_API_URL, "cache", _REGION)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [], "nextCursor": None})

    client = ApiClient(transport=httpx.MockTransport(handler))
    assert client.get("/configurations") == {"items": [], "nextCursor": None}

    authorization = seen[0].headers["authorization"]
    assert f"/{_REGION}/execute-api/aws4_request" in authorization, (
        "the resolved region is also the SigV4 signing region, so honouring a cache from "
        "another region without threading that region back signs an execute-api request "
        f"for the ambient region -- a bare 403, not a clean failure; got {authorization}"
    )
    assert f"/{_OTHER_REGION}/" not in authorization, (
        f"the ambient region must not appear in the credential scope; got {authorization}"
    )


def test_a_cache_naming_another_stack_is_ignored_even_unpinned(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `stackName` guard is unconditional, and its region is not borrowed either.

    One assertion for two claims: the foreign-stack document was not served, and the
    region discovery fell back to was this machine's, not the one that document names.
    """
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _OTHER_REGION)
    _write_cache(_API_URL, stack="OtherApi", region=_REGION)

    with mock_aws():
        _create_stack(url=_OTHER_URL, region=_OTHER_REGION)
        assert resolve_endpoint() == (_OTHER_URL, "cloudformation", _OTHER_REGION)


def test_an_explicitly_pinned_region_ignores_a_cache_from_another_one(home: Path) -> None:
    """A developer with two deployments who names one must not be served the other.

    Pinning is what keeps the region half of the cache guard live, and an explicit
    `region=` is the strongest form of it.
    """
    _write_cache(_API_URL, region=_REGION)

    with mock_aws():
        _create_stack(url=_OTHER_URL, region=_OTHER_REGION)
        assert resolve_endpoint(region=_OTHER_REGION) == (
            _OTHER_URL,
            "cloudformation",
            _OTHER_REGION,
        )


def test_no_method_outside_get_and_post_can_be_requested() -> None:
    """The refusal is the first statement in `request`, before any transport.

    This is the whole safety story of the package as one assertion: `DELETE` of a
    configuration is unreachable by construction, not by having omitted a tool for it.
    """
    assert set(ApiClient.ALLOWED_METHODS) == {"GET", "POST"}

    client = ApiClient(endpoint=_API_URL, transport=httpx.MockTransport(_refuse))
    for method in ("DELETE", "PUT", "PATCH", "HEAD", "OPTIONS", "get"):
        with pytest.raises(ValueError) as raised:
            client.request(method, "/configurations/cfg_1")
        assert "DELETE /configurations/{configId}" in str(raised.value), (
            "the refusal names the route it makes unreachable, so a reader of the error "
            f"learns that teardown is a human at a terminal; got {raised.value}"
        )


def test_provenance_reports_the_settled_endpoint_without_resolving_one(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reporting accessor sends nothing and builds no session.

    `endpoint()` is lazy so `main()` starts against an account with no deployment; an
    accessor that resolved on demand would turn a line of *reporting* into a
    `describe_stacks` on every tool call, and would make the "/sample-rca:setup" answer
    depend on a credential chain. Both stubs are armed: any AWS session is an assertion,
    and so is any request reaching the transport.

    The real resolution happens *before* the session stub is armed, because a cache hit is
    not session-free: `resolve_endpoint` builds one to settle the region the cache is keyed
    on, and only the `RCA_API_URL` arm returns ahead of that. It makes no AWS *call* -- no
    `mock_aws` here, so a `describe_stacks` escaping would fail on conftest's fake
    credentials -- and once the endpoint is settled, every `provenance()` below runs with
    both stubs live.
    """
    _write_cache(_API_URL)

    # A real resolution first: after it, the source and region are the cache's, unchanged.
    resolved = ApiClient(transport=httpx.MockTransport(_refuse))
    assert resolved.endpoint() == _API_URL

    def _no_session(**kwargs: Any) -> Any:
        raise AssertionError(
            "provenance built an AWS session; it reports what is already settled and "
            "must not resolve an endpoint of its own"
        )

    monkeypatch.setattr(boto3.session, "Session", _no_session)

    assert resolved.provenance() == (_API_URL, "cache", _REGION)

    # Nothing settled yet: no line, and specifically not a region with no URL even though
    # conftest's `_aws_env` set AWS_REGION.
    assert ApiClient(transport=httpx.MockTransport(_refuse)).provenance() == ("", "", "")

    # An endpoint handed in is `explicit`, and is reported without touching either stub.
    explicit = ApiClient(
        endpoint=f"{_API_URL}/", region=_REGION, transport=httpx.MockTransport(_refuse)
    )
    assert explicit.provenance() == (_API_URL, "explicit", _REGION)


def test_requests_are_sigv4_signed() -> None:
    """Every request carries a SigV4 signature scoped to `execute-api`."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [], "nextCursor": None})

    client = ApiClient(endpoint=_API_URL, transport=httpx.MockTransport(handler))
    assert client.get("/configurations", limit=5, status=None) == {"items": [], "nextCursor": None}

    request = seen[0]
    assert str(request.url) == f"{_API_URL}/configurations?limit=5", (
        "a None query item is dropped rather than sent as the string 'None'"
    )
    authorization = request.headers["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256"), authorization
    assert f"/{_REGION}/execute-api/aws4_request" in authorization, (
        "the control plane is an IAM-authorized HTTP API, so the credential scope must "
        f"name execute-api in the API's own region; got {authorization}"
    )
    assert request.headers["x-amz-date"]
    # conftest's `_aws_env` sets AWS_SESSION_TOKEN, so the signer must forward it --
    # without the header a session credential is rejected with a bare 403.
    assert request.headers["x-amz-security-token"] == "testing"


def test_a_refused_request_becomes_an_ApiError() -> None:  # noqa: N802 -- the class is the point
    """The control plane's error body is `{"error": <code>, "message": <text>}`."""
    body = {
        "error": "ConfigurationNotReady",
        "message": "the configuration is CREATING; wait for READY",
        "configId": "cfg_1a2b3c",
    }
    client = ApiClient(
        endpoint=_API_URL,
        transport=httpx.MockTransport(lambda _request: httpx.Response(409, json=body)),
    )

    with pytest.raises(ApiError) as raised:
        client.post("/tasks", {"configId": "cfg_1a2b3c", "prompt": "add a --version flag"})

    error = raised.value
    assert error.status == 409
    assert error.code == "ConfigurationNotReady", (
        "the code is a bare string in the body, not a nested object (api/src/handlers/"
        "_common.py::ApiError.body), and reading it wrongly turns every refusal into 'HttpError'"
    )
    assert error.message == body["message"]
    assert "ConfigurationNotReady" in str(error)


def test_a_stale_cached_endpoint_is_refreshed_once_on_403(home: Path) -> None:
    """A redeploy gives the HTTP API a new id, so a cached URL answers 403.

    Both stubs at once: moto answers `describe_stacks` with the new URL, and
    `httpx.MockTransport` decides which URL is alive.
    """
    with mock_aws():
        _create_stack(url=_API_URL)
        _write_cache(_STALE_URL)
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if str(request.url).startswith(_STALE_URL):
                return httpx.Response(403, json={"message": "Forbidden"})
            return httpx.Response(200, json={"items": []})

        client = ApiClient(transport=httpx.MockTransport(handler))
        assert client.get("/configurations") == {"items": []}
        assert seen == [f"{_STALE_URL}/configurations", f"{_API_URL}/configurations"], (
            "the stale endpoint is retried exactly once, against the URL re-read from "
            f"CloudFormation; got {seen}"
        )
        assert json.loads(cache_path().read_text())["apiUrl"] == _API_URL

        # Once per process, and only on 403: a 404 is a legitimate NotFound from the
        # control plane, and invalidating the cache on one would be wrong.
        _write_cache(_STALE_URL)
        refused: list[str] = []

        def always_403(request: httpx.Request) -> httpx.Response:
            refused.append(str(request.url))
            return httpx.Response(403, json={"error": "Forbidden", "message": "not this account"})

        with pytest.raises(ApiError) as raised:
            ApiClient(transport=httpx.MockTransport(always_403)).get("/configurations")

        assert raised.value.status == 403
        assert len(refused) == 2, (
            "a second 403 after the refresh is the answer, not another lookup: retrying "
            f"in a loop would be one describe_stacks per call forever; got {refused}"
        )


def test_no_request_body_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """`register_repository`'s body carries the operator's GitHub token.

    A DEBUG log line is the easiest place in this system to leak it from, so the client
    logs method, path and status and never a body. `api/`'s handlers hold the same rule
    through `scrub`; here it is held by hand, and by this test.
    """
    token = FAKE_UNLOGGED_PAT
    client = ApiClient(
        endpoint=_API_URL,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                201, json={"configId": "cfg_9z8y7x", "status": "PENDING"}
            )
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="rca_mcp.client"):
        client.post(
            "/configurations",
            {
                "repositoryUrl": "https://github.com/example-org/sample-remote-coding-agent",
                "githubToken": token,
            },
        )

    assert caplog.records, "the client logs the method, the path and the status at DEBUG"
    assert any("POST /configurations" in record.getMessage() for record in caplog.records)
    assert token not in caplog.text
    for record in caplog.records:
        assert token not in record.getMessage()
        assert token not in str(record.args), (
            "a token passed as a logging argument is still in the record, and still "
            "reaches every handler attached downstream"
        )
