"""`/configurations` -- registering a repository, and the eight routes over it.

A configuration is one repository plus the AgentCore runtime, execution role and GitHub
secret that serve it, so registering one is a *provisioning* act: this module writes the
item and hands the slow half to `provisioner.enqueue`. That is also why `DELETE` is a
`202` marking the item `DELETING` rather than a delete: the runtime, the role and the
secret have to go first, and the provisioner removes the item last so a crash
mid-teardown is resumable (invariant 22).

The GitHub token is the one value in this system that must never be readable
afterwards. It goes straight into Secrets Manager, it is never an attribute on the item,
it is never in a response, and the only log line that sees a request body puts it
through `scrub`.

The two routes that write a token also *check* it first, in that order: an offline shape
check (`_require_supported_token`, an allowlist) and then a live one that proves the
token can push (`_require_pushable`, over `github_probe`). Both run before anything is
written, so a refused token leaves nothing behind.

`memory` is the eighth route and the one exception to note 30 ("a read handler never gains
a permission to look something up live"): what an operator is asking for lives in the
AgentCore Memory data plane and nowhere in DynamoDB, so a route over stored attributes
could only ever answer with the `memoryId` the console already prints. What bounds it is
the grant -- exactly two read-only actions, `RetrieveMemoryRecords` and
`ListMemoryRecords`, on `memory/*` (`infra/stacks/api_stack.py`'s `memory:read` arm) --
plus `tests/test_api_grant_contract.py`'s guard, which asserts as an *equality* that this
handler reaches those two operations and one `GetItem` and nothing else. A repository with
no memory -- registered before memory existed, or one whose `CreateMemory` failed -- is a
`200` with an empty list and no AgentCore call at all, not a 404: the configuration
exists, only its memory does not.
"""

from __future__ import annotations

import re
import secrets as secrets_module  # `_common.secrets` is the Secrets Manager client
from collections.abc import Iterable, Mapping
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from . import _common, github_probe, provisioner
from ._common import (
    LOG,
    ApiError,
    decode_cursor,
    encode_cursor,
    handler,
    int_query_param,
    parse_body,
    path_param,
    query_param,
    require,
    scrub,
)

EDITABLE_FIELDS = ("verifyCommand", "setupCommand", "defaultBranch")

# `PENDING` is this module's -- the item exists before the provisioner has seen it. The
# other five are written by `provisioner.py`, which cannot import this tuple because the
# import runs the other way; `tests/test_api_configurations.py` pins the subset.
CONFIG_STATUSES = ("PENDING", "CREATING", "READY", "UPDATING", "FAILED", "DELETING")

# The statuses that do not hold the repository name.
#
# `DELETING` (unchanged): teardown is asynchronous, and refusing to re-register while it
# drains would make the failure look permanent.
#
# `FAILED`: a failed CREATE was a dead end nothing could clear. `mcp/src/rca_mcp/client.py`'s
# `ALLOWED_METHODS` is `{"GET", "POST"}` by construction, so removing a configuration is
# unreachable from the plugin, and a permanent 409 here could only be cleared in the
# console. Observed for real: a first registration failed on a missing
# `bedrock-agentcore:CreateAgentRuntimeEndpoint` grant, and re-registering the same
# repository after the grant was fixed was impossible. `create` therefore retires the
# stale item down the ordinary teardown path and registers a new one.
#
# Both strings are literals here rather than imported from `provisioner`: the import runs
# the other way -- see `provisioner.py`'s comment above `CREATING, READY, ...`.
_NOT_HOLDING = frozenset({"DELETING", "FAILED"})

