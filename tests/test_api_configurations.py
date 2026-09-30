"""`/configurations`: registration, the closed patch allow-list, and token rotation.

Registration is the one route that writes a credential, so most of what is pinned here
is about the token: that a shape outside the allowlist is refused offline with the
reason, that a token which *reads* but cannot push is refused in GitHub's own words, that
the value reaches neither the item nor the response nor a log line, and that rotation
updates the one credential provider the execution role is allowed to read.

Handler modules build their clients at import, so `_load` reloads them inside `mock_aws`
-- see `test_api_fakes.load_handlers`. `provisioner` is reloaded first because
`configurations` imports it.

Every test here gets a fake GitHub that says the token can push (the autouse `github`
fixture), so no test in this file opens a socket. `github_probe` is deliberately absent
from `test_api_fakes._HANDLER_MODULES`: it is never reloaded, so the patch on its one
socket seam survives `_load(...)` -- which is the reason the probe is a module of its own
rather than more code in `configurations.py`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from conftest import (
    FAKE_APP_INSTALLATION_PAT,
    FAKE_APP_REFRESH_PAT,
    FAKE_APP_USER_PAT,
    FAKE_CLASSIC_HEX_PAT,
    FAKE_CLASSIC_PREFIXED_PAT,
    FAKE_FINE_GRAINED_PAT,
    FAKE_FINE_GRAINED_REPLACEMENT_PAT,
    FAKE_OAUTH_PAT,
    api_event,
)
from test_api_fakes import FakeAgentCore, install
from test_api_fakes import load_handlers as _load

_URL = "https://github.com/owner/repo"
_TOKEN = FAKE_FINE_GRAINED_PAT
_CONFIG_ID = "cfg_0123456789ab"
# A realistic memory id, and the length is load-bearing: botocore's own `memoryId` shape has
# a minimum of 12 characters and `FakeAgentCore` validates against it, so a short stand-in
# would raise a `ParamValidationError` inside the fake before any assertion here ran.
_MEMORY_ID = "rca_mem_cfg_0123456789ab-AbCdEfGhIj"
# What `provisioner.EPISODIC_NAMESPACE` resolves to for `_CONFIG_ID`, written out. The tests
# below assert both this literal and the `.format()` of the constant: the literal catches an
# edit to the template, the `.format` catches the handler re-spelling it.
_EPISODIC = "/rca/cfg_0123456789ab/episodic"


class _FakeGitHub:
    """A recording stand-in for `github_probe._request`.

    Routed by method rather than driven by a queue, so one instance serves any number of
    probes -- `test_rotation_writes_a_new_version_of_the_one_secret` registers and then
    rotates, which is two. A test that cares about a refusal overrides one member.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.commits: tuple[int, Any, dict[str, str]] = (200, [{"sha": "a" * 40}], {})
        self.create_ref: tuple[int, Any, dict[str, str]] = (201, {"ref": "refs/heads/probe"}, {})
        self.delete_ref: tuple[int, Any, dict[str, str]] = (204, None, {})

    def __call__(
        self, method: str, url: str, *, token: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, Any, dict[str, str]]:
        self.calls.append({"method": method, "url": url, "token": token, "payload": payload})
        if method == "GET":
            return self.commits
        return self.create_ref if method == "POST" else self.delete_ref


@pytest.fixture(autouse=True)
def github(monkeypatch: pytest.MonkeyPatch) -> _FakeGitHub:
    """A GitHub that says the token can push, installed for every test in this file.

    Autouse because *every* successful registration now probes GitHub, and a test that
    forgot the fake would reach `api.github.com` for real -- which it can, from CI: the
    stand-in token comes back 401 "Bad credentials" in milliseconds, so the symptom would
    be ten unrelated tests failing with a 400 rather than a slow suite.
    """
    from handlers import github_probe

    fake = _FakeGitHub()
    monkeypatch.setattr(github_probe, "_request", fake)
    return fake


# The Identity control plane the autouse fixture below installs, readable from a test
# without taking the fixture as a parameter. A plain module global rather than a fixture
# argument because most assertions here are "nothing was written", and threading one more
# parameter through every refusal test would say less than `_providers() == {}` does.
_IDENTITY: list[FakeAgentCore] = []


def _providers() -> dict[str, dict[str, Any]]:
    """The credential providers this test created, by name."""
    return _IDENTITY[-1].credential_providers


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch) -> FakeAgentCore:
    """An AgentCore Identity control plane, installed for every test in this file.

    Autouse because *every* successful registration now creates a credential provider --
    the GitHub token lives in AgentCore Identity rather than in a secret this code writes.
    moto has no `bedrock-agentcore-control` backend, so without this a registration answers
    500 with `NotImplementedError` from deep inside botocore.

    Patched on `boto3.Session.client` rather than on `_common.agentcore_control`, and that is
    not a preference: `test_api_fakes.load_handlers` calls `importlib.reload(_common)` inside
    each test body, which rebuilds every module attribute and would discard a patch applied
    by a fixture beforehand. `Session.client` is a class method the reload does not touch, so
    the interception survives. Every other service falls through to moto.
    """
    fake = FakeAgentCore("bedrock-agentcore-control")
    original = boto3.Session.client

    def client(self: Any, service_name: str, *args: Any, **kwargs: Any) -> Any:
        if service_name == "bedrock-agentcore-control":
            return fake
        return original(self, service_name, *args, **kwargs)

    monkeypatch.setattr(boto3.Session, "client", client)
    _IDENTITY.append(fake)
    return fake


