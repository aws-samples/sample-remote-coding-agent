"""The provisioning Lambda: four phases over the provision queue.

`CREATE` mints the per-configuration execution role and the AgentCore runtime, `CHECK`
polls the runtime until it is `READY`, `UPDATE` rolls a live runtime onto current fleet
policy, and `DELETE` tears the whole configuration down. The entry point is an
**undecorated** `lambda_handler` -- there is no HTTP route here, and
`tests/test_api_route_contract.py` asserts exactly that.

The expensive lessons in this module are commented where they live. Two of them shape
whole functions:

* `UpdateAgentRuntime` is a **full replace**. **Every** member not sent is stripped from a
  live runtime, not just the ones named here; `environmentVariables`,
  `lifecycleConfiguration` and `filesystemConfigurations` are the three whose loss is
  *silent* -- the runtime then starts and fails with no signal, or quietly loses its
  persistent workspace (invariant 24). So `_update` reads the live runtime back and
  `_desired_runtime` classifies every field as *carry forward* or *converge*.
  `networkConfiguration` converges in one direction only (invariant 30): `_fleet_network`
  is a ratchet, so a deploy that arrives with no `AGENT_SUBNET_IDS` cannot move a live
  VPC runtime back to public egress.
* IAM is eventually consistent, so a `CreateAgentRuntime` moments after `CreateRole` fails
  on a role the service cannot assume yet. That is retried as a *new* SQS message so the
  receive count resets instead of burning the redrive budget on a healthy configuration
  (invariant 15), inside a wall-clock budget -- `_TRANSIENT_BUDGET_SECONDS`, 90 seconds --
  rather than a count of attempts nobody can read as a duration. What may be retried at all
  is an **allowlist**, `_TRANSIENT_CREATE_FAILURES`: an error code, the operation that
  produced it, and for propagation a closed set of pinned message shapes. An *unrecognised*
  message out of `CreateAgentRuntime` gets exactly one retry -- the allowlist's floor -- and
  is then terminal with the vendor's own words in `statusReason`; everything else is
  terminal on its first attempt. The floor is there because a shape nobody had enumerated
  yet must cost one slow success, never the ability to provision at all.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from typing import Any

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from . import _common, github_app, observability
from ._common import LOG

PHASES = ("CREATE", "CHECK", "DELETE", "UPDATE")

# Exactly one level under `/mnt` -- `MountPath`'s own pattern is `/mnt/[a-zA-Z0-9._-]+/?`
# and botocore does not enforce a `pattern` client-side, so `/mnt/a/b` is a bare
# server-side `ValidationException`. `observability.is_valid_mount_path` is the guard.
MOUNT_PATH = "/mnt/workspace"

# 8 hours. The service default is 2, which is insufficient for tasks that include a
# verification step: a run that reaches the review node after a long verify would be
# killed mid-session with no signal from the container.
MAX_LIFETIME_SECONDS = 28800

# Under the mount, not beside it: the work root has to survive a resume, and only the
# session-storage mount does. Forwarded as an environment variable rather than baked
# into the image because a Dockerfile `ENV` cannot be *unset* by a runtime's
# `environmentVariables`, only overridden (invariant 26).
WORK_ROOT = "/mnt/workspace/work"

SERVER_PROTOCOL = "HTTP"

# This deployment is V2 only, and that is a constant rather than a knob on purpose. A
# platform version is not a per-deployment taste: V1 and V2 differ in how a container is
# *started*, so supporting both would mean the 120-second health requirement below holds on
# one path and not the other, and every failure report would have to say which.
#
# V2 starts the agent from a snapshot instead of initialising on each cold start. The
# snapshot path has substantially lower cold-start latency than V1 for images of this
# size (~1.3 GB), so cold start -- paid on every dispatched task -- is most of what this
# buys.
#
# Sent on **every** create and update, never conditionally, because the service's omission
# semantics are asymmetric: omitted on create means V1, and omitted on *update* means "keep
# whatever this runtime has". A runtime created before this constant existed therefore
# reports no value at all, which is V1, and converges here on its next update
# (invariant 24) -- see `_matches_intent`, which is where absence is read as V1 so that the
# convergence happens exactly once per runtime rather than never.
#
# Three consequences, none optional:
#
# * The container must answer `/ping` healthy within **120 seconds**, because the snapshot
#   is taken on the first healthy ping. Provisioning then takes several minutes for the
#   snapshot to complete; `_check` polls, so that is already the
#   shape this code expects.
# * An update or delete issued before a runtime reaches a terminal state raises
#   `ConflictException`. `_update` lets it raise, so the SQS redelivery is the retry --
#   deliberately not the `_TRANSIENT_CREATE_FAILURES` budget, which speaks only for
#   `CreateAgentRuntime` (`_TRANSIENT_OPERATION`).
#
# **V2 exists in us-east-1, us-east-2, us-west-2, eu-west-1 and ap-northeast-1 only**, so
# this deployment does too. `CreateAgentRuntime` in another region does not reject the
# parameter -- it accepts it and gives you a V1 runtime -- so nothing at run time would say
# the region was wrong. `make preflight` refuses it, unconditionally, because there is no
# longer a configuration under which a non-V2 region is acceptable.
PLATFORM_VERSION = "V2"

# Every role this Lambda mints lives under one path, which is one of the three fences
# on its own `iam:CreateRole` grant (invariant 17: path, boundary, and a `PassRole`
# limited to `bedrock-agentcore.amazonaws.com`).
ROLE_PATH = "/rca/"
_POLICY_NAME = "rca-execution"

# One AgentCore Memory per registered repository. 90 days is inside `eventExpiryDuration`'s
# own `3..365` range; module-scope literals rather than environment variables, deliberately,
# because `tests/test_grant_contract.py` pins this module's module-scope environment reads
# against what `ApiStack` forwards and a new one fails a test in a file this change does not
# own.
_MEMORY_EVENT_EXPIRY_DAYS = 90
EPISODIC_STRATEGY_NAME = "repository_episodic"
SESSION_STRATEGY_NAME = "session_summary"
# The namespace templates, and the container's half of the contract: `{actorId}` is the
# configuration id and `{sessionId}` is the task id, so the first is **one** namespace shared
# by every session of this repository -- what it has learned across tasks -- and the second is
# one namespace per task. This module owns only the templates; the keying is the container's.
EPISODIC_NAMESPACE = "/rca/{actorId}/episodic"
SESSION_NAMESPACE = "/rca/{actorId}/session/{sessionId}"

MAX_CHECK_ATTEMPTS = 60
CHECK_DELAY_SECONDS = 30

# Fleet environment variables this code used to send and now actively **removes** from every
# runtime it updates. A tombstone list, and it exists because deletion is not the inverse of
# addition here: `_desired_runtime` merges `{**create-time, **live, **fleet}`, so a key
# dropped from `_fleet_environment` merely stops being converged and the live value is carried
# forward forever (invariant 24's carry-forward rule, working exactly as intended and against
# you). An entry here is the only thing that takes a variable off a runtime that already has
# it.
#
# These five configure an OTLP exporter, which the container does not run (invariant 32b). `CLAUDE_CODE_ENABLE_TELEMETRY` is **not**
# here: it is still fleet policy, pinned `"0"`, so it converges by being sent rather than
# by being deleted.
#
# An entry is safe to delete from this list only once no live runtime can still carry the key,
# which in practice means never: a runtime created before the retirement outlives the deploy
# that retired it. Leaving them costs one `pop` each.
RETIRED_FLEET_KEYS = frozenset(
    {
        "OTEL_METRICS_EXPORTER",
        "OTEL_LOGS_EXPORTER",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_METRIC_EXPORT_INTERVAL",
    }
)

# The transient-failure budget, in wall-clock seconds: "IAM gets 90 seconds". A count of
# attempts says nothing about how long an operator waits -- the ten geometric sleeps this
# replaces were 26 minutes -- and the number an operator needs is a duration.
_TRANSIENT_BUDGET_SECONDS = 90
# A fixed step rather than a doubling one: over a 90s budget a geometric backoff spends
# most of it asleep in a single long delay, and IAM propagation is seconds.
_RETRY_DELAY_SECONDS = 15
# The furthest-out deadline a message body may claim. `retryDeadline` travels in a message
# this module also writes, so a replayed or hand-edited body must not buy an unbounded
# retry loop. Not an environment variable, deliberately: `tests/test_grant_contract.py`
# pins this module's module-scope environment reads against what `ApiStack` forwards, so a
# new one fails a test in a file this change does not own.
_MAX_TRANSIENT_BUDGET_SECONDS = 300

# The five configuration statuses this module writes. `configurations.CONFIG_STATUSES`
# is the published vocabulary and carries `PENDING` too, but it cannot be imported here
# -- `configurations` imports `enqueue` from this module -- so the strings are spelled
# twice on purpose and `tests/test_api_configurations.py` pins the subset relation.
CREATING, READY, UPDATING, FAILED, DELETING = "CREATING", "READY", "UPDATING", "FAILED", "DELETING"

# AgentCore's own runtime statuses, as `GetAgentRuntime` reports them.
_RUNTIME_PENDING = ("CREATING", "UPDATING")
_RUNTIME_FAILED = ("CREATE_FAILED", "UPDATE_FAILED")


def _csv_environment(name: str) -> list[str]:
    """A comma-separated environment variable as a list, tolerating spaces and an empty
    value. Every lookup in this module carries a default so an import with nothing set
    cannot raise -- a missing variable must read as a missing grant, not a broken
    bundle."""
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


AGENT_IMAGE_URI = os.environ.get("AGENT_IMAGE_URI", "")
EXECUTION_ROLE_BOUNDARY_ARN = os.environ.get("EXECUTION_ROLE_BOUNDARY_ARN", "")
AGENT_SUBNET_IDS = _csv_environment("AGENT_SUBNET_IDS")
AGENT_SECURITY_GROUP_IDS = _csv_environment("AGENT_SECURITY_GROUP_IDS")
PLAN_MODEL_ID = os.environ.get("PLAN_MODEL_ID", "")
CODE_MODEL_ID = os.environ.get("CODE_MODEL_ID", "")
HAIKU_MODEL_ID = os.environ.get("HAIKU_MODEL_ID", "")

# Defaults to the code model rather than to "" deliberately. `config.load_config` runs
# `validate_model` on this value, and an EMPTY string is *present*, so the container's
# own `os.environ.get("ANTHROPIC_FALLBACK_MODEL", "sonnet")` default does not apply --
# the container would die at start-up on a variable nobody set. `"sonnet"` matches
# `MODEL_RE` and is the same default `load_config` carries, so what is forwarded is
# never invalid.
FALLBACK_MODEL_ID = os.environ.get("FALLBACK_MODEL_ID", "") or CODE_MODEL_ID or "sonnet"

# Normalised to "1"/"0" rather than forwarded raw: `config._env_flag` treats an empty
# value as *unset* and falls back to its own default, so forwarding "" would put a
# variable on the runtime that says nothing. "1"/"0" is a value a `GetAgentRuntime` diff
# can be read against.
CODE_GOAL_ENABLED = (
    "0" if os.environ.get("CODE_GOAL_ENABLED", "").strip().lower() in ("0", "false", "no") else "1"
)

# The commit identity every run's pull request carries, forwarded raw rather than
# normalised: unlike the two flags above these are free text, and `git_ops._identity`
# already treats "" as unset and falls back to its own default. Fleet policy because there
# is one identity per deployment -- see `agent/src/coding_agent/git_ops.py`, which explains
# what the default costs: `agent@localhost` resolves to no GitHub account, so GitHub reports
# no author and a check gating on commit attribution (a Vercel preview, a DCO policy) blocks
# the pull request with nothing in the diff to explain it.
AGENT_GIT_AUTHOR_NAME = os.environ.get("AGENT_GIT_AUTHOR_NAME", "").strip()
AGENT_GIT_AUTHOR_EMAIL = os.environ.get("AGENT_GIT_AUTHOR_EMAIL", "").strip()


def enqueue(
    config_id: str, phase: str, *, attempt: int = 0, delay: int = 0, deadline: int | None = None
) -> None:
    """The single owner of the provision message shape, `{"phase", "configId",
    "attempt"}`. `configurations.py` calls this rather than building a body of its own,
    for the same reason `runs.queue_run` exists: two writers of one message shape drift.

    Plus an optional `retryDeadline`, the wall-clock instant the transient budget in
    `_retry_create` runs out, and written **only** when a caller passes one. Omitted
    entirely otherwise, because `tests/test_api_configurations.py` and
    `tests/test_api_provisioner.py` assert these bodies whole and an unconditional member
    would break every one of those assertions.
    """
    if phase not in PHASES:
        raise ValueError(
            f"{phase} is not a provisioning phase; expected one of {', '.join(PHASES)}"
        )
    body: dict[str, Any] = {"phase": phase, "configId": config_id, "attempt": attempt}
    if deadline is not None:
        body["retryDeadline"] = int(deadline)
    _common.sqs.send_message(
        QueueUrl=_common.PROVISION_QUEUE_URL,
        MessageBody=json.dumps(body),
        # SQS caps a per-message delay at 900s; clamping here keeps a future backoff
        # change from turning into an `InvalidParameterValue` nobody expects.
        DelaySeconds=max(0, min(int(delay), 900)),
    )
    LOG.info("queued %s config=%s attempt=%s delay=%s", phase, config_id, attempt, delay)


# -- configuration item access ----------------------------------------------------


def _get_config(config_id: str) -> dict[str, Any] | None:
    return _common.config_table.get_item(Key={"configId": config_id}).get("Item")


def _set_config(config_id: str, **fields: Any) -> None:
    """Update the configuration item, conditional on it still existing.

    The condition is invariant 23: `update_item` *creates* an absent item, so a late
    `CHECK` landing after a successful teardown would resurrect a configuration whose
    runtime, role and memory are all gone -- and would even flip it to `READY`. A lost
    condition is therefore expected, not an error.
    """
    fields["updatedAt"] = _common.now_iso()
    names = {f"#f{index}": key for index, key in enumerate(fields)}
    values = {f":v{index}": value for index, value in enumerate(fields.values())}
    try:
        _common.config_table.update_item(
            Key={"configId": config_id},
            UpdateExpression="SET "
            + ", ".join(f"{n} = {v}" for n, v in zip(names, values, strict=True)),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression=Attr("configId").exists(),
        )
    except ClientError as err:
        if not _common.is_condition_failure(err):
            raise
        LOG.info("config %s no longer exists; dropped its %s update", config_id, sorted(fields))


def _live_config(config_id: str, phase: str) -> dict[str, Any] | None:
    """The configuration item, or `None` when this phase has nothing left to do.

    The other half of invariant 23: a message for a configuration that is gone, or on
    its way out, is finished work. Without it a late `CHECK` landing after a successful
    teardown resurrects a configuration whose runtime, role and memory are all gone --
    and flips it to `READY`.
    """
    config = _get_config(config_id)
    if config is None:
        LOG.info("%s for config %s: the item is gone, nothing to do", phase, config_id)
        return None
    if config.get("status") == DELETING:
        LOG.info("%s for config %s: it is DELETING, nothing to do", phase, config_id)
        return None
    return config


# -- naming and identity ----------------------------------------------------------


def _runtime_name(config_id: str) -> str:
    """`AgentRuntimeName`'s pattern is `[a-zA-Z][a-zA-Z0-9_]{0,47}`: **no hyphens**, and
    48 characters at most. Passing a `cfg-...`-shaped id straight through is a
    `ValidationException` at create time that reads like a service problem rather than
    the client bug it is."""
    return f"rca_{re.sub(r'[^a-zA-Z0-9]', '_', config_id)}"[:48]


def _memory_name(config_id: str) -> str:
    """`rca_mem_<configId>`, sanitised and truncated exactly as `_runtime_name` is:
    `CreateMemory`'s `name` pattern is `[a-zA-Z][a-zA-Z0-9_]{0,47}` too, so a hyphen in a
    config id is a server-side `ValidationException` that reads like a service problem
    rather than the client bug it is. A pure function of the id, so teardown never needs a
    stored name."""
    return f"rca_mem_{re.sub(r'[^a-zA-Z0-9]', '_', config_id)}"[:48]


def _role_name(config_id: str) -> str:
    return f"rca-{config_id}"


def _account_and_region(_config: Mapping[str, Any] | None = None) -> tuple[str, str]:
    """The account and region for the trust policy's `aws:SourceAccount` and the ARNs in
    `_execution_role_policy`, read from this Lambda's own environment.

    Deliberately **not** `sts:GetCallerIdentity`: this Lambda's grant surface is exactly
    the table `infra/stacks/api_stack.py` declares, and an STS client would widen it for a
    value the deploy already knows at synth time. Same reasoning keeps `_ensure_role` off
    `iam:GetRole`.

    `AWS_ACCOUNT_ID` is forwarded by `_provisioner_environment` rather than derived:
    the runtime ARN and role ARN do not exist yet at the point `_create` needs this, and
    `sts:GetCallerIdentity` would widen the Lambda's grant surface for a value the deploy
    already carries. `*` and `AWS_REGION` stay as the fallbacks, so a local run with no
    environment still produces a policy IAM will accept.

    The parameter is kept, unused, so that every call site reads the same and a future value
    that genuinely is per configuration has somewhere to come from.
    """
    account = os.environ.get("AWS_ACCOUNT_ID", "").strip()
    return account or "*", os.environ.get("AWS_REGION", "") or "*"


def _asset_repository_arn(image_uri: str) -> str:
    """The ECR repository ARN the agent image lives in, read off `AGENT_IMAGE_URI`
    (`<account>.dkr.ecr.<region>.amazonaws.com/<repository>[:<tag>|@<digest>]`) in the
    same spirit as `_account_and_region` reading its account out of the environment: the
    value is derivable from something every deploy already carries, so it needs no second
    environment variable and no `ecr:DescribeRepositories` grant.

    `""` for an empty URI, and for anything that is not an ECR URI -- a Docker Hub image
    must not yield a nonsense ARN, and `tests/test_api_grant_contract.py` builds policies
    with no fleet environment at all. **Never raises**: a raise here would break `CREATE`,
    `UPDATE` and the re-provision path at once, where returning `""` only degrades to the
    grant this file carried before the image pull was on the list.
    """
    host, _, path = image_uri.partition("/")
    if not host or not path:
        return ""
    parts = host.split(".")
    if not (len(parts) > 4 and parts[1] == "dkr" and parts[2] == "ecr"):
        return ""
    account, region = parts[0], parts[3]
    # Digest first, then tag -- and by `rpartition`, since a repository name may itself
    # contain `/` (`cdk-hnb659fds-container-assets-.../team/rca`) and a bare split on `:`
    # over the whole path would cut the wrong place.
    path = path.split("@", 1)[0]
    head, sep, tail = path.rpartition(":")
    repository = head if sep and "/" not in tail else path
    if not repository:
        return ""
    return f"arn:aws:ecr:{region}:{account}:repository/{repository}"


# -- IAM --------------------------------------------------------------------------


def _trust_policy(account: str) -> dict[str, Any]:
    statement: dict[str, Any] = {
        "Effect": "Allow",
        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }
    if account != "*":
        # The confused-deputy fence. Omitted rather than written as `*` when the account
        # is unknown: `StringEquals` treats `*` as a literal, so it would deny every
        # assume instead of allowing any.
        statement["Condition"] = {"StringEquals": {"aws:SourceAccount": account}}
    return {"Version": "2012-10-17", "Statement": [statement]}


def _execution_role_policy(config: Mapping[str, Any]) -> dict[str, Any]:
    """The per-configuration inline policy, and the thing
    `tests/test_api_grant_contract.py` reads.

    The fence is negative and is the part worth stating: nothing here carries `iam:*`,
    `bedrock-agentcore:*` or `s3:*`, and every statement is scoped to one resource or to
    this configuration's own partition key.

    What it grants is the `Sid` list in the `Statement` block below. That list is the
    authority -- a prose summary here would lag it, so: read the `Sid`s.

    The `AuthenticateToEcr`/`PullAgentImage` pair is not the container's own call -- the
    **runtime** pulls `AGENT_IMAGE_URI` using this role, before any container code runs, and
    `CreateAgentRuntime` pre-validates it. Without the two ECR statements the create
    fails `AccessDenied` with a **null** `errorMessage` in CloudTrail,
    `CreateWorkloadIdentity` is never reached and no runtime is ever created, which is
    exactly how this omission survived: the failure names nothing.

    The matching ceiling is `agent_stack.execution_role_boundary_statements`, which is
    the union of what this grants and no wider -- an action allowed here and omitted
    there is silently denied at runtime, so if either moves, both move. Same rule the
    `InvokeClaude` comment states for the model ARN.
    """
    config_id = str(config["configId"])
    account, region = _account_and_region(config)
    repository_arn = _asset_repository_arn(AGENT_IMAGE_URI)
    # Omitted entirely when the image URI is absent or unparseable: a runtime cannot be
    # created without an image anyway, so an auth statement with nothing to authenticate
    # against would be surface for free.
    image_pull: list[dict[str, Any]] = (
        [
            {
                "Sid": "AuthenticateToEcr",
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                # `*` is what the action *requires*, not laziness: it supports no
                # resource types at all, so a scoped ARN here matches no authorization
                # and the pull fails. The token it returns grants nothing on its own --
                # it still authorizes against `PullAgentImage`, which names one
                # repository.
                "Resource": "*",
            },
            {
                "Sid": "PullAgentImage",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                # The repository `AGENT_IMAGE_URI` names, and no other. Layers themselves
                # come down over a presigned S3 URL, which is why nothing here needs
                # `s3:`.
                "Resource": repository_arn,
            },
        ]
        if repository_arn
        else []
    )
    # The same shape as `image_pull` above, gated per configuration rather than per fleet: a configuration
    # whose `CreateMemory` failed carries no `memoryId` and gets no statement at all, which
    # is the control-plane half of the container's no-op path -- a memory that could not be
    # made costs the repository its memory and nothing else.
    #
    # Exactly six actions, and the omissions are the point: `DeleteEvent`,
    # `DeleteMemoryRecord`, `BatchDeleteMemoryRecords`, `CreateMemory`, `UpdateMemory`,
    # `DeleteMemory` and `ListMemories` are **deliberately absent**. A prompt-injected session
    # must be able to add to and read this repository's shared memory, never to destroy it and
    # never to enumerate another repository's -- which is also why the resource is the one
    # memory's **literal** ARN (invariant 31's division of labour: the per-repository fence
    # lives here). The matching ceiling is
    # `agent_stack.execution_role_boundary_statements`' `MayUseOwnMemory`, which takes
    # `memory/*` because the boundary is minted once per deploy, before any memory exists; if
    # either moves, both move.
    memory_id = str(config.get("memoryId") or "")
    use_memory: list[dict[str, Any]] = (
        [
            {
                "Sid": "UseOwnMemory",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                    "bedrock-agentcore:GetMemoryRecord",
                ],
                "Resource": f"arn:aws:bedrock-agentcore:{region}:{account}:memory/{memory_id}",
            }
        ]
        if memory_id
        else []
    )
    # **Refreshing the GitHub token reads the app's client secret as this role.** AgentCore
    # Identity performs that Secrets Manager read with the caller's permissions, so a container
    # without this grant can use a token for its initial lifetime but cannot renew it. Without
    # it, watchdog resumes and runs that outlast an 8-hour token would fail at `submit`.
    #
    # One literal ARN, `github_app.provider_secret_arn()`, and never the
    # `bedrock-agentcore-identity!*` prefix the boundary allows: the prefix covers every
    # credential provider's secret in the account. The ARN is the same for every repository,
    # because every repository shares one GitHub App -- there is no per-repository secret to
    # name. Omitted, like `UseOwnMemory`, when there is nothing to name yet.
    github_secret_arn = str(config.get("githubSecretArn") or "")
    refresh_credential: list[dict[str, Any]] = (
        [
            {
                "Sid": "RefreshGitHubCredential",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": github_secret_arn,
            }
        ]
        if github_secret_arn
        else []
    )
    configurations_arn = f"arn:aws:dynamodb:{region}:{account}:table/{_common.config_table.name}"
    tasks_arn = f"arn:aws:dynamodb:{region}:{account}:table/{_common.tasks_table.name}"
    # Invariant 31 / Tier 1 #7 and #8: both table statements are fenced to this
    # configuration's own partition key. Without it a compromised container can read --
    # and on Tasks write -- every other repository's rows, and editing another
    # configuration's `verifyCommand` is remote code execution in that container.
    own_partition = {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [config_id]}}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeClaude",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": [
                    # Not region-scoped, on purpose (invariant 21): a `global.` inference
                    # profile resolves to a foundation model in whichever region serves
                    # the request, so a region here denies the call from every other one.
                    # A residency-constrained deployment that pins a geo-scoped profile
                    # (`us.`/`eu.`/`apac.`) instead can region-scope this ARN, so
                    # residency and least privilege point the same way -- see CLAUDE.md
                    # "Data residency".
                    "arn:aws:bedrock:*::foundation-model/anthropic.*",
                    f"arn:aws:bedrock:*:{account}:inference-profile/*",
                ],
            },
            {
                "Sid": "ReadOwnConfiguration",
                "Effect": "Allow",
                # `GetItem` only: the container never writes a configuration item.
                "Action": ["dynamodb:GetItem"],
                "Resource": configurations_arn,
                "Condition": own_partition,
            },
            {
                "Sid": "OwnTasks",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:Query",
                ],
                "Resource": tasks_arn,
                "Condition": own_partition,
            },
            {
                "Sid": "UseGitHubCredential",
                "Effect": "Allow",
                # The two calls `agent/src/coding_agent/store.github_token` makes, and the
                # whole of this container's credential path: mint a workload access token
                # for this configuration's id as the 3LO `userId`, then exchange it for the
                # GitHub token AgentCore Identity holds in its vault. The Secrets Manager
                # read that a *refresh* needs is the separate `RefreshGitHubCredential`
                # statement below, because it can name one literal resource and this one
                # cannot.
                "Action": [
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                    "bedrock-agentcore:GetResourceOauth2Token",
                ],
                # **Both resources are as narrow as the service permits, and neither is a
                # fence.** `GetWorkloadAccessTokenForUserId` authorises only on
                # `workload-identity-directory/*`; `GetResourceOauth2Token` authorises only on
                # `*`. There is no condition key for the user id. So unlike `UseOwnMemory`
                # beside it, this statement cannot name one repository's credential.
                #
                # What bounds it instead is stated in `docs/security.md`: a container knows
                # exactly one `configId` -- its own, because invariant 31 lets it read exactly
                # one configuration item -- and the GitHub App's installation list is what
                # bounds the token's reach on GitHub. Do not reword this into a claim of
                # least privilege it does not have.
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/*",
                    "*",
                ],
            },
            *refresh_credential,
            {
                "Sid": "WriteRuntimeLogs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup"],
                # Tier 1 #9: scoped to the runtime log-group prefix rather than the
                # account's whole log-group namespace. The name prefix is the fence, so
                # wildcarding region and account costs nothing here.
                "Resource": f"arn:aws:logs:*:*:log-group:{observability.LOG_GROUP_PREFIX}/*",
            },
            {
                "Sid": "WriteRuntimeLogStreams",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:*:*:log-group:{observability.LOG_GROUP_PREFIX}/*:log-stream:*",
            },
            *image_pull,
            *use_memory,
        ],
    }


def _role_kwargs(config_id: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """`create_role`'s arguments, as a value a test can read without calling IAM."""
    account, _region = _account_and_region(config)
    kwargs: dict[str, Any] = {
        "RoleName": _role_name(config_id),
        "Path": ROLE_PATH,
        "AssumeRolePolicyDocument": json.dumps(_trust_policy(account)),
        "Description": f"AgentCore execution role for {config.get('repository') or config_id}",
        # Invariant 16: `Tags` on `CreateRole` triggers a separate `TagRole`
        # authorization whose context has no `iam:PermissionsBoundary` key, so the
        # matching grant in `infra/` must not carry that condition on `iam:TagRole` --
        # or this whole call is denied.
        "Tags": [
            {"Key": "rca:configId", "Value": config_id},
            {"Key": "rca:repository", "Value": str(config.get("repository") or config_id)},
        ],
    }
    if EXECUTION_ROLE_BOUNDARY_ARN:
        # Invariant 17: the boundary is what stops this Lambda minting a role wider than
        # its own. Omitted when unset so a local run still works.
        kwargs["PermissionsBoundary"] = EXECUTION_ROLE_BOUNDARY_ARN
    return kwargs


