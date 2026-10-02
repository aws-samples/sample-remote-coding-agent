"""`/configurations`: registration, the closed patch allow-list, and authorisation.

Registration writes **no credential at all** -- one GitHub App serves the whole deployment
and AgentCore Identity's token vault holds its credentials -- so what is pinned here is the
shape that replaced it: that a `githubToken` is refused rather than quietly ignored, that
registering against a deployment with no GitHub App is refused before anything is written,
and that `github_status` reaches a verdict by *writing* to the repository rather than
reading from it. The read-is-not-evidence lesson carried over intact and got worse: with a
GitHub App, `GET /repos/{owner}/{repo}` reports `push=True` for a public repository the app
was never installed on, because that field describes the **user's** rights and not the
token's.

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

import pytest
from botocore.exceptions import ClientError
from conftest import FAKE_FINE_GRAINED_PAT, api_event
from test_api_fakes import FakeAgentCore, client_error, install
from test_api_fakes import load_handlers as _load

_URL = "https://github.com/owner/repo"
# Imported and asserted in exactly one place: `create` refuses a body carrying a token, so
# the suite needs a value shaped like the thing an operator would try to paste.
_REFUSED_PAT = FAKE_FINE_GRAINED_PAT
_CONFIG_ID = "cfg_0123456789ab"
# What the vault hands back for `_CONFIG_ID`. `FakeAgentCore` answers
# `GetResourceOauth2Token` with `ghu_<userId>`, and `ghu_` is the real prefix of a GitHub
# App's user-to-server token -- which is what the 3LO leg issues.
_VAULT_TOKEN = f"ghu_{_CONFIG_ID}"
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
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:00.000Z",
    }
    item.update(overrides)
    common.config_table.put_item(Item=item)
    return item


def _provision_messages(aws: Any) -> list[dict[str, Any]]:
    received = aws.sqs.receive_message(QueueUrl=aws.provision_queue_url, MaxNumberOfMessages=10)
    return [json.loads(message["Body"]) for message in received.get("Messages", [])]


def _connect(common: Any, *, slug: str = "rca-coding-agent-abc123") -> None:
    """Make `github_app.is_connected()` true, without going through the connect flow.

    The sentinel item is the whole of "a GitHub App is connected", so seeding it is both the
    cheapest setup and an assertion in itself: if `is_connected` ever starts reading
    something else, every registration test in this file fails at once rather than one
    carefully-written test failing alone.
    """
    # `clientSecretArn` too, because `convert` stores it: provisioning names it in every
    # execution role, and a settings item without it sends `provider_secret_arn` to the live
    # provider -- which moto does not implement.
    common.config_table.put_item(
        Item={
            "configId": "rca:settings",
            "appSlug": slug,
            "appId": "5143354",
            "clientSecretArn": "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:bedrock-agentcore-identity!default/oauth2/rca_github-fdf929fb-0JG5Oo",
        }
    )


def configurations_connect(common: Any) -> dict[str, Any]:
    """`POST /github-app/conversions`, the leg that adopts a freshly created app."""
    from handlers import github_app

    return github_app.convert(
        api_event("POST", "/github-app/conversions", body={"code": "manifest-code"}), None
    )


def _register(configurations: Any, *, url: str = _URL, **extra: Any) -> dict[str, Any]:
    body = {"repositoryUrl": url, **extra}
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
    _connect(common)
    response = _register(configurations, verifyCommand="make test")
    assert response["statusCode"] == 201

    body = _body(response)
    assert body["repository"] == "owner/repo"
    assert body["status"] == "PENDING"
    assert body["verifyCommand"] == "make test"
    assert body["configId"].startswith("cfg_")
    # No `secretArn`, and the absence is the assertion: registration creates no credential
    # of any kind. `_PUBLIC_FIELDS` is a closed allow-list, so a key reappearing here would
    # mean something started storing one again.
    assert "secretArn" not in body

    stored = common.config_table.get_item(Key={"configId": body["configId"]})["Item"]
    assert stored["repositoryUrl"] == _URL
    assert _provision_messages(aws) == [
        {"phase": "CREATE", "configId": body["configId"], "attempt": 0}
    ]


def test_an_absent_optional_command_stays_absent_rather_than_empty(aws: Any) -> None:
    """The three editable fields fall back to per-run detection when they are absent; a
    stored empty string is a declared "run nothing" and means something else."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    body = _body(_register(configurations))
    stored = common.config_table.get_item(Key={"configId": body["configId"]})["Item"]
    assert "verifyCommand" not in stored
    assert "setupCommand" not in stored


