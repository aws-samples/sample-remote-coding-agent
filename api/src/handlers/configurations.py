"""`/configurations` -- registering a repository, and the nine routes over it.

A configuration is one repository plus the AgentCore runtime, execution role and memory
that serve it, so registering one is a *provisioning* act: this module writes the item and
hands the slow half to `provisioner.enqueue`. That is also why `DELETE` is a `202` marking
the item `DELETING` rather than a delete: the runtime, the role and the memory have to go
first, and the provisioner removes the item last so a crash mid-teardown is resumable
(invariant 22).

**Registration takes no credential at all.** There is no `githubToken` field on any route
here and no secret anywhere in the system: one GitHub App serves the whole deployment and
AgentCore Identity's token vault holds its credentials -- see `github_app.py` for the whole
design. What a repository needs beyond its registration is an **authorisation**, which
`github_authorize` starts and `github_status` reports, keyed on this configuration's own id
as the 3LO `userId`. A task started before that authorisation exists is refused with
`409 GitHubNotAuthorized`, the same shape as `409 ConfigurationNotReady`.

`github_status` is also where the one check worth making lives: it fetches the live token
and proves it can push (`_push_verdict`, over `github_probe`). That is what turns "did
you remember to install the app on this repository?" from a question nobody can answer into
a definite yes or no, 25 minutes before a container would otherwise find out.

`memory` is the one exception to note 30 ("a read handler never gains
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
import secrets as secrets_module
from collections.abc import Iterable, Mapping
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from . import _common, github_app, github_probe, provisioner
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


def _push_verdict(repository: str, token: str) -> dict[str, Any]:
    """Can this authorisation actually push to this repository? A verdict, never a raise.

    A read-only check is not evidence. Selecting a repository grants `Metadata: read`
    automatically, so `GET /repos/{owner}/{repo}` answers 200 for a token with no
    `Contents` permission at all -- and with a GitHub App the same call answers 200 for a
    *public* repository the app was never installed on, because `permissions.push` there
    reports the **user's** rights rather than the token's. Measured, and it is the trap this
    function exists for. `github_probe` therefore creates a throwaway ref and deletes it,
    which is the only check that proves a write.

    So the one failure an operator is overwhelmingly likely to hit -- authorising the app
    but forgetting to install it on *this* repository -- is detected here rather than 25
    minutes later inside a container. GitHub's answer to that is a 403 "Resource not
    accessible by integration", which is passed through verbatim beside the install link.

    A verdict rather than an `ApiError`, because this runs on a *status* route: the
    authorisation is real either way, and the console shows "authorised, but the app is not
    installed here" as a state with an action attached, not as a failed request. A GitHub
    that cannot be reached is `"unknown"` for the same reason -- an incident at GitHub must
    not make a registered repository look misconfigured.
    """
    try:
        github_probe.check_can_push(repository, token=token)
    except github_probe.PushDenied as denied:
        return {
            "canPush": False,
            "reason": denied.github_message,
            "githubStatus": denied.status,
            "acceptedPermissions": denied.accepted_permissions,
        }
    except github_probe.ProbeUnavailable as unavailable:
        LOG.warning("could not check push access to %s: %s", repository, unavailable)
        return {"canPush": None, "reason": str(unavailable)}
    return {"canPush": True, "reason": ""}


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
    already made an IAM role at `provisioner.ROLE_PATH` and an AgentCore memory, and
    `provisioner._delete` is the only code that takes the runtime, the memory, the role, its
    inline policy and the item in dependency order --
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
    (repository_url,) = require(body, "repositoryUrl")
    repository = _repository_from_url(repository_url)
    if "githubToken" in body:
        # Named rather than ignored. An operator -- or a script, or an older plugin -- still
        # sending a PAT has to be told it is not used, because silently dropping it would
        # leave them believing a credential was stored and then confused by a
        # `GitHubNotAuthorized` on their first task.
        raise ApiError(
            400,
            "TokenNotAccepted",
            "registration takes no credential. This deployment authenticates through one "
            "GitHub App held in AgentCore Identity, so there is no token to store: register "
            "the repository, then authorise it at "
            "POST /configurations/{configId}/github-authorize.",
        )

    if not github_app.is_connected():
        # Before anything is written: a repository registered against a deployment with no
        # GitHub App can never be authorised, so it would provision a runtime for 10 minutes
        # and then be useless.
        raise ApiError(
            409,
            "GitHubAppNotConnected",
            "no GitHub App is connected to this deployment yet; connect one first "
            "(the console's Connect GitHub page, or POST /github-app)",
        )

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

    # A FAILED configuration serves nothing, so retiring it first costs no availability --
    # and if the registration below then fails, the operator retries against a repository
    # that is genuinely free.
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
    stamp = _common.now_iso()
    item: dict[str, Any] = {
        "configId": config_id,
        "repository": repository,
        "repositoryUrl": repository_url,
        "status": "PENDING",
        "statusReason": "",
        "createdAt": stamp,
        "updatedAt": stamp,
    }
    for field in EDITABLE_FIELDS:
        value = body.get(field)
        # Absent rather than empty: every one of these three falls back to per-run
        # detection, and an empty string is a *declared* "run nothing".
        if isinstance(value, str) and value.strip():
            item[field] = value
    # One write and no credential to order it against: with the secret gone, the item is the
    # first thing this registration creates, so a failure here leaves nothing behind at all.
    _common.config_table.put_item(Item=item, ConditionExpression=Attr("configId").not_exists())

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
        # `github_app.SETTINGS_ID` is filtered here and not by a scan filter: a
        # `FilterExpression` is applied *after* `Limit`, so a page that happened to contain
        # the sentinel would come back one item short and a reader would see a gap rather
        # than a hidden item. One item, removed in Python, costs nothing.
        "items": [
            _public(item)
            for item in page.get("Items", [])
            if item.get("configId") != github_app.SETTINGS_ID
        ],
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
                "configuration at a different one. There is no credential field: this "
                "deployment authenticates through one GitHub App, so what a repository "
                "needs is an authorisation, at "
                f"POST /configurations/{config_id}/github-authorize.",
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
    # The item is removed by the provisioner, after the runtime, the memory and the role
    # are gone (invariant 22). A `202` says exactly that: accepted, not finished.
    #
    # The GitHub authorisation held in the token vault under this `configId` is *not* torn
    # down here and does not need to be: it is keyed on an id that will never be issued
    # again, so it is unreachable the moment the item is gone. What revokes credentials
    # wholesale is `DELETE /github-app`, which deletes the provider and with it every
    # authorisation in its vault entry.
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
def github_authorize(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Start -- or restart -- the 3LO leg that gives this repository a GitHub credential.

    Returns either `authorizationUrl` for the console to open, or `authorized: true` when the
    vault already holds a live authorisation for this configuration and `force` was not
    asked for. One route for both because `GetResourceOauth2Token` answers both from one
    call, and asking separately would be a second round trip that could disagree with the
    first.

    `consoleOrigin` is where AgentCore returns the operator's browser once GitHub is done,
    and it has to be one of the URLs registered on the workload identity -- which is why the
    console sends its own origin here and `github_app.connect` registered it. An
    unregistered one is refused by the service, which is the check that stops a caller
    redirecting an authorisation somewhere it chose.

    No `READY` gate. Authorising is independent of provisioning and takes seconds where
    provisioning takes 8-12 minutes, so the two deliberately run in parallel: an operator
    who had to wait for `READY` first would spend that wait unable to do the one thing left
    to do.
    """
    config_id = path_param(event, "configId")
    body = parse_body(event)
    config = _load_config(config_id)
    (console_origin,) = require(body, "consoleOrigin")
    if not github_app.is_connected():
        raise ApiError(
            409,
            "GitHubAppNotConnected",
            "no GitHub App is connected to this deployment yet; connect one first",
            configId=config_id,
        )
    answer = github_app.resolve_token(
        config_id,
        return_url=github_app.require_https(str(console_origin), "consoleOrigin"),
        force=bool(body.get("force")),
    )
    if answer["accessToken"]:
        return 200, {
            "configId": config_id,
            "authorized": True,
            "repository": config.get("repository"),
        }
    if not answer["authorizationUrl"]:
        raise ApiError(
            500,
            "NoAuthorizationUrl",
            "AgentCore returned neither a token nor an authorization URL",
            configId=config_id,
        )
    return 200, {
        "configId": config_id,
        "authorized": False,
        "authorizationUrl": answer["authorizationUrl"],
        "repository": config.get("repository"),
    }