def _ensure_memory(config_id: str, config: Mapping[str, Any]) -> str:
    """The one AgentCore Memory this repository owns, as an id, or `""`.

    Called before `_ensure_role`, because `_execution_role_policy`'s `UseOwnMemory` names
    the memory's **exact** ARN and `_ensure_role` writes that policy on every provision.

    Idempotent the same way `_ensure_role` is, and for the same reason: a `CREATE` retried
    inside the transient budget (invariant 15) re-enters `_create` with the *same*
    configuration item, which by then carries a `memoryId`. A second `CreateMemory` on that
    name answers `ConflictException`, this function would return `""`, and the single
    `_set_config` write at the end of `_create` would **overwrite a live memory id with an
    empty string** -- silently un-granting `UseOwnMemory` on the next provision. So the
    stored id wins, and `clientToken` is the second line of defence rather than the only one.

    A `ClientError` is a warning and `""`, never a raise: `_create`'s contract is that every
    arm returns, and a memory `ValidationException` reaching its `except ClientError` would
    fall past the transient allowlist -- which is gated on `CreateAgentRuntime` -- into the
    terminal arm and call the configuration `FAILED` because a *memory* could not be made.
    The container degrades to a no-op when `memoryId` is absent, so this swallow is what
    keeps a memory failure from costing the operator a working repository.
    """
    stored = str(config.get("memoryId") or "")
    if stored:
        LOG.info("config %s already has memory %s; reusing it", config_id, stored)
        return stored
    repository = str(config.get("repository") or config_id)
    try:
        created = _common.agentcore_control().create_memory(
            name=_memory_name(config_id),
            description=f"AgentCore memory for {repository}",
            eventExpiryDuration=_MEMORY_EVENT_EXPIRY_DAYS,
            # So a transient retry inside the service's idempotency window does not mint a
            # second memory for one configuration.
            clientToken=config_id,
            # A **map**, not `create_role`'s list of `{"Key": ..., "Value": ...}` pairs:
            # `CreateMemory`'s `tags` member really is a map in the service model, so the
            # content matches `_role_kwargs`' tagging while the shape deliberately does not.
            tags={"rca:configId": config_id, "rca:repository": repository},
            memoryStrategies=[
                {
                    "semanticMemoryStrategy": {
                        "name": EPISODIC_STRATEGY_NAME,
                        "description": (
                            "What this repository has learned across every task: one namespace "
                            "shared by all of its sessions."
                        ),
                        "namespaces": [EPISODIC_NAMESPACE],
                    }
                },
                {
                    "summaryMemoryStrategy": {
                        "name": SESSION_STRATEGY_NAME,
                        "description": (
                            "One rolling summary per task session, so a resumed task reads back "
                            "what its own run has already done."
                        ),
                        "namespaces": [SESSION_NAMESPACE],
                    }
                },
            ],
        )
    except ClientError as err:
        LOG.warning(
            "config %s: no memory was created (%s); the repository gets none", config_id, err
        )
        return ""
    # Chained `.get`, not `created["memory"]["id"]`: a `KeyError` here is outside every
    # `except ClientError` in this file and would break `_create`'s never-raises contract.
    memory_id = str((created.get("memory") or {}).get("id") or "")
    LOG.info("config %s: created memory %s", config_id, memory_id)
    return memory_id