# The response allow-list. Fixed rather than "the item minus a deny-list" so an internal
# attribute added later is not published by accident.
_PUBLIC_FIELDS = (
    "configId",
    "repository",
    "repositoryUrl",
    "status",
    "statusReason",
    "verifyCommand",
    "setupCommand",
    "defaultBranch",
    "secretArn",
    "executionRoleArn",
    "agentRuntimeArn",
    "agentRuntimeId",
    "agentRuntimeVersion",
    # Absent from the item for a repository registered before memory existed, and nothing
    # here defaults it: `_public` omits the key, which is what "no memory" reads as.
    "memoryId",
    "containerUri",
    "createdAt",
    "updatedAt",
)

_DEFAULT_PAGE = 25
_MAX_PAGE = 100

# The two scopes `provisioner`'s namespace templates spell, and the count bounds. 100 is
# not a taste: botocore's own `ListMemoryRecords.maxResults` is `1..100`, and
# `RetrieveMemoryRecords.searchCriteria.topK` has a minimum of 1 -- a `minimum=0` here
# would let `?limit=0` become a ParamValidationError inside botocore, i.e. a 500 on a
# request that is merely silly.
_MEMORY_SCOPES = ("episodic", "session")
_DEFAULT_MEMORY_RECORDS = 25
_MAX_MEMORY_RECORDS = 100

# The two forms `git clone` accepts for a GitHub repository. Anything else is refused
# naming both, because the interesting failure is a URL that is *nearly* right.
_HTTPS_REPOSITORY = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?/?$"
)
_SSH_REPOSITORY = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?$"
)

# The gate is this one: an *allowlist*, because a denylist admits every token type
# nobody thought of, and each of those fails 25 minutes later inside a provisioned
# container rather than here. The system supports exactly one credential -- a
# fine-grained personal access token scoped to one repository. `{6,}` rather than a real
# token's 93 characters because `_common._TOKEN_PATTERN` already spells the shape that
# way, and one definition of "looks like a fine-grained PAT" in the bundle beats two.
_FINE_GRAINED = re.compile(r"^github_pat_[A-Za-z0-9_]{6,}$")

# A classic personal access token: the `ghp_` prefix, or the bare 40-hex shape of a
# pre-2021 one. **Accepted**, with a warning -- see `_require_supported_token`.
_CLASSIC_PREFIX = re.compile(r"^ghp_")
_CLASSIC_HEX = re.compile(r"^[0-9a-f]{40}$")

# The three GitHub App token prefixes, which stay refused. Not a security judgement: they
# expire an hour after they are issued and nothing in this codebase refreshes one, so an
# accepted `ghs_` registers fine and then fails inside a container an hour later, which is
# the worst failure shape available. `gho_` is an OAuth user token and is refused with them
# -- accepting it buys nothing a `ghp_` does not, and it cannot be scoped either.
_EXPIRING_OR_OAUTH_PREFIX = re.compile(r"^gh[osur]_")


def _new_config_id() -> str:
    return f"cfg_{secrets_module.token_hex(6)}"


def _repository_from_url(url: str) -> str:
    """`owner/repo`, which is what every later step wants -- invariant 11 hands subagents
    the slug and never the URL, because on a local run the URL is a `file://` path and
    subagents followed it out of their working tree."""
    match = _HTTPS_REPOSITORY.match(url) or _SSH_REPOSITORY.match(url)
    if not match:
        raise ApiError(
            400,
            "InvalidRepositoryUrl",
            "repositoryUrl must be https://github.com/owner/repo(.git) or "
            "git@github.com:owner/repo.git",
            repositoryUrl=url,
        )
    return f"{match.group('owner')}/{match.group('repo')}"


