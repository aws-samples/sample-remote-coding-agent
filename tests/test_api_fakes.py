"""The AgentCore test harness, and the `load_handlers` helper every `test_api_*` uses.

Two things live here rather than in `tests/conftest.py`, which is jointly owned in this
wave and must not be edited:

1. `FakeAgentCore` -- AgentCore is *not* tested through `moto`. moto 5.2.2 has a
   partial `bedrock-agentcore-control` backend and it is a trap: `create_agent_runtime`
   accepts `filesystemConfigurations` and `get_agent_runtime` then drops the key
   entirely, so a carry-forward/converge test written against it asserts moto's bug and
   passes *while the mount is being stripped* -- the exact silent failure invariant 24
   exists for. `update_agent_runtime` also raises `KeyError: 'networkConfiguration'`
   when the key is omitted, and `stop_runtime_session` answers "Not yet implemented".
   So the fake validates its parameters against the **real botocore service model**
   instead, and `test_the_fake_rejects_a_call_the_real_service_model_rejects` keeps it
   from drifting into accepting something the real API would refuse.

2. `load_handlers` -- no test file may import a handler module at module level:
   `boto3.resource("dynamodb")` raises `NoRegionError` at *construction*, and pytest
   imports test modules at collection time, before `conftest._aws_env` sets a region.
   That is a collection error, which reads as a broken suite rather than as a test.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, ParamValidationError
from botocore.validate import validate_parameters

_REGION = "ap-southeast-1"


# Reload order is a dependency order: `_common` builds the clients, and everything
# else binds names out of it.
_HANDLER_MODULES = (
    "_common",
    "observability",
    "github_app",
    "runs",
    "provisioner",
    "configurations",
    "cards",
    "tasks",
    "dispatcher",
    "watchdog",
)


def load_handlers(*names: str) -> tuple[ModuleType, ...]:
    """`(_common, *modules)`, reloaded so their clients are built inside `mock_aws`.

    Every handler module *already imported* is reloaded too, not only the ones asked
    for, and that is not thoroughness for its own sake: `importlib.reload(_common)`
    builds a **new** `ApiError` class, so a module left holding the old one raises an
    exception the freshly decorated `@handler` does not recognise -- a 500 where the
    route meant a 409, and a traceback that says `ApiError` was not caught by
    `except ApiError`. Reloading everything in play keeps one identity per shared name.
    """
    common = importlib.reload(importlib.import_module("handlers._common"))
    common.agentcore_control.cache_clear()
    common.agentcore_data.cache_clear()
    # A default AgentCore **data-plane** fake, set on the freshly reloaded module rather than
    # monkeypatched: moto answers `GetWorkloadAccessTokenForUserId` with "Not yet implemented",
    # and `runs.queue_run` now calls it on every dispatch to refuse a repository with no
    # GitHub authorisation. Without this every card-move and task-create test in the suite
    # would have to opt in to a fake for a credential it is not about. A test that cares
    # passes its own through `install`, which replaces this one.
    #
    # It defaults to **authorised**, because the alternative default -- every repository
    # unauthorised -- would make the ordinary path the one nobody tests.
    common.agentcore_data = lambda: FakeAgentCore("bedrock-agentcore")  # type: ignore[assignment]
    # The same argument for the prompt guardrail: `runs.queue_run` screens every prompt, and
    # moto has no `ApplyGuardrail`. A configured guardrail that **passes** is the default, so
    # every card-move and task-create test exercises the screening path rather than the
    # disabled one; a test about a refusal installs its own `FakeGuardrail`.
    common.bedrock_runtime.cache_clear()
    common.GUARDRAIL_ID = FakeGuardrail.ARN
    common.GUARDRAIL_VERSION = "1"
    common.bedrock_runtime = lambda: FakeGuardrail()  # type: ignore[assignment]
    stale = [
        name for name in _HANDLER_MODULES[1:] if name in names or f"handlers.{name}" in sys.modules
    ]
    modules = {
        name: importlib.reload(importlib.import_module(f"handlers.{name}")) for name in stale
    }
    return (common, *(modules[name] for name in names))


def client_error(
    code: str, message: str, *, status: int = 400, operation: str = "Unknown"
) -> ClientError:
    """A `ClientError` shaped like the real one, including the HTTP status -- the
    watchdog's retry branch keys on a 409 `RetryableConflictException`."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


