"""The GitHub App and the AgentCore Identity provider behind every repository's credential.

There is no GitHub token stored anywhere in this system. One **GitHub App** is created per
deployment, one **AgentCore Identity OAuth2 credential provider** holds that app's client
secret inside AgentCore's own token vault, and each registered repository's container
retrieves a short-lived user-to-server token from that vault at the moment it needs one.
Nothing in this repository ever sees the token at rest.

The whole flow is three operator actions, and this module is the API half of each:

1. **Connect** -- `POST /github-app` mints the provider with a placeholder secret so that
   its `callbackUrl` exists, then hands the console a GitHub App *manifest* carrying that
   callback. The operator presses one button, GitHub creates the app and redirects back
   with a short-lived `code`, and `POST /github-app/conversions` exchanges that code for
   the app's real `client_id`/`client_secret` and writes them into the provider. The secret
   passes through this Lambda's memory once and is never stored by us.
2. **Install** -- the operator installs the app on the repositories they intend to
   register. `install_url` is published for that, and it is the *only* thing that bounds
   which repositories a token can reach: see `docs/security.md`.
3. **Authorise** -- `POST /configurations/{configId}/github-authorize` starts the 3LO leg
   for one repository and returns an `authorizationUrl`. Afterwards the vault answers
   `GetResourceOauth2Token` with a live token for as long as the operator leaves the
   authorisation in place, refreshing it without anyone being asked again.

**Why the callback dance is in this order.** A GitHub App needs a callback URL at creation
time, and the only correct callback is AgentCore's -- a per-provider URL that does not
exist until the provider does. That URL is assigned on `CreateOauth2CredentialProvider`
and is preserved across `UpdateOauth2CredentialProvider`, which is what makes
"create the provider empty, then fill in the credentials" safe and what makes the one-click
manifest possible at all. Creating the app first and the provider second would need the
operator to paste the callback into GitHub's settings page by hand.

**`request_oauth_on_install` is `false` in the manifest, deliberately.** With it `true`
GitHub redirects the operator into the OAuth leg the instant they install, before the
provider holds any credentials, leaving a validation error the operator cannot act on.

**The deployment settings item.** The app's slug and id are stored on one sentinel item in
the Configurations table, `configId = "rca:settings"`, which carries **no `status`
attribute** so the watchdog's configuration sweep cannot see it and is filtered out of
`GET /configurations` by id. Nothing secret is on it: the client secret lives in the vault
and the client id is public by construction.
"""

from __future__ import annotations

import os
import re
import secrets as secrets_module
from collections.abc import Mapping
from typing import Any

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from . import _common, github_probe
from ._common import LOG, ApiError, handler, parse_body, require

# The vocabulary AgentCore uses for a GitHub app, spelled once. `GithubOauth2` is the
# vendor enum and `githubOauth2ProviderConfig` the matching member of the config union --
# they are not derivable from each other, and a mismatch is a `ValidationException` that
# reads like a service problem.
PROVIDER_VENDOR = "GithubOauth2"
PROVIDER_CONFIG_MEMBER = "githubOauth2ProviderConfig"

# One provider and one workload identity per deployment, both named off `APP_NAME`.
# Deployment-wide rather than per configuration because the **callbackUrl is per
# provider**: a second provider has a second callback, and a GitHub App would have to
# carry one callback URL per registered repository. Both names match
# `[a-zA-Z][a-zA-Z0-9_]{0,47}` -- no hyphens, the same constraint `provisioner._runtime_name`
# records.
APP_NAME = os.environ.get("APP_NAME", "rca")
PROVIDER_NAME = f"{re.sub(r'[^a-zA-Z0-9]', '_', APP_NAME)}_github"
WORKLOAD_NAME = f"{PROVIDER_NAME}_wl"

# The one sentinel item id. A colon keeps it outside `_new_config_id`'s `cfg_<hex>` shape,
# so no registration can ever collide with it.
SETTINGS_ID = "rca:settings"

# The scopes the 3LO leg asks GitHub for. `repo` is the one that covers pushing a branch
# and opening a pull request on a repository the app is installed on; the app's own
# permissions (`contents: write`, `pull_requests: write`) are what actually bound it, and
# the scope cannot widen past them.
OAUTH_SCOPES = ("repo",)