def _ensure_role(config_id: str, config: Mapping[str, Any]) -> str:
    """Idempotent: the role may already exist from an attempt whose
    `CreateAgentRuntime` failed, and the inline policy is rewritten every time so a
    fleet-wide policy change lands on a re-provision."""
    role_name = _role_name(config_id)
    account, _region = _account_and_region(config)
    role_arn = f"arn:aws:iam::{account}:role{ROLE_PATH}{role_name}"
    try:
        role_arn = _common.iam.create_role(**_role_kwargs(config_id, config))["Role"]["Arn"]
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "EntityAlreadyExists":
            raise
        # Derived rather than fetched: see `_account_and_region` on why no `iam:GetRole`
        # grant is added for a string this code can already spell.
        LOG.info("execution role %s already exists; reusing %s", role_name, role_arn)
    # Resolved here rather than stored on the configuration item: the secret belongs to the
    # deployment's one provider, so it is the same for every repository and changes only if the
    # GitHub App is reconnected -- at which point the next UPDATE rewrites every role with the
    # new ARN, because `_update` calls this before its intent check.
    policy = _execution_role_policy({**config, "githubSecretArn": github_app.provider_secret_arn()})
    _common.iam.put_role_policy(
        RoleName=role_name,
        PolicyName=_POLICY_NAME,
        PolicyDocument=json.dumps(policy),
    )
    return role_arn


