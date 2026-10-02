"""The one HTTP boundary in this package: endpoint discovery, SigV4, one `httpx` call.

Two things are deliberately concentrated here rather than spread over the tools.

**Every request goes through `ApiClient.request`, and `ALLOWED_METHODS` is
`{"GET", "POST"}`.** Teardown -- a `DELETE` of a configuration -- is therefore
unreachable from this package *by construction*, not by the weaker "we did not write
that tool". That is exactly what makes a broad, free-prose `/remote` skill safe to
ship: the worst a prompt injection can talk this server into is reading, or starting a
run the operator can see. Widening this set is not a refactor, it is a change of
threat model (PLAN's `## Claude Code surface`).

**The endpoint is discovered, never typed.** The API URL is a CloudFormation output, so
asking the operator for it makes the install two-phase: deploy, read an output, install
the plugin, paste it in. Reading the output *from the stack* removes that step
entirely, which is why `plugins/sample-rca/.claude-plugin/plugin.json` needs no required
`userConfig` at all. Resolution is lazy -- on the first tool call, not at import -- so
the server still starts against an account that has never been deployed to, and the
"run `make deploy`" message arrives as tool output a model can act on rather than as a
startup crash Claude Code reports as "server failed".

**The region pin is `RCA_REGION`, never `AWS_REGION`.** `AWS_REGION` is ambient: Claude
Code on Bedrock sets it for the *model's* region, and every MCP server it starts inherits
it. Using it as a pin would send the server to the model's region regardless of where the
API is deployed, and a plugin exporting `aws_region` as `AWS_REGION` would blank the
shell's own value. `RCA_REGION` is the one variable that means "I chose this region";
`AWS_REGION` is left to boto3 as the ambient default it is.

No request body is ever logged. `register_repository`'s body carries the operator's
GitHub token; method, path and status are the whole of what is interesting anyway.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError, NoCredentialsError, NoRegionError

LOG = logging.getLogger("rca_mcp.client")

# The stack `infra/` deploys the HTTP API in, and the output it publishes the URL as.
# Pinned to `app.API_STACK_NAME` by `tests/test_grant_contract.py`, so the two move
# together; the `Sample` prefix is there because this deployment shares an account with an
# unrelated `CodingAgent*` fleet (see the comment on `app.API_STACK_NAME`).
DEFAULT_STACK_NAME = "SampleCodingAgentApi"
API_URL_OUTPUT_KEY = "ApiUrl"

# API Gateway's SigV4 service name. The control plane is IAM-authorized, so the
# operator's own credentials are the only authentication this server has.
SERVICE = "execute-api"


class RcaError(RuntimeError):
    """Something the operator can fix, phrased so a model can read it out verbatim."""


class SetupRequired(RcaError):  # noqa: N818 -- the name is the instruction, not an "Error"
    """The control plane is not deployed (or carries no API URL yet).

    The message always names `make deploy`, because the failure this class exists for is a
    plugin installed *before* the deploy: a bare connection error there tells the operator
    nothing, and the next step is a named command. The deploy targets exist only in a
    checkout of the repository.
    """


class ApiError(RcaError):
    """A response the control plane chose. `{"error": <code>, "message": <text>, ...}`
    is its error body (`api/src/handlers/_common.py`'s `ApiError.body`) -- the code is
    a bare string, not a nested object."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _env(name: str) -> str | None:
    """An environment variable, or `None` if it is unset *or blank*.

    Load-bearing, not tidiness: the plugin's `.mcp.json` interpolates `userConfig`
    values into `env`, and an override the operator never set arrives as the **empty
    string**. A bare `os.environ.get("RCA_API_URL")` would then make `""` the endpoint
    and every request would be sent against a relative URL. `tests/test_mcp_client.py`
    pins this.
    """
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def cache_path() -> Path:
    """`~/.rca/endpoint.json` -- the resolved endpoint, so discovery is one
    `describe_stacks` per machine rather than one per server start.

    `Path.home()` rather than an invented `RCA_HOME`: tests redirect it with
    `monkeypatch.setenv("HOME", str(tmp_path))`, and it keeps this the same path
    `make deploy` writes.
    """
    return Path.home() / ".rca" / "endpoint.json"


def invalidate_cache() -> None:
    """Forget the cached endpoint. Called when a cached URL answers 403, which is what
    a redeployed API with a new id looks like from here."""
    cache_path().unlink(missing_ok=True)


def _load_cache() -> Mapping[str, Any] | None:
    """The parsed cache document, or `None`.

    Every failure is a `debug` and a fall-through: a corrupt file is a `describe_stacks`,
    never a broken tool.
    """
    path = cache_path()
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError) as err:
        LOG.debug("no usable endpoint cache at %s (%s)", path, err)
        return None
    if not isinstance(cached, dict):
        LOG.debug("the endpoint cache at %s is not an object; ignoring it", path)
        return None
    return cached