def _body(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(response["body"])


def _seed(common: Any, **overrides: Any) -> dict[str, Any]:
    """A stored configuration, without going through the registration route -- the tests
    about `PATCH`/`DELETE` should not also depend on `create` working."""
    item = {
        "configId": _CONFIG_ID,
        "repository": "owner/repo",
        "repositoryUrl": _URL,
        "status": "READY",
        "statusReason": "",
        "verifyCommand": "make test",
        "credentialProviderName": f"rca_{_CONFIG_ID}",
        "credentialProviderArn": (
            f"arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:"
            f"token-vault/default/apikeycredentialprovider/rca_{_CONFIG_ID}"
        ),
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:00.000Z",
    }
    item.update(overrides)
    common.config_table.put_item(Item=item)
    return item


def _provision_messages(aws: Any) -> list[dict[str, Any]]:
    received = aws.sqs.receive_message(QueueUrl=aws.provision_queue_url, MaxNumberOfMessages=10)
    return [json.loads(message["Body"]) for message in received.get("Messages", [])]


def _register(
    configurations: Any, *, url: str = _URL, token: str = _TOKEN, **extra: Any
) -> dict[str, Any]:
    body = {"repositoryUrl": url, "githubToken": token, **extra}
    return configurations.create(api_event("POST", "/configurations", body=body), None)


def _for(config_id: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    return api_event(method, path, path_parameters={"configId": config_id}, **kwargs)


def _memory_client(monkeypatch: pytest.MonkeyPatch, common: Any, **kwargs: Any) -> FakeAgentCore:
    """The memory data plane as a recording fake, mirroring `test_api_watchdog.py::_data`.

    Installed for the no-memory and the two 400 tests as well as the reading ones, because
    `fake.calls == []` is the assertion that a route which answers without AgentCore really
    did not call it -- an absent fake would only prove `_common.agentcore_data` was never
    resolved, and would reach moto's low-fidelity backend if it ever were.
    """
    fake = FakeAgentCore("bedrock-agentcore", **kwargs)
    install(monkeypatch, common, data=fake)
    return fake


def test_create_registers_the_repository_and_queues_the_provisioner(aws: Any) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    response = _register(configurations, verifyCommand="make test")
    assert response["statusCode"] == 201

    body = _body(response)
    assert body["repository"] == "owner/repo"
    assert body["status"] == "PENDING"
    assert body["verifyCommand"] == "make test"
    assert body["configId"].startswith("cfg_")
    assert body["credentialProviderArn"]

    stored = common.config_table.get_item(Key={"configId": body["configId"]})["Item"]
    assert stored["repositoryUrl"] == _URL
    assert _provision_messages(aws) == [
        {"phase": "CREATE", "configId": body["configId"], "attempt": 0}
    ]


def test_an_absent_optional_command_stays_absent_rather_than_empty(aws: Any) -> None:
    """The three editable fields fall back to per-run detection when they are absent; a
    stored empty string is a declared "run nothing" and means something else."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    body = _body(_register(configurations))
    stored = common.config_table.get_item(Key={"configId": body["configId"]})["Item"]
    assert "verifyCommand" not in stored
    assert "setupCommand" not in stored


@pytest.mark.parametrize(
    "url", ["https://github.com/owner/repo.git", "git@github.com:owner/repo.git"]
)
def test_both_clone_url_forms_resolve_to_the_owner_repo_slug(aws: Any, url: str) -> None:
    _common, _provisioner, configurations = _load("provisioner", "configurations")
    assert _body(_register(configurations, url=url))["repository"] == "owner/repo"


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/owner/repo",
        "https://github.com/owner",
        "owner/repo",
        "https://github.com/",
    ],
)
def test_an_unrecognised_repository_url_names_the_accepted_forms(aws: Any, url: str) -> None:
    _common, _provisioner, configurations = _load("provisioner", "configurations")
    response = _register(configurations, url=url)
    assert response["statusCode"] == 400
    body = _body(response)
    assert body["error"] == "InvalidRepositoryUrl"
    assert "https://github.com/owner/repo" in body["message"]
    assert "git@github.com:owner/repo.git" in body["message"]


@pytest.mark.parametrize(
    "token",
    [
        FAKE_CLASSIC_PREFIXED_PAT,
        # A pre-2021 classic token: bare 40 hex characters, no prefix at all.
        FAKE_CLASSIC_HEX_PAT,
    ],
)
def test_a_classic_pat_is_accepted(aws: Any, github: _FakeGitHub, token: str) -> None:
    """A classic PAT registers, and that is a deliberate trade rather than an oversight.

    It cannot be scoped to one repository, so a stolen one reaches everything its owner can.
    What bought the change is setup churn: three decisions to create one against roughly nine
    for a fine-grained token, and people were giving up before they saw the agent run. The
    bound is stated in `README.md` -- a dedicated, disposable account -- which is the same
    bound this system already relies on for AWS.

    The live push check still runs, so an under-permissioned classic token is still caught in
    milliseconds rather than inside a container.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    response = _register(configurations, token=token)
    assert response["statusCode"] == 201, _body(response)
    stored = common.config_table.scan()["Items"]
    assert len(stored) == 1
    # It was proved pushable before it was stored, exactly as a fine-grained one is.
    assert github.calls, "the live push probe must run for a classic token too"


@pytest.mark.parametrize(
    "token",
    [
        # GitHub App installation and user-to-server tokens, and an OAuth user token. All
        # refused for the same non-security reason: they expire an hour after issue and
        # nothing here refreshes one, so accepting one moves the failure into a container an
        # hour later instead of reporting it now.
        FAKE_APP_INSTALLATION_PAT,
        FAKE_APP_USER_PAT,
        FAKE_APP_REFRESH_PAT,
        FAKE_OAUTH_PAT,
    ],
)
def test_a_token_that_expires_in_an_hour_is_refused_with_the_reason(
    aws: Any, github: _FakeGitHub, token: str
) -> None:
    """The refusal has to say *why*, or the operator retries with the same class of token."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    response = _register(configurations, token=token)
    assert response["statusCode"] == 400

    body = _body(response)
    assert body["error"] == "ExpiringTokenRejected"
    assert "expires an hour later" in body["message"]
    assert "github_pat_" in body["message"]
    # Nothing was written on the way to the refusal, and nothing was asked of GitHub: the
    # shape check is offline and comes first.
    assert common.config_table.scan()["Items"] == []
    assert _providers() == {}
    assert github.calls == []


@pytest.mark.parametrize(
    ("token", "error"),
    [
        # An App installation token: refused for its one-hour expiry, which there is no
        # refresh path for.
        (FAKE_APP_INSTALLATION_PAT, "ExpiringTokenRejected"),
        ("xoxb-slack-not-github-000000", "UnsupportedTokenType"),
        ("glpat-ABCDEFGHIJKLMNOPQRST", "UnsupportedTokenType"),
        ("just-a-string-someone-pasted", "UnsupportedTokenType"),
    ],
)
def test_a_token_outside_the_supported_shape_is_refused_without_a_network_call(
    aws: Any, github: _FakeGitHub, token: str, error: str
) -> None:
    """The allowlist half. A denylist admits every token type nobody thought of, and each
    of those is discovered 25 minutes later inside a container -- so the gate is
    `github_pat_...` and everything else is refused here, offline, naming what is
    supported."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    response = _register(configurations, token=token)
    assert response["statusCode"] == 400
    assert _body(response)["error"] == error
    assert "github_pat_" in _body(response)["message"]
    # Offline: the shape check runs before the probe, so a token this shape can be refused
    # with GitHub unreachable, and a registration attempt costs GitHub nothing.
    assert github.calls == []
    assert common.config_table.scan()["Items"] == []
    assert _providers() == {}


def test_a_token_that_reads_but_cannot_push_is_refused_in_githubs_own_words(
    aws: Any, github: _FakeGitHub
) -> None:
    """The headline regression, and the fact that cost four round trips to learn: passing
    the read check is not evidence. `GET /commits` answers 200 for a token with no
    `Contents` permission, and only the ref creation says no -- in GitHub's own words,
    which is what reaches the operator."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    github.create_ref = (
        403,
        {"message": "Resource not accessible by personal access token"},
        {"x-accepted-github-permissions": "contents=write"},
    )
    response = _register(configurations)
    assert response["statusCode"] == 400

    body = _body(response)
    assert body["error"] == "TokenCannotPush"
    assert "Resource not accessible by personal access token" in body["message"]
    assert body["acceptedPermissions"] == "contents=write"
    assert body["repository"] == "owner/repo"
    # Nothing was written on the way to the refusal.
    assert common.config_table.scan()["Items"] == []
    assert _providers() == {}
    # The read really did succeed first: this is the whole reason the probe is a write.
    assert [call["method"] for call in github.calls] == ["GET", "POST"]


def test_the_write_probe_creates_a_throwaway_ref_and_deletes_it(
    aws: Any, github: _FakeGitHub
) -> None:
    """The happy path, in three calls: a commit to branch from, a ref created from it, and
    the same ref removed again."""
    _common, _provisioner, configurations = _load("provisioner", "configurations")
    assert _register(configurations)["statusCode"] == 201

    assert [call["method"] for call in github.calls] == ["GET", "POST", "DELETE"]
    assert github.calls[0]["url"].endswith("/repos/owner/repo/commits?per_page=1")
    created = github.calls[1]["payload"]
    assert created["ref"].startswith("refs/heads/rca-write-probe-")
    assert created["sha"] == "a" * 40
    # The *same* generated name, or the probe leaves a ref behind on every registration.
    assert github.calls[2]["url"].endswith(f"heads/{created['ref'].removeprefix('refs/heads/')}")


def test_an_unreachable_github_does_not_fail_registration(
    aws: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The deliberate degrade: a control plane that cannot register a repository while
    GitHub has an incident is worse than one that finds out late."""
    from handlers import github_probe

    def unreachable(*_args: Any, **_kwargs: Any) -> None:
        raise github_probe.ProbeUnavailable("Name or service not known")

    monkeypatch.setattr(github_probe, "_request", unreachable)
    _common, _provisioner, configurations = _load("provisioner", "configurations")
    caplog.set_level(logging.WARNING, logger="handlers")

    response = _register(configurations)
    assert response["statusCode"] == 201
    stored = _body(response)
    assert _providers()[f"rca_{stored['configId']}"]["apiKey"] == _TOKEN

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "owner/repo" in logged, (
        "the warning names the repository, so the late failure is greppable"
    )
    assert _TOKEN not in logged


def test_a_github_5xx_does_not_fail_registration(aws: Any, github: _FakeGitHub) -> None:
    """The other half of "GitHub has an incident": it answered, but not with a verdict."""
    _common, _provisioner, configurations = _load("provisioner", "configurations")
    github.commits = (503, {"message": "Server Error"}, {})
    assert _register(configurations)["statusCode"] == 201


def test_the_probe_never_puts_the_token_in_a_url(aws: Any, github: _FakeGitHub) -> None:
    """The credential travels in the header argument and nowhere else: a query string is
    logged verbatim by every proxy between this Lambda and the API."""
    _common, _provisioner, configurations = _load("provisioner", "configurations")
    assert _register(configurations)["statusCode"] == 201
    assert github.calls, "the probe ran"
    for call in github.calls:
        assert _TOKEN not in call["url"]
        assert call["token"] == _TOKEN


def test_the_token_is_never_stored_on_the_item_or_echoed(
    aws: Any, caplog: pytest.LogCaptureFixture
) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    caplog.set_level(logging.DEBUG, logger="handlers")
    response = _register(configurations)

    assert _TOKEN not in response["body"]
    stored = common.config_table.get_item(Key={"configId": _body(response)["configId"]})["Item"]
    assert "githubToken" not in stored
    assert _TOKEN not in json.dumps(stored, default=str)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert _TOKEN not in logged
    assert "***" in logged, "the registration line is logged, scrubbed"
    # Identity is where it went, and reading it back is a deliberate act.
    assert _providers()[f"rca_{stored['configId']}"]["apiKey"] == _TOKEN


def test_registering_the_same_repository_twice_is_a_conflict(aws: Any) -> None:
    _common, _provisioner, configurations = _load("provisioner", "configurations")
    first = _body(_register(configurations))
    response = _register(configurations)
    assert response["statusCode"] == 409
    body = _body(response)
    assert body["error"] == "RepositoryAlreadyRegistered"
    assert body["configId"] == first["configId"]
    assert len(_providers()) == 1, "the second attempt created no credential provider"


def test_a_repository_being_torn_down_can_be_registered_again(aws: Any) -> None:
    """Teardown is asynchronous, so a `DELETING` configuration must not make the
    repository look permanently taken."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common, status="DELETING")
    assert _register(configurations)["statusCode"] == 201


def test_a_failed_registration_can_be_registered_again(aws: Any) -> None:
    """A `FAILED` configuration used to hold its repository name forever, and nothing the
    plugin can send clears it -- `ApiClient.ALLOWED_METHODS` is `{"GET", "POST"}`. So the
    registration succeeds and the stale item goes down the ordinary teardown path."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    stale = _seed(
        common,
        status="FAILED",
        statusReason="the provisioner lacked bedrock-agentcore:CreateAgentRuntimeEndpoint",
    )
    response = _register(configurations)
    assert response["statusCode"] == 201

    body = _body(response)
    assert body["configId"].startswith("cfg_")
    assert body["configId"] != _CONFIG_ID
    assert body["status"] == "PENDING"
    assert body["replacedConfigIds"] == [_CONFIG_ID]

    # Still present, and `DELETING`: the provisioner removes the item last (invariant 22),
    # so this route marks it and queues the teardown rather than deleting anything.
    retired = common.config_table.get_item(Key={"configId": _CONFIG_ID})["Item"]
    assert retired["status"] == "DELETING"
    fresh = common.config_table.get_item(Key={"configId": body["configId"]})["Item"]
    assert fresh["credentialProviderName"] != stale["credentialProviderName"]

    # Sorted: SQS does not promise order, and this is the first request that sends two.
    assert sorted(_provision_messages(aws), key=lambda message: message["phase"]) == [
        {"phase": "CREATE", "configId": body["configId"], "attempt": 0},
        {"phase": "DELETE", "configId": _CONFIG_ID, "attempt": 0},
    ]


def test_re_registering_stores_the_new_token_in_a_new_credential_provider(aws: Any) -> None:
    """`configId` is random, so a replacement gets its own provider name. The stale provider
    is neither reused for the new token nor mutated -- it belongs to the configuration
    being torn down, and the runtime that could read it is going away with it."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    stale_name = f"rca_{_CONFIG_ID}"
    _IDENTITY[-1].credential_providers[stale_name] = {"name": stale_name, "apiKey": "stale-token"}
    _seed(common, status="FAILED")

    new_token = FAKE_FINE_GRAINED_REPLACEMENT_PAT
    body = _body(_register(configurations, token=new_token))
    assert f"rca_{body['configId']}" != stale_name
    assert _providers()[f"rca_{body['configId']}"]["apiKey"] == new_token
    assert _providers()[stale_name]["apiKey"] == "stale-token"


def test_the_provisioner_tears_the_replaced_configuration_down(aws: Any) -> None:
    """The no-orphans half, end to end: the role at `ROLE_PATH`, its inline policy, the
    credential provider holding the operator's old token and the item all go, and the
    replacement is untouched."""
    common, provisioner, configurations = _load("provisioner", "configurations")
    stale_name = f"rca_{_CONFIG_ID}"
    _IDENTITY[-1].credential_providers[stale_name] = {"name": stale_name, "apiKey": "stale-token"}
    stale = _seed(common, status="FAILED")
    # Minted the way a failed CREATE minted it, rather than by hand, so the teardown is
    # tested against the real role name, path and inline policy.
    provisioner._ensure_role(_CONFIG_ID, stale)
    role_name = provisioner._role_name(_CONFIG_ID)
    assert common.iam.get_role(RoleName=role_name)["Role"]["Path"] == provisioner.ROLE_PATH

    body = _body(_register(configurations))
    (teardown,) = [message for message in _provision_messages(aws) if message["phase"] == "DELETE"]
    provisioner.lambda_handler(
        {"Records": [{"messageId": "msg-1", "body": json.dumps(teardown)}]}, None
    )

    assert common.config_table.get_item(Key={"configId": _CONFIG_ID}).get("Item") is None
    assert stale_name not in _providers(), (
        "the replaced configuration's credential provider goes with it -- and with the "
        "provider goes the service-linked secret underneath, which nothing here names"
    )
    with pytest.raises(ClientError) as raised:
        common.iam.get_role(RoleName=role_name)
    assert raised.value.response["Error"]["Code"] == "NoSuchEntity"

    fresh = common.config_table.get_item(Key={"configId": body["configId"]})["Item"]
    assert fresh["status"] == "PENDING"
    assert _providers()[fresh["credentialProviderName"]]["apiKey"] == _TOKEN


def test_a_repository_being_torn_down_is_not_torn_down_twice(aws: Any) -> None:
    """`DELETING` is ignored by the conflict check but must *not* be retired: its teardown
    is already in flight, and a second `DELETE` message would be a redundant replay."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common, status="DELETING")
    response = _register(configurations)
    assert response["statusCode"] == 201

    body = _body(response)
    assert "replacedConfigIds" not in body
    assert (
        common.config_table.get_item(Key={"configId": _CONFIG_ID})["Item"]["status"] == "DELETING"
    )
    assert _provision_messages(aws) == [
        {"phase": "CREATE", "configId": body["configId"], "attempt": 0}
    ]


def test_a_live_configuration_still_wins_over_a_stale_failed_one(
    aws: Any, github: _FakeGitHub
) -> None:
    """A `FAILED` item can sit beside a live one on the GSI, and the 409 has to name the
    configuration that actually holds the repository -- `web/js/views/register.js` reads
    `details.configId` to offer "Open that repository's board"."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common)
    _seed(common, configId="cfg_ffffffffffff", status="FAILED", statusReason="ran out of grants")

    response = _register(configurations)
    assert response["statusCode"] == 409
    body = _body(response)
    assert body["error"] == "RepositoryAlreadyRegistered"
    assert body["configId"] == _CONFIG_ID
    # A refused request retires nothing, writes nothing, and does not spend seconds
    # against GitHub on its way to a conflict it could see locally.
    assert _provision_messages(aws) == []
    assert github.calls == []
    assert _providers() == {}
    assert (
        common.config_table.get_item(Key={"configId": "cfg_ffffffffffff"})["Item"]["status"]
        == "FAILED"
    )


def test_get_and_list_are_read_only_and_404_when_absent(aws: Any) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common)

    found = configurations.get(_for(_CONFIG_ID, "GET", "/configurations/{configId}"), None)
    assert found["statusCode"] == 200
    assert _body(found)["repository"] == "owner/repo"

    missing = configurations.get(_for("cfg_nope", "GET", "/configurations/{configId}"), None)
    assert missing["statusCode"] == 404
    assert _body(missing)["error"] == "NotFound"

    listed = configurations.list_(api_event("GET", "/configurations"), None)
    assert [item["configId"] for item in _body(listed)["items"]] == [_CONFIG_ID]
    assert _body(listed)["nextCursor"] is None


def test_get_publishes_the_memory_id_and_omits_the_key_when_there_is_none(aws: Any) -> None:
    """`memoryId` is on the item or it is not, and the response says exactly that.

    A repository registered before memory existed will never have one, so the absent case
    is not an edge case -- it is the state of every configuration that predates it. The
    key is omitted rather than published as null: `_public` is an allow-list comprehension
    over the item, and a null would make the console print one.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common, memoryId="rca_owner_repo_mem-AbCdEf")

    found = configurations.get(_for(_CONFIG_ID, "GET", "/configurations/{configId}"), None)
    assert found["statusCode"] == 200
    assert _body(found)["memoryId"] == "rca_owner_repo_mem-AbCdEf"

    listed = configurations.list_(api_event("GET", "/configurations"), None)
    assert _body(listed)["items"][0]["memoryId"] == "rca_owner_repo_mem-AbCdEf"

    _seed(common)  # the same configId, put back without the attribute
    again = configurations.get(_for(_CONFIG_ID, "GET", "/configurations/{configId}"), None)
    assert "memoryId" not in _body(again)