# `USER_FEDERATION` is the 3LO flow -- a human authorises and the vault keeps the result.
# `M2M` is the other value and is unusable here: GitHub implements no `client_credentials`
# grant, so there is no machine-to-machine path to a GitHub token at all.
OAUTH_FLOW = "USER_FEDERATION"

# The placeholder the provider is created with so that its `callbackUrl` exists before the
# app does. Never a credential: it is replaced by the real client id on conversion, and a
# provider still carrying it cannot complete an authorisation.
_PLACEHOLDER_CLIENT_ID = "pending-github-app-creation"

# GitHub's manifest endpoints. The creation one is a browser form target and not an API
# call, so it is `github.com` rather than `api.github.com`.
_MANIFEST_FORM_URL = "https://github.com/settings/apps/new"

# The marker on the manifest's `redirect_url`, and it exists because **`?code=` is not ours
# alone**. Cognito's PKCE sign-in returns to this same origin with a `code`, and
# `web/js/auth.js::complete` runs at boot, claims it, and strips the query -- so GitHub's
# single-use manifest code was consumed and thrown away before the connect view could read
# it, and the operator was told "that sign-in did not start in this tab" instead. Two
# unrelated flows on one redirect URI need something to tell them apart, and the parameter
# belongs on the URL we hand GitHub rather than being inferred from which keys happen to be
# in sessionStorage.
#
# Spelled here and matched in `web/js/auth.js` and `web/js/app.js`;
# `tests/test_web_console.py::test_the_manifest_return_marker_is_the_handlers_spelling`
# holds the three together, because a mismatch is silent: sign-in keeps working and the app
# conversion simply never happens.
MANIFEST_RETURN_PARAM = "rca"
MANIFEST_RETURN_VALUE = "github-app"

# The query parameter AgentCore puts on `resourceOauth2ReturnUrl` when it sends the operator's
# browser back from the 3LO leg. Its value is the same `sessionUri` the authorize call handed
# out, and `complete_authorization` has to be called with it before the vault holds anything --
# see that function. Named here because the console reads it (`web/js/views/register.js`) and
# `tests/test_web_console.py` holds the two spellings together.
AUTH_RETURN_PARAM = "session_id"
_INSTALL_URL_TEMPLATE = "https://github.com/apps/{slug}/installations/new"

# The app's own permission request, and it is the tightest pair that can clone, push a
# branch and open a pull request. `metadata: read` is granted implicitly by selecting a
# repository and is spelled anyway so the manifest states the whole ask.
APP_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}

# No webhook, no events: nothing in this system consumes one, and an app that asks for a
# webhook URL it never reads is surface an operator has to reason about.
_MANIFEST_HOOK_ACTIVE = False


def require_https(url: str, field: str) -> str:
    """A browser-origin URL this deployment is willing to hand to GitHub or AgentCore.

    The console supplies its own origin rather than the API stack knowing it -- the console
    stack already depends on the API stack for `ApiUrl`, so the reverse would be a cycle.
    That makes this value *request-supplied*, and it ends up registered on the workload
    identity as a permitted OAuth return target, so it is checked rather than trusted.
    `http://localhost` is allowed because `make deploy` is not the only way this console is
    served during development.
    """
    cleaned = url.strip().rstrip("/")
    if cleaned.startswith("https://") or re.match(
        r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", cleaned
    ):
        return cleaned
    raise ApiError(
        400,
        "InsecureConsoleOrigin",
        f"{field} must be an https origin (or http://localhost for development)",
        **{field: url},
    )


def _settings() -> dict[str, Any]:
    """The deployment's GitHub App settings item, or `{}` when nothing is connected yet."""
    item = _common.config_table.get_item(Key={"configId": SETTINGS_ID}).get("Item")
    return dict(item) if item else {}