def _cached_region(stack_name: str) -> str | None:
    """The region the cache names for `stack_name`, or `None`.

    Consulted only when the caller pinned no region: there the document's own region
    beats this machine's ambient default, which is the whole of the cross-region deploy
    bug -- a `make deploy` into another region wrote the URL seconds ago and the ambient
    default made it read as "a different deployment".
    """
    cached = _load_cache()
    if cached is None or cached.get("stackName") != stack_name:
        return None
    region = cached.get("region")
    if not isinstance(region, str) or not region.strip():
        return None
    return region.strip()


def _read_cache(stack_name: str, region: str) -> str | None:
    """The cached API URL, but only if it was written for *this* stack in *this*
    region -- a developer with two deployments must not be served the other one.

    The region asked about is the *resolved* region, which for a caller that pinned
    nothing is the cache's own (`_cached_region`); the equality guard itself is
    unweakened, it is simply asked about the right region.

    Every failure is a `debug` and a fall-through. A corrupt cache file is a
    `describe_stacks` call, never a broken tool.
    """
    path = cache_path()
    cached = _load_cache()
    if cached is None:
        return None
    if cached.get("stackName") != stack_name or cached.get("region") != region:
        LOG.debug(
            "the endpoint cache names %s in %s, not %s in %s; ignoring it",
            cached.get("stackName"),
            cached.get("region"),
            stack_name,
            region,
        )
        return None
    url = cached.get("apiUrl")
    if not isinstance(url, str) or not url.strip():
        LOG.debug("the endpoint cache at %s carries no apiUrl; ignoring it", path)
        return None
    return url.strip().rstrip("/")


def _write_cache(url: str, *, stack_name: str, region: str, stack: Mapping[str, Any]) -> None:
    """Write `~/.rca/endpoint.json`.

    The shape is a contract shared with the deploy, not private to this module:
    `{"apiUrl", "stackName", "region", "stackLastUpdated", "writtenBy"}`.

    `make deploy` writes this same file at the end of a deploy, so a running MCP server
    picks the new endpoint up with no restart. The keys and spellings must match on both
    sides, or the deploy's file reads as "a different stack" here and every start pays
    for a `describe_stacks`.

    Written to `endpoint.json.tmp` and `replace`d, because a half-written cache is
    worse than none. `OSError` is a warning and nothing else: a read-only home degrades
    to one `describe_stacks` per start rather than to a server that cannot answer.
    """
    # moto's `describe_stacks` returns no `LastUpdatedTime` at all, and neither does a
    # real stack that has never been updated, so the fallback is required rather than
    # defensive.
    stamp = stack.get("LastUpdatedTime") or stack.get("CreationTime")
    document = {
        "apiUrl": url,
        "stackName": stack_name,
        "region": region,
        "stackLastUpdated": stamp.isoformat() if isinstance(stamp, datetime) else str(stamp or ""),
        "writtenBy": "rca-mcp",
    }
    path = cache_path()
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(document, indent=2) + "\n")
        temporary.replace(path)
    except OSError as err:
        LOG.warning("could not write the endpoint cache at %s (%s); carrying on", path, err)


# `https://<api-id>.execute-api.<region>.amazonaws.com[/stage]`, the only shape API Gateway
# issues. A custom domain names no region, and falls through to the ambient one.
_EXECUTE_API_HOST = re.compile(r"^[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com$")


def _region_from_url(url: str) -> str | None:
    """The region an `execute-api` URL names, or `None` for any other host."""
    match = _EXECUTE_API_HOST.match(urllib.parse.urlsplit(url).hostname or "")
    return match.group(1) if match else None