def test_the_memory_route_browses_the_repositorys_episodic_namespace(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no `q`, the route browses -- and the namespace it browses is the repository's own.

    A mismatched namespace does not fail: `list_memory_records` simply returns nothing,
    forever, with no error and no warning. So the namespace is pinned twice, as the literal
    `provisioner.EPISODIC_NAMESPACE` resolves to *and* as the `.format()` of that constant:
    the literal fails if the template changes, the `.format` fails if the handler re-spells
    the string instead of importing it.
    """
    common, provisioner, configurations = _load("provisioner", "configurations")
    _seed(common, memoryId=_MEMORY_ID)
    fake = _memory_client(monkeypatch, common)

    response = configurations.memory(
        _for(_CONFIG_ID, "GET", "/configurations/{configId}/memory"), None
    )
    assert response["statusCode"] == 200
    # Through `_body`, i.e. through the real envelope's `json.dumps`: that is what proves the
    # `datetime` the service hands back for `createdAt` survives `_common._json_default`
    # rather than being a 500 on the way out.
    body = _body(response)
    assert body["memoryId"] == _MEMORY_ID
    assert body["scope"] == "episodic"
    assert body["namespace"] == _EPISODIC
    assert body["namespace"] == provisioner.EPISODIC_NAMESPACE.format(actorId=_CONFIG_ID)

    assert fake.operations() == ["ListMemoryRecords"], "no `q` is a browse, not a search"
    assert fake.last("ListMemoryRecords") == {
        "memoryId": _MEMORY_ID,
        "namespace": _EPISODIC,
        "maxResults": configurations._DEFAULT_MEMORY_RECORDS,
    }

    item = body["items"][0]
    assert set(item) == {"recordId", "text", "createdAt"}
    assert item["text"] == "`make test` is the verify command for this repository"
    assert item["createdAt"] == "2026-01-01T00:00:00+00:00"
    assert "score" not in item, "a browse ranks nothing, so a score would be invented"


def test_a_q_selects_the_search_arm_and_carries_the_bound_as_top_k(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`q` is the whole difference between the two arms, and `limit` bounds both."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common, memoryId=_MEMORY_ID)
    fake = _memory_client(monkeypatch, common)

    response = configurations.memory(
        _for(
            _CONFIG_ID,
            "GET",
            "/configurations/{configId}/memory",
            query={"q": "flaky test", "limit": "3"},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert fake.operations() == ["RetrieveMemoryRecords"]
    assert fake.calls_to("ListMemoryRecords") == [], "a search must not also browse"
    assert fake.last("RetrieveMemoryRecords") == {
        "memoryId": _MEMORY_ID,
        "namespace": _EPISODIC,
        # `topK` and not `maxResults`: the same bound, spelled the way each operation's own
        # input shape spells it -- and never 0, which botocore refuses outright.
        "searchCriteria": {"searchQuery": "flaky test", "topK": 3},
    }
    item = _body(response)["items"][0]
    assert item["score"] == 0.75, "the search arm ranks, so the rank is published"


def test_scope_session_reads_the_tasks_namespace_and_needs_a_task_id(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`actorId` is the configuration id and `sessionId` the task id -- D6's pairing.

    Getting them the other way round resolves to a namespace nobody ever writes to, which is
    why the expected value here is the `.format()` of `provisioner`'s own template rather
    than a string typed out twice.
    """
    common, provisioner, configurations = _load("provisioner", "configurations")
    _seed(common, memoryId=_MEMORY_ID)
    fake = _memory_client(monkeypatch, common)
    task_id = "tsk_1700000000000_abcdef"

    found = configurations.memory(
        _for(
            _CONFIG_ID,
            "GET",
            "/configurations/{configId}/memory",
            query={"scope": "session", "taskId": task_id},
        ),
        None,
    )
    assert found["statusCode"] == 200
    assert _body(found)["scope"] == "session"
    assert _body(found)["namespace"] == provisioner.SESSION_NAMESPACE.format(
        actorId=_CONFIG_ID, sessionId=task_id
    )
    assert fake.last("ListMemoryRecords")["namespace"] == _body(found)["namespace"]

    without = configurations.memory(
        _for(_CONFIG_ID, "GET", "/configurations/{configId}/memory", query={"scope": "session"}),
        None,
    )
    assert without["statusCode"] == 400
    assert _body(without)["error"] == "MissingQueryParameter"
    assert "taskId" in _body(without)["message"]
    # The scope check runs before the read, so the malformed request costs no call: a 400 that
    # had already read the whole episodic namespace would be a client bug charged to the
    # service.
    assert fake.calls_to("RetrieveMemoryRecords") == []
    assert len(fake.calls) == 1, "only the well-formed request reached AgentCore"


def test_an_unknown_scope_is_refused_rather_than_treated_as_episodic(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-facing route refuses what a model tool tolerates.

    `agentcore_memory.TaskMemory._namespace` warns and falls back to episodic because a
    *model* supplies its scope inside a tool call, where an unrecognised value has to be
    cheap. Here the supplier is an operator's client, so the value is a client bug -- and a
    silent fallback would answer a question nobody asked, with records from a namespace the
    caller did not name.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common, memoryId=_MEMORY_ID)
    fake = _memory_client(monkeypatch, common)

    response = configurations.memory(
        _for(_CONFIG_ID, "GET", "/configurations/{configId}/memory", query={"scope": "semantic"}),
        None,
    )
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "UnknownMemoryScope"
    assert _body(response)["scopes"] == list(configurations._MEMORY_SCOPES)
    assert fake.calls == [], "a refused scope reads nothing at all"


def test_a_repository_with_no_memory_answers_an_empty_list_and_calls_nothing(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No memory is a 200 with an empty list, and `memoryId` is present and null.

    `_public` omits the key when there is none; this route publishes it as null instead,
    because this route's whole answer is *about* the memory -- "there is none" has to be
    sayable, and an absent key reads as a response shape that changed rather than as a fact.

    Not a 404 either: the configuration exists. `provisioner._ensure_memory` swallows its own
    `ClientError` and returns `""`, so a repository whose `CreateMemory` failed still reaches
    `READY` with no memory, and every configuration registered before memory existed carries
    no key at all -- neither is an error. And no AgentCore call, which is not merely an
    optimisation: an empty `memoryId` is a `ParamValidationError` inside botocore, i.e. a 500
    on a repository that is only new.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common)  # no memoryId
    fake = _memory_client(monkeypatch, common)

    response = configurations.memory(
        _for(_CONFIG_ID, "GET", "/configurations/{configId}/memory"), None
    )
    assert response["statusCode"] == 200
    body = _body(response)
    assert body["items"] == []
    assert body["memoryId"] is None
    assert "memoryId" in body, "null, not omitted -- see the docstring"
    assert body["namespace"] == _EPISODIC, (
        "the namespace it would have read, so a client can say so"
    )
    assert fake.calls == [], (
        "an empty `memoryId` is a `ParamValidationError` in botocore, and a repository "
        "registered before memory existed is not an error to report as a 500"
    )


def test_an_unknown_config_id_is_a_404_from_the_memory_route(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The module's usual 404, because the route loads the item first -- and it never asks
    AgentCore about a repository this API does not know."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    fake = _memory_client(monkeypatch, common)

    response = configurations.memory(
        _for("cfg_nope", "GET", "/configurations/{configId}/memory"), None
    )
    assert response["statusCode"] == 404
    assert _body(response)["error"] == "NotFound"
    assert fake.calls == []


def test_a_record_with_no_content_renders_as_empty_text(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defensive-read contract: the response shape is AWS's, not this API's.

    A summary with no `content` is what the service is free to hand back, and a `KeyError`
    there would be a 500 on the one route whose whole job is to show what is present.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common, memoryId=_MEMORY_ID)
    fake = _memory_client(
        monkeypatch,
        common,
        responses={
            "ListMemoryRecords": {
                "memoryRecordSummaries": [{"memoryRecordId": "mem_rec_0123456789"}]
            }
        },
    )

    response = configurations.memory(
        _for(_CONFIG_ID, "GET", "/configurations/{configId}/memory"), None
    )
    assert response["statusCode"] == 200
    assert _body(response)["items"] == [{"recordId": "mem_rec_0123456789", "text": ""}]
    assert fake.operations() == ["ListMemoryRecords"]


def test_patch_refuses_a_field_outside_the_allow_list_and_says_where_to_go(aws: Any) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common)
    response = configurations.patch(
        _for(_CONFIG_ID, "PATCH", "/configurations/{configId}", body={"agentRuntimeArn": "arn:x"}),
        None,
    )
    assert response["statusCode"] == 400

    body = _body(response)
    assert body["error"] == "NotEditable"
    assert body["field"] == "agentRuntimeArn"
    assert body["editable"] == list(configurations.EDITABLE_FIELDS)
    # Both sentences have to reach the client: a bare "unknown field" is what makes
    # someone try `repositoryUrl` twice and then look for a `githubToken` field.
    assert "re-register" in body["message"]
    assert "github-token" in body["message"]


def test_repository_url_is_not_patchable(aws: Any) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common)
    response = configurations.patch(
        _for(
            _CONFIG_ID,
            "PATCH",
            "/configurations/{configId}",
            body={"repositoryUrl": "https://github.com/someone/else"},
        ),
        None,
    )
    assert response["statusCode"] == 400
    assert _body(response)["field"] == "repositoryUrl"
    assert "re-register" in _body(response)["message"]
    stored = common.config_table.get_item(Key={"configId": _CONFIG_ID})["Item"]
    assert stored["repositoryUrl"] == _URL