@pytest.mark.parametrize(
    "url", ["https://github.com/owner/repo.git", "git@github.com:owner/repo.git"]
)
def test_both_clone_url_forms_resolve_to_the_owner_repo_slug(aws: Any, url: str) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
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


def test_a_body_carrying_a_token_is_refused_rather_than_ignored(aws: Any) -> None:
    """Dropping it silently is the failure this prevents.

    An operator -- or a script, or an older copy of the MCP plugin -- still sending a PAT has
    to be told it is not used. Ignored, they would believe a credential was stored and then be
    unable to explain the `GitHubNotAuthorized` on their first task. The message names the
    route that *is* the credential step.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    response = _register(configurations, githubToken=_REFUSED_PAT)
    assert response["statusCode"] == 400

    body = _body(response)
    assert body["error"] == "TokenNotAccepted"
    assert "github-authorize" in body["message"]
    # Nothing was written on the way to the refusal, and the token is not echoed back.
    assert [item["configId"] for item in common.config_table.scan()["Items"]] == ["rca:settings"]
    assert _REFUSED_PAT not in response["body"]


def test_connecting_derives_the_apps_bot_account_as_the_commit_identity(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo path configures no git author, so connecting has to produce one.

    The address is `<botUserId>+<slug>[bot]@users.noreply.github.com`, and the **id is the bot
    user's, not the app's**. That distinction is the whole test: both forms are well-formed,
    both look like the documented convention, and only one resolves. Measured against a live
    app -- the app-id form came back with `author: null`, the bot-user-id form resolved to
    `<slug>[bot] (Bot)`, and a control commit with a human's own noreply address resolved to
    that user, so the negative was real rather than an artefact of the probe.

    Getting it wrong is silent in the worst way: the commits look attributed in the pull
    request's own metadata and GitHub reports no author, so a Vercel preview answers
    "Deployment was blocked" and a DCO check fails with a perfect diff (invariant 4b).
    """
    common, _provisioner = _load("provisioner")
    from handlers import github_probe

    app_id, bot_user_id, slug = "5146691", "336429684", "rca-coding-agent-298bc4"

    def fake(method: str, url: str, **_kwargs: Any) -> tuple[int, Any, dict[str, str]]:
        if url.endswith("/conversions"):
            return (
                201,
                {
                    "id": app_id,
                    "slug": slug,
                    "client_id": "Iv23livTEST",
                    "client_secret": "shhh",
                    "html_url": f"https://github.com/apps/{slug}",
                    "owner": {"login": "lazydragon"},
                },
                {},
            )
        # The bot-account lookup. Its `id` is deliberately NOT the app id above.
        assert url.endswith(f"/users/{slug}[bot]"), url
        return 200, {"id": bot_user_id, "login": f"{slug}[bot]", "type": "Bot"}, {}

    monkeypatch.setattr(github_probe, "_request", fake)
    install(monkeypatch, common, control=FakeAgentCore("bedrock-agentcore-control"))

    response = configurations_connect(common)
    assert response["statusCode"] == 200, _body(response)

    stored = common.config_table.get_item(Key={"configId": "rca:settings"})["Item"]
    assert stored["gitAuthorName"] == f"{slug}[bot]"
    assert stored["gitAuthorEmail"] == f"{bot_user_id}+{slug}[bot]@users.noreply.github.com"
    assert app_id not in stored["gitAuthorEmail"], (
        "the address must carry the BOT USER id, not the app id -- the app-id form is "
        "well-formed and resolves to no GitHub account at all"
    )