@handler
def github_authorize_complete(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Finish the 3LO leg the operator has just approved, and report the result.

    This route exists because the flow does not finish itself. AgentCore returns the browser
    to the console with `?session_id=<sessionUri>` and **no error**, and the vault holds
    nothing until that session is completed -- so without this call every visible signal says
    the authorisation worked while `github_status` answers "not authorised" indefinitely.

    The verdict is re-read rather than assumed, so the console shows what the vault actually
    holds: a completion that succeeded and produced no token is a state worth seeing rather
    than one worth reporting as success.
    """
    config_id = path_param(event, "configId")
    body = parse_body(event)
    (session_uri,) = require(body, "sessionUri")
    _load_config(config_id)

    try:
        github_app.complete_authorization(config_id, str(session_uri))
    except ClientError as err:
        # An expired or already-spent session is the ordinary way this fails -- the operator
        # reloaded, or came back twice -- and it is a 400 naming the action rather than a 500,
        # because the fix is to authorise again and nothing is wrong with the deployment.
        raise ApiError(
            400,
            "AuthorizationNotCompleted",
            "that authorisation could not be completed; it is single-use and short-lived, so "
            "press Authorise GitHub again. "
            f"AgentCore says: {err.response.get('Error', {}).get('Message') or err}",
            configId=config_id,
        ) from err

    answer = github_app.resolve_token(config_id)
    return 200, {"configId": config_id, "authorized": bool(answer["accessToken"])}


@handler
def github_status(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Is this repository authorised, and can that authorisation actually push to it?

    Two questions, because an operator can get the first right and the second wrong -- the
    app is authorised account-wide but installed per repository, and forgetting to install
    it on *this* one is the single most likely mistake in the whole setup. `_push_verdict`
    settles it with a real write, and a refusal carries GitHub's own sentence plus the
    install URL.

    `canPush` is deliberately three-valued: `true`, `false`, or `null` when GitHub could not
    be reached. A GitHub incident must not make a correctly configured repository look
    broken.

    This route holds a live GitHub token in memory for the duration of the probe and returns
    **no part of it** -- not a prefix, not a length. The response is a verdict.
    """
    config_id = path_param(event, "configId")
    config = _load_config(config_id)
    repository = str(config.get("repository") or "")
    if not github_app.is_connected():
        return 200, {
            "configId": config_id,
            "repository": repository or None,
            "authorized": False,
            "reason": "no GitHub App is connected to this deployment",
            "canPush": None,
        }
    # No `return_url`, no `force`: this is a read, and passing a return URL would make a
    # status check capable of starting an authorisation.
    answer = github_app.resolve_token(config_id)
    token = answer["accessToken"]
    if not token:
        return 200, {
            "configId": config_id,
            "repository": repository or None,
            "authorized": False,
            "reason": "this repository has not been authorised yet",
            "canPush": None,
        }
    verdict = _push_verdict(repository, token) if repository else {"canPush": None, "reason": ""}
    return 200, {
        "configId": config_id,
        "repository": repository or None,
        "authorized": True,
        **verdict,
        # Only when the answer is a definite no: an install link beside a working repository
        # is an invitation to change something that is already right.
        "installUrl": github_app.install_url() if verdict.get("canPush") is False else None,
    }


# The one definition of this module's routes. `infra/stacks/api_stack.py` wires them up and
# `tests/test_api_route_contract.py` holds the two in agreement, so a route added here and
# not there fails a test rather than answering 404 the first time a console asks for it.
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/configurations", "create"),
    ("GET", "/configurations", "list_"),
    ("GET", "/configurations/{configId}", "get"),
    ("PATCH", "/configurations/{configId}", "patch"),
    ("DELETE", "/configurations/{configId}", "delete"),
    ("POST", "/configurations/{configId}/runtime-update", "runtime_update"),
    ("POST", "/configurations/{configId}/github-authorize", "github_authorize"),
    (
        "POST",
        "/configurations/{configId}/github-authorize/complete",
        "github_authorize_complete",
    ),
    ("GET", "/configurations/{configId}/github-status", "github_status"),
    ("GET", "/configurations/{configId}/memory", "memory"),
)