def _require_supported_token(token: str) -> bool:
    """The shape of a credential this system can use, checked without a network call.
    Returns `True` when the token is a classic one, so the caller can warn.

    **A classic PAT is accepted, and that is a deliberate trade.** It cannot be scoped to a
    single repository, so a stolen one reaches everything its owner can -- but it takes three
    decisions to create where a fine-grained token takes about nine, and the setup churn was
    turning people away before they ever saw the agent run. The bound that matters is stated
    in `README.md` instead and is the same one this system already relies on for AWS: a
    dedicated, disposable account. On an account that owns only the demo repository the
    distinction is academic; on an account that owns real work it is not, which is why the
    README says so in as many words rather than leaving it implied.

    Still an allowlist, and two shapes stay out:

    * `ghs_`/`ghu_`/`ghr_` GitHub App tokens, because they expire an hour after issue and
      nothing here refreshes one. Accepting one means registration succeeds and every task an
      hour later fails inside a container -- later and more confusing than refusing it now.
    * `gho_` OAuth user tokens, for the same expiry reason and because they buy nothing a
      classic PAT does not.

    Anything unrecognised is refused rather than passed through, for the reason the allowlist
    has always had: an unknown shape is discovered 25 minutes later as a `git clone` failure
    inside a provisioned container.

    Free, and first: `_require_pushable` must never be reached by a token this can refuse.
    """
    if _FINE_GRAINED.match(token):
        return False
    if _CLASSIC_PREFIX.match(token) or _CLASSIC_HEX.match(token):
        LOG.warning(
            "a classic personal access token was accepted; it cannot be scoped to one "
            "repository, so its blast radius is every repository its owner can reach"
        )
        return True
    if _EXPIRING_OR_OAUTH_PREFIX.match(token):
        raise ApiError(
            400,
            "ExpiringTokenRejected",
            "that is a GitHub App installation or OAuth token. The API would accept it and "
            "it expires an hour later, and this codebase has no refresh path -- so it would "
            "register cleanly and then fail inside a container an hour from now. Use a "
            "personal access token: fine-grained (github_pat_...) scoped to this repository "
            "with contents: write and pull_requests: write, or a classic one (ghp_...), "
            "which is accepted and is not scoped to a single repository.",
        )
    raise ApiError(
        400,
        "UnsupportedTokenType",
        "that does not look like a GitHub personal access token. This system takes a "
        "fine-grained token (github_pat_...) scoped to this repository with contents: write "
        "and pull_requests: write, or a classic one (ghp_...). The check is an allowlist on "
        "purpose -- anything else is accepted here and then fails inside a provisioned "
        "container, half an hour after the mistake was made.",
    )


def _require_pushable(repository: str, token: str) -> None:
    """PLAN Tier 0 #2, the live half: prove the token can push, before storing it.

    A read-only check is not evidence. Selecting a repository grants `Metadata: read`
    automatically, so `GET /repos/{owner}/{repo}` answers 200 for a token with no
    `Contents` permission at all -- a token was declared good three times that way and
    still failed the push. `github_probe` therefore creates a throwaway ref and deletes
    it, which is the only check that actually proves a write.

    The 400 carries GitHub's own sentence and the permission its
    `x-accepted-github-permissions` header names. This system's contribution here is
    delivering that accurate message in a few hundred milliseconds instead of 25 minutes;
    rewording it would be a step backwards.

    A GitHub that cannot be reached, or that answers 5xx, or a repository with no commit
    to branch from, is **not** a registration failure: the token is stored with a warning,
    because a control plane that cannot register a repository during a GitHub incident is
    worse than one that finds out late.
    """
    try:
        github_probe.check_can_push(repository, token=token)
    except github_probe.PushDenied as denied:
        raise ApiError(
            400,
            "TokenCannotPush",
            f"that token cannot push to {repository}. GitHub says: {denied.github_message}",
            repository=repository,
            githubStatus=denied.status,
            acceptedPermissions=denied.accepted_permissions,
        ) from denied
    except github_probe.ProbeUnavailable as unavailable:
        LOG.warning(
            "could not check whether the token can push to %s (%s); storing it anyway -- an "
            "unreachable GitHub must not fail a registration",
            repository,
            unavailable,
        )


def _public(config: Mapping[str, Any]) -> dict[str, Any]:
    return {field: config[field] for field in _PUBLIC_FIELDS if field in config}