_GONE_CODES = frozenset({"ResourceNotFoundException", "NoSuchEntity", "NoSuchEntityException"})


def _is_gone(err: ClientError) -> bool:
    """Whether an error means "that was already deleted". Teardown is replayed by design
    -- the item is deleted last, so a crash mid-teardown leaves a `DELETING`
    configuration the next `DELETE` message finishes -- and the second pass must not
    fail on what the first one removed."""
    return err.response.get("Error", {}).get("Code") in _GONE_CODES


# -- the desired runtime, and the carry-forward / converge classification ----------


def _fleet_network(live: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fleet policy, not carry-forward (invariant 30), and a **one-way ratchet**. Getting
    this wrong silently returns a runtime to public egress, which is invisible until
    something exfiltrates.

    `live` is required rather than defaulted to `None`: a default is exactly how a caller
    gets the downgrading behaviour back without noticing.
    """
    if AGENT_SUBNET_IDS:
        # Converged: a runtime born in a VPC that has since been re-laid-out gets the
        # current subnets, and a runtime born `PUBLIC` moves in.
        return {
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets": list(AGENT_SUBNET_IDS),
                "securityGroups": list(AGENT_SECURITY_GROUP_IDS),
            },
        }
    live_network = dict((live or {}).get("networkConfiguration") or {})
    if live_network.get("networkMode") == "VPC":
        # The asymmetry *is* invariant 30. Converging in this direction would move a live
        # fleet back to public egress on one bad deploy -- an `AGENT_SUBNET_IDS` that
        # arrived empty because a `select_subnets` returned nothing, or a hand-rolled
        # deploy -- and nothing would say so until something exfiltrated.
        LOG.warning(
            "AGENT_SUBNET_IDS is empty but the live runtime is in VPC mode; kept its "
            "network configuration rather than converging it back to public egress"
        )
        return live_network
    # No VPC on either side: a local or pre-VPC deployment still works.
    return {"networkMode": "PUBLIC"}


def _git_identity() -> tuple[str, str]:
    """The commit identity every pull request carries, as `(name, email)`.

    Three sources, in order, and the order is the point:

    1. **The deploy-time context pair** (`-c gitAuthorName=` / `-c gitAuthorEmail=`). An
       explicit choice outranks a derived one, so a deployment that wants its own bot account
       keeps it. Either value present selects this source for both, because a name from one
       source and an address from another is an identity nobody chose.
    2. **The connected GitHub App's own bot account**, which needs no operator input and is
       why the demo path configures nothing. `github_app.commit_identity` reads it off the
       settings item; the address is `<botUserId>+<slug>[bot]@users.noreply.github.com`, and
       the id is the **bot user's** rather than the app's -- see that function for the
       measurement, because the app-id form resolves to no account.
    3. **Nothing**, which leaves `git_ops` to its default `agent@localhost` -- and that
       resolves to no GitHub author, so anything gating on commit attribution refuses the
       pull request (invariant 4b).

    Fleet policy, so an existing runtime converges on the next update rather than keeping
    whatever it was born with (invariant 24). That matters more here than for the other knobs:
    the app is connected *after* the deploy, so every runtime provisioned before the connect
    was necessarily born without an identity.
    """
    if AGENT_GIT_AUTHOR_NAME or AGENT_GIT_AUTHOR_EMAIL:
        return AGENT_GIT_AUTHOR_NAME, AGENT_GIT_AUTHOR_EMAIL
    return github_app.commit_identity()


def _fleet_environment() -> dict[str, str]:
    """The environment keys this deployment owns, and therefore converges. Anything not
    named here is carried forward from the live runtime. Takes no configuration on
    purpose: a key that varies per configuration is by definition not fleet policy.

    Which is why a fleet-wide knob put in `_configuration_environment` instead is a
    silent bug: it would be *carried forward* from whatever the live runtime was born
    with rather than converged, so a rollout would quietly fail to take (invariant 24)
    and nothing short of diffing a live runtime would say so.

    The Claude Code CLI's suppression group is part of this group for exactly that reason;
    its expected values are pinned in `tests/test_env_contract.CLI_SUPPRESSIONS`, because
    deleting one is invisible at run time.
    """
    # Resolved once, and assigned to names rather than splatted in: the two keys below are
    # read out of this function's AST by
    # `tests/test_env_contract.py::test_every_container_environment_read_is_forwarded_or_a_declared_default`,
    # and a `**dict(...)` makes them invisible to it -- which is how a forwarded knob stops
    # being checked while still appearing to work.
    author_name, author_email = _git_identity()
    return {
        "WORK_ROOT": WORK_ROOT,
        "CLAUDE_CODE_USE_BEDROCK": "1",
        # The `'opus'`/`'sonnet'`/`'haiku'` aliases have to resolve to Bedrock ids, or
        # the CLI picks a default of its own and a role silently runs on the wrong model.
        "ANTHROPIC_MODEL": PLAN_MODEL_ID,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": PLAN_MODEL_ID,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": CODE_MODEL_ID,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": HAIKU_MODEL_ID,
        # Fleet policy, beside the four routing values above: one fallback for the whole
        # fleet, so a momentarily unavailable model DEGRADES a run rather than killing
        # it. A per-configuration fallback would be a second routing table, and two
        # routing tables disagree (see `config.AgentConfig.fallback_model`).
        "ANTHROPIC_FALLBACK_MODEL": FALLBACK_MODEL_ID,
        # The `/goal` on/off switch, and fleet policy for the same reason: a deployment
        # turns the loop off once, for the fleet, rather than by hand-editing a live
        # runtime.
        "CODE_GOAL_ENABLED": CODE_GOAL_ENABLED,
        # The commit identity, fleet policy so one deployment-wide value converges onto
        # every live runtime (invariant 24). Both are always sent, empty included: an empty
        # value means "use the container's default", and *omitting* the key instead would
        # leave a runtime that was born with a real identity carrying it forever, since
        # anything not named in this group is carried forward. Sending "" is what makes
        # clearing the context key actually clear the identity.
        "AGENT_GIT_AUTHOR_NAME": author_name,
        "AGENT_GIT_AUTHOR_EMAIL": author_email,
        # The CLI's suppression group, whose names and values are pinned in
        # `tests/test_env_contract.CLI_SUPPRESSIONS` -- that test is the count, not this
        # sentence. The reason it is fleet policy rather than a per-configuration default:
        # a runtime provisioned before this group existed must CONVERGE onto it on the
        # next UPDATE, and only the fleet group converges (invariant 24).
        #
        # On Bedrock the vendor telemetry and error-reporting endpoints are never
        # contacted at all -- that traffic exists only when the CLI talks to the Anthropic
        # API directly -- so these do not close a hole that is open today. They make the
        # posture EXPLICIT AND AUDITABLE in a `GetAgentRuntime` response rather than a
        # default a reader has to know about, which is why deleting one is invisible at
        # run time and is guarded by a test instead (tests/test_env_contract.py).
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_BUG_COMMAND": "1",
        "DISABLE_FEEDBACK_COMMAND": "1",
        # The one ENABLE-shaped name in the group, so its value is "false" and not "1".
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_DISABLE_ARTIFACT": "1",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
        # Not belt-and-braces: the CROSS-TENANT one. Auto-memory loads REGARDLESS of
        # `setting_sources`, and one container serves many tasks over its life, so
        # without this one task's notes can surface in another's session. Set here for
        # the container and AGAIN as a literal in `subagents.build_options`' `env`, which
        # is what holds on a local run and on a runtime that has not converged yet.
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        # The eleventh variable, and a SEPARATE mechanism from the ten above rather than
        # more of the same: those ten govern *Anthropic-bound* traffic, which on Bedrock
        # does not exist, and none of them affects OTel export at all.
        # `CLAUDE_CODE_ENABLE_TELEMETRY` is the only thing that turns the CLI's OTLP
        # exporter on, and it is pinned off because this deployment ships no OTLP receiver
        # for it to reach -- so an exporter left to a future default would retry against a
        # port nothing is listening on, and any endpoint it *did* reach would be an egress
        # destination invariant 33 has not sanctioned.
        #
        # This deployment ships no OTLP receiver. Adding an exporter requires choosing an
        # endpoint first and granting it in the VPC; do not reintroduce it as a default
        # (invariant 32b).
        #
        # Fleet policy rather than a Dockerfile `ENV` (invariant 26): an `ENV` cannot be
        # *unset* by a runtime's `environmentVariables`, only overridden. Set here for the
        # runtime and AGAIN as a literal in `subagents.build_options`' `env`, which is what
        # holds on a local run and on a runtime that has not converged. Pinned in
        # `tests/test_env_contract.CLI_SUPPRESSIONS`.
        "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
        "CONFIG_TABLE": _common.config_table.name,
        "TASKS_TABLE": _common.tasks_table.name,
    }


def _configuration_environment(config: Mapping[str, Any]) -> dict[str, str]:
    """Create-time defaults for the per-configuration keys. On an update these lose to
    whatever the live runtime carries, because the live value is the one the container
    has been using."""
    # `CONFIG_ID` alone, and it is now load-bearing twice over: it keys the container's
    # DynamoDB reads *and* it is the 3LO `userId` the GitHub credential is stored under, so
    # `store.github_token` needs no second variable.
    return {"CONFIG_ID": str(config.get("configId", ""))}


def _container_uri(spec: Mapping[str, Any]) -> str | None:
    artifact = spec.get("agentRuntimeArtifact") or {}
    return (artifact.get("containerConfiguration") or {}).get("containerUri")


def _desired_runtime(live: Mapping[str, Any] | None, config: Mapping[str, Any]) -> dict[str, Any]:
    """What the runtime should look like, given what it looks like now.

    `UpdateAgentRuntime` is a full replace, so every member below is sent on every
    update. The classification is the whole point (invariant 24):

    | field                                    | class        | value                     |
    |------------------------------------------|--------------|---------------------------|
    | `...containerConfiguration.containerUri`  | fleet        | `AGENT_IMAGE_URI`         |
    | `lifecycleConfiguration.maxLifetime`      | **fleet**    | `MAX_LIFETIME_SECONDS`    |
    | `lifecycleConfiguration.*` (idle timeout) | carry forward| from `live`               |
    | `filesystemConfigurations`                | **fleet**    | `sessionStorage` @ mount  |
    | `networkConfiguration`                    | **fleet**    | VPC when subnets are set; never downgraded |
    | `environmentVariables` -- fleet keys      | **fleet**    | `_fleet_environment`      |
    | `environmentVariables` -- everything else | carry forward| from `live`               |
    | `roleArn`, `protocolConfiguration`, `description` | carry forward | from `live`      |
    | `platformVersion`                         | **fleet**    | `PLATFORM_VERSION`, always sent |

    The `desired` dict below is the authority and that table is a reading aid: a member
    added to one and not the other is a carry-forward/converge misclassification
    (invariant 24) that reads as deliberate, which is the whole shape of this bug.

    The environment merge is therefore
    `{**create-time defaults, **live, **fleet}`: a key this version of the code has
    never heard of survives, and a stale `WORK_ROOT` converges.
    """
    if not observability.is_valid_mount_path(MOUNT_PATH):
        # A guard on this module's own constant, checked before the call because
        # botocore does not validate `MountPath`'s pattern client-side and the service
        # reports a violation as a bare `ValidationException`.
        raise ValueError(f"{MOUNT_PATH} is not a mount path AgentCore accepts")

    current = dict(live or {})
    environment = {
        **_configuration_environment(config),
        **{str(k): str(v) for k, v in (current.get("environmentVariables") or {}).items()},
        **_fleet_environment(),
    }
    # Retirement, which the merge above cannot express. `{**create-time, **live, **fleet}`
    # carries forward every key the fleet group does not name -- deliberately, so a key this
    # version has never heard of survives -- and the mirror of that is that **deleting a key
    # from `_fleet_environment` does not remove it from a live runtime**. It stops being
    # converged, so the live value is carried forward for the rest of the runtime's life.
    #
    # A key dropped from `_fleet_environment` stops being converged and the live value is
    # carried forward indefinitely. `RETIRED_FLEET_KEYS` is the tombstone list that actively
    # removes such keys from any runtime that still carries them.
    for name in RETIRED_FLEET_KEYS:
        environment.pop(name, None)
    desired: dict[str, Any] = {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": AGENT_IMAGE_URI}},
        "roleArn": current.get("roleArn") or str(config.get("executionRoleArn") or ""),
        # `live`, not `current`: the argument is the live runtime the ratchet reads, not
        # the in-progress desired dict.
        "networkConfiguration": _fleet_network(live),
        "protocolConfiguration": current.get("protocolConfiguration")
        or {"serverProtocol": SERVER_PROTOCOL},
        "description": current.get("description")
        or f"rca runtime for {config.get('repository') or config.get('configId')}",
        "lifecycleConfiguration": {
            **(current.get("lifecycleConfiguration") or {}),
            "maxLifetime": MAX_LIFETIME_SECONDS,
        },
        "filesystemConfigurations": [{"sessionStorage": {"mountPath": MOUNT_PATH}}],
        "environmentVariables": environment,
        # Fleet, and always sent -- omitting it on an update means "keep what you have",
        # which would make the deploy-time key one-way. See `PLATFORM_VERSION`.
        "platformVersion": PLATFORM_VERSION,
    }
    # The identifying member is the one thing that differs between the two calls this
    # dict feeds: `CreateAgentRuntime` names the runtime, `UpdateAgentRuntime` names it
    # by id and rejects a name.
    if live:
        desired["agentRuntimeId"] = str(live["agentRuntimeId"])
    else:
        desired["agentRuntimeName"] = _runtime_name(str(config["configId"]))
    return desired


# Compared per key rather than by whole-object equality: a `GetAgentRuntime` response
# also carries `createdAt`, `status` and `agentRuntimeVersion`, so whole-object equality
# would make every SQS redelivery an update.
_INTENT_KEYS = (
    "lifecycleConfiguration",
    "filesystemConfigurations",
    "networkConfiguration",
    "protocolConfiguration",
    "roleArn",
    "description",
    "environmentVariables",
    # Fleet policy, so it belongs here: without it a fleet moved from V1 to V2 would
    # never converge, because nothing else in this tuple changes when the key does.
    # `GetAgentRuntime` echoes `platformVersion` back, which is what makes it comparable
    # at all -- `CreateAgentRuntime` and `UpdateAgentRuntime` do **not** return it.
    "platformVersion",
)


# Members `GetAgentRuntime` returns that this code can never send, so they must not count
# as a difference. Keyed by the enclosing member, and enumerated rather than handled by a
# general "ignore keys `desired` omits" rule: that rule would also suppress a stale member
# this code wants *removed*, which is real drift and a test already pins it.
#
# `requireServiceS3Endpoint` is present here because `GetAgentRuntime` returns it on every
# VPC-mode runtime but `CreateAgentRuntime` and `UpdateAgentRuntime` reject it as a
# `ValidationException`. Without this entry, `_matches_intent` always returns `False` for
# VPC-mode runtimes, triggering a needless update on every reconcile -- and on V2 an update
# is a snapshot rebuild of several minutes.
#
# The next member the vendor adds will fail the same way. That is deliberate: it surfaces as
# a test going red and one line added here, rather than as a guard that quietly stopped
# comparing.
_VENDOR_ONLY_MEMBERS: dict[str, frozenset[str]] = {
    "networkModeConfig": frozenset({"requireServiceS3Endpoint"}),
}


def _comparable(value: Any, member: str | None = None) -> Any:
    """`value` with the members only the service may set removed, recursively.

    Applied to **both** sides so the comparison stays exact everywhere else: a key `live`
    carries and `desired` does not is still a difference, which is what keeps a stale
    `networkModeConfig` on a `PUBLIC` runtime visible as drift.
    """
    if isinstance(value, Mapping):
        drop = _VENDOR_ONLY_MEMBERS.get(member or "", frozenset())
        return {k: _comparable(v, k) for k, v in value.items() if k not in drop}
    if isinstance(value, list):
        return [_comparable(v, member) for v in value]
    return value


def _matches_intent(live: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    """Whether the live runtime already *is* the desired one.

    The guard covers **every converged field**, not the image alone. An image-only guard
    short-circuits before convergence, so a runtime created before session storage or the
    8-hour lifetime existed would never receive either, however many runtime updates were
    issued.
    """
    if _container_uri(live) != _container_uri(desired):
        return False
    # A runtime created before `platformVersion` existed reports **no value** for it:
    # `GetAgentRuntime` answers with the member absent. Absence is the service's default,
    # which is `V1`, so normalising it here is
    # what stops a `V1` fleet reading as drift against a desired `V1`. Without this every
    # existing runtime would take one needless update, and under V2 an update is a snapshot
    # rebuild of several minutes rather than an idle API call.
    live_view = {**live, "platformVersion": str(live.get("platformVersion") or "V1")}
    return all(
        _comparable(live_view.get(key), key) == _comparable(desired.get(key), key)
        for key in _INTENT_KEYS
    )


# -- the four phases --------------------------------------------------------------


# Both spellings AWS uses, plus the API name, because the wording differs between the
# `ValidationException` AgentCore raises during `CreateAgentRuntime` and the
# `AccessDeniedException` IAM raises for the same missing grant.
_SERVICE_LINKED_ROLE_HINTS = (
    "service linked role",
    "service-linked role",
    "servicelinkedrole",
)

# The report, and the one place the true cause is written down. It deliberately does not
# contain the word "assumable": that wording belongs to `_retry_create`, for the case
# where it is true, and claiming it here is the lie this predicate exists to end.
_SERVICE_LINKED_ROLE_REASON = (
    "AgentCore could not create the service-linked role a networkMode: VPC runtime needs "
    "(invariant 30). This is neither the execution role nor IAM propagation: the "
    "provisioner's own role needs iam:CreateServiceLinkedRole for "
    "bedrock-agentcore.amazonaws.com. Waiting does not add a statement to a policy, so "
    "re-issue POST /configurations once the grant is deployed."
)


def _is_missing_service_linked_role(err: ClientError) -> bool:
    """Whether `CreateAgentRuntime` failed because *this* Lambda may not create the
    service-linked role AgentCore needs for a VPC-mode runtime.

    Permanent, and named separately from propagation because the message is the only
    signal there is: AgentCore returns the same `ValidationException` for an unassumable
    role, an unpullable image and this, so the code carries no information (invariant 15).
    """
    error = err.response.get("Error", {})
    if error.get("Code") not in ("ValidationException", "AccessDeniedException"):
        return False
    message = str(error.get("Message", "")).lower()
    return any(hint in message for hint in _SERVICE_LINKED_ROLE_HINTS)


def _now_epoch() -> int:
    """Wall-clock seconds since the epoch. A named function so the transient budget has one
    clock and a test can freeze it. `_common.now_iso` is the string form and is what
    `updatedAt` carries, but a deadline travels in a JSON message body, where an integer
    survives the round trip with no parse to get wrong."""
    return int(time.time())


# The allowlist, and the whole mechanism. A `CreateAgentRuntime` failure is retryable to the
# whole budget only if it is named here. Everything else is terminal on its first attempt,
# except that an unrecognised message out of `CreateAgentRuntime` gets exactly one retry
# first -- the floor in `_create`, which bounds what a gap in this enumeration costs without
# turning the default back into retry.
#
# An allowlist is the right shape because the default must be terminal. With a floor, an
# unrecognised message costs one retry step and then AWS's own words, where a wrong retry
# costs the full wall-clock budget AND a report naming a cause the code never established.
#
# Each entry is `(codes, message shapes, the cause it establishes)`. An empty shape tuple
# means the code and the operation are the whole test. The label is why the report on
# exhaustion may name a cause at all: it is the condition the allowlist proved, not one the
# wrapper assumed.
_TRANSIENT_CREATE_FAILURES: tuple[tuple[frozenset[str], tuple[str, ...], str], ...] = (
    (
        # IAM propagation: the one condition here that heals with time.
        # `CreateAgentRuntime` runs moments after `CreateRole`, and AgentCore reports a
        # role it cannot assume yet by failing to assume it. Matched on the service's
        # phrasing of that failed attempt and on nothing else -- notably NOT on a bare
        # `sts:AssumeRole` inside an authorisation failure, which is word for word what an
        # SCP or boundary denial of the same action looks like. These shapes also exclude
        # the image pull for free ("could not be pulled" is not "could not assume").
        #
        # The last shape uses "assumption" (a noun) rather than "assume" (a verb), and is
        # anchored on the TAIL of the AgentCore message rather than the head, because the
        # tail cannot be confused with an SCP or permissions-boundary denial -- those say
        # "not authorized to perform" or "explicit deny" -- while the head is a prefix the
        # service also uses for unrelated failures. A genuinely wrong trust policy produces
        # this same message; the wall-clock budget bounds the time wasted in that case, and
        # "the execution role was still not assumable" is then the accurate report.
        frozenset({"ValidationException", "AccessDeniedException"}),
        (
            "cannot be assumed",
            "could not be assumed",
            "unable to assume",
            "failed to assume",
            "could not assume",
            "cannot assume",
            "allows assumption by this service",
        ),
        "the execution role was still not assumable",
    ),
    (
        # Throttling, and the reason the entry shape carries a code set at all: a throttle
        # has no cause worth matching, so the code and the operation are the whole test.
        # Independent of the entry above -- delete it and nothing else here changes.
        frozenset({"ThrottlingException", "TooManyRequestsException"}),
        (),
        "AgentCore was still throttling CreateAgentRuntime",
    ),
)

# The only operation this allowlist speaks for. `CREATE` is the one phase that calls
# moments after minting a role, so it is the only propagation window there is; the same
# code out of `GetAgentRuntime` or `UpdateAgentRuntime` means something else entirely and
# must not borrow this budget.
_TRANSIENT_OPERATION = "CreateAgentRuntime"

# The floor's label. It names no cause on purpose: nothing was classified, so the report may
# say only that the failure was unrecognised and was retried. It must never contain the word
# "assumable" -- that wording belongs to the allowlist entry that PROVED it.
_UNRECOGNISED_CREATE_FAILURE = (
    "CreateAgentRuntime failed for a reason this allowlist does not recognise, and one "
    "retry did not clear it"
)


def _transient_create_failure(err: ClientError) -> str | None:
    """The allowlist label for a `CreateAgentRuntime` failure that genuinely resolves with
    time, or `None`.

    `None` is the default and the safe answer. A caller that cannot classify a failure
    reports it verbatim instead of retrying it, so a permanent cause this code has never
    met is terminal the first time it arrives rather than retryable by omission -- which is
    the property the keyword matcher could not have.
    """
    if _is_missing_service_linked_role(err):
        # Permanent, and `_is_missing_service_linked_role` is the one place that condition
        # is classified. Checked here as well as in `_create` so this predicate is correct
        # when a test calls it on its own; the positive shapes below could not match that
        # message anyway.
        return None
    if str(getattr(err, "operation_name", "") or "") != _TRANSIENT_OPERATION:
        return None
    error = err.response.get("Error", {}) or {}
    code = str(error.get("Code") or "")
    message = str(error.get("Message") or "").lower()
    for codes, shapes, cause in _TRANSIENT_CREATE_FAILURES:
        if code in codes and (not shapes or any(shape in message for shape in shapes)):
            return cause
    return None


def _vendor_detail(err: ClientError) -> str:
    """What AWS actually said, as one string: the raw error code, the operation that
    produced it, and the message verbatim.

    Never a paraphrase and never a conclusion. Every caller puts this in a `statusReason`,
    so the report names what the code established rather than what it assumed.
    """
    error = err.response.get("Error", {}) or {}
    code = str(error.get("Code") or "an error with no code")
    operation = str(getattr(err, "operation_name", "") or "an unnamed operation")
    message = str(error.get("Message") or "").strip()
    if not message:
        # AWS returns no message for some `AccessDenied` errors. Reporting the absence
        # explicitly directs the operator to CloudTrail, which has the full detail.
        return (
            f"{operation} failed with {code} and AWS returned no message at all; the "
            f"CloudTrail event for this call is the only remaining detail"
        )
    return f"{operation} failed with {code}: {message}"


def _retry_deadline(carried: Any) -> int:
    """The epoch second the transient budget runs out.

    Carried in the message body rather than recomputed per receive, which is what makes the
    budget 90 seconds of real time across however many receives that takes instead of 90
    seconds each time round. An absent or unreadable value starts a fresh budget. Clamped
    only from ABOVE: a body is not a trusted input and must not buy an unbounded loop,
    while a deadline already in the past is exhaustion -- a verdict, not a value to fix.
    """
    now = _now_epoch()
    try:
        return min(int(carried), now + _MAX_TRANSIENT_BUDGET_SECONDS)
    except (TypeError, ValueError):
        return now + _TRANSIENT_BUDGET_SECONDS


def _retry_create(config_id: str, attempt: int, deadline: int, cause: str, detail: str) -> None:
    """Re-enqueue `CREATE` as a **new** message rather than letting this one fail.

    A raised exception would return the message to the queue and burn one of three
    receives, so a configuration whose role takes four attempts to propagate would
    DLQ while perfectly healthy. A new message resets the receive count; the caller
    returns normally so the current message is deleted.

    The budget is `deadline`, a wall-clock instant carried in the message body, not a count
    of attempts: "IAM gets 90 seconds" is a sentence an operator can act on, where "ten
    attempts" was 26 minutes and read as neither. `cause` is the allowlist label that
    matched, so the report on exhaustion names a condition `_transient_create_failure`
    established -- and `detail` carries what AWS actually said beside it, so the two can be
    compared instead of the wrapper being taken on trust.
    """
    remaining = deadline - _now_epoch()
    if remaining <= 0:
        LOG.error(
            "config %s: %s, and the %ss transient budget is spent",
            config_id,
            cause,
            _TRANSIENT_BUDGET_SECONDS,
        )
        _set_config(
            config_id,
            status=FAILED,
            statusReason=(
                f"{cause} after {_TRANSIENT_BUDGET_SECONDS}s of retries. "
                f"The last thing AWS said was: {detail}"
            ),
        )
        return
    enqueue(
        config_id,
        "CREATE",
        attempt=attempt + 1,
        # Never past the deadline: the final retry lands on it, so the budget is spent
        # rather than overshot by a delay that outlives it.
        delay=min(_RETRY_DELAY_SECONDS, remaining),
        deadline=deadline,
    )


def _create(config_id: str, attempt: int, deadline: Any = None) -> None:
    config = _live_config(config_id, "CREATE")
    if config is None:
        return
    try:
        # First, so the inline policy `_ensure_role` writes can name the memory's exact ARN.
        # It never raises: see `_ensure_memory` on why a memory failure must not reach the
        # classifier below.
        memory_id = _ensure_memory(config_id, config)
        role_arn = _ensure_role(config_id, {**config, "memoryId": memory_id})
        # `_desired_runtime` reads nothing named `memoryId` today; it is passed for the same
        # reason `executionRoleArn` is, so the shaping call sees the whole intended
        # configuration in one dict and a future `memoryConfiguration` member has one obvious
        # place to be classified as carry-forward or converge (invariant 24).
        desired = _desired_runtime(
            None, {**config, "executionRoleArn": role_arn, "memoryId": memory_id}
        )
        # No `create_agent_runtime_endpoint` call anywhere (invariant 14):
        # `create_agent_runtime` makes `DEFAULT` itself, a second endpoint call
        # conflicts while the runtime is still `CREATING`, and polling an endpoint that
        # was never created reports `ResourceNotFound` forever while the runtime is
        # `READY`.
        created = _common.agentcore_control().create_agent_runtime(**desired)
    except ClientError as err:
        # Every arm returns; none raises. A raise DLQs a message body naming the
        # configuration but leaves the item in `PENDING` with no reason at all, and
        # `statusReason` is where the API and the console look.
        if _is_missing_service_linked_role(err):
            LOG.error("config %s: %s", config_id, _SERVICE_LINKED_ROLE_REASON)
            _set_config(config_id, status=FAILED, statusReason=_SERVICE_LINKED_ROLE_REASON)
            return
        cause = _transient_create_failure(err)
        if cause is None:
            detail = _vendor_detail(err)
            operation = str(getattr(err, "operation_name", "") or "")
            if attempt == 0 and operation == _TRANSIENT_OPERATION:
                # The floor on the allowlist. An unrecognised message from
                # `CreateAgentRuntime` gets exactly one retry so that a new vendor phrasing
                # costs one slow success rather than an immediate permanent failure. The retry
                # claims nothing: if the failure recurs at `attempt >= 1` the arm below
                # reports AWS's own words exactly, one attempt later.
                #
                # Gated on the operation because `_ensure_role` raises into this same
                # `except`: an IAM `CreateRole`/`PutRolePolicy` denial is permanent and must
                # not borrow this retry. Reached only after `_is_missing_service_linked_role`
                # has returned above, so that permanent cause stays terminal on its first
                # attempt.
                LOG.warning(
                    "config %s: %s; unrecognised, retrying once before calling it permanent",
                    config_id,
                    detail,
                )
                _retry_create(
                    config_id,
                    attempt,
                    _retry_deadline(deadline),
                    _UNRECOGNISED_CREATE_FAILURE,
                    detail,
                )
                return
            # Unclassifiable and the floor is spent, so the report asserts nothing: the raw
            # code, the operation and the vendor's own words, and the operator decides. This
            # is the arm that used to `raise`.
            LOG.error("config %s: %s", config_id, detail)
            _set_config(config_id, status=FAILED, statusReason=detail)
            return
        LOG.warning(
            "config %s: %s on attempt %s; retrying inside the %ss budget",
            config_id,
            cause,
            attempt,
            _TRANSIENT_BUDGET_SECONDS,
        )
        _retry_create(config_id, attempt, _retry_deadline(deadline), cause, _vendor_detail(err))
        return
    _set_config(
        config_id,
        status=CREATING,
        statusReason="",
        agentRuntimeArn=created.get("agentRuntimeArn", ""),
        agentRuntimeId=created.get("agentRuntimeId", ""),
        agentRuntimeVersion=str(created.get("agentRuntimeVersion", "")),
        executionRoleArn=role_arn,
        # `""` when `_ensure_memory` could not make one, and `_check` is deliberately not
        # gated on the memory reaching `ACTIVE`: gating would turn a slow memory into a
        # repository nobody can use, and a memory still `CREATING` when the first task runs
        # is the container's no-op path.
        memoryId=memory_id,
        containerUri=AGENT_IMAGE_URI,
    )
    enqueue(config_id, "CHECK", delay=CHECK_DELAY_SECONDS)


def _check(config_id: str, attempt: int) -> None:
    config = _live_config(config_id, "CHECK")
    if config is None:
        return
    runtime_id = str(config.get("agentRuntimeId") or "")
    if not runtime_id:
        # `CHECK` is only ever enqueued after the id is stored, so an absent one means
        # the create never got that far. Failing names it instead of polling forever.
        _set_config(config_id, status=FAILED, statusReason="no runtime was ever created to check")
        return

    live = _common.agentcore_control().get_agent_runtime(agentRuntimeId=runtime_id)
    status = str(live.get("status", ""))
    if status == "READY":
        LOG.info("config %s runtime %s is READY", config_id, runtime_id)
        _set_config(
            config_id,
            status=READY,
            statusReason="",
            agentRuntimeVersion=str(live.get("agentRuntimeVersion", "")),
        )
    elif status in _RUNTIME_FAILED:
        reason = str(live.get("failureReason") or f"the runtime reported {status}")
        LOG.error("config %s runtime %s failed: %s", config_id, runtime_id, reason)
        _set_config(config_id, status=FAILED, statusReason=reason)
    elif status in _RUNTIME_PENDING:
        if attempt >= MAX_CHECK_ATTEMPTS:
            _set_config(
                config_id,
                status=FAILED,
                statusReason=(
                    f"the runtime was still {status} after {MAX_CHECK_ATTEMPTS} checks "
                    f"{CHECK_DELAY_SECONDS}s apart"
                ),
            )
            return
        enqueue(config_id, "CHECK", attempt=attempt + 1, delay=CHECK_DELAY_SECONDS)
    else:
        _set_config(config_id, status=FAILED, statusReason=f"the runtime reported {status}")


def _runtime_exists(runtime_id: str) -> bool:
    """Whether the runtime is still there, asked before `DeleteAgentRuntime` rather than
    after.

    **`DeleteAgentRuntime` on a runtime that no longer exists answers
    `AccessDeniedException`, not `ResourceNotFoundException`.** This breaks the replay
    `_delete` depends on: the item is deleted last so a crash mid-teardown is finishable,
    but once the runtime is gone every replay raises `AccessDenied`, the item is never
    deleted, and the configuration stays `DELETING` indefinitely.

    `_is_gone` is deliberately **not** widened to accept `AccessDeniedException`: a real
    permission failure presents identically, so that would turn a missing grant into a
    silent teardown that leaves a live runtime billing. `GetAgentRuntime` answers a missing
    runtime with a clean `ResourceNotFoundException`, so checking existence first costs one
    extra call and avoids any message matching.
    """
    try:
        _common.agentcore_control().get_agent_runtime(agentRuntimeId=runtime_id)
    except ClientError as err:
        if _is_gone(err):
            LOG.info("runtime %s is already gone; nothing to delete", runtime_id)
            return False
        raise
    return True


def _delete_memory(config_id: str, config: Mapping[str, Any]) -> None:
    """The memory teardown step, lifted out of `_delete` rather than written inline beside
    the other four: `_delete` already sits at the `ruff.toml` complexity ceiling of 12, which
    `ruff.toml`'s own comment names it at, and a fifth inline block fails `make lint`. Any
    further resource added to teardown wants the same treatment.

    `DeleteMemory` reports "already gone" as `ResourceNotFoundException` only, which
    `_GONE_CODES` already carries. `ConflictException` is deliberately **not** treated as
    gone: a delete refused because the memory is busy is not a delete that already happened,
    and swallowing it would orphan the memory.
    """
    memory_id = str(config.get("memoryId") or "")
    if not memory_id:
        return
    try:
        _common.agentcore_control().delete_memory(memoryId=memory_id)
    except ClientError as err:
        if not _is_gone(err):
            raise
        LOG.info("the memory for config %s was already gone", config_id)


def _delete(config_id: str) -> None:
    """Tear the configuration down in dependency order, item **last**.

    Invariant 22: this is the teardown path, not `cdk destroy` -- the tables are
    `RETAIN`. Deleting the item last is what makes a crash mid-teardown recoverable: the
    configuration stays `DELETING` and the next `DELETE` message finishes the job.
    """
    config = _get_config(config_id)
    if config is None:
        LOG.info("DELETE for config %s: already gone", config_id)
        return

    runtime_id = str(config.get("agentRuntimeId") or "")
    if runtime_id and _runtime_exists(runtime_id):
        try:
            _common.agentcore_control().delete_agent_runtime(agentRuntimeId=runtime_id)
        except ClientError as err:
            if not _is_gone(err):
                raise
            LOG.info("runtime %s for config %s was already gone", runtime_id, config_id)

    # After the runtime and before the inline policy: nothing the runtime does can reach a
    # memory that is gone, and the policy that grants `UseOwnMemory` outlives what it names.
    _delete_memory(config_id, config)

    role_name = _role_name(config_id)
    try:
        _common.iam.delete_role_policy(RoleName=role_name, PolicyName=_POLICY_NAME)
    except ClientError as err:
        if not _is_gone(err):
            raise
        LOG.info("inline policy on %s was already gone", role_name)
    try:
        _common.iam.delete_role(RoleName=role_name)
    except ClientError as err:
        if not _is_gone(err):
            raise
        LOG.info("role %s was already gone", role_name)

    # No secret to delete, and no stored GitHub authorisation to revoke either: the token
    # vault keys this repository's credential on `configId`, which is random and never
    # reissued, so removing the item below is what makes it unreachable. `DELETE /github-app`
    # is the wholesale revoke.
    _common.config_table.delete_item(Key={"configId": config_id})
    LOG.info("config %s torn down", config_id)


def _update(config_id: str) -> None:
    config = _live_config(config_id, "UPDATE")
    if config is None:
        return
    runtime_id = str(config.get("agentRuntimeId") or "")
    if not runtime_id:
        LOG.warning("UPDATE for config %s: it has no runtime yet", config_id)
        return

    # The execution role's inline policy is rewritten **before** the intent check below, and
    # that ordering is the whole point. `UPDATE` is the only path that can carry a change in
    # `_execution_role_policy` to a configuration that is already live, and
    # `_matches_intent` returns early on a converged runtime -- which is exactly what a
    # policy-only change looks like, since the policy is not part of the runtime at all. So a
    # role write placed after that check would never run on the one fleet that needs it.
    # `_ensure_role` rewrites the inline policy every time, so a fleet-wide policy change
    # lands on a re-provision. `_update` must call it before the `_matches_intent` check:
    # a policy-only change looks like a converged runtime because the policy is not part of
    # the runtime at all, and `_matches_intent` would return early before the role write.
    #
    # What omitting it costs is the expensive silent shape invariant 41 exists to prevent:
    # a missing grant is a fact about the container, while `runs.queue_run` can only check
    # what the API can reach -- so the run dispatches, clones, plans, codes and verifies,
    # and the missing permission surfaces only at `submit`.
    #
    # Unconditional rather than compared: `put_role_policy` is idempotent, and one IAM write
    # per update is cheaper than a read to decide whether to write.
    _ensure_role(config_id, config)

    # Read the live runtime back. `UpdateAgentRuntime` is a full replace, so this
    # is the only source for the members that are carried forward.
    live = _common.agentcore_control().get_agent_runtime(agentRuntimeId=runtime_id)
    desired = _desired_runtime(live, config)
    if _matches_intent(live, desired):
        # No status write either: parking a converged fleet in `UPDATING` forever is how
        # `POST /runtime-update` stops meaning anything.
        LOG.info(
            "config %s runtime %s already matches intent; no update issued", config_id, runtime_id
        )
        return

    updated = _common.agentcore_control().update_agent_runtime(**desired)
    _set_config(
        config_id,
        status=UPDATING,
        statusReason="",
        agentRuntimeVersion=str(updated.get("agentRuntimeVersion", "")),
        containerUri=AGENT_IMAGE_URI,
    )
    enqueue(config_id, "CHECK", delay=CHECK_DELAY_SECONDS)


def lambda_handler(event: Mapping[str, Any], _context: Any = None) -> None:
    """The provision queue's consumer. Undecorated: there is no HTTP route here.

    An unexpected exception is deliberately *not* caught -- SQS redelivers, and the
    redrive policy is what eventually parks a genuinely broken configuration on the DLQ.
    Only a message this function cannot even read is dropped, because redelivering it
    cannot make it parse.
    """
    for record in event.get("Records", []):
        try:
            message = json.loads(record.get("body") or "{}")
        except (TypeError, ValueError):
            LOG.exception("a provision message was not JSON; dropping it")
            continue
        if not isinstance(message, Mapping):
            LOG.error("a provision message was not a JSON object; dropping it")
            continue
        phase = str(message.get("phase", ""))
        config_id = str(message.get("configId", ""))
        attempt = int(message.get("attempt", 0) or 0)
        deadline = message.get("retryDeadline")
        if not config_id or phase not in PHASES:
            LOG.error("provision message names no phase this function owns: phase=%s", phase)
            continue
        LOG.info("provision %s config=%s attempt=%s", phase, config_id, attempt)
        if phase == "CREATE":
            _create(config_id, attempt, deadline)
        elif phase == "CHECK":
            _check(config_id, attempt)
        elif phase == "DELETE":
            _delete(config_id)
        else:
            _update(config_id)