def resolve_endpoint(
    *, stack_name: str | None = None, region: str | None = None, refresh: bool = False
) -> tuple[str, str, str]:
    """`(url, source, region)` for the control plane, where `source` is `env`, `cache` or
    `cloudformation` and `region` is the region settled on -- the caller signs in it.

    The precedence is fixed: an explicit `RCA_API_URL` (a hand-run server, or a second
    deployment), then the cache, then the stack's `ApiUrl` output. `refresh=True` skips
    the cache and is how the 403 recovery path re-reads a redeployed endpoint.

    The region is *pinned* when the caller passed one, or set `RCA_REGION` or
    `RCA_STACK_NAME` -- never by `AWS_REGION`, which is ambient (see the module docstring).
    An operator who named a region or a stack is steering discovery by hand and must not
    have it redirected by a stale file. Pinned, this behaves as it always has.
    Unpinned, the cache's own region beats this machine's ambient default: that is how a
    `make deploy` into another region is picked up with no restart, instead of being
    reported as "not deployed". `RCA_STACK_NAME` counting as a *region* pin is
    asymmetric on its face; naming a stack is manual steering, and the escape hatches
    are the same three variables either way.
    """
    override = _env("RCA_API_URL")
    if override:
        # No session on this arm: the override exists so a hand-run server needs no
        # `cloudformation:DescribeStacks`, and `""` means "no opinion on the region".
        # The URL's own region outranks the ambient `AWS_REGION`: an `execute-api` URL
        # names the region it answers in, and signing for any other region returns 403.
        signing_region = (
            region or _env("RCA_REGION") or _region_from_url(override) or _env("AWS_REGION") or ""
        )
        return override.rstrip("/"), "env", signing_region

    pinned = bool(region or _env("RCA_REGION") or _env("RCA_STACK_NAME"))
    stack_name = stack_name or _env("RCA_STACK_NAME") or DEFAULT_STACK_NAME
    # The session resolves the region the same way every other AWS call on this machine
    # does, so the cache is keyed on the region actually in force rather than on
    # whatever was passed in.
    session = boto3.session.Session(region_name=region or _env("RCA_REGION"))
    resolved_region = session.region_name or ""

    if not pinned:
        from_cache = _cached_region(stack_name)
        if from_cache and from_cache != resolved_region:
            LOG.info(
                "no region was pinned, so honouring the endpoint cache's own region %s "
                "rather than this machine's default %s",
                from_cache,
                resolved_region or "(none)",
            )
            resolved_region = from_cache
            # Rebuilt so the `describe_stacks` fallback lands in the same region the
            # cache read is keyed on; the two must not disagree.
            session = boto3.session.Session(region_name=resolved_region)

    if not refresh:
        cached = _read_cache(stack_name, resolved_region)
        if cached:
            return cached, "cache", resolved_region

    return (
        _from_cloudformation(session, stack_name, resolved_region),
        "cloudformation",
        resolved_region,
    )


def _from_cloudformation(session: Any, stack_name: str, region: str) -> str:
    """The stack's `ApiUrl` output, and the cache written as a side effect.

    Every failure arm below is a *message*, because the reader is a model relaying it to
    an operator: a missing stack and a missing credential need different fixes, so they
    get different wording, and only the first names `make deploy`.
    """
    try:
        cloudformation = session.client("cloudformation")
        stacks = cloudformation.describe_stacks(StackName=stack_name).get("Stacks") or []
    except (NoCredentialsError, NoRegionError) as err:
        raise RcaError(
            f"cannot look up the control plane's endpoint: {err}. This is a credential or "
            "region problem on this machine, not a missing deployment -- set AWS_PROFILE (or "
            "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY) and RCA_REGION, then try again."
        ) from err
    except ClientError as err:
        error = err.response.get("Error", {})
        if error.get("Code") == "ValidationError" and "does not exist" in error.get("Message", ""):
            raise SetupRequired(
                f"there is no CloudFormation stack named {stack_name} in "
                f"{region or 'the default region'}, so the remote coding agent is not deployed "
                "in this account and region yet. Deploy it with `make deploy EMAIL=...` from a "
                "checkout of the repository. If it is deployed somewhere else, set RCA_REGION "
                "or RCA_STACK_NAME (or RCA_API_URL) instead."
            ) from err
        raise

    for stack in stacks:
        for output in stack.get("Outputs") or []:
            if output.get("OutputKey") != API_URL_OUTPUT_KEY:
                continue
            url = str(output.get("OutputValue") or "").strip().rstrip("/")
            if url:
                LOG.info("resolved the control plane endpoint from stack %s", stack_name)
                _write_cache(url, stack_name=stack_name, region=region, stack=stack)
                return url

    raise SetupRequired(
        f"the stack {stack_name} exists in {region or 'the default region'} but carries no "
        f"{API_URL_OUTPUT_KEY} output, so its HTTP API is not deployed. That is a half-finished "
        "deployment: re-run `make deploy EMAIL=...`, or set RCA_API_URL if you know the URL."
    )


