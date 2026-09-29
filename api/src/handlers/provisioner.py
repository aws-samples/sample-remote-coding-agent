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
  yet must cost one slow success, never the ability to provision at all. The
  allowlist replaced keyword matching on the vendor's prose, whose default was retry and
  which therefore reported three separate permanent failures as an execution role that was
  never the problem.
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

from . import _common, observability
from ._common import LOG

PHASES = ("CREATE", "CHECK", "DELETE", "UPDATE")

# Exactly one level under `/mnt` -- `MountPath`'s own pattern is `/mnt/[a-zA-Z0-9._-]+/?`
# and botocore does not enforce a `pattern` client-side, so `/mnt/a/b` is a bare
# server-side `ValidationException`. `observability.is_valid_mount_path` is the guard.
MOUNT_PATH = "/mnt/workspace"

# 8 hours. The service default is 2, and a real task ran 3h34m before that default
# killed it mid-review; the failure looks like the container crashing.
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
# V2 starts the agent from a snapshot instead of initialising on each cold start. AWS
# measured P75 1.9-2.0s against 5.4-30s on V1 for images from 200 MB to 2 GB, and this
# image is ~1.3 GB, so cold start -- paid on every dispatched task -- is most of what this
# buys.
#
# Sent on **every** create and update, never conditionally, because the service's omission
# semantics are asymmetric: omitted on create means V1, and omitted on *update* means "keep
# whatever this runtime has". A runtime created before this constant existed therefore
# reports no value at all, which is V1, and converges here on its next update
# (invariant 24) -- see `_matches_intent`, which is where absence is read as V1 so that the
# convergence happens exactly once per runtime rather than never.
#
# Three consequences, none optional, and the first two are why this needed measuring rather
# than assuming:
#
# * The container must answer `/ping` healthy within **120 seconds**, because the snapshot
#   is taken on the first healthy ping. **Measured**: the real agent image, in VPC mode with
#   the fleet's own environment, reached `READY` in 508s of snapshot preparation with no
#   failure -- so the ping lands well inside the window. A dummy image that never pings
#   fails at ~102s instead, which is what the earlier probes did.
# * Create and update take *minutes*, not seconds. `_check` polls, so that is already the
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
_CHECK_DELAY_SECONDS = 30

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
    runtime, role and secret are all gone -- and would even flip it to `READY`. A lost
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
    teardown resurrects a configuration whose runtime, role and secret are all gone --
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


def _account_and_region(config: Mapping[str, Any]) -> tuple[str, str]:
    """The account and region for the trust policy's `aws:SourceAccount` and the table
    ARNs in `_execution_role_policy`, read off the configuration's own `secretArn`
    (`arn:aws:secretsmanager:<region>:<account>:secret:...`).

    Deliberately **not** `sts:GetCallerIdentity`: this Lambda's grant surface is exactly
    the table `infra/stacks/api_stack.py` declares, and an STS client would widen it for
    a value every provisioned configuration already carries. Same reasoning keeps
    `_ensure_role` off `iam:GetRole`. `AWS_REGION` and a `*` account are the fallbacks,
    so a half-written configuration still produces a policy IAM will accept.
    """
    parts = str(config.get("secretArn") or "").split(":")
    region, account = (parts[3], parts[4]) if len(parts) > 5 else ("", "")
    return account or "*", region or os.environ.get("AWS_REGION", "") or "*"