def test_an_unreadable_bot_account_costs_the_identity_and_not_the_connect(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed attribution lookup must not fail the connect.

    The app exists on GitHub by this point and its credentials are already in the vault, so
    refusing here would leave the operator with an app this deployment cannot finish adopting
    -- trading a cosmetic problem (commits resolve to no author) for a blocking one.
    """
    common, _provisioner = _load("provisioner")
    from handlers import github_probe

    def fake(method: str, url: str, **_kwargs: Any) -> tuple[int, Any, dict[str, str]]:
        if url.endswith("/conversions"):
            return (
                201,
                {
                    "id": "5146691",
                    "slug": "rca-app",
                    "client_id": "Iv23livTEST",
                    "client_secret": "shhh",
                },
                {},
            )
        return 404, {"message": "Not Found"}, {}

    monkeypatch.setattr(github_probe, "_request", fake)
    install(monkeypatch, common, control=FakeAgentCore("bedrock-agentcore-control"))

    assert configurations_connect(common)["statusCode"] == 200
    stored = common.config_table.get_item(Key={"configId": "rca:settings"})["Item"]
    assert stored["appSlug"] == "rca-app", "the app was still adopted"
    assert stored["gitAuthorName"] == ""
    assert stored["gitAuthorEmail"] == ""


def test_a_deployment_connected_before_the_identity_existed_backfills_itself(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings item with a slug and no identity closes its own gap, once.

    This is the state every deployment connected before the identity was derived is in, and
    the reason it self-heals rather than being an operator task: the only other recovery is to
    disconnect and connect again, which creates a **second** GitHub App and orphans the first.
    Leaving litter in someone's GitHub account to repair a field is the wrong trade when the
    slug already present is enough to derive the identity.

    The write is asserted as well as the return value, because the point is that it happens
    once -- a derivation that answered correctly and stored nothing would ask GitHub on every
    provision for a value that cannot change.
    """
    common, _provisioner = _load("provisioner")
    from handlers import github_app, github_probe

    slug, bot_user_id = "rca-coding-agent-298bc4", "336429684"
    common.config_table.put_item(Item={"configId": "rca:settings", "appSlug": slug})

    calls: list[str] = []

    def fake(method: str, url: str, **_kwargs: Any) -> tuple[int, Any, dict[str, str]]:
        calls.append(url)
        return 200, {"id": bot_user_id, "login": f"{slug}[bot]", "type": "Bot"}, {}

    monkeypatch.setattr(github_probe, "_request", fake)

    name, email = github_app.commit_identity()
    assert name == f"{slug}[bot]"
    assert email == f"{bot_user_id}+{slug}[bot]@users.noreply.github.com"
    stored = common.config_table.get_item(Key={"configId": "rca:settings"})["Item"]
    assert stored["gitAuthorEmail"] == email, "the backfill has to persist, or it repeats"

    # Second call reads the stored pair and asks GitHub nothing.
    assert github_app.commit_identity() == (name, email)
    assert len(calls) == 1, f"the identity was re-derived instead of read back: {calls}"


def test_a_failed_bot_lookup_is_not_cached_as_an_empty_identity(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storing `("", "")` would be indistinguishable from a successful derivation of nothing,
    and would stop the backfill ever retrying -- so a failure writes nothing and the next
    provision tries again."""
    common, _provisioner = _load("provisioner")
    from handlers import github_app, github_probe

    common.config_table.put_item(Item={"configId": "rca:settings", "appSlug": "rca-app"})

    def not_found(*_args: Any, **_kwargs: Any) -> tuple[int, Any, dict[str, str]]:
        return 404, {"message": "Not Found"}, {}

    monkeypatch.setattr(github_probe, "_request", not_found)

    assert github_app.commit_identity() == ("", "")
    stored = common.config_table.get_item(Key={"configId": "rca:settings"})["Item"]
    assert "gitAuthorName" not in stored
    assert "gitAuthorEmail" not in stored


def test_the_secret_arn_is_backfilled_once_and_a_failed_read_does_not_block_provisioning(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment connected before the ARN was stored learns it from the live provider, once.

    That backfill runs inside every provision, so its failure mode matters more than its
    success: a raise would fail runtime creates and updates over a grant that only matters
    eight hours later. So a failed read is `""` and stores nothing, and the next provision
    tries again.
    """
    common, _provisioner = _load("provisioner")
    from handlers import github_app

    common.config_table.put_item(Item={"configId": "rca:settings", "appSlug": "rca-app"})
    control = FakeAgentCore("bedrock-agentcore-control")
    control.providers[github_app.PROVIDER_NAME] = {
        "name": github_app.PROVIDER_NAME,
        "clientSecretArn": {"secretArn": "arn:aws:secretsmanager:ap-southeast-1:1:secret:x-Ab"},
    }
    install(monkeypatch, common, control=control)

    assert github_app.provider_secret_arn() == "arn:aws:secretsmanager:ap-southeast-1:1:secret:x-Ab"
    assert github_app.provider_secret_arn() == "arn:aws:secretsmanager:ap-southeast-1:1:secret:x-Ab"
    assert len(control.calls_to("GetOauth2CredentialProvider")) == 1, "backfilled once, then read"

    # A provider read that fails must not raise into the provision that called it.
    common.config_table.put_item(Item={"configId": "rca:settings", "appSlug": "rca-app"})
    control.errors["GetOauth2CredentialProvider"] = [
        client_error("AccessDeniedException", "no", operation="GetOauth2CredentialProvider")
    ]
    assert github_app.provider_secret_arn() == ""
    stored = common.config_table.get_item(Key={"configId": "rca:settings"})["Item"]
    assert "clientSecretArn" not in stored, "a failure stores nothing, so the next one retries"


def test_registering_without_a_github_app_is_refused_before_anything_is_written(
    aws: Any,
) -> None:
    """A repository registered against a deployment with no GitHub App can never be
    authorised, so it would provision a runtime for ten minutes and then be useless. Refused
    where the operator is, naming the step they have not done."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    response = _register(configurations)
    assert response["statusCode"] == 409

    body = _body(response)
    assert body["error"] == "GitHubAppNotConnected"
    assert "connect one first" in body["message"]
    assert common.config_table.scan()["Items"] == []
    assert _provision_messages(aws) == []


def test_registration_asks_github_for_nothing_at_all(aws: Any, github: _FakeGitHub) -> None:
    """Registration used to spend three round trips on GitHub proving a token could push.

    With no token to prove anything about, it spends none -- which is the point: the check
    moved to `github_status`, where it can be re-run after the operator installs the app,
    instead of being a one-shot gate at the moment they have the least information.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    assert _register(configurations)["statusCode"] == 201
    assert github.calls == []


def test_an_unauthorised_repository_reports_itself_unauthorised(aws: Any) -> None:
    """The vault answers an unauthorised `userId` with an authorization URL and no token, and
    that is a *state* rather than a fault -- so the route is a 200 describing it, not a 4xx.

    `canPush` is `None` and not `False`: nothing was asked of GitHub, so there is no verdict
    to report, and a `False` here would send the operator to the install page for a problem
    they do not have.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    _seed(common)
    # An empty authorised set: this configuration is the one the vault does not know.
    data = FakeAgentCore("bedrock-agentcore")
    data.authorized = set()
    common.agentcore_data = lambda: data

    response = configurations.github_status(_for(_CONFIG_ID, "GET", "/github-status"), None)
    assert response["statusCode"] == 200
    body = _body(response)
    assert body["authorized"] is False
    assert body["canPush"] is None
    assert "not been authorised" in body["reason"]


def test_a_validation_error_about_anything_else_is_not_reported_as_unauthorised(
    aws: Any,
) -> None:
    """The fence on the translation, and the reason it is two conditions rather than one.

    `resolve_token` turns "you must provide a ResourceOauth2ReturnUrl" into "not authorised",
    because that is what the service says when it has no token and was not offered a way to
    ask for one. But a **wrong provider name** arrives as the same error *code*, and reporting
    that as an unauthorised repository would leave a misconfigured deployment looking like a
    repository nobody had got round to authorising -- forever, with nothing to find.

    So the message has to name the return URL. Anything else is re-raised and becomes a 500
    carrying the vendor's own words, which is the safe direction to fail in: loud and
    attributable rather than quiet and wrong.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    _seed(common)
    data = FakeAgentCore(
        "bedrock-agentcore",
        errors={
            "GetResourceOauth2Token": [
                client_error(
                    "ValidationException",
                    "No credential provider found with name rca_github",
                    operation="GetResourceOauth2Token",
                )
            ]
        },
    )
    common.agentcore_data = lambda: data

    response = configurations.github_status(_for(_CONFIG_ID, "GET", "/github-status"), None)
    assert response["statusCode"] == 500, (
        "an unrecognised ValidationException must not be flattened into `authorized: false`"
    )
    assert "authorized" not in _body(response)


def test_an_authorised_repository_is_proved_by_a_write_and_not_a_read(
    aws: Any, github: _FakeGitHub
) -> None:
    """The read-is-not-evidence lesson, carried over and made worse by the GitHub App.

    `GET /repos/{owner}/{repo}` reports `push=True` for a public repository the app was never
    installed on, because that field is the **user's** rights and not the token's -- measured
    against a live app. So the verdict is still a throwaway ref created and deleted, and the
    three calls are asserted in order.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    _seed(common)

    response = configurations.github_status(_for(_CONFIG_ID, "GET", "/github-status"), None)
    assert response["statusCode"] == 200
    body = _body(response)
    assert body["authorized"] is True
    assert body["canPush"] is True
    # No install link on the happy path: one beside a working repository invites an operator
    # to change something that is already right.
    assert body["installUrl"] is None

    assert [call["method"] for call in github.calls] == ["GET", "POST", "DELETE"]
    created = github.calls[1]["payload"]
    assert created["ref"].startswith("refs/heads/rca-write-probe-")
    assert github.calls[2]["url"].endswith(f"heads/{created['ref'].removeprefix('refs/heads/')}")


def test_an_authorised_app_that_is_not_installed_here_says_so_and_links_the_fix(
    aws: Any, github: _FakeGitHub
) -> None:
    """The single most likely mistake in the whole setup: authorised account-wide, not
    installed on this repository. GitHub's own words for it are "Resource not accessible by
    integration", and the fix is on GitHub -- so the response carries both the sentence and
    the install URL."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common, slug="rca-coding-agent-89c770")
    _seed(common)
    github.create_ref = (
        403,
        {"message": "Resource not accessible by integration"},
        {"x-accepted-github-permissions": "contents=write"},
    )

    body = _body(configurations.github_status(_for(_CONFIG_ID, "GET", "/github-status"), None))
    # Authorised *and* unable to write: two different facts, and collapsing them into one
    # boolean is what would send the operator to re-authorise, which would not help.
    assert body["authorized"] is True
    assert body["canPush"] is False
    assert "Resource not accessible by integration" in body["reason"]
    assert body["acceptedPermissions"] == "contents=write"
    assert body["installUrl"] == (
        "https://github.com/apps/rca-coding-agent-89c770/installations/new"
    )


def test_an_unreachable_github_is_not_a_verdict(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A GitHub incident must not make a correctly configured repository look broken, so
    `canPush` is three-valued and `None` means the check did not run."""
    from handlers import github_probe

    def unreachable(*_args: Any, **_kwargs: Any) -> None:
        raise github_probe.ProbeUnavailable("Name or service not known")

    monkeypatch.setattr(github_probe, "_request", unreachable)
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    _seed(common)

    body = _body(configurations.github_status(_for(_CONFIG_ID, "GET", "/github-status"), None))
    assert body["authorized"] is True
    assert body["canPush"] is None
    assert "Name or service not known" in body["reason"]


def test_the_status_route_returns_no_part_of_the_token(
    aws: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """It holds a live GitHub token in memory for the duration of the probe. Not a prefix and
    not a length reaches the response or a log line."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    caplog.set_level(logging.DEBUG, logger="handlers")
    _connect(common)
    _seed(common)

    response = configurations.github_status(_for(_CONFIG_ID, "GET", "/github-status"), None)
    assert _VAULT_TOKEN not in response["body"]
    assert _VAULT_TOKEN not in "\n".join(record.getMessage() for record in caplog.records)
    stored = common.config_table.get_item(Key={"configId": _CONFIG_ID})["Item"]
    assert _VAULT_TOKEN not in json.dumps(stored, default=str)


def test_the_probe_never_puts_the_token_in_a_url(aws: Any, github: _FakeGitHub) -> None:
    """The credential travels in the header argument and nowhere else: a query string is
    logged verbatim by every proxy between this Lambda and the API."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    _seed(common)
    configurations.github_status(_for(_CONFIG_ID, "GET", "/github-status"), None)
    assert github.calls, "the probe ran"
    for call in github.calls:
        assert _VAULT_TOKEN not in call["url"]
        assert call["token"] == _VAULT_TOKEN


def test_authorize_returns_the_url_to_open_and_registers_the_console_origin(aws: Any) -> None:
    """The start of the 3LO leg. The console's own origin is what AgentCore returns the
    browser to, and it has to be registered on the workload identity first -- an
    unregistered one is refused by the service."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    _seed(common)
    data = FakeAgentCore("bedrock-agentcore")
    data.authorized = set()
    common.agentcore_data = lambda: data

    response = configurations.github_authorize(
        _for(
            _CONFIG_ID, "POST", "/github-authorize", body={"consoleOrigin": "https://console.test"}
        ),
        None,
    )
    assert response["statusCode"] == 200
    body = _body(response)
    assert body["authorized"] is False
    assert body["authorizationUrl"].startswith("https://github.com/login/oauth/authorize")
    # The return URL really was sent, and it is the console's own origin rather than anything
    # this deployment hard-codes.
    assert data.last("GetResourceOauth2Token")["resourceOauth2ReturnUrl"] == "https://console.test"
    # And the `userId` is the configuration id -- the keying rule the whole design rests on.
    assert data.last("GetWorkloadAccessTokenForUserId")["userId"] == _CONFIG_ID


def test_authorize_refuses_a_console_origin_that_is_not_https(aws: Any) -> None:
    """The origin is request-supplied and ends up registered as a permitted OAuth return
    target, so it is checked rather than trusted."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    _seed(common)
    response = configurations.github_authorize(
        _for(
            _CONFIG_ID,
            "POST",
            "/github-authorize",
            body={"consoleOrigin": "http://evil.example.com"},
        ),
        None,
    )
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "InsecureConsoleOrigin"


def test_authorize_says_so_when_the_repository_is_already_authorised(aws: Any) -> None:
    """The vault answers an already-authorised id with a token and no URL, so this route has
    to report the state rather than hand back an empty URL for the console to open."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    _seed(common)
    response = configurations.github_authorize(
        _for(
            _CONFIG_ID,
            "POST",
            "/github-authorize",
            body={"consoleOrigin": "https://console.test"},
        ),
        None,
    )
    body = _body(response)
    assert body["authorized"] is True
    assert "authorizationUrl" not in body


def test_a_run_is_refused_while_the_repository_has_no_authorisation(aws: Any) -> None:
    """The gate that saves a whole task. A repository can be perfectly `READY` and still have
    no credential, and without this the run clones, plans, codes, verifies and then dies at
    `submit` having spent a full task's tokens.

    In `runs.queue_run` rather than in `tasks.create`, because that is the one definition of
    "queue a run" -- so a card dragged into Planning is refused for the same reason.
    """
    common, _provisioner, runs = _load("provisioner", "runs")
    _connect(common)
    config = _seed(common)
    data = FakeAgentCore("bedrock-agentcore")
    data.authorized = set()
    common.agentcore_data = lambda: data

    with pytest.raises(common.ApiError) as raised:
        runs.queue_run(config, prompt="add a readme", source="test")
    assert raised.value.status == 409
    assert raised.value.code == "GitHubNotAuthorized"
    assert "Authorise GitHub" in raised.value.message
    # Nothing was queued and no task item was written.
    assert common.tasks_table.scan()["Items"] == []


def test_registering_the_same_repository_twice_is_a_conflict(aws: Any) -> None:
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    first = _body(_register(configurations))
    response = _register(configurations)
    assert response["statusCode"] == 409
    body = _body(response)
    assert body["error"] == "RepositoryAlreadyRegistered"
    assert body["configId"] == first["configId"]
    # The MCP client shows only the message, so the id has to be in it, and the next step
    # has to be one an operator can take -- not "patch or delete", which no tool can send.
    assert first["configId"] in body["message"]
    assert "console" in body["message"] and "patch or delete" not in body["message"]
    # The second attempt wrote nothing at all: the sentinel item plus the one configuration.
    assert len(common.config_table.scan()["Items"]) == 2


def test_a_repository_being_torn_down_can_be_registered_again(aws: Any) -> None:
    """Teardown is asynchronous, so a `DELETING` configuration must not make the
    repository look permanently taken."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    _seed(common, status="DELETING")
    assert _register(configurations)["statusCode"] == 201


def test_a_failed_registration_can_be_registered_again(aws: Any) -> None:
    """A `FAILED` configuration used to hold its repository name forever, and nothing the
    plugin can send clears it -- `ApiClient.ALLOWED_METHODS` is `{"GET", "POST"}`. So the
    registration succeeds and the stale item goes down the ordinary teardown path."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    _seed(
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
    # Sorted: SQS does not promise order, and this is the first request that sends two.
    assert sorted(_provision_messages(aws), key=lambda message: message["phase"]) == [
        {"phase": "CREATE", "configId": body["configId"], "attempt": 0},
        {"phase": "DELETE", "configId": _CONFIG_ID, "attempt": 0},
    ]


def test_a_replacement_registration_needs_its_own_authorisation(aws: Any) -> None:
    """`configId` is random and it is the 3LO `userId`, so a replacement does not inherit the
    retired configuration's authorisation -- it has one fewer step done than the operator
    might assume, and the status route is what says so.

    This is the per-repository half of the keying rule: the credential's lifetime is exactly
    the registration's, which is also what makes deregistering a repository end its access
    without anything having to be revoked.
    """
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
    _seed(common, status="FAILED")
    data = FakeAgentCore("bedrock-agentcore")
    # Only the retired configuration is authorised.
    data.authorized = {_CONFIG_ID}
    common.agentcore_data = lambda: data

    body = _body(_register(configurations))
    assert body["configId"] != _CONFIG_ID
    fresh = _body(
        configurations.github_status(_for(body["configId"], "GET", "/github-status"), None)
    )
    assert fresh["authorized"] is False


def test_the_provisioner_tears_the_replaced_configuration_down(aws: Any) -> None:
    """The no-orphans half, end to end: the role at `ROLE_PATH`, its inline policy and the
    item all go, and the replacement is untouched.

    There is no secret in this list any more, and nothing replaced it: the retired
    configuration's GitHub authorisation is keyed on a `configId` that will never be issued
    again, so removing the item is what makes it unreachable.
    """
    common, provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
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
    with pytest.raises(ClientError) as raised:
        common.iam.get_role(RoleName=role_name)
    assert raised.value.response["Error"]["Code"] == "NoSuchEntity"

    fresh = common.config_table.get_item(Key={"configId": body["configId"]})["Item"]
    assert fresh["status"] == "PENDING"


def test_a_repository_being_torn_down_is_not_torn_down_twice(aws: Any) -> None:
    """`DELETING` is ignored by the conflict check but must *not* be retired: its teardown
    is already in flight, and a second `DELETE` message would be a redundant replay."""
    common, _provisioner, configurations = _load("provisioner", "configurations")
    _connect(common)
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
    _connect(common)
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
    assert "github-authorize" in body["message"]


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