def test_the_memory_id_is_not_patchable(aws: Any) -> None:
    """The provisioner stamps it; a client may only read it."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common, memoryId="rca_owner_repo_mem-AbCdEf")
    assert "memoryId" not in configurations.EDITABLE_FIELDS

    response = configurations.patch(
        _for(_CONFIG_ID, "PATCH", "/configurations/{configId}", body={"memoryId": "forged"}),
        None,
    )
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "NotEditable"
    assert _body(response)["field"] == "memoryId"
    stored = common.config_table.get_item(Key={"configId": _CONFIG_ID})["Item"]
    assert stored["memoryId"] == "rca_owner_repo_mem-AbCdEf"


def test_patch_null_clears_a_field_back_to_detection(aws: Any) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common)
    response = configurations.patch(
        _for(_CONFIG_ID, "PATCH", "/configurations/{configId}", body={"verifyCommand": None}),
        None,
    )
    assert response["statusCode"] == 200
    assert "verifyCommand" not in _body(response)
    stored = common.config_table.get_item(Key={"configId": _CONFIG_ID})["Item"]
    assert "verifyCommand" not in stored, "a cleared field is REMOVEd, not stored as ''"


def test_patch_sets_and_clears_in_one_request(aws: Any) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common)
    response = configurations.patch(
        _for(
            _CONFIG_ID,
            "PATCH",
            "/configurations/{configId}",
            body={"verifyCommand": None, "setupCommand": "make setup"},
        ),
        None,
    )
    assert response["statusCode"] == 200
    stored = common.config_table.get_item(Key={"configId": _CONFIG_ID})["Item"]
    assert "verifyCommand" not in stored
    assert stored["setupCommand"] == "make setup"
    assert stored["updatedAt"] != "2026-01-01T00:00:00.000Z"


def test_an_empty_patch_is_a_400_rather_than_a_no_op_write(aws: Any) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common)
    response = configurations.patch(
        _for(_CONFIG_ID, "PATCH", "/configurations/{configId}", body={}), None
    )
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "EmptyPatch"
    stored = common.config_table.get_item(Key={"configId": _CONFIG_ID})["Item"]
    assert stored["updatedAt"] == "2026-01-01T00:00:00.000Z"


def test_patch_of_a_missing_configuration_is_a_404_not_a_resurrection(aws: Any) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    response = configurations.patch(
        _for("cfg_nope", "PATCH", "/configurations/{configId}", body={"defaultBranch": "main"}),
        None,
    )
    assert response["statusCode"] == 404
    assert common.config_table.get_item(Key={"configId": "cfg_nope"}).get("Item") is None


def test_delete_marks_deleting_and_queues_the_teardown(aws: Any) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common)
    response = configurations.delete(_for(_CONFIG_ID, "DELETE", "/configurations/{configId}"), None)

    assert response["statusCode"] == 202
    assert _body(response)["status"] == "DELETING"
    # The item is still there: the provisioner removes it after the runtime, role and
    # secret are gone (invariant 22).
    stored = common.config_table.get_item(Key={"configId": _CONFIG_ID})["Item"]
    assert stored["status"] == "DELETING"
    assert _provision_messages(aws) == [{"phase": "DELETE", "configId": _CONFIG_ID, "attempt": 0}]


def test_delete_of_a_missing_configuration_is_a_404(aws: Any) -> None:
    _common, _provisioner, configurations = _load("provisioner", "configurations")
    response = configurations.delete(_for("cfg_nope", "DELETE", "/configurations/{configId}"), None)
    assert response["statusCode"] == 404
    assert _provision_messages(aws) == []


def test_runtime_update_requires_ready_and_queues_update(aws: Any) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common, status="CREATING")
    event = _for(_CONFIG_ID, "POST", "/configurations/{configId}/runtime-update")

    refused = configurations.runtime_update(event, None)
    assert refused["statusCode"] == 409
    assert _body(refused)["error"] == "ConfigurationNotReady"
    assert _body(refused)["status"] == "CREATING"
    assert _provision_messages(aws) == []

    _seed(common, status="READY")
    accepted = configurations.runtime_update(event, None)
    assert accepted["statusCode"] == 202
    assert _body(accepted) == {"queued": True, "phase": "UPDATE"}
    assert _provision_messages(aws) == [{"phase": "UPDATE", "configId": _CONFIG_ID, "attempt": 0}]
    # No status write: the provisioner sets `UPDATING` only if an update was needed, so a
    # converged fleet does not park every configuration in `UPDATING` forever.
    assert common.config_table.get_item(Key={"configId": _CONFIG_ID})["Item"]["status"] == "READY"


def test_rotation_updates_the_one_credential_provider(aws: Any) -> None:
    _common, _provisioner, configurations = _load("provisioner", "configurations")
    created = _body(_register(configurations))
    provider_name = f"rca_{created['configId']}"
    assert _providers()[provider_name]["apiKey"] == _TOKEN
    rotated_token = FAKE_FINE_GRAINED_REPLACEMENT_PAT

    response = configurations.rotate_token(
        _for(
            created["configId"],
            "PUT",
            "/configurations/{configId}/github-token",
            body={"githubToken": rotated_token},
        ),
        None,
    )
    assert response["statusCode"] == 200
    body = _body(response)
    assert body["rotated"] is True
    assert rotated_token not in response["body"], "the credential body is never reflected back"

    assert _providers()[provider_name]["apiKey"] == rotated_token
    # One provider, updated in place. A second provider would not be reachable by the
    # execution role at all -- its inline policy names this literal provider ARN. It is also
    # the only way to write this credential: the secret Identity keeps underneath is
    # service-linked, and `secretsmanager:PutSecretValue` on it is refused.
    assert list(_providers()) == [provider_name]
    assert _IDENTITY[-1].calls_to("UpdateApiKeyCredentialProvider") == [
        {"name": provider_name, "apiKey": rotated_token}
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"githubToken": _TOKEN, "verifyCommand": "make test"},
        {"githubToken": _TOKEN, "note": "rotating after a leak"},
        {"token": _TOKEN},
    ],
)
def test_rotation_refuses_a_body_carrying_any_other_key(aws: Any, body: dict[str, Any]) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common)
    response = configurations.rotate_token(
        _for(_CONFIG_ID, "PUT", "/configurations/{configId}/github-token", body=body), None
    )
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "CredentialBodyOnly"
    assert "PATCH /configurations/" in _body(response)["message"]


def test_rotation_refuses_a_token_that_expires_in_an_hour(aws: Any) -> None:
    """Rotation runs the same shape check as registration, so it cannot widen what
    registration narrowed -- nor accept a shape registration would not.

    A classic PAT is now accepted on both routes; what stays refused is the App/OAuth family,
    because nothing here refreshes an hour-long token and an accepted one would fail inside a
    container long after the rotation looked successful.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _seed(common)
    response = configurations.rotate_token(
        _for(
            _CONFIG_ID,
            "PUT",
            "/configurations/{configId}/github-token",
            body={"githubToken": FAKE_APP_INSTALLATION_PAT},
        ),
        None,
    )
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "ExpiringTokenRejected"