def _load_config(config_id: str) -> dict[str, Any]:
    found = _common.config_table.get_item(Key={"configId": config_id}).get("Item")
    if not found:
        raise ApiError(404, "NotFound", "no configuration with that id", configId=config_id)
    return found


def _memory_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """One `memoryRecordSummaries` entry, in this API's field names rather than AWS's.

    The reason this helper exists at all: the record text lives at `content.text`, and that
    shape is AWS's -- returning the response verbatim would make this route's body theirs
    to change. The read is defensive for the same reason
    `agent/src/coding_agent/agentcore_memory.py::format_records` reads it defensively: a
    record with no `content` must render as an empty `text`, not a `KeyError`, on a route
    whose whole job is to show what is there.

    Unlike `format_records`, an empty text is **not** a reason to skip the record: that
    formatter's reader is a model and this one's is an operator counting what came back, who
    has to see every record the service returned.

    `createdAt` and `score` are included only when the record carries them -- a browse ranks
    nothing, so publishing a score for one would be inventing it -- which is `_public`'s
    omit-rather-than-null habit. `metadata` and `memoryStrategyId` are AWS's internals and
    stay out: publishing them would make this body AWS's shape again by the back door.
    """
    rendered: dict[str, Any] = {
        "recordId": record.get("memoryRecordId"),
        "text": ((record.get("content") or {}).get("text") or "").strip(),
    }
    for field in ("createdAt", "score"):
        if record.get(field) is not None:
            rendered[field] = record[field]
    return rendered


def _memory_namespace(event: Mapping[str, Any], config_id: str) -> tuple[str, str]:
    """The `(scope, namespace)` a request asks for, or an `ApiError`.

    `actorId` is the **configuration id** and `sessionId` the **task id** -- deliverable
    D6's pairing, and getting it backwards resolves both templates to a namespace nobody
    writes to. The templates are `provisioner`'s and are `.format()`-ed here rather than
    re-spelled, because a mismatched namespace does not fail: the read simply returns
    nothing, forever, with no error and no warning.

    An unrecognised scope is refused, where `agentcore_memory.TaskMemory._namespace` warns
    and falls back to episodic. The divergence is deliberate: that caller is a *model*
    inside a tool call, so an unrecognised value has to be cheap, while this one is an
    operator's client, where it is a client bug -- and a silent fallback would answer a
    question nobody asked.
    """
    scope = query_param(event, "scope", "episodic")
    if scope == "session":
        task_id = query_param(event, "taskId")
        if not task_id:
            raise ApiError(
                400,
                "MissingQueryParameter",
                "scope=session reads one task's namespace, so taskId is required",
                field="taskId",
                scope=scope,
            )
        return scope, provisioner.SESSION_NAMESPACE.format(actorId=config_id, sessionId=task_id)
    if scope != "episodic":
        raise ApiError(
            400,
            "UnknownMemoryScope",
            f"scope must be one of {', '.join(_MEMORY_SCOPES)}",
            scope=scope,
            scopes=list(_MEMORY_SCOPES),
        )
    return scope, provisioner.EPISODIC_NAMESPACE.format(actorId=config_id)


def _repository_configurations(repository: str) -> list[dict[str, Any]]:
    """Every configuration on the `byRepository` GSI for this slug, in one query.

    One query read twice rather than two queries: `create` asks both "is there a live
    configuration?" and "is there a stale FAILED one to retire?", the GSI is eventually
    consistent, and two queries could answer inconsistently -- which would be a 409 and
    a retirement in the same request.
    """
    page = _common.config_table.query(
        IndexName="byRepository",
        KeyConditionExpression=Key("repository").eq(repository),
    )
    return list(page.get("Items", []))