class ApiClient:
    """The control plane, over SigV4. One method sends; everything else is a wrapper.

    The signing region follows the resolved endpoint: whatever region `resolve_endpoint`
    settles on is the region every request is signed for (`_adopt`).
    """

    # The whole safety story of this package, as one line. `DELETE` of a configuration
    # is teardown, and teardown is a human at a terminal: the runtime, the execution
    # role and the GitHub secret go first (invariant 22), and no amount of prompt
    # injection should be able to start that from a chat window.
    ALLOWED_METHODS = frozenset({"GET", "POST"})

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        stack_name: str | None = None,
        region: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/") if endpoint else ""
        # `explicit` is not one of `resolve_endpoint`'s sources on purpose: only a
        # *cached* endpoint may be invalidated on a 403.
        self._endpoint_source = "explicit" if endpoint else ""
        self._stack_name = stack_name or _env("RCA_STACK_NAME") or DEFAULT_STACK_NAME
        self._region = region or _env("RCA_REGION")
        # For tests only: `httpx.MockTransport` stands in for the network, because
        # `moto.mock_aws` patches botocore and never sees an httpx call.
        self._transport = transport
        self._timeout = timeout
        self._session: Any = None
        self._refreshed = False

    def _aws(self) -> Any:
        """The boto3 session, built on first use: constructing an `ApiClient` must not
        depend on a working credential chain."""
        if self._session is None:
            self._session = boto3.session.Session(region_name=self._region)
        return self._session

    def endpoint(self) -> str:
        """The API's base URL, resolved once and memoised.

        Lazy so `main()` starts fine against an account with no deployment, and the
        `make deploy` message is delivered as tool output instead of a startup crash.
        """
        if not self._endpoint:
            self._adopt(*resolve_endpoint(stack_name=self._stack_name, region=self._region))
        return self._endpoint

    def _adopt(self, url: str, source: str, region: str) -> None:
        """Take a freshly resolved endpoint, and with it the region to sign in.

        The resolved region *is* the SigV4 signing region (`_send` signs with
        `session.region_name`), so honouring a cache from another region without this
        assignment is an `execute-api` request signed for the wrong region -- a bare 403,
        not a clean failure. A session already built for the old region is dropped.
        """
        self._endpoint, self._endpoint_source = url, source
        if region and region != self._region:
            self._region = region
            self._session = None

    def provenance(self) -> tuple[str, str, str]:
        """`(url, source, region)` for the endpoint actually in force. Purely local.

        Sends no request, resolves nothing and builds no session of its own: it reports
        what `_adopt` (or the constructor) has already settled, and `endpoint()` is lazy
        on purpose -- a line of *reporting* must not be the thing that triggers a
        `describe_stacks`.

        `source` is `resolve_endpoint`'s `env`/`cache`/`cloudformation`, or `explicit`
        for an endpoint handed to the constructor. Before anything is resolved -- which
        includes a tool that failed *during* discovery -- this is `("", "", "")` and the
        caller renders nothing. Gated on `_endpoint` rather than on `_endpoint_source`:
        `_region` can be set from `RCA_REGION` while no endpoint has been settled at
        all, and a region with no URL is a worse answer than no line.
        """
        if not self._endpoint:
            return "", "", ""
        return self._endpoint, self._endpoint_source, self._region or ""

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Send one signed request and return its parsed body. Non-2xx is an `ApiError`."""
        if method not in self.ALLOWED_METHODS:
            raise ValueError(
                f"{method} is not a method this package can send: ALLOWED_METHODS is "
                f"{sorted(self.ALLOWED_METHODS)}, so removing a configuration "
                "(DELETE /configurations/{configId}) is unreachable from here by "
                "construction. Teardown is a human at a terminal -- it takes the AgentCore "
                "runtime, the execution role and the GitHub secret with it -- and that "
                "absence is what makes a broad skill over these tools safe to ship."
            )

        response = self._send(method, path, query=query, body=body)
        if response.status_code == 403 and self._endpoint_source == "cache" and not self._refreshed:
            # A redeploy gives the HTTP API a new id, so a cached URL points at an API
            # this account no longer has and API Gateway answers 403. Once per process,
            # and only on 403: a 404 is a legitimate `NotFound` from the control plane
            # (`get_repository` of an unknown id) and invalidating on it would be wrong.
            LOG.warning("the cached endpoint answered 403; re-reading it from CloudFormation")
            self._refreshed = True
            invalidate_cache()
            self._adopt(
                *resolve_endpoint(stack_name=self._stack_name, region=self._region, refresh=True)
            )
            response = self._send(method, path, query=query, body=body)

        payload = self._decode(response)
        if not 200 <= response.status_code < 300:
            details = payload if isinstance(payload, Mapping) else {}
            raise ApiError(
                response.status_code,
                str(details.get("error") or "HttpError"),
                str(details.get("message") or f"the control plane answered {response.status_code}"),
            )
        return payload

    def get(self, path: str, **query: Any) -> Any:
        return self.request("GET", path, query=query)

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        return self.request("POST", path, body=body)

    def _send(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None,
        body: Mapping[str, Any] | None,
    ) -> httpx.Response:
        url = f"{self.endpoint()}{path}"
        items = {name: value for name, value in (query or {}).items() if value is not None}
        if items:
            url = f"{url}?{urllib.parse.urlencode(items)}"

        content = json.dumps(dict(body)).encode() if body is not None else None
        headers = {"content-type": "application/json"} if content is not None else {}
        signed = AWSRequest(method=method, url=url, data=content, headers=headers)
        session = self._aws()
        credentials = session.get_credentials()
        if credentials is None:
            raise RcaError(
                "no AWS credentials are available, so the request cannot be signed. The "
                "control plane is IAM-authorized: set AWS_PROFILE, or the access-key "
                "variables, to the account the agent is deployed in."
            )
        SigV4Auth(credentials.get_frozen_credentials(), SERVICE, session.region_name).add_auth(
            signed
        )

        # Method, path and status, and never the body: `register_repository`'s body
        # carries the operator's GitHub token, and a debug log is the easiest place in
        # the system to leak it from.
        LOG.debug("%s %s", method, path)
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as http:
                response = http.request(method, url, content=content, headers=dict(signed.headers))
        except httpx.TransportError as err:
            raise RcaError(self._unreachable(err)) from err
        LOG.debug("%s %s -> %s", method, path, response.status_code)
        return response

    def _unreachable(self, err: httpx.TransportError) -> str:
        """What a host that does not answer means, named by where the URL came from.

        A bare `[Errno 8] nodename nor servname provided` was what an operator actually saw
        when a deleted deployment's URL was still configured: true, and no help, because the
        fix depends entirely on *which* of the four sources supplied the dead URL.
        """
        host = urllib.parse.urlsplit(self._endpoint).netloc or self._endpoint
        fix = {
            "env": (
                "It came from RCA_API_URL -- the plugin's `api_url` setting, if this is the "
                "plugin -- which outranks discovery. Clear it (or point it at the live "
                "deployment) and restart Claude Code."
            ),
            "cache": (
                f"It came from {cache_path()}, which the last `make deploy` on this machine "
                "wrote. That deployment is probably gone: delete the file and the next call "
                "re-discovers the endpoint from CloudFormation."
            ),
            "cloudformation": (
                f"It came from the {self._stack_name} stack's {API_URL_OUTPUT_KEY} output, so "
                "the stack exists but its API does not answer; check the deployment."
            ),
            "explicit": "It was passed in directly; check the URL.",
        }.get(self._endpoint_source, "")
        return f"the control plane at {host} could not be reached ({err}). {fix}".strip()

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        """The response body as JSON. An empty body is `{}` -- the control plane answers
        one of those -- and a body that is not JSON is reported as text rather than
        raising a decode error nobody can act on."""
        raw = response.text.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            LOG.debug("the control plane answered %s with a non-JSON body", response.status_code)
            return {"message": raw[:500]}