def test_rotation_refuses_a_token_that_cannot_push_and_keeps_the_old_credential(
    aws: Any, github: _FakeGitHub
) -> None:
    """Rotation is the other route that writes a credential, and it refuses before the
    write: the operator rotating *because* a token was wrong must not be left with a
    second one that cannot push either."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    provider_name = f"rca_{_CONFIG_ID}"
    _IDENTITY[-1].credential_providers[provider_name] = {"name": provider_name, "apiKey": _TOKEN}
    _seed(common)
    github.create_ref = (
        403,
        {"message": "Resource not accessible by personal access token"},
        {"x-accepted-github-permissions": "contents=write"},
    )

    response = configurations.rotate_token(
        _for(
            _CONFIG_ID,
            "PUT",
            "/configurations/{configId}/github-token",
            body={"githubToken": "github_pat_22ZYXWVUTSRQPONMLKJI9876543210"},
        ),
        None,
    )
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "TokenCannotPush"
    assert "Resource not accessible by personal access token" in _body(response)["message"]
    assert _providers()[provider_name]["apiKey"] == _TOKEN


def test_the_provisioner_writes_only_declared_statuses(aws: Any) -> None:
    """`provisioner.py` cannot import `CONFIG_STATUSES` -- the import runs the other way
    -- so the five statuses it spells are pinned against the published vocabulary here."""
    _common, provisioner, configurations = _load("provisioner", "configurations")
    written = {
        provisioner.CREATING,
        provisioner.READY,
        provisioner.UPDATING,
        provisioner.FAILED,
        provisioner.DELETING,
    }
    assert written <= set(configurations.CONFIG_STATUSES)
    assert "PENDING" in configurations.CONFIG_STATUSES, (
        "the status an item carries before provisioning"
    )


def test_every_route_names_a_function_this_module_defines(aws: Any) -> None:
    _common, _provisioner, configurations = _load("provisioner", "configurations")
    for method, path, function_name in configurations.ROUTES:
        assert method in {"GET", "POST", "PATCH", "PUT", "DELETE"}
        assert path.startswith("/configurations")
        assert getattr(configurations, function_name).__api_handler__ is True