def _already_registered(items: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The live configuration among `items`, if there is one.

    A `DELETING` one does not count: teardown is asynchronous, and refusing to
    re-register while it drains would make the failure look permanent. A `FAILED` one
    does not count either -- see `_NOT_HOLDING`. The scan returns the first *live* item
    rather than the first item, because after a replacement the GSI transiently holds a
    `DELETING` one beside the new one, so a bare "first item" would name the wrong
    configuration in the 409.
    """
    for item in items:
        if item.get("status") not in _NOT_HOLDING:
            return dict(item)
    return None


def _retire_failed(config_id: str) -> None:
    """Send a stale `FAILED` configuration down the ordinary teardown path.

    Exactly what `delete` does, and deliberately the same two lines: a failed CREATE has
    already made an IAM role at `provisioner.ROLE_PATH` and a secret at
    `rca/github/{configId}`, and `provisioner._delete` is the only code that takes the
    runtime, the role, its inline policy, the secret and the item in dependency order --
    tolerating whatever the failed attempt never got as far as creating (`_is_gone`).
    Marking it `DELETING` first is what makes that teardown resumable (invariant 22) and
    what keeps it out of `_already_registered` while it drains.

    A lost condition is not an error: the item was queried off an eventually-consistent
    GSI and may already be gone, and gone is the outcome this function wanted.
    """
    try:
        _common.config_table.update_item(
            Key={"configId": config_id},
            UpdateExpression="SET #status = :status, #updatedAt = :updatedAt",
            ExpressionAttributeNames={"#status": "status", "#updatedAt": "updatedAt"},
            ExpressionAttributeValues={":status": "DELETING", ":updatedAt": _common.now_iso()},
            ConditionExpression=Attr("configId").exists(),
        )
    except ClientError as err:
        if not _common.is_condition_failure(err):
            raise
        LOG.info("the FAILED configuration %s was already gone; queued no teardown", config_id)
        return
    provisioner.enqueue(config_id, "DELETE")
    LOG.info(
        "retired the FAILED configuration %s so its repository can be registered again", config_id
    )


@handler
def create(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    body = parse_body(event)
    # The only line in this module that sees a request body, and it is scrubbed. The
    # non-credential half is genuinely useful when a registration goes wrong.
    LOG.debug("registering %s", scrub(body))
    repository_url, token = require(body, "repositoryUrl", "githubToken")
    repository = _repository_from_url(repository_url)
    _require_supported_token(token)

    siblings = _repository_configurations(repository)
    existing = _already_registered(siblings)
    if existing is not None:
        raise ApiError(
            409,
            "RepositoryAlreadyRegistered",
            f"{repository} is already registered; patch or delete that configuration instead",
            configId=existing.get("configId"),
            repository=repository,
        )

    # After the 409 and before every write: there is no point spending seconds against
    # GitHub for a request that is about to conflict, and a token refused here must not
    # have retired a stale configuration or created a secret on its way to the 400.
    _require_pushable(repository, token)

    # Before the secret, not after: a FAILED configuration serves nothing, so retiring it
    # first costs no availability -- and if the registration below then fails, the operator
    # retries against a repository that is genuinely free. The other order leaves an IAM
    # role AND a secret holding a live GitHub token orphaned when the retirement write
    # fails, and failing to tear down beats leaking a credential.
    #
    # Only FAILED, never DELETING: a DELETING configuration's teardown is already in
    # flight, and a second DELETE message would be a redundant replay.
    retired = [
        str(item["configId"])
        for item in siblings
        if item.get("status") == "FAILED" and item.get("configId")
    ]
    for stale_id in retired:
        _retire_failed(stale_id)

    config_id = _new_config_id()
    # The secret is written **before** the item, so a stored configuration always has a
    # resolvable `secretArn` -- the execution role's inline policy names that literal
    # ARN, and an item pointing at nothing is a runtime that can never authenticate.
    secret = _common.secrets.create_secret(
        Name=f"rca/github/{config_id}",
        SecretString=token,
        Description=f"GitHub token for {repository}",
    )
    stamp = _common.now_iso()
    item: dict[str, Any] = {
        "configId": config_id,
        "repository": repository,
        "repositoryUrl": repository_url,
        "status": "PENDING",
        "statusReason": "",
        "secretArn": secret["ARN"],
        "createdAt": stamp,
        "updatedAt": stamp,
    }
    for field in EDITABLE_FIELDS:
        value = body.get(field)
        # Absent rather than empty: every one of these three falls back to per-run
        # detection, and an empty string is a *declared* "run nothing".
        if isinstance(value, str) and value.strip():
            item[field] = value
    try:
        _common.config_table.put_item(Item=item, ConditionExpression=Attr("configId").not_exists())
    except ClientError:
        # Ordering the secret first buys a resolvable ARN at the cost of this arm: an
        # orphan secret holds the name `rca/github/<configId>` and a scheduled deletion
        # would make it unrecoverable for 7 days.
        _common.secrets.delete_secret(SecretId=secret["ARN"], ForceDeleteWithoutRecovery=True)
        raise

    provisioner.enqueue(config_id, "CREATE")
    response = _public(item)
    if retired:
        # Outside `_public` on purpose: that tuple is a closed allow-list of *item*
        # attributes, and this is a fact about the request. Without it a re-registration is
        # indistinguishable from a first one, and the operator is never told that a
        # configuration they may be looking at is being torn down.
        response["replacedConfigIds"] = retired
    return 201, response


@handler
def list_(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    limit = int_query_param(event, "limit", _DEFAULT_PAGE, minimum=1, maximum=_MAX_PAGE)
    cursor = decode_cursor(query_param(event, "cursor"))
    # A `scan`, deliberately: there are as many configurations as there are registered
    # repositories, and a GSI to order a handful of items would be a cost with no reader.
    arguments: dict[str, Any] = {"Limit": limit}
    if cursor:
        arguments["ExclusiveStartKey"] = cursor
    page = _common.config_table.scan(**arguments)
    return 200, {
        "items": [_public(item) for item in page.get("Items", [])],
        "nextCursor": encode_cursor(page.get("LastEvaluatedKey")),
    }


@handler
def get(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, _public(_load_config(path_param(event, "configId")))


@handler
def memory(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """What this repository remembers: a browse, or a semantic search with `q`.

    The second read of one configuration, and the only route in this module that reaches a
    data plane other than DynamoDB -- see the module docstring for the grant that bounds it.
    """
    config_id = path_param(event, "configId")
    config = _load_config(config_id)
    # Before the memoryId check: a `scope=session` with no `taskId` is a client bug, and
    # answering it 200-with-an-empty-list because this particular repository happens to
    # have no memory would hide that bug behind a fact about the data -- i.e. hide it on
    # exactly the repositories that are new.
    scope, namespace = _memory_namespace(event, config_id)
    # `limit` and `q` are the whole page control: no `nextToken` is published and no cursor
    # is issued, because `encode_cursor`/`decode_cursor` are the DynamoDB
    # `LastEvaluatedKey` vocabulary and a second, differently-shaped cursor on one API is
    # how a client sends one to the wrong route.
    limit = int_query_param(
        event, "limit", _DEFAULT_MEMORY_RECORDS, minimum=1, maximum=_MAX_MEMORY_RECORDS
    )
    search = query_param(event, "q")

    memory_id = str(config.get("memoryId") or "")
    if not memory_id:
        # Not a 404: the configuration exists, only its memory does not.
        # `provisioner._ensure_memory` swallows its own `ClientError` and returns `""`, so a
        # repository whose `CreateMemory` failed still reaches `READY` with no memory at
        # all, and every configuration registered before memory existed carries no key. The
        # container degrades to a no-op and `web/js/views/register.js` prints "none:
        # registered before memory, or provisioning did not create one" -- this is that same
        # state, said in JSON. And no AgentCore call at all: an empty `memoryId` is a
        # `ParamValidationError` inside botocore, i.e. a 500 for a repository that is
        # merely new.
        return 200, {
            "configId": config_id,
            "memoryId": None,
            "scope": scope,
            "namespace": namespace,
            "items": [],
        }

    # A `ClientError` out of either arm is deliberately **not** swallowed into an empty
    # list: `@handler` turns it into a 500 with a logged exception, and an operator asking
    # what a repository remembers must not be told "nothing" when the truth is "the call
    # failed". The container's D11 degradation exists to stop a *run* dying, which is not
    # this route's problem.
    if search:
        # The semantic arm. `retrieve_memory_records`' input shape requires
        # `searchCriteria`, and `topK` is the same bound as the browse's `maxResults`.
        found = _common.agentcore_data().retrieve_memory_records(
            memoryId=memory_id,
            namespace=namespace,
            searchCriteria={"searchQuery": search, "topK": limit},
        )
    else:
        # The browse arm, and the reason `q` is optional: an operator with no particular
        # question came for the one thing a required search query would deny them -- the
        # list of what is in there.
        found = _common.agentcore_data().list_memory_records(
            memoryId=memory_id, namespace=namespace, maxResults=limit
        )
    return 200, {
        "configId": config_id,
        "memoryId": memory_id,
        "scope": scope,
        "namespace": namespace,
        "items": [_memory_record(record) for record in found.get("memoryRecordSummaries") or []],
    }


@handler
def patch(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    config_id = path_param(event, "configId")
    body = parse_body(event)

    for field in body:
        if field not in EDITABLE_FIELDS:
            raise ApiError(
                400,
                "NotEditable",
                f"{field} is not editable; the editable fields are "
                f"{', '.join(EDITABLE_FIELDS)}. repositoryUrl is deliberately absent -- "
                "re-register the repository rather than repointing an existing "
                "configuration at a different one. githubToken goes to "
                f"PUT /configurations/{config_id}/github-token.",
                field=field,
                editable=list(EDITABLE_FIELDS),
            )
    if not body:
        # A 400 rather than a no-op write: an empty patch is a client bug, and answering
        # 200 to one is how a console reports a setting saved that was never sent.
        raise ApiError(400, "EmptyPatch", f"send at least one of {', '.join(EDITABLE_FIELDS)}")

    names: dict[str, str] = {"#updatedAt": "updatedAt"}
    values: dict[str, Any] = {":updatedAt": _common.now_iso()}
    sets = ["#updatedAt = :updatedAt"]
    removes: list[str] = []
    for index, (field, value) in enumerate(body.items()):
        names[f"#f{index}"] = field
        if value is None:
            # `REMOVE`, not `SET ""`: clearing one of these three fields puts it back to
            # per-run detection, and a stored empty string means "run nothing".
            removes.append(f"#f{index}")
            continue
        if not isinstance(value, str):
            raise ApiError(400, "InvalidField", f"{field} must be a string or null", field=field)
        values[f":f{index}"] = value
        sets.append(f"#f{index} = :f{index}")

    expression = "SET " + ", ".join(sets)
    if removes:
        expression = f"{expression} REMOVE {', '.join(removes)}"
    try:
        updated = _common.config_table.update_item(
            Key={"configId": config_id},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression=Attr("configId").exists(),
            ReturnValues="ALL_NEW",
        )
    except ClientError as err:
        # `update_item` *creates* an absent item, so the condition is what makes a patch
        # of a deleted configuration a 404 instead of a resurrection.
        if _common.is_condition_failure(err):
            raise ApiError(
                404, "NotFound", "no configuration with that id", configId=config_id
            ) from err
        raise
    return 200, _public(updated.get("Attributes", {}))


@handler
def delete(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    config_id = path_param(event, "configId")
    try:
        _common.config_table.update_item(
            Key={"configId": config_id},
            UpdateExpression="SET #status = :status, #updatedAt = :updatedAt",
            ExpressionAttributeNames={"#status": "status", "#updatedAt": "updatedAt"},
            ExpressionAttributeValues={":status": "DELETING", ":updatedAt": _common.now_iso()},
            ConditionExpression=Attr("configId").exists(),
        )
    except ClientError as err:
        if _common.is_condition_failure(err):
            raise ApiError(
                404, "NotFound", "no configuration with that id", configId=config_id
            ) from err
        raise
    # The item is removed by the provisioner, after the runtime, the role and the secret
    # are gone (invariant 22). A `202` says exactly that: accepted, not finished.
    provisioner.enqueue(config_id, "DELETE")
    return 202, {"configId": config_id, "status": "DELETING", "queued": True, "phase": "DELETE"}


@handler
def runtime_update(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    config_id = path_param(event, "configId")
    config = _load_config(config_id)
    status = config.get("status")
    if status != "READY":
        raise ApiError(
            409,
            "ConfigurationNotReady",
            "a runtime update needs a READY configuration",
            configId=config_id,
            status=status,
        )
    # No status write here on purpose: the provisioner sets `UPDATING` only if
    # `_matches_intent` said an update was actually needed, so re-running this across a
    # converged fleet does not park every configuration in `UPDATING` forever.
    provisioner.enqueue(config_id, "UPDATE")
    return 202, {"queued": True, "phase": "UPDATE"}


@handler
def rotate_token(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    config_id = path_param(event, "configId")
    body = parse_body(event)
    unexpected = sorted(field for field in body if field != "githubToken")
    if unexpected:
        # The credential/settings split, enforced rather than documented: a route that
        # quietly accepted `verifyCommand` here would be a second writer of the item,
        # and this is the one request body in the system that is never echoed back.
        raise ApiError(
            400,
            "CredentialBodyOnly",
            "this route takes githubToken and nothing else; settings go to "
            f"PATCH /configurations/{config_id}",
            unexpected=unexpected,
        )
    (token,) = require(body, "githubToken")
    _require_supported_token(token)

    config = _load_config(config_id)
    secret_arn = config.get("secretArn")
    if not secret_arn:
        raise ApiError(
            409,
            "NoSecret",
            "this configuration has no secret to rotate; re-register the repository",
            configId=config_id,
        )
    # The probe needs the repository, so it cannot run before the item is loaded -- but it
    # still runs before the write. A stored configuration always carries `repository`; if
    # one somehow does not, an unprobed rotation beats a 500 on the route the operator
    # reaches for *because* their token is wrong.
    repository = config.get("repository")
    if repository:
        _require_pushable(str(repository), token)
    else:
        LOG.warning(
            "configuration %s carries no repository; rotating without the push check", config_id
        )
    # A new **version** of the existing secret, never a second secret: the execution
    # role's inline policy names this literal ARN, so a second secret would be
    # unreadable by the runtime that needs it.
    rotated = _common.secrets.put_secret_value(SecretId=secret_arn, SecretString=token)
    LOG.info("rotated the github token for config %s", config_id)
    return 200, {"rotated": True, "configId": config_id, "versionId": rotated.get("VersionId")}


# FOLLOW-UP (phase 7): `infra/stacks/api_stack.py`'s `ApiStack` should build its
# `HttpApi` routes and its `lambda_handler` strings from this tuple rather than repeating
# them -- a route declared in two places is a route that will be added to one of them.
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/configurations", "create"),
    ("GET", "/configurations", "list_"),
    ("GET", "/configurations/{configId}", "get"),
    ("PATCH", "/configurations/{configId}", "patch"),
    ("DELETE", "/configurations/{configId}", "delete"),
    ("POST", "/configurations/{configId}/runtime-update", "runtime_update"),
    ("PUT", "/configurations/{configId}/github-token", "rotate_token"),
    ("GET", "/configurations/{configId}/memory", "memory"),
)