def _asset_repository_arn(image_uri: str) -> str:
    """The ECR repository ARN the agent image lives in, read off `AGENT_IMAGE_URI`
    (`<account>.dkr.ecr.<region>.amazonaws.com/<repository>[:<tag>|@<digest>]`) in the
    same spirit as `_account_and_region` reading the account off `secretArn`: the value
    is derivable from something every deploy already carries, so it needs no second
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

    What it grants is the `Sid` list in the `Statement` block below, and that list is the
    authority -- not this docstring. The previous version of this paragraph enumerated the
    permissions and closed by claiming that was the whole list, while the ECR image pull was
    missing from both the prose and the policy, and the confident enumeration is what made
    the omission read as a decision. So: read the `Sid`s. A summary here can only ever lag
    them, and a reader who trusts the summary is the failure this note replaces.

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
                "Sid": "ReadOwnSecret",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                # The **literal** ARN, which is why rotation writes a new *version* of
                # this secret and never a second secret: a second one would not be
                # readable by this role at all.
                "Resource": str(config.get("secretArn") or ""),
            },
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
    _common.iam.put_role_policy(
        RoleName=role_name,
        PolicyName=_POLICY_NAME,
        PolicyDocument=json.dumps(_execution_role_policy(config)),
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
        "AGENT_GIT_AUTHOR_NAME": AGENT_GIT_AUTHOR_NAME,
        "AGENT_GIT_AUTHOR_EMAIL": AGENT_GIT_AUTHOR_EMAIL,
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
        # This is what remains of an in-container ADOT collector that SigV4-signed the
        # CLI's metrics to CloudWatch for Coding Agent Insights. It was removed: the
        # dashboards read a Prometheus-compatible store queried by PromQL, AWS documents
        # only a bearer-token path (which invariant 32b refused, a bearer token being a
        # long-term IAM-user credential) and a vendor gateway path, and metrics signed this
        # third way never appeared in any namespace. Restoring an exporter means choosing an
        # endpoint first; do not reintroduce it as a default.
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
    return {
        "CONFIG_ID": str(config.get("configId", "")),
        "SECRET_ARN": str(config.get("secretArn") or ""),
    }


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
# general "ignore keys `desired` omits" rule: that rule also stops seeing a stale member
# this code wants *removed*, which is real drift and a test already pins it.
#
# `requireServiceS3Endpoint` is the one that forced this. AWS added it inside
# `networkModeConfig` in May 2026 and returns it on every VPC-mode runtime, including ones
# created today -- but it is a `ValidationException` on `CreateAgentRuntime`, and on
# `UpdateAgentRuntime` for any runtime created after that rollout. So `_fleet_network`
# cannot send it, whole-object equality on `networkConfiguration` could never match again,
# and `_matches_intent` answered `False` on every single call. On V1 that cost one wasted
# `UpdateAgentRuntime` per reconcile; on V2 each one prepares a fresh snapshot, runs for
# minutes, and leaves the superseded snapshot pending deletion for up to the 8-hour session
# maximum, with a `ConflictException` for anything that lands meanwhile.
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
    short-circuits before convergence and makes the mount unreachable by any number of
    re-issued runtime updates -- both runtimes on the deploy this was measured against
    predated session storage and the 8-hour lifetime, and neither would ever have got
    them.
    """
    if _container_uri(live) != _container_uri(desired):
        return False
    # A runtime created before `platformVersion` existed reports **no value** for it --
    # measured, not assumed: every runtime in the live fleet answered `GetAgentRuntime` with
    # it absent. Absence is the service's default, which is `V1`, so normalising it here is
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
# Keyword matching on the vendor's prose was the defect this replaces. That predicate's
# default was RETRY -- anything containing "assume" and none of four permanent-cause
# phrases -- so every permanent cause nobody had met yet was retryable by omission. Three
# of them each looped ten times over 26 minutes and were then reported as an unassumable
# execution role that was healthy the whole time: a missing
# `bedrock-agentcore:CreateAgentRuntimeEndpoint`, a missing `iam:CreateServiceLinkedRole`,
# and an `AccessDenied` whose CloudTrail `errorMessage` was null. Adding a fifth exclusion
# phrase would have been a fourth incident waiting on a fifth vendor sentence.
#
# An allowlist inverts the default, and the asymmetry is the argument: with the floor below
# it, an unrecognised message costs one retry step and then AWS's own words, where a wrong
# retry costs 26 minutes AND a report naming a cause that is false. (Re-issuing
# `POST /configurations` is NOT the operator's way out, which is what the outage taught:
# `_new_config_id` is random, so a re-issue mints a new role and races the same window.)
#
# Each entry is `(codes, message shapes, the cause it establishes)`. An empty shape tuple
# means the code and the operation are the whole test. The label is why the report on
# exhaustion may name a cause at all: it is the condition the allowlist PROVED, not one the
# wrapper assumed, which is the distinction the old terminal message could not draw.
_TRANSIENT_CREATE_FAILURES: tuple[tuple[frozenset[str], tuple[str, ...], str], ...] = (
    (
        # IAM propagation: the one condition here ever observed to heal with time.
        # `CreateAgentRuntime` runs moments after `CreateRole`, and AgentCore reports a
        # role it cannot assume yet by failing to assume it. Matched on the service's
        # phrasing of that failed attempt and on nothing else -- notably NOT on a bare
        # `sts:AssumeRole` inside an authorization failure, which is word for word what an
        # SCP or boundary denial of the same action looks like. These shapes also exclude
        # the image pull for free ("could not be pulled" is not "could not assume"), which
        # is why this file no longer carries an `ecr`/`image`/`container` exclusion list to
        # forget to extend. No count is written here on purpose -- the last one went stale.
        #
        # The last shape is the message AgentCore actually returns, and its absence broke
        # provisioning outright: `Role validation failed for '<arn>'. Please verify that the
        # role exists and its trust policy allows assumption by this service`. The service
        # says **"assumption"**, a noun, so every shape above it -- all built around
        # "assume"/"assumed" -- missed, the failure was unrecognised, and no configuration
        # could be created at all. `_new_config_id` is random, so an operator re-issuing
        # `POST /configurations` minted a fresh role and raced the same window again; the
        # internal retry was the only mechanism that could clear it.
        #
        # Anchored on the TAIL of that sentence rather than on "role validation failed",
        # because the tail cannot be confused with an SCP or permissions-boundary denial of
        # `sts:AssumeRole` -- those say "not authorized to perform" or "explicit deny" --
        # while the head is a prefix the service also uses for things that are not
        # propagation. A genuinely wrong trust policy produces this same sentence, and that
        # is ACCEPTABLE: the wall-clock budget bounds the time wasted, and "the execution
        # role was still not assumable" is then the *true* report rather than a lie. That is
        # the distinction that matters here, because the incident this file documents was
        # about reports claiming causes the code never established, not about slow ones.
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
    and the whole point of this change is that the report cannot claim a cause the code did
    not establish.
    """
    error = err.response.get("Error", {}) or {}
    code = str(error.get("Code") or "an error with no code")
    operation = str(getattr(err, "operation_name", "") or "an unnamed operation")
    message = str(error.get("Message") or "").strip()
    if not message:
        # The live `AccessDenied` whose CloudTrail `errorMessage` was null. The absence is
        # the only fact there is, so it is reported as an absence: this arm used to fall
        # through to `raise`, which DLQ'd the message and left the item `PENDING` with no
        # `statusReason` at all, and guessing at a cause to fill the gap is the lie the
        # rest of this change exists to end.
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
                # The floor on the allowlist, and the answer to "what does the NEXT
                # unenumerated phrasing cost?". Before this, the answer was an immediate
                # permanent failure: the live "allows assumption by this service" message
                # was not on the list, so every `POST /configurations` failed on its first
                # attempt, and `_new_config_id` being random meant an operator re-issuing it
                # just minted another role and raced the same window again. The answer is
                # now "one slow success" -- never "immediate permanent failure".
                #
                # It claims nothing. One retry, inside the same wall-clock deadline, with a
                # label that names no cause; if the failure recurs at `attempt >= 1` the arm
                # below reports AWS's own words exactly as it does today, one attempt later.
                # That is the whole asymmetry: one retry costs seconds, a missing shape cost
                # the ability to provision at all.
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
    enqueue(config_id, "CHECK", delay=_CHECK_DELAY_SECONDS)


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
                    f"{_CHECK_DELAY_SECONDS}s apart"
                ),
            )
            return
        enqueue(config_id, "CHECK", attempt=attempt + 1, delay=_CHECK_DELAY_SECONDS)
    else:
        _set_config(config_id, status=FAILED, statusReason=f"the runtime reported {status}")


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
    if runtime_id:
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

    secret_arn = str(config.get("secretArn") or "")
    if secret_arn:
        try:
            _common.secrets.delete_secret(SecretId=secret_arn, ForceDeleteWithoutRecovery=True)
        except ClientError as err:
            if not _is_gone(err):
                raise
            LOG.info("secret for config %s was already gone", config_id)

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

    # Read the live runtime back first. `UpdateAgentRuntime` is a full replace, so this
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
    enqueue(config_id, "CHECK", delay=_CHECK_DELAY_SECONDS)


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