def runtime(**overrides: Any) -> dict[str, Any]:
    """A `GetAgentRuntime`-shaped response. Defaults are a *converged* runtime; a test
    that cares about drift overrides exactly the members it is about."""
    live = {
        "agentRuntimeArn": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/rca_cfg_abc-XYZ",
        "agentRuntimeId": "rca_cfg_abc-XYZ",
        "agentRuntimeName": "rca_cfg_abc",
        "agentRuntimeVersion": "1",
        "status": "READY",
        "roleArn": "arn:aws:iam::123456789012:role/rca/rca-cfg-abc",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "description": "rca runtime for owner/repo",
        "lifecycleConfiguration": {"maxLifetime": 28800, "idleRuntimeSessionTimeout": 900},
        "filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/workspace"}}],
        "environmentVariables": {},
        # A converged runtime in a V2-only deployment reports V2. A runtime that reports
        # nothing here is a *pre-V2* one, which is drift -- there is a test for that, and it
        # pops this key rather than relying on the default.
        "platformVersion": "V2",
        "agentRuntimeArtifact": {
            "containerConfiguration": {
                "containerUri": "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/rca:latest"
            }
        },
    }
    live.update(overrides)
    return live


class FakeAgentCore:
    """One class, two service models: `bedrock-agentcore-control` and
    `bedrock-agentcore`.

    `__getattr__` resolves a snake_case attribute through a real (never called,
    never signed) client's `method_to_api_mapping`, validates the keyword arguments
    against that operation's input shape, records the call, and then answers from
    `errors` / `responses` / a small in-memory runtime store.
    """

    def __init__(
        self,
        service: str,
        *,
        runtimes: dict[str, dict[str, Any]] | None = None,
        responses: dict[str, Any] | None = None,
        errors: dict[str, Iterable[BaseException]] | None = None,
    ) -> None:
        self.service = service
        # Constructing a client neither signs nor sends anything; it is here purely as
        # the authority on what the real API accepts.
        # No credentials passed: `conftest._aws_env` is autouse and has already put fake ones
        # in the environment, which is where boto3 looks.
        self._client = boto3.client(service, region_name=_REGION)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.runtimes: dict[str, dict[str, Any]] = dict(runtimes or {})
        self.memories: dict[str, dict[str, Any]] = {}
        # The AgentCore Identity half. `authorized` is the set of 3LO `userId`s -- which this
        # system keys on `configId` -- that the token vault holds a credential for, and
        # `None` means "every one of them", which is the default on purpose: almost every
        # test in the suite is about something else and a repository with no GitHub
        # authorisation cannot queue a run at all (`runs.queue_run`). A test about the
        # unauthorised path sets this to an explicit set.
        self.authorized: set[str] | None = None
        self.providers: dict[str, dict[str, Any]] = {}
        self.workload_identities: dict[str, dict[str, Any]] = {}
        self.responses: dict[str, Any] = dict(responses or {})
        self.errors: dict[str, list[BaseException]] = {
            k: list(v) for k, v in (errors or {}).items()
        }

    # -- introspection helpers a test asserts against -----------------------------

    def operations(self) -> list[str]:
        return [operation for operation, _ in self.calls]

    def calls_to(self, operation: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == operation]

    def last(self, operation: str) -> dict[str, Any]:
        return self.calls_to(operation)[-1]

    # -----------------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        operation = self._client.meta.method_to_api_mapping.get(name)
        if operation is None:
            raise AttributeError(f"{self.service} has no operation {name}")
        model = self._client.meta.service_model.operation_model(operation)

        def call(**kwargs: Any) -> Any:
            validate_parameters(kwargs, model.input_shape)
            self.calls.append((operation, kwargs))
            queued = self.errors.get(operation)
            if queued:
                raise queued.pop(0)
            if operation in self.responses:
                canned = self.responses[operation]
                if isinstance(canned, list):
                    return canned.pop(0) if len(canned) > 1 else canned[0]
                return canned(**kwargs) if callable(canned) else canned
            return self._default(operation, kwargs)

        return call

    def _default(self, operation: str, kwargs: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0911 -- one arm per AgentCore operation; a dict dispatch would need ten closures to say what ten `if`s say plainly
        """The runtime and memory arms. Identity lives in `_identity_default`, split out
        purely for the complexity bound -- one flat `if` chain over every AgentCore operation
        this repository calls is past it, and nesting the two halves differently would make
        the fake's shape stop resembling the service's."""
        if operation == "CreateAgentRuntime":
            runtime_id = f"{kwargs['agentRuntimeName']}-ABC123"
            item = runtime(
                agentRuntimeId=runtime_id,
                agentRuntimeName=kwargs["agentRuntimeName"],
                agentRuntimeArn=(
                    f"arn:aws:bedrock-agentcore:{_REGION}:123456789012:runtime/{runtime_id}"
                ),
                status="CREATING",
                **{
                    k: v
                    for k, v in kwargs.items()
                    if k not in ("agentRuntimeName", "clientToken", "tags")
                },
            )
            self.runtimes[runtime_id] = item
            return {
                "agentRuntimeArn": item["agentRuntimeArn"],
                "agentRuntimeId": runtime_id,
                "agentRuntimeVersion": "1",
                "status": "CREATING",
            }
        if operation == "GetAgentRuntime":
            found = self.runtimes.get(kwargs["agentRuntimeId"])
            if found is None:
                raise client_error(
                    "ResourceNotFoundException",
                    "runtime not found",
                    status=404,
                    operation=operation,
                )
            return dict(found)
        if operation == "UpdateAgentRuntime":
            runtime_id = kwargs["agentRuntimeId"]
            found = dict(self.runtimes.get(runtime_id) or runtime(agentRuntimeId=runtime_id))
            found.update({k: v for k, v in kwargs.items() if k != "clientToken"})
            found["agentRuntimeVersion"] = str(int(found.get("agentRuntimeVersion", "1")) + 1)
            found["status"] = "UPDATING"
            self.runtimes[runtime_id] = found
            return {
                "agentRuntimeArn": found["agentRuntimeArn"],
                "agentRuntimeId": runtime_id,
                "agentRuntimeVersion": found["agentRuntimeVersion"],
                "status": "UPDATING",
            }
        if operation == "DeleteAgentRuntime":
            self.runtimes.pop(kwargs["agentRuntimeId"], None)
            return {"status": "DELETING"}
        if operation == "InvokeAgentRuntime":
            return {"statusCode": 200, "contentType": "application/json"}
        if operation == "StopRuntimeSession":
            return {"runtimeSessionId": kwargs["runtimeSessionId"]}
        if operation in ("RetrieveMemoryRecords", "ListMemoryRecords"):
            # One `memoryRecordSummaries` entry, in the real response's shape: the text lives
            # at `content.text`, `createdAt` is a timestamp botocore parses into an aware
            # datetime (which is what `_common._json_default` has to serialise), and
            # `namespaces` echoes the namespace asked for so a test can see the route resolved
            # the template it meant to. `score` only on the search arm: a browse ranks nothing,
            # and a route that published a score for a browse would be inventing one.
            summary: dict[str, Any] = {
                "memoryRecordId": "mem_rec_0123456789",
                "content": {"text": "`make test` is the verify command for this repository"},
                "namespaces": [kwargs.get("namespace", "")],
                # Fixed, never `datetime.now`: a fake whose clock moves makes an assertion on
                # `createdAt` untestable.
                "createdAt": datetime(2026, 1, 1, tzinfo=UTC),
            }
            if operation == "RetrieveMemoryRecords":
                summary["score"] = 0.75
            return {"memoryRecordSummaries": [summary]}
        if operation == "CreateMemory":
            # `-ABCDEF1234` is not decoration: a real memory id is `<name>-<10 alphanumerics>`
            # (botocore's own `memoryId` pattern), and the id this fake returns is fed straight
            # back into `delete_memory`, where this class validates it against that pattern. A
            # plainer suffix would make the teardown test fail inside the fake.
            memory_id = f"{kwargs['name']}-ABCDEF1234"
            item = {
                "id": memory_id,
                "arn": f"arn:aws:bedrock-agentcore:{_REGION}:123456789012:memory/{memory_id}",
                "name": kwargs["name"],
                "status": "CREATING",
                "eventExpiryDuration": kwargs["eventExpiryDuration"],
            }
            self.memories[memory_id] = item
            return {"memory": item}
        if operation == "DeleteMemory":
            self.memories.pop(kwargs["memoryId"], None)
            return {"memoryId": kwargs["memoryId"], "status": "DELETING"}
        return self._identity_default(operation, kwargs)

    def _identity_default(self, operation: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """The AgentCore Identity arms: the two-call token exchange, the one OAuth2 credential
        provider and the one workload identity."""
        if operation == "GetWorkloadAccessTokenForUserId":
            # The `userId` is echoed into the opaque token so the exchange below can tell
            # which identity it was minted for -- the real service carries it inside a signed
            # blob, and a fake that dropped it could not model per-user isolation at all,
            # which is the one property worth testing here.
            return {"workloadAccessToken": f"wat.{kwargs['userId']}"}
        if operation == "GetResourceOauth2Token":
            user_id = str(kwargs["workloadIdentityToken"]).removeprefix("wat.")
            if self.authorized is not None and user_id not in self.authorized:
                # Two different answers for "not authorised", and the fake must model both
                # because the callers do. With a `resourceOauth2ReturnUrl` the service
                # answers an `authorizationUrl` and no `accessToken`; without one it refuses
                # outright, because the absence of a return URL means "do not start a flow"
                # and there is no token to hand back.
                if not kwargs.get("resourceOauth2ReturnUrl"):
                    raise client_error(
                        "ValidationException",
                        "You must provide a ResourceOauth2ReturnUrl to proceed with this flow",
                        operation=operation,
                    )
                return {
                    "authorizationUrl": f"https://github.com/login/oauth/authorize?state={user_id}"
                }
            return {"accessToken": f"ghu_{user_id}"}
        return self._provider_default(operation, kwargs)

    def _provider_default(self, operation: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """The credential provider and workload identity CRUD `github_app.py` drives."""
        if operation == "CreateOauth2CredentialProvider":
            name = kwargs["name"]
            if name in self.providers:
                raise client_error(
                    "ConflictException", "provider exists", status=409, operation=operation
                )
            item = {
                "name": name,
                "credentialProviderArn": (
                    f"arn:aws:bedrock-agentcore:{_REGION}:123456789012"
                    f":token-vault/default/oauth2credentialprovider/{name}"
                ),
                # The per-provider callback, which is the value the whole connect flow is
                # ordered around: assigned here and never changed by an update, which
                # matches the service's own behaviour.
                "callbackUrl": (
                    "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/"
                    "callback/00000000-0000-4000-8000-000000000000"
                ),
            }
            self.providers[name] = item
            return dict(item)
        if operation == "GetOauth2CredentialProvider":
            found = self.providers.get(kwargs["name"])
            if found is None:
                raise client_error(
                    "ResourceNotFoundException", "no provider", status=404, operation=operation
                )
            return dict(found)
        if operation == "UpdateOauth2CredentialProvider":
            found = dict(self.providers.get(kwargs["name"]) or {"name": kwargs["name"]})
            # Deliberately does NOT touch `callbackUrl`: `github_app.convert` relies on it
            # surviving, and a fake that regenerated it would hide that dependency.
            self.providers[kwargs["name"]] = found
            return dict(found)
        if operation == "DeleteOauth2CredentialProvider":
            if kwargs["name"] not in self.providers:
                raise client_error(
                    "ResourceNotFoundException", "no provider", status=404, operation=operation
                )
            self.providers.pop(kwargs["name"], None)
            return {}
        return self._workload_identity_default(operation, kwargs)

    def _workload_identity_default(self, operation: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """The one workload identity, whose `allowedResourceOauth2ReturnUrls` is what stops an
        authorisation being redirected anywhere the console is not."""
        if operation == "CreateWorkloadIdentity":
            name = kwargs["name"]
            if name in self.workload_identities:
                raise client_error(
                    "ConflictException", "identity exists", status=409, operation=operation
                )
            item = {
                "name": name,
                "workloadIdentityArn": (
                    f"arn:aws:bedrock-agentcore:{_REGION}:123456789012"
                    f":workload-identity-directory/default/workload-identity/{name}"
                ),
                "allowedResourceOauth2ReturnUrls": list(
                    kwargs.get("allowedResourceOauth2ReturnUrls") or []
                ),
            }
            self.workload_identities[name] = item
            return dict(item)
        if operation == "GetWorkloadIdentity":
            found = self.workload_identities.get(kwargs["name"])
            if found is None:
                raise client_error(
                    "ResourceNotFoundException", "no identity", status=404, operation=operation
                )
            return dict(found)
        if operation == "UpdateWorkloadIdentity":
            item = dict(self.workload_identities.get(kwargs["name"]) or {"name": kwargs["name"]})
            item["allowedResourceOauth2ReturnUrls"] = list(
                kwargs.get("allowedResourceOauth2ReturnUrls") or []
            )
            self.workload_identities[kwargs["name"]] = item
            return dict(item)
        return {}


class FakeGuardrail(FakeAgentCore):
    """`bedrock-runtime`'s `ApplyGuardrail`, validated against the real service model like
    every AgentCore call. `blocked` is the assessment list a refusal carries; `None` passes."""

    ARN = "arn:aws:bedrock:ap-southeast-1:123456789012:guardrail/abcd1234efgh"

    def __init__(
        self,
        *,
        blocked: list[dict[str, Any]] | None = None,
        errors: dict[str, Iterable[BaseException]] | None = None,
    ) -> None:
        super().__init__("bedrock-runtime", errors=errors)
        self.blocked = blocked

    def _default(self, operation: str, _kwargs: dict[str, Any]) -> dict[str, Any]:
        if operation != "ApplyGuardrail":
            raise AssertionError(f"no handler calls bedrock-runtime {operation}")
        if self.blocked is None:
            return {"action": "NONE", "outputs": [], "assessments": []}
        return {"action": "GUARDRAIL_INTERVENED", "outputs": [], "assessments": self.blocked}


def blocked_by(policy: str, entries: str, **entry: str) -> list[dict[str, Any]]:
    """One `ApplyGuardrail` assessment, in the real response's shape, that blocked."""
    return [{policy: {entries: [{"action": "BLOCKED", "detected": True, **entry}]}}]


def install(
    monkeypatch: pytest.MonkeyPatch,
    common: ModuleType,
    *,
    control: FakeAgentCore | None = None,
    data: FakeAgentCore | None = None,
    guardrail: FakeGuardrail | None = None,
) -> None:
    """Point `_common.agentcore_control`/`agentcore_data`/`bedrock_runtime` at fakes.
    Wholesale replacement of the accessor, not of its cache, so nothing reaches moto's
    low-fidelity AgentCore backend."""
    if control is not None:
        monkeypatch.setattr(common, "agentcore_control", lambda: control)
    if data is not None:
        monkeypatch.setattr(common, "agentcore_data", lambda: data)
    if guardrail is not None:
        monkeypatch.setattr(common, "bedrock_runtime", lambda: guardrail)


def test_the_fake_rejects_a_call_the_real_service_model_rejects() -> None:
    control = FakeAgentCore("bedrock-agentcore-control")
    with pytest.raises(ParamValidationError):
        control.get_agent_runtime(agentRuntimeId="abc", bogusParameter="x")

    data = FakeAgentCore("bedrock-agentcore")
    with pytest.raises(ParamValidationError):
        # A raw 20-character task id: botocore's own `SessionType` minimum is 33, and
        # the fake has to refuse it or invariant 18 is untestable.
        data.stop_runtime_session(
            agentRuntimeArn="arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/r",
            runtimeSessionId="tsk_1700000000_abc",
        )
    assert data.calls == [], "a call that failed validation never reached the service"