def _save_settings(**fields: Any) -> None:
    """Merge fields onto the sentinel item, creating it if it is not there.

    No `status` attribute is ever written here: `watchdog._sweep_configuration` selects on
    `status`, so an item without one is invisible to the sweep. That is the whole reason
    this is safe to keep in the Configurations table rather than in a table of its own.
    """
    names = {"#updatedAt": "updatedAt"}
    values: dict[str, Any] = {":updatedAt": _common.now_iso()}
    sets = ["#updatedAt = :updatedAt"]
    for index, (field, value) in enumerate(sorted(fields.items())):
        names[f"#f{index}"] = field
        values[f":f{index}"] = value
        sets.append(f"#f{index} = :f{index}")
    _common.config_table.update_item(
        Key={"configId": SETTINGS_ID},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _provider() -> dict[str, Any]:
    """The live provider, or `{}` when it has not been created.

    `ResourceNotFoundException` is the "not connected yet" answer and is the only error
    swallowed: anything else is a real failure and becomes a 500, because an operator on
    the connect page must not be told "not connected" when the truth is "the call failed".
    """
    try:
        return dict(_common.agentcore_control().get_oauth2_credential_provider(name=PROVIDER_NAME))
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return {}
        raise


def provider_secret_arn() -> str:
    """The ARN of the Secrets Manager secret AgentCore Identity keeps the app's client secret in.

    **Every execution role is granted `GetSecretValue` on exactly this ARN, and that grant is
    what lets a container refresh its own token.** Identity performs that Secrets Manager read
    with the caller's permissions, so a container without the grant can use a token for its
    initial lifetime but cannot renew it. The ARN is the narrowest resource that grant can
    name. It is the same for every repository, because every repository shares this
    deployment's one GitHub App; what is per repository is the token in the vault, which the
    service gives no way to fence.

    Read off the settings item, where `convert` stores it, so a provision normally costs no
    AgentCore call. A deployment connected before this existed has no stored ARN; it is read off
    the live provider once and stored, the same backfill `commit_identity` does. `""` when no
    app is connected or the provider carries no managed secret -- the caller then omits the
    statement rather than writing an Allow on an empty resource.
    """
    settings = _settings()
    stored = str(settings.get("clientSecretArn") or "")
    if stored:
        return stored
    if not settings.get("appSlug"):
        return ""
    try:
        provider = _provider()
    except ClientError as err:
        # A warning and `""`, never a raise: this runs inside every provision, and failing a
        # runtime create or update because a *refresh* grant could not be named trades an
        # eight-hour-later problem for an immediate one. The role then carries no
        # `RefreshGitHubCredential`, which is the failure the warning names, and the next
        # provision retries -- nothing empty is stored.
        LOG.warning(
            "could not read the credential provider's secret ARN (%s); execution roles will "
            "not be able to refresh a GitHub token older than eight hours",
            err.response.get("Error", {}).get("Code"),
        )
        return ""
    arn = str((provider.get("clientSecretArn") or {}).get("secretArn") or "")
    if arn:
        _save_settings(clientSecretArn=arn)
    return arn


def _ensure_provider() -> dict[str, Any]:
    """The provider, created with a placeholder secret if it does not exist yet.

    Idempotent, and it has to be: the console may reload the connect page at any point, and
    a second `CreateOauth2CredentialProvider` on the same name answers
    `ConflictException`. The live one is returned unchanged in that case -- never
    re-created, because re-creating it would mint a **new `callbackUrl`** and silently
    invalidate the GitHub App already pointing at the old one.
    """
    live = _provider()
    if live:
        return live
    return dict(
        _common.agentcore_control().create_oauth2_credential_provider(
            name=PROVIDER_NAME,
            credentialProviderVendor=PROVIDER_VENDOR,
            oauth2ProviderConfigInput={
                PROVIDER_CONFIG_MEMBER: {
                    "clientId": _PLACEHOLDER_CLIENT_ID,
                    "clientSecret": secrets_module.token_urlsafe(32),
                }
            },
        )
    )


def _ensure_workload_identity(return_url: str) -> dict[str, Any]:
    """The deployment's workload identity, with `return_url` permitted as an OAuth return.

    `resourceOauth2ReturnUrl` on `GetResourceOauth2Token` is mandatory **and** must already
    be registered here -- an unregistered one is refused, which is the check that stops a
    prompt-injected caller redirecting an authorisation to a host it chose. So the console's
    own origin is registered the first time it connects, and re-registered on every connect
    so a console moved to a new domain keeps working.

    Create-then-update rather than create-or-update: `CreateWorkloadIdentity` answers
    `ConflictException` on an existing name, and the update is what converges the URL list.
    """
    control = _common.agentcore_control()
    urls = sorted({return_url})
    try:
        return dict(
            control.create_workload_identity(
                name=WORKLOAD_NAME, allowedResourceOauth2ReturnUrls=list(urls)
            )
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") not in (
            "ConflictException",
            "ValidationException",
        ):
            raise
    live = dict(control.get_workload_identity(name=WORKLOAD_NAME))
    existing = [str(url) for url in live.get("allowedResourceOauth2ReturnUrls") or []]
    if return_url not in existing:
        # Appended rather than replaced: a deployment whose console moved still has to
        # honour an authorisation started from the previous origin.
        live = dict(
            control.update_workload_identity(
                name=WORKLOAD_NAME,
                allowedResourceOauth2ReturnUrls=[*existing, return_url],
            )
        )
    return live


def _manifest(*, console_origin: str, callback_url: str) -> dict[str, Any]:
    """The GitHub App manifest the console POSTs to GitHub, as a plain dict.

    Returned to the console rather than rendered here because the manifest flow is a
    **browser form submission** -- GitHub needs the operator's own session to create an app
    they own, so no server-side call can stand in for it.

    `name` carries random hex because a GitHub App name is globally unique across all of
    GitHub, and a fixed one makes the second person who ever runs this fail at the final
    step with "Name has already been taken".
    """
    return {
        "name": f"{APP_NAME}-coding-agent-{secrets_module.token_hex(3)}",
        "url": console_origin,
        "description": (
            "Lets the remote coding agent clone, push a branch and open a pull request on "
            "the repositories you install it on."
        ),
        # AgentCore's callback, never ours: the authorisation code has to land in the token
        # vault, and this URL is the only thing that puts it there.
        "callback_urls": [callback_url],
        # Where GitHub sends the operator once the app exists, with `&code=` appended. Two
        # things about this URL are load-bearing. It carries no fragment, because GitHub
        # appends the query *before* a fragment and `?code=` inside a `#/...` route is
        # invisible to `location.search`. And it carries the marker above, which is what
        # stops `auth.js` mistaking GitHub's code for a Cognito one and spending it.
        "redirect_url": f"{console_origin}/?{MANIFEST_RETURN_PARAM}={MANIFEST_RETURN_VALUE}",
        "public": False,
        "default_permissions": dict(APP_PERMISSIONS),
        "default_events": [],
        "hook_attributes": {"url": console_origin, "active": _MANIFEST_HOOK_ACTIVE},
        # The dead end described in the module docstring. Keep it false.
        "request_oauth_on_install": False,
        # The operator authorises from the console, which already knows who they are, so
        # there is nothing for a setup URL to do.
        "setup_on_update": False,
    }


def _public_settings(settings: Mapping[str, Any], provider: Mapping[str, Any]) -> dict[str, Any]:
    """What `GET /github-app` publishes: the app's public identity and the callback.

    No client secret, and no client id either -- the id is public on GitHub but publishing
    it here would invite a reader to treat this response as the credential's home, which is
    the vault.
    """
    slug = str(settings.get("appSlug") or "")
    return {
        "connected": bool(slug),
        "appSlug": slug or None,
        "appId": settings.get("appId") or None,
        "appUrl": str(settings.get("appUrl") or "") or None,
        "installUrl": _INSTALL_URL_TEMPLATE.format(slug=slug) if slug else None,
        "providerName": PROVIDER_NAME if provider else None,
        "callbackUrl": str(provider.get("callbackUrl") or "") or None,
    }


def _bot_commit_identity(slug: str) -> tuple[str, str]:
    """The app's own bot account as a git author, or `("", "")`.

    GitHub resolves a commit to an account by matching the author **email**, and the
    `users.noreply.github.com` form for a bot carries the **bot user's** id -- not the app id.
    The two differ, and using the app id resolves to no account. The id is therefore fetched
    rather than derived, from `GET /users/<slug>[bot]`, which needs
    no credential. A failure returns `("", "")` and costs the deployment nothing but the
    default identity: the alternative is a connect flow that fails because an *attribution*
    lookup did, which trades a cosmetic problem for a blocking one. `AGENT_GIT_AUTHOR_*` set
    at deploy time still wins over this (`provisioner._git_identity`).
    """
    login = f"{slug}[bot]"
    status, decoded, _headers = github_probe.request(
        "GET", f"{github_probe.API_ROOT}/users/{login}"
    )
    if status != 200 or not isinstance(decoded, Mapping) or not decoded.get("id"):
        LOG.warning(
            "could not read the bot account for %s (HTTP %s); commits will carry the "
            "container's default identity and GitHub will resolve no author for them",
            login,
            status,
        )
        return "", ""
    return login, f"{decoded['id']}+{login}@users.noreply.github.com"


def commit_identity() -> tuple[str, str]:
    """The connected app's bot identity as `(name, email)`, or `("", "")`.

    Read off the settings item, so the ordinary provision costs no GitHub call: `convert`
    derives it once when the app is adopted.

    **A deployment whose app was connected before this existed backfills itself here**, and
    that is worth the write rather than leaving it to the operator. The alternative recovery
    is to disconnect and connect again, which creates a *second* GitHub App and orphans the
    first -- litter in someone's GitHub account, to repair a field. The slug is enough to
    derive the identity at any time, so a missing pair with a slug present is a gap this can
    simply close, once, and never think about again.

    `("", "")` only when no app is connected at all, or when the bot lookup fails -- both of
    which fall back to the container's default identity.
    """
    settings = _settings()
    name = str(settings.get("gitAuthorName") or "")
    email = str(settings.get("gitAuthorEmail") or "")
    if name and email:
        return name, email

    slug = str(settings.get("appSlug") or "")
    if not slug:
        return "", ""
    name, email = _bot_commit_identity(slug)
    if name and email:
        # Stored so the next provision reads it rather than asking GitHub again. Not stored
        # when the lookup failed: an empty pair written here would be indistinguishable from
        # a successful derivation of nothing, and would stop this from ever retrying.
        _save_settings(gitAuthorName=name, gitAuthorEmail=email)
        LOG.info("derived the commit identity for %s and stored it", slug)
    return name, email


def is_connected() -> bool:
    """Has a GitHub App been converted into this deployment's provider yet?

    Reads the settings item and not the provider: `_ensure_provider` creates a provider with
    a placeholder the moment the connect page is opened, so a live provider does not mean a
    usable one. An `appSlug` is only written by `convert`, which is the step that puts real
    credentials in the vault.
    """
    return bool(_settings().get("appSlug"))


def install_url() -> str:
    """Where the operator installs the app on a repository, or `""` when not connected.

    Published on the refusal paths that an operator can only clear by installing the app --
    being told "the app is not installed on this repository" without being told where to fix
    it is the kind of accurate, useless message this codebase tries not to emit.
    """
    slug = str(_settings().get("appSlug") or "")
    return _INSTALL_URL_TEMPLATE.format(slug=slug) if slug else ""


def workload_token(config_id: str) -> str:
    """A workload access token scoped to one configuration, as the 3LO `userId`.

    `userId = configId` is the keying rule for GitHub credentials, and it is the direct
    analogue of AgentCore Memory's D6: one authorisation per registered repository, created
    and revoked with the registration, so deregistering a repository ends its access.

    It is **not** an IAM fence: `GetWorkloadAccessTokenForUserId` authorises only on
    `workload-identity-directory/*` -- not on an individual identity -- and
    `GetResourceOauth2Token` authorises only on `*`. Neither accepts a condition on the
    user id. So what stops one container reading another repository's token is that a
    container can read exactly one configuration item (invariant 31) and therefore knows
    exactly one random `configId`. `docs/security.md` states that plainly rather than
    letting the policy imply a fence it does not have.
    """
    return str(
        _common.agentcore_data().get_workload_access_token_for_user_id(
            workloadName=WORKLOAD_NAME, userId=config_id
        )["workloadAccessToken"]
    )


def resolve_token(config_id: str, *, return_url: str = "", force: bool = False) -> dict[str, Any]:
    """Either this repository's live GitHub token, or the URL that obtains one.

    One call answers both questions on purpose: `GetResourceOauth2Token` returns an
    `accessToken` when the vault already holds a valid authorisation for this `userId` and
    an `authorizationUrl` when it does not, and asking "is it authorised?" separately would
    be a second round trip that could disagree with the first.

    **Omitting `return_url` is a read, and an unauthorised repository answers it with an
    exception rather than an empty result.** The service refuses
    `GetResourceOauth2Token` with `ValidationException: You must provide a
    ResourceOauth2ReturnUrl to proceed with this flow` whenever it would have to *start* a
    flow -- so the absence of a return URL does not mean "just tell me", it means "and do not
    offer to fix it". Callers that only want to know (`configurations.github_status`,
    `runs.queue_run`) pass none, and both of them want "not authorised" rather than a 500.
    That is translated here rather than in each caller, because every caller that omits the
    parameter needs the same translation and the one that forgets it answers 500 to the most
    ordinary state a new repository is in.

    A `ValidationException` whose message does not name the return URL is **re-raised**: a
    wrong provider name arrives as the same error code, and silently reporting that as "not
    authorised" would leave a misconfigured deployment looking like an unauthorised repository
    forever. If the vendor rewords the message this stops matching and the failure is a loud
    500 carrying their words, which is the safe direction to fail in.

    `force` re-authorises an identity that already has a token, which is what the console's
    "Re-authorise" action needs.
    """
    arguments: dict[str, Any] = {
        "workloadIdentityToken": workload_token(config_id),
        "resourceCredentialProviderName": PROVIDER_NAME,
        "scopes": list(OAUTH_SCOPES),
        "oauth2Flow": OAUTH_FLOW,
    }
    if return_url:
        arguments["resourceOauth2ReturnUrl"] = return_url
    if force:
        arguments["forceAuthentication"] = True
    try:
        answer = dict(_common.agentcore_data().get_resource_oauth2_token(**arguments))
    except ClientError as err:
        if return_url or not _needs_a_return_url(err):
            raise
        LOG.info("configuration %s has no stored GitHub authorisation", config_id)
        return {"accessToken": "", "authorizationUrl": ""}
    return {
        "accessToken": str(answer.get("accessToken") or ""),
        "authorizationUrl": str(answer.get("authorizationUrl") or ""),
    }


def complete_authorization(config_id: str, session_uri: str) -> None:
    """Finish the 3LO leg that the operator has just approved in their browser.

    **Without this call the vault stores nothing.** GitHub redirects to AgentCore's callback,
    AgentCore redirects to `resourceOauth2ReturnUrl` with `?session_id=<sessionUri>` and
    **no error** -- so every signal says the authorisation succeeded, while
    `GetResourceOauth2Token` continues answering with an `authorizationUrl`. The `sessionUri`
    is the only thing that finishes it.

    `userIdentifier` is a **structure** (`{"userId": ...}`) rather than a string, which is the
    second thing to get wrong and costs a `ParamValidationError` inside botocore.

    `userId` is the configuration id, the same keying rule the rest of this module uses, so a
    session can only be completed for the repository it was started for.
    """
    _common.agentcore_data().complete_resource_token_auth(
        userIdentifier={"userId": config_id}, sessionUri=session_uri
    )
    LOG.info("completed the GitHub authorisation for configuration %s", config_id)


def _needs_a_return_url(err: ClientError) -> bool:
    """Whether this error is the service saying "there is no token and I would have to ask".

    Both halves are the fence. The code alone is too broad -- a wrong provider name is also a
    `ValidationException` -- and the message alone would match a reworded error about some
    other parameter. `ReturnUrl` is matched case-insensitively and as a substring because the
    sentence around it is the vendor's to change; the parameter name is theirs to keep.
    """
    error = err.response.get("Error", {})
    if error.get("Code") != "ValidationException":
        return False
    return "returnurl" in str(error.get("Message") or "").lower()


@handler
def get(_event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Is a GitHub App connected to this deployment, and what does the operator do next?

    A read, and read-only: it reaches the provider to publish the callback URL, and the
    settings item for the slug. A deployment with neither answers `connected: false` with
    every other field `null`, which is what the console's connect page routes on.
    """
    return 200, _public_settings(_settings(), _provider())


@handler
def connect(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Step 1: mint the provider and hand back the manifest the operator submits.

    Idempotent in the way that matters -- calling it twice does not re-create the provider
    and therefore does not invalidate an app already pointing at its callback -- but it
    does mint a **fresh manifest** each time, because the app name in it must be unique and
    a reloaded page that offered the same name would fail at GitHub.
    """
    body = parse_body(event)
    (console_origin,) = require(body, "consoleOrigin")
    console_origin = require_https(str(console_origin), "consoleOrigin")

    provider = _ensure_provider()
    callback_url = str(provider.get("callbackUrl") or "")
    if not callback_url:
        # Nothing downstream works without it and the manifest would carry an empty
        # callback, so this is a 500 rather than a response the console would act on.
        raise ApiError(
            500,
            "NoCallbackUrl",
            "AgentCore created the credential provider without a callbackUrl; "
            "nothing can be authorised until that is resolved",
            providerName=PROVIDER_NAME,
        )
    _ensure_workload_identity(console_origin)
    _save_settings(consoleOrigin=console_origin, providerName=PROVIDER_NAME)

    settings = _settings()
    return 200, {
        **_public_settings(settings, provider),
        # The three things the console needs to render the one-click button: where to POST,
        # what to POST, and the CSRF value GitHub echoes back.
        "manifestFormUrl": _MANIFEST_FORM_URL,
        "manifest": _manifest(console_origin=console_origin, callback_url=callback_url),
        "state": secrets_module.token_urlsafe(16),
    }


@handler
def convert(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Step 1b: exchange GitHub's manifest `code` for the app, and fill in the provider.

    The client secret exists in this process for the duration of one
    `UpdateOauth2CredentialProvider` call and is written nowhere else -- not to the item,
    not to a log line, not to the response. `scrub` covers the one log line that could
    otherwise carry it.

    The code is single-use and expires in **one hour**; a reused or stale one answers 404
    from GitHub, which is reported as a 400 naming the action rather than passed through as
    a 404 that reads like a missing route.
    """
    body = parse_body(event)
    (code,) = require(body, "code")
    status, decoded, _headers = github_probe.request(
        "POST", f"{github_probe.API_ROOT}/app-manifests/{code}/conversions"
    )
    if status != 201:
        raise ApiError(
            400,
            "ManifestConversionFailed",
            "GitHub would not convert that app manifest code; it is single-use and expires "
            f"after an hour, so start the connect step again. GitHub says: "
            f"{github_probe.github_message(decoded)}",
            githubStatus=status,
        )
    if not isinstance(decoded, Mapping):
        raise ApiError(500, "ManifestConversionUnreadable", "GitHub's response was not an object")

    client_id = str(decoded.get("client_id") or "")
    client_secret = str(decoded.get("client_secret") or "")
    slug = str(decoded.get("slug") or "")
    if not (client_id and client_secret and slug):
        raise ApiError(
            500,
            "ManifestConversionIncomplete",
            "GitHub created the app but did not return a client id, client secret and slug",
        )

    updated = _common.agentcore_control().update_oauth2_credential_provider(
        name=PROVIDER_NAME,
        credentialProviderVendor=PROVIDER_VENDOR,
        # `callbackUrl` is **not** sent and must not be: it is assigned by AgentCore and
        # preserved across this call, which is the property the whole ordering rests on.
        oauth2ProviderConfigInput={
            PROVIDER_CONFIG_MEMBER: {"clientId": client_id, "clientSecret": client_secret}
        },
    )
    name, email = _bot_commit_identity(slug)
    _save_settings(
        appSlug=slug,
        appId=str(decoded.get("id") or ""),
        appUrl=str(decoded.get("html_url") or ""),
        appOwner=str((decoded.get("owner") or {}).get("login") or ""),
        gitAuthorName=name,
        gitAuthorEmail=email,
        # Stored so provisioning can name it in every execution role without an AgentCore
        # call -- see `provider_secret_arn`.
        clientSecretArn=str((updated.get("clientSecretArn") or {}).get("secretArn") or ""),
    )
    LOG.info("connected the GitHub App %s to this deployment", slug)
    return 200, _public_settings(_settings(), _provider())


@handler
def disconnect(_event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Forget the GitHub App: delete the provider, and with it every stored authorisation.

    Deleting the provider is what revokes access, because the tokens live in its vault
    entry and nowhere else. The **GitHub App itself survives** -- it belongs to the
    operator's GitHub account, not to this deployment, and nothing here can delete it. The
    response says so rather than leaving an operator to discover an orphan app later.

    A missing provider is a success: this route's job is "ensure nothing is connected".
    """
    try:
        _common.agentcore_control().delete_oauth2_credential_provider(name=PROVIDER_NAME)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        LOG.info("the credential provider %s was already gone", PROVIDER_NAME)
    settings = _settings()
    try:
        _common.config_table.delete_item(
            Key={"configId": SETTINGS_ID}, ConditionExpression=Attr("configId").exists()
        )
    except ClientError as err:
        if not _common.is_condition_failure(err):
            raise
    return 200, {
        "connected": False,
        "revokedAuthorizations": True,
        # Named, because the operator has to go and do it and nothing else will tell them.
        "githubAppRemains": str(settings.get("appUrl") or "") or None,
    }


# The one definition of this module's routes -- see `configurations.ROUTES` on why this
# lives beside the handlers rather than only in the CDK app.
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", "/github-app", "get"),
    ("POST", "/github-app", "connect"),
    ("POST", "/github-app/conversions", "convert"),
    ("DELETE", "/github-app", "disconnect"),
)
