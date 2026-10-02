"""The control plane: the IAM-authorized HTTP API, one Lambda per entry point, the two
queue consumers, the watchdog schedule and the provisioner's three-way-fenced
`iam:CreateRole` grant alongside its AgentCore-only `iam:CreateServiceLinkedRole` one.

**This stack cannot be synthesized from the test suite.** The handler asset is
`lambda_.Code.from_asset("../api", bundling=...)`, which runs `pip install` inside a
container at *construction* time, so no test can build it and `cd infra && cdk synth` is
its only real check -- a keyword threaded wrongly from `app.py` is a synth-time
`TypeError` the suite never sees. `tests/test_grant_contract.py` is the static
replacement: everything a test can reach without Docker is a module-level constant or a
pure function here (`ROUTES`, `QUEUE_ENTRY_POINTS`, `_ENTRY_POINT_GRANTS`,
`_provisioner_environment`, `provisioner_role_statements`), and that file also checks
every keyword `infra/` passes against the callee's own signature.

One Lambda per *entry point*, not per module: `tests/test_api_grant_contract.py`
declares a distinct set of AWS calls per `(module, function)`, and that is only
meaningful if each entry point has its own role. So this stack creates 23 functions --
the 19 declared routes plus the four queue- and schedule-driven ones -- all sharing one
bundled asset, which CDK therefore stages once.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_event_sources
from aws_cdk import aws_logs as logs
from constructs import Construct
from stacks.agent_stack import AgentStack
from stacks.data_stack import DataStack

# The CloudFormation output id the MCP plugin looks up, so that nobody ever types the
# API URL. This is a contract with an already-merged card, not a free choice:
# `mcp/src/rca_mcp/client.py` pins `API_URL_OUTPUT_KEY = "ApiUrl"` and
# `DEFAULT_STACK_NAME = "SampleCodingAgentApi"` (the latter is `app.API_STACK_NAME`), and
# `tests/test_grant_contract.py` asserts both halves still agree.
API_URL_OUTPUT_KEY = "ApiUrl"

# The union of the three HTTP modules' own `ROUTES` tuples, as
# `(method, path, module, function)`. Spelled out here because `api/src` is not on the
# CDK app's import path, so this stack cannot read the handlers' own declarations --
# `tests/test_grant_contract.py` pins this table against them with `ast` instead, so a
# route added to a module and not here fails the suite rather than silently never being
# wired up.
ROUTES: tuple[tuple[str, str, str, str], ...] = (
    ("POST", "/configurations", "configurations", "create"),
    ("GET", "/configurations", "configurations", "list_"),
    ("GET", "/configurations/{configId}", "configurations", "get"),
    ("PATCH", "/configurations/{configId}", "configurations", "patch"),
    ("DELETE", "/configurations/{configId}", "configurations", "delete"),
    ("POST", "/configurations/{configId}/runtime-update", "configurations", "runtime_update"),
    ("POST", "/configurations/{configId}/github-authorize", "configurations", "github_authorize"),
    (
        "POST",
        "/configurations/{configId}/github-authorize/complete",
        "configurations",
        "github_authorize_complete",
    ),
    ("GET", "/configurations/{configId}/github-status", "configurations", "github_status"),
    ("GET", "/configurations/{configId}/memory", "configurations", "memory"),
    ("GET", "/github-app", "github_app", "get"),
    ("POST", "/github-app", "github_app", "connect"),
    ("POST", "/github-app/conversions", "github_app", "convert"),
    ("DELETE", "/github-app", "github_app", "disconnect"),
    ("POST", "/cards", "cards", "create"),
    ("GET", "/cards", "cards", "list_"),
    ("GET", "/cards/{cardId}", "cards", "get"),
    ("PATCH", "/cards/{cardId}", "cards", "patch"),
    ("DELETE", "/cards/{cardId}", "cards", "delete"),
    ("POST", "/cards/{cardId}/move", "cards", "move"),
    ("POST", "/tasks", "tasks", "create"),
    ("GET", "/tasks", "tasks", "list_"),
    ("GET", "/tasks/{taskId}", "tasks", "get"),
    ("GET", "/tasks/{taskId}/activity", "tasks", "activity"),
    ("POST", "/tasks/{taskId}/cancel", "tasks", "cancel"),
)

# The read routes that need a write route's timeout. Declared rather than inferred from the
# grant list, because what makes one slow is an outbound call's own timeout budget and
# nothing in a capability name says so.
_SLOW_READ_ROUTES: frozenset[tuple[str, str]] = frozenset({("configurations", "github_status")})

# The entry points no HTTP route reaches: two queue consumers, the DLQ's terminal owner
# and the schedule.
QUEUE_ENTRY_POINTS: tuple[tuple[str, str], ...] = (
    ("provisioner", "lambda_handler"),
    ("dispatcher", "lambda_handler"),
    ("dispatcher", "dlq_handler"),
    ("watchdog", "lambda_handler"),
)

# The whole grant vocabulary. `_apply_grants` raises on anything not in here, so a typo
# in `_ENTRY_POINT_GRANTS` is a synth-time error rather than a function that quietly
# ends up with one permission fewer than it needs.
_CAPABILITIES: frozenset[str] = frozenset(
    {
        "config:read",
        "config:write",
        "tasks:read",
        "tasks:write",
        "cards:read",
        "cards:write",
        "tasks-queue:send",
        "provision-queue:send",
        "agentcore:control",
        "agentcore:invoke",
        "agentcore:stop",
        # Named for what it grants and not for the service on purpose:
        # `tests/test_grant_contract.py::test_read_routes_get_no_write_capability` refuses a
        # read route's capability containing `agentcore`, so `"agentcore:memory-read"` would
        # force that test to be weakened. The fence that matters is the statement below --
        # two read-only actions on `memory/*` -- not the string.
        "memory:read",
        # Named for what it grants rather than for the service, the same way `memory:read`
        # is: `tests/test_grant_contract.py::test_read_routes_get_no_write_capability`
        # refuses a read route's capability containing `agentcore`, and
        # `configurations.github_status` is a GET that legitimately reaches the token vault.
        # The fence that matters is the statement, not the string.
        "github:credential",
        # The deployment-wide GitHub App: creating, reading, updating and deleting the one
        # OAuth2 credential provider and the one workload identity. Separate from
        # `github:credential` because these are *control-plane* actions no read route may
        # have, and separate from `agentcore:control` because that one is about runtimes and
        # is held by the provisioner, which has no business reconfiguring the credential.
        "github:provider-admin",
        "iam:mint-execution-roles",
    }
)

# One declared grant set per entry point, mirroring
# `tests/test_api_grant_contract.py::_GRANTS`. Declarative rather than derived for two
# reasons: the read routes must be *provably* read-only (note 30 -- a read handler never
# gains a permission to look something up live), and a test can then assert coverage and
# vocabulary without synthesizing anything.
#
# `provision-queue:send` vs `tasks-queue:send` is not cosmetic: `provisioner.enqueue`
# writes to the provision queue and `runs.queue_run`/`runs.enqueue_dispatch` to the task
# queue, so an entry point granted the wrong one fails at run time with an AccessDenied
# that reads like a service problem.
_ENTRY_POINT_GRANTS: dict[tuple[str, str], tuple[str, ...]] = {
    # No credential capability: registration stores no credential at all. `config:read`
    # covers both the sibling query and the `github_app.is_connected()` check, which is a
    # `GetItem` on the same table.
    ("configurations", "create"): ("config:read", "config:write", "provision-queue:send"),
    ("configurations", "list_"): ("config:read",),
    ("configurations", "get"): ("config:read",),
    ("configurations", "patch"): ("config:read", "config:write"),
    ("configurations", "delete"): ("config:read", "config:write", "provision-queue:send"),
    ("configurations", "runtime_update"): ("config:read", "provision-queue:send"),
    # Starting an authorisation and reading one back are the same two AgentCore calls, so
    # both carry the same capability. Neither writes the configuration item: the vault is
    # the only thing that changes, which is why there is no `config:write` here and no
    # stored `githubAuthorized` flag anywhere -- a flag would be a second, staler answer to
    # a question the vault already answers.
    ("configurations", "github_authorize"): ("config:read", "github:credential"),
    # The return leg of the 3LO flow. Same capability as starting it: completing a session is
    # `CompleteResourceTokenAuth`, which lives in the same grant as the exchange it finishes.
    ("configurations", "github_authorize_complete"): ("config:read", "github:credential"),
    ("configurations", "github_status"): ("config:read", "github:credential"),
    # The one read route that reaches a second data plane: the records an operator is asking
    # about exist only in AgentCore Memory, so `config:read` alone could answer with nothing
    # but the `memoryId` the console already prints. `tests/test_api_grant_contract.py`'s
    # memory guard is what bounds it, as an equality over the calls the handler reaches.
    ("configurations", "memory"): ("config:read", "memory:read"),
    ("cards", "create"): ("config:read", "cards:write"),
    ("cards", "list_"): ("cards:read", "tasks:read"),
    ("cards", "get"): ("cards:read", "tasks:read"),
    ("cards", "patch"): ("cards:read", "cards:write", "tasks:read"),
    ("cards", "delete"): ("cards:read", "cards:write", "tasks:read"),
    # `github:credential` on both of the two routes that reach `runs.queue_run`: it refuses
    # a run against a repository with no GitHub authorisation, which costs one AgentCore call
    # on the dispatch path and saves a whole task that would otherwise die at `submit`.
    ("cards", "move"): (
        "cards:read",
        "cards:write",
        "config:read",
        "tasks:read",
        "tasks:write",
        "tasks-queue:send",
        "github:credential",
    ),
    # `cards:write` and not `cards:read`: every run has a card (invariant 38), so
    # `tasks.create` calls `cards.create_claimed` -- a `put_item` and an `update_item`,
    # never a `get_item`. Withholding the read grant is what makes a Cards *read* growing
    # into this handler a grant-contract failure rather than a silent widening.
    ("tasks", "create"): (
        "config:read",
        "tasks:write",
        "tasks-queue:send",
        "cards:write",
        "github:credential",
    ),
    ("tasks", "list_"): ("tasks:read",),
    ("tasks", "get"): ("tasks:read", "config:read"),
    ("tasks", "activity"): ("tasks:read",),
    # The one write route in `tasks.py` that reaches AgentCore: it marks the task and then
    # calls `watchdog.stop_session`, so `agentcore:stop` and nothing wider. No
    # `config:read` -- `agentRuntimeArn` and `sessionId` are already on the task item -- and
    # no `tasks-queue:send`, because cancelling a run dispatches nothing.
    ("tasks", "cancel"): ("tasks:read", "tasks:write", "agentcore:stop"),
    ("github_app", "get"): ("config:read", "github:provider-admin"),
    ("github_app", "connect"): ("config:read", "config:write", "github:provider-admin"),
    ("github_app", "convert"): ("config:read", "config:write", "github:provider-admin"),
    ("github_app", "disconnect"): ("config:read", "config:write", "github:provider-admin"),
    ("provisioner", "lambda_handler"): (
        "config:read",
        "config:write",
        "provision-queue:send",
        "agentcore:control",
        "iam:mint-execution-roles",
    ),
    ("dispatcher", "lambda_handler"): (
        "tasks:read",
        "tasks:write",
        "config:read",
        "agentcore:invoke",
    ),
    ("dispatcher", "dlq_handler"): ("tasks:write",),
    # `config:write` and `provision-queue:send` are the configuration half of the sweep:
    # a provisioning chain whose next message was lost is stranded forever, so the watchdog
    # re-drives it (`CHECK` or `DELETE`, never `CREATE` -- see `watchdog.CONFIG_DRIVERS`) and
    # records the nudge on the item. `provision-queue:send` and not `tasks-queue:send` for
    # that half: a provision message on the task queue is an AccessDenied that reads like a
    # service problem.
    ("watchdog", "lambda_handler"): (
        "tasks:read",
        "tasks:write",
        "config:read",
        "config:write",
        "tasks-queue:send",
        "provision-queue:send",
        "agentcore:stop",
    ),
}


def _provisioner_environment(
    *,
    account: str,
    image_uri: str,
    boundary_arn: str,
    subnet_ids: Sequence[str],
    security_group_ids: Sequence[str],
    plan_model_id: str,
    code_model_id: str,
    haiku_model_id: str,
    fallback_model_id: str,
    goal_enabled: bool,
    git_author_name: str,
    git_author_email: str,
) -> dict[str, str]:
    """The provisioner Lambda's own environment, as a value a test can read.

    The variable *names* here are the contract with `provisioner.py`'s
    `os.environ.get(...)` reads, and a new knob means editing **both** sides -- plus a
    third place for anything the container needs, because that has to be forwarded into
    the runtime's environment at create time *and* converged on update (invariant 24).
    `tests/test_grant_contract.py` calls this function directly and cross-checks the key
    set against the names `provisioner.py` actually reads, so a rename on either side
    fails the suite.

    Every parameter is keyword-only and required, with **no defaults**: a forgotten
    value should be a `TypeError` the signature test catches, not a silently empty
    variable that reaches a runtime and reads there as a missing grant.
    """
    return {
        # The account every ARN in `provisioner._execution_role_policy` is built from, and
        # the trust policy's `aws:SourceAccount`. Forwarded rather than looked up: there is
        # no account id in a Lambda's own environment, `sts:GetCallerIdentity` would be a
        # grant this control plane otherwise does not need, and the value is a literal at
        # synth time. It replaces parsing the account out of a per-configuration secret ARN,
        # which stopped being possible when the GitHub credential moved into AgentCore
        # Identity.
        "AWS_ACCOUNT_ID": account,
        "AGENT_IMAGE_URI": image_uri,
        "EXECUTION_ROLE_BOUNDARY_ARN": boundary_arn,
        "AGENT_SUBNET_IDS": ",".join(subnet_ids),
        "AGENT_SECURITY_GROUP_IDS": ",".join(security_group_ids),
        "PLAN_MODEL_ID": plan_model_id,
        "CODE_MODEL_ID": code_model_id,
        "HAIKU_MODEL_ID": haiku_model_id,
        "FALLBACK_MODEL_ID": fallback_model_id,
        # Normalised to "1"/"0" here rather than passed through as a Python bool, because
        # a Lambda environment variable is a string either way and `provisioner.py` reads
        # this one with `config._env_flag`'s vocabulary.
        "CODE_GOAL_ENABLED": "1" if goal_enabled else "0",
        # The `gitAuthorName`/`gitAuthorEmail` context keys, forwarded as free text rather
        # than normalised: `git_ops._identity` in the container treats "" as unset and falls
        # back to its own default, so "" is a meaningful value here and not a missing one.
        # Both reach exactly one place inside the provisioner -- the fleet group -- so a
        # deploy converges the identity onto every live runtime (invariant 24).
        "AGENT_GIT_AUTHOR_NAME": git_author_name,
        "AGENT_GIT_AUTHOR_EMAIL": git_author_email,
    }


def provisioner_role_statements(
    *, app_name: str, account: str, boundary_arn: str
) -> list[iam.PolicyStatement]:
    """The three-way fence on `iam:CreateRole` (invariant 17) plus the AgentCore
    service-linked-role grant, returned as statements a test can `to_json()` with no Stack.

    The path is `provisioner.ROLE_PATH == "/rca/"` and the name is
    `provisioner._role_name`, i.e. `rca-<configId>` -- so one resource pattern covers
    both fences.

    Invariant 17's fence is unchanged by the fourth statement: that one carries
    `iam:CreateServiceLinkedRole` only, fenced by `iam:AWSServiceName` on AgentCore's own
    service principals, and it neither adds an action to nor relaxes a condition on the
    three statements that mint, tag and pass an execution role.
    """
    minted_roles = [f"arn:aws:iam::{account}:role/{app_name}/{app_name}-*"]
    return [
        iam.PolicyStatement(
            sid="MintExecutionRoles",
            effect=iam.Effect.ALLOW,
            actions=[
                "iam:CreateRole",
                "iam:PutRolePolicy",
                "iam:DeleteRolePolicy",
                "iam:DeleteRole",
            ],
            resources=minted_roles,
            # Fence two of three. Without it this Lambda could mint a role with any
            # policy at all, including an administrator one -- the boundary is a
            # *ceiling* on every role it creates, and requiring it here is what makes
            # that ceiling non-optional.
            conditions={"StringEquals": {"iam:PermissionsBoundary": boundary_arn}},
        ),
        iam.PolicyStatement(
            sid="TagExecutionRoles",
            effect=iam.Effect.ALLOW,
            actions=["iam:TagRole"],
            resources=minted_roles,
            # Invariant 16, and the reason this is a SEPARATE statement carrying NO
            # condition at all: `provisioner._role_kwargs` passes `Tags`, and a
            # `CreateRole` with tags triggers a second, independent `TagRole`
            # authorization whose request context has no `iam:PermissionsBoundary` key.
            # A conditioned statement therefore never matches that authorization, and
            # the *whole* `CreateRole` is denied -- with an error naming TagRole, which
            # reads like a tagging problem rather than the condition it actually is.
            # Path-scoping is the control here; there is nothing else to fence with.
        ),
        iam.PolicyStatement(
            sid="PassToAgentCoreOnly",
            effect=iam.Effect.ALLOW,
            actions=["iam:PassRole"],
            resources=minted_roles,
            # Fence three of three. A role this Lambda may create is only useful if it
            # can also be handed to something; limiting *what* it may be handed to is
            # what stops a minted role being passed to a service of the caller's
            # choosing.
            conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
        ),
        iam.PolicyStatement(
            sid="CreateAgentCoreServiceLinkedRoles",
            effect=iam.Effect.ALLOW,
            actions=["iam:CreateServiceLinkedRole"],
            # `*`, and NOT `role/aws-service-role/*` -- the pattern AWS's own managed
            # policies use. Same lesson as the `agentcore:control` grant below, whose
            # `runtime/*` fence "looked tighter and simply did not work":
            # `CreateServiceLinkedRole` is authorized *before* the role exists, so there is
            # no role ARN to match, and the ARN's path embeds the very service name we
            # cannot confirm from here. That fence bought `implicitDeny` and a null
            # CloudTrail message last time; here it would buy no security either, because
            # `iam:AWSServiceName` already decides *which* service-linked role may be
            # created and AWS -- not this Lambda -- fixes that role's trust policy and its
            # attached managed policy. So this is not the widening `iam:CreateRole` would
            # be: the caller chooses nothing but the service name.
            resources=["*"],
            # `StringLike`, not `StringEquals`. Without this action every
            # `CreateAgentRuntime` in a live eu-west-1 deployment failed `Failed creating
            # service linked role`, because invariant 30 mandates `networkMode: VPC` and
            # AgentCore creates a service-linked role for VPC-mode networking; the only
            # provisioner in that account that succeeds uses `networkMode: PUBLIC` and
            # never reaches this. The exact service name for the VPC-mode role is not
            # documented anywhere reachable, and the AgentCore service-linked role already
            # in the account -- `AWSServiceRoleForBedrockAgentCoreRuntimeIdentity`, for
            # `runtime-identity.bedrock-agentcore.amazonaws.com` -- proves the names are
            # subdomain-qualified, so a `StringEquals` on one guessed name would simply
            # fail the same way again.
            #
            # The wildcard is confined to the subdomain label: both patterns are anchored
            # on the literal suffix `bedrock-agentcore.amazonaws.com`, so the grant covers
            # the bare principal and any `<label>.bedrock-agentcore.amazonaws.com` variant
            # and cannot reach `bedrock.amazonaws.com` or any other service. Two patterns
            # rather than one `*bedrock-agentcore.amazonaws.com`, which would also match a
            # `notbedrock-agentcore.amazonaws.com`; IAM ORs the values of one
            # operator/key pair, so a list is the right spelling.
            #
            # A SEPARATE statement, not another action on `MintExecutionRoles`: that one
            # requires `iam:PermissionsBoundary`, a service-linked role never carries one,
            # so the condition would never match and the call would be denied -- invariant
            # 16's lesson, one statement up.
            conditions={
                "StringLike": {
                    "iam:AWSServiceName": [
                        "bedrock-agentcore.amazonaws.com",
                        "*.bedrock-agentcore.amazonaws.com",
                    ]
                }
            },
        ),
    ]


def _common_environment(data: DataStack) -> dict[str, str]:
    """The five names `api/src/handlers/_common.py` binds at import, plus the log level
    it reads. Every one of the 23 functions gets these: `_common` is imported by all of
    them and an unset table name reads there as a missing grant."""
    return {
        "CONFIG_TABLE": data.configurations.table_name,
        "TASKS_TABLE": data.tasks.table_name,
        "CARDS_TABLE": data.cards.table_name,
        "TASK_QUEUE_URL": data.task_queue.queue_url,
        "PROVISION_QUEUE_URL": data.provision_queue.queue_url,
        "LOG_LEVEL": "INFO",
    }


def _construct_id(module: str, function: str) -> str:
    """`("configurations", "list_")` -> `ConfigurationsList`. A CDK construct id may not
    carry an underscore-heavy handler name, and the id is what the logical id -- and so
    resource replacement -- keys on, so it is derived once here."""
    return "".join(part.title() for part in f"{module}_{function}".strip("_").split("_"))


class ApiStack(cdk.Stack):
    """The control plane. Takes the other two stacks rather than nine loose resources,
    which keeps the keyword count -- and therefore the threading risk from `app.py` --
    down."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        app_name: str,
        data: DataStack,
        agent: AgentStack,
        plan_model_id: str,
        code_model_id: str,
        haiku_model_id: str,
        fallback_model_id: str,
        goal_enabled: bool,
        # The commit identity, keyword-only and required like every other value here. The
        # default lives in the container (`git_ops._DEFAULT_AUTHOR_EMAIL`) and not in a
        # default argument, so there is exactly one place that says what it is.
        git_author_name: str,
        git_author_email: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._app_name = app_name
        self._data = data
        self._agent = agent

        # One asset, built once and reused by all 23 functions.
        #
        # `platform="linux/arm64"` matters for exactly the reason `Architecture.ARM_64`
        # does (invariant 2): a wheel built for x86 installs happily and then fails to
        # import on an ARM64 runtime. `api/requirements.txt` is bundled rather than
        # layered because `bedrock-agentcore` needs a newer botocore than Lambda ships
        # (invariant 20).
        code = lambda_.Code.from_asset(
            str(Path(__file__).resolve().parents[2] / "api"),
            bundling=cdk.BundlingOptions(
                image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                platform="linux/arm64",
                command=[
                    "bash",
                    "-c",
                    "pip install --no-cache-dir -r requirements.txt -t /asset-output "
                    "&& cp -r src/. /asset-output",
                ],
            ),
        )
        self._code = code
        common = _common_environment(data)

        provisioner_environment = {
            **common,
            **_provisioner_environment(
                account=self.account,
                image_uri=agent.image.image_uri,
                boundary_arn=agent.role_boundary.managed_policy_arn,
                # The same subnets the runtime's ENIs land in. Read off the VPC rather
                # than re-declared, so a change to `AgentStack`'s subnet layout cannot
                # leave the provisioner forwarding ids that no longer exist.
                subnet_ids=agent.vpc.select_subnets(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ).subnet_ids,
                security_group_ids=[agent.security_group.security_group_id],
                plan_model_id=plan_model_id,
                code_model_id=code_model_id,
                haiku_model_id=haiku_model_id,
                fallback_model_id=fallback_model_id,
                goal_enabled=goal_enabled,
                git_author_name=git_author_name,
                git_author_email=git_author_email,
            ),
        }

        # -- the HTTP API ------------------------------------------------------------
        #
        # No `cors_preflight` anywhere in this repository: the MCP client SigV4-signs its
        # requests and the console is same-origin behind CloudFront, so there is no
        # cross-origin caller to configure for.
        api = apigwv2.HttpApi(
            self,
            "Api",
            api_name=f"{app_name}-api",
            default_authorizer=apigwv2_authorizers.HttpIamAuthorizer(),
        )

        # `handler_arns` is what `WebStack` consumes, and ARNs are plain strings, so the
        # dependency runs one way only. Passing the `Function` objects across the stack
        # boundary makes the console's integration write a Lambda permission naming the
        # *web* API's id, the two stacks then reference each other, and `cdk synth` fails
        # with a cyclic reference.
        self.handler_arns: dict[str, str] = {}

        for method, path, module, function in ROUTES:
            # A GET is a read route and nothing else is -- which is exactly the entry points
            # `tests/test_grant_contract.py` holds to note 30, including
            # `configurations.memory` and `configurations.github_status`, the two that hold a
            # read-only AgentCore grant beside their DynamoDB one -- so the size
            # classification is derived rather than hand-listed.
            read_route = method == "GET"
            # `github_status` is the one read route that cannot live inside ten seconds:
            # `github_probe.check_can_push` makes three calls to GitHub at a four-second
            # timeout each, so a slow GitHub would otherwise turn a correct answer into a
            # Lambda timeout, which the console would render as a broken repository. It gets
            # a write route's budget without a write route's grants.
            slow_read = (module, function) in _SLOW_READ_ROUTES
            fn = self._function(
                _construct_id(module, function),
                module=module,
                function=function,
                timeout=Duration.seconds(10)
                if read_route and not slow_read
                else Duration.seconds(30),
                memory=256 if read_route else 512,
                environment=common,
                capabilities=_ENTRY_POINT_GRANTS[(module, function)],
            )
            api.add_routes(
                path=path,
                methods=[apigwv2.HttpMethod(method)],
                integration=apigwv2_integrations.HttpLambdaIntegration(f"{module}-{function}", fn),
            )
            self.handler_arns[f"{method} {path}"] = fn.function_arn

        # -- the queue consumers and the schedule ------------------------------------

        provisioner = self._function(
            _construct_id("provisioner", "lambda_handler"),
            module="provisioner",
            function="lambda_handler",
            timeout=Duration.minutes(5),
            memory=512,
            environment=provisioner_environment,
            capabilities=_ENTRY_POINT_GRANTS[("provisioner", "lambda_handler")],
        )
        dispatcher = self._function(
            _construct_id("dispatcher", "lambda_handler"),
            module="dispatcher",
            function="lambda_handler",
            timeout=Duration.minutes(1),
            memory=512,
            environment=common,
            capabilities=_ENTRY_POINT_GRANTS[("dispatcher", "lambda_handler")],
        )
        dlq_handler = self._function(
            _construct_id("dispatcher", "dlq_handler"),
            module="dispatcher",
            function="dlq_handler",
            timeout=Duration.minutes(1),
            memory=512,
            environment=common,
            capabilities=_ENTRY_POINT_GRANTS[("dispatcher", "dlq_handler")],
        )
        watchdog = self._function(
            _construct_id("watchdog", "lambda_handler"),
            module="watchdog",
            function="lambda_handler",
            timeout=Duration.minutes(5),
            memory=512,
            environment=common,
            capabilities=_ENTRY_POINT_GRANTS[("watchdog", "lambda_handler")],
        )

        # `batch_size=1` on all three: a provision message and a dispatch message are
        # each one unit of work, and a partial batch failure would redeliver the healthy
        # ones alongside the broken one.
        provisioner.add_event_source(
            lambda_event_sources.SqsEventSource(data.provision_queue, batch_size=1)
        )
        dispatcher.add_event_source(
            lambda_event_sources.SqsEventSource(data.task_queue, batch_size=1)
        )
        dlq_handler.add_event_source(
            lambda_event_sources.SqsEventSource(data.task_dlq, batch_size=1)
        )

        # 5 minutes against `watchdog.QUIET_THRESHOLD_SECONDS = 2700`, so a stalled task
        # is noticed within a fraction of the window rather than a multiple of it.
        events.Rule(
            self,
            "WatchdogSchedule",
            schedule=events.Schedule.rate(Duration.minutes(5)),
        ).add_target(targets.LambdaFunction(watchdog))

        # The id **is** the output's logical name, and `mcp/src/rca_mcp/client.py` looks
        # that key up on this stack by name -- see `API_URL_OUTPUT_KEY` above on why the
        # stack name and this key are a contract with an already-merged card.
        cdk.CfnOutput(self, API_URL_OUTPUT_KEY, value=api.api_endpoint)

    def _function(
        self,
        key: str,
        *,
        module: str,
        function: str,
        timeout: Duration,
        memory: int,
        environment: dict[str, str],
        capabilities: tuple[str, ...],
    ) -> lambda_.Function:
        fn = lambda_.Function(
            self,
            key,
            runtime=lambda_.Runtime.PYTHON_3_12,
            # Invariant 2. An x86 function pulling ARM64-built wheels, or the reverse,
            # is accepted at deploy time and fails on the first import.
            architecture=lambda_.Architecture.ARM_64,
            handler=f"handlers.{module}.{function}",
            code=self._code,
            timeout=timeout,
            memory_size=memory,
            environment=environment,
            # `log_retention=` is deprecated in aws-cdk-lib 2.266 (it provisions a
            # custom resource per function and emits a warning); an explicit log group is
            # one line in one helper and leaves no Lambda-backed resource behind.
            log_group=logs.LogGroup(
                self,
                f"{key}Logs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        self._apply_grants(fn, capabilities)
        return fn

    def _apply_grants(  # noqa: C901, PLR0912 -- one arm per capability; complexity IS the vocabulary size
        self, fn: lambda_.Function, capabilities: tuple[str, ...]
    ) -> None:
        """Map each declared capability string onto the CDK grant it names, raising on
        an unknown one so a typo in `_ENTRY_POINT_GRANTS` fails at synth rather than
        producing a function with one permission fewer than it needs."""
        data, agent = self._data, self._agent
        for capability in capabilities:
            if capability not in _CAPABILITIES:
                raise ValueError(
                    f"{capability!r} is not a capability this stack knows how to grant; "
                    f"expected one of {sorted(_CAPABILITIES)}"
                )
            if capability == "config:read":
                data.configurations.grant_read_data(fn)
            elif capability == "config:write":
                data.configurations.grant_write_data(fn)
            elif capability == "tasks:read":
                data.tasks.grant_read_data(fn)
            elif capability == "tasks:write":
                data.tasks.grant_write_data(fn)
            elif capability == "cards:read":
                data.cards.grant_read_data(fn)
            elif capability == "cards:write":
                data.cards.grant_write_data(fn)
            elif capability == "tasks-queue:send":
                data.task_queue.grant_send_messages(fn)
            elif capability == "provision-queue:send":
                data.provision_queue.grant_send_messages(fn)
            elif capability == "github:credential":
                fn.add_to_role_policy(
                    iam.PolicyStatement(
                        # The same two actions the container's execution role gets, for the
                        # same exchange -- see `provisioner._execution_role_policy`'s
                        # `UseGitHubCredential`, which carries the measurement behind these
                        # resource patterns. In short: the service authorizes
                        # `GetWorkloadAccessTokenForUserId` only on
                        # `workload-identity-directory/*` and `GetResourceOauth2Token` only
                        # on `*`, with no condition key for the user id, so neither can be
                        # narrowed further. That is why this capability is spelled out
                        # separately instead of folded into `agentcore:control`: a reader has
                        # to see that it is wide, and `CLAUDE.md`'s "What remains exposed"
                        # names it.
                        actions=[
                            "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                            "bedrock-agentcore:GetResourceOauth2Token",
                            # The return leg: without it the operator approves on GitHub, every
                            # signal says success, and the vault stores nothing.
                            "bedrock-agentcore:CompleteResourceTokenAuth",
                        ],
                        resources=[
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
                            f":workload-identity-directory/*",
                            "*",
                        ],
                    )
                )
                fn.add_to_role_policy(
                    iam.PolicyStatement(
                        sid="ExchangeTokensWithTheProvidersSecret",
                        # **AgentCore Identity does its Secrets Manager work with the CALLER's
                        # permissions**, so the code exchange inside
                        # `CompleteResourceTokenAuth` reads the provider's client secret as this
                        # role and fails `AccessDenied` without this. Measured the hard way: the
                        # operator approved on GitHub, the console reported
                        # `AuthorizationNotCompleted`, and the message named this exact secret
                        # ARN and this exact role.
                        #
                        # This is the same property that disqualified the API-key credential
                        # provider earlier -- Identity is a broker that borrows the caller's
                        # reach rather than insulating it from the store -- and it is the reason
                        # the OAuth path cannot honestly be described as "no Secrets Manager
                        # anywhere". It is *narrower* here than it was there: only these four
                        # control-plane Lambdas hold it, and only on AgentCore's own
                        # service-linked prefix, never on a secret this repository creates.
                        actions=["secretsmanager:GetSecretValue"],
                        resources=[
                            f"arn:aws:secretsmanager:{self.region}:{self.account}"
                            f":secret:bedrock-agentcore-identity!*"
                        ],
                    )
                )
            elif capability == "github:provider-admin":
                fn.add_to_role_policy(
                    iam.PolicyStatement(
                        actions=[
                            # The one OAuth2 credential provider, read/written on the connect
                            # page. `List*` is deliberately absent: nothing here enumerates
                            # providers, and a provider this deployment was not given is not
                            # one it should be able to find.
                            "bedrock-agentcore:CreateOauth2CredentialProvider",
                            "bedrock-agentcore:GetOauth2CredentialProvider",
                            "bedrock-agentcore:UpdateOauth2CredentialProvider",
                            "bedrock-agentcore:DeleteOauth2CredentialProvider",
                            # The one workload identity whose `allowedResourceOauth2ReturnUrls`
                            # is what stops an authorisation being redirected anywhere the
                            # console is not.
                            "bedrock-agentcore:CreateWorkloadIdentity",
                            "bedrock-agentcore:GetWorkloadIdentity",
                            "bedrock-agentcore:UpdateWorkloadIdentity",
                            # `CreateTokenVault` is needed because the account's default token
                            # vault is created lazily, by the first
                            # `CreateOauth2CredentialProvider` -- measured: without it the
                            # very first connect in a fresh account fails `AccessDenied`
                            # naming a vault nobody asked for. `SetTokenVaultCMK` is **not**
                            # granted: changing the vault's encryption key is not something a
                            # console page should be able to do.
                            "bedrock-agentcore:CreateTokenVault",
                            "bedrock-agentcore:GetTokenVault",
                        ],
                        # `*` for the same reason `agentcore:control` takes it: every one of
                        # these creates is authorized before the resource exists, so no
                        # pattern over the resource would match. The fence is the action list.
                        resources=["*"],
                    )
                )
                fn.add_to_role_policy(
                    iam.PolicyStatement(
                        sid="AgentCoreIdentityServiceLinkedSecret",
                        # The **only** Secrets Manager grant left in this system, and it is a
                        # control-plane one: no container has it, nothing in this repository
                        # names the secret, reads it, or knows its ARN.
                        #
                        # AgentCore Identity keeps a provider's client secret in a
                        # service-linked secret of its own and authorizes that write against
                        # the *caller*, so `CreateOauth2CredentialProvider` fails
                        # `AccessDenied` without `CreateSecret` and
                        # `DeleteOauth2CredentialProvider` fails the same way without
                        # `DeleteSecret` -- both measured, the second one only after the first
                        # looked finished. `PutSecretValue` is the update path, which is what
                        # writes the real client secret once the GitHub App exists.
                        actions=[
                            "secretsmanager:CreateSecret",
                            "secretsmanager:PutSecretValue",
                            "secretsmanager:DeleteSecret",
                            "secretsmanager:TagResource",
                        ],
                        # Scoped to AgentCore's own name prefix rather than `*`, which was
                        # measured to work and matters: on `*` this Lambda could mint and
                        # destroy any secret in the account. The `!` is part of the
                        # service-linked naming scheme (`bedrock-agentcore-identity!default/...`)
                        # and the trailing `*` also absorbs the six random characters every
                        # secret ARN ends with, so a pattern stopping at the name matches
                        # nothing.
                        resources=[
                            f"arn:aws:secretsmanager:{self.region}:{self.account}"
                            f":secret:bedrock-agentcore-identity!*"
                        ],
                    )
                )
            elif capability == "agentcore:control":
                fn.add_to_role_policy(
                    iam.PolicyStatement(
                        actions=[
                            "bedrock-agentcore:CreateAgentRuntime",
                            "bedrock-agentcore:GetAgentRuntime",
                            "bedrock-agentcore:UpdateAgentRuntime",
                            "bedrock-agentcore:DeleteAgentRuntime",
                            # The four `*AgentRuntimeEndpoint` actions are NOT here because
                            # this code calls them -- it never does, invariant 14 keeps
                            # `ENDPOINT_NAME = "DEFAULT"` and creates no custom endpoint.
                            # They are here because `CreateAgentRuntime` creates that DEFAULT
                            # endpoint *for* you and authorizes it as a separate action, so
                            # the caller needs the grant anyway. Without it the very first
                            # provision in a fresh deployment fails:
                            #
                            #   not authorized to perform:
                            #   bedrock-agentcore:CreateAgentRuntimeEndpoint on resource:
                            #   ...:runtime/* because no identity-based policy allows ...
                            #
                            # and it fails *silently for 26 minutes*: the message contains
                            # "not authorized", which `provisioner._is_role_propagation_error`
                            # reads as IAM propagation, so it retried ten times and then
                            # reported "the execution role was still not assumable", a
                            # diagnosis with nothing to do with the cause. Delete/Update are
                            # included for the same reason on the teardown and update paths.
                            "bedrock-agentcore:ListAgentRuntimes",
                            # `TagResource` and the four workload-identity actions are the
                            # rest of what `CreateAgentRuntime` does under the covers: it
                            # creates a **workload identity** for the runtime, and that is
                            # authorized as its own action against its own resource type
                            # (`workload-identity-directory/...`, not `runtime/...`).
                            "bedrock-agentcore:TagResource",
                            # The provisioner mints one AgentCore Memory per registered
                            # repository in `_create` and tears it down in `_delete`, so it
                            # needs the two control-plane actions for it. `TagResource`
                            # above is also what `create_memory(tags=...)` authorizes --
                            # separately from `CreateMemory`, the same shape invariant 16
                            # records for `CreateRole` passing `Tags` -- so a
                            # `TagResource` dropped as "runtime-only" breaks the memory
                            # creation too. The six *data-plane* memory actions the
                            # container uses are not here: they belong to the execution
                            # role's inline policy (`provisioner._execution_role_policy`'s
                            # `UseOwnMemory`), not to this Lambda.
                            "bedrock-agentcore:CreateMemory",
                            "bedrock-agentcore:DeleteMemory",
                            # Read-only, and only to learn one ARN: the provider's client
                            # secret, which every execution role is granted `GetSecretValue` on
                            # so a container can refresh its own token. `convert` stores it on
                            # the settings item; this is the backfill for a deployment connected
                            # before that existed (`github_app.provider_secret_arn`). Without it
                            # that backfill is an AccessDenied inside every provision.
                            "bedrock-agentcore:GetOauth2CredentialProvider",
                        ]
                        + [
                            f"bedrock-agentcore:{verb}AgentRuntimeEndpoint"
                            for verb in ("Create", "Get", "Update", "Delete")
                        ]
                        + [
                            f"bedrock-agentcore:{verb}WorkloadIdentity"
                            for verb in ("Create", "Get", "Update", "Delete")
                        ],
                        # `*`, and NOT the `runtime/*` fence this used to carry. That fence
                        # looked tighter and simply did not work: `CreateAgentRuntime` is
                        # authorized before any runtime exists, so there is no runtime ARN for
                        # `runtime/*` to match, and `CreateWorkloadIdentity` acts on a
                        # different resource type altogether. `aws iam
                        # simulate-principal-policy` against the real ARN is unambiguous --
                        # `CreateAgentRuntimeEndpoint` came back `allowed` while
                        # `CreateAgentRuntime` came back `implicitDeny` from the *same*
                        # statement -- and every `CreateAgentRuntime` call failed
                        # `AccessDenied` with a null message in CloudTrail.
                        #
                        # `CreateMemory` has the same shape as `CreateAgentRuntime` here: it
                        # is authorized before the memory exists, so no `memory/*` pattern
                        # would match it either.
                        #
                        # So the fence here is the ACTION LIST, not the resource: sixteen
                        # named actions on one service, no wildcards. What still bounds which
                        # role can be handed to a runtime is the `iam:PassRole` statement,
                        # which stays scoped to `role/rca/rca-*` with an
                        # `iam:PassedToService` condition -- that is the grant that actually
                        # matters, because it is what stops this Lambda attaching an arbitrary
                        # role to a container.
                        resources=["*"],
                    )
                )
            elif capability == "agentcore:invoke":
                fn.add_to_role_policy(
                    iam.PolicyStatement(
                        actions=["bedrock-agentcore:InvokeAgentRuntime"],
                        resources=[
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*",
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
                            f":runtime/*/runtime-endpoint/*",
                        ],
                    )
                )
            elif capability == "agentcore:stop":
                fn.add_to_role_policy(
                    iam.PolicyStatement(
                        actions=["bedrock-agentcore:StopRuntimeSession"],
                        resources=[
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*",
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
                            f":runtime/*/runtime-endpoint/*",
                        ],
                    )
                )
            elif capability == "memory:read":
                fn.add_to_role_policy(
                    iam.PolicyStatement(
                        # Exactly two actions, both read-only. No `GetMemoryRecord` (this
                        # route never fetches one record by id), no `CreateEvent` (a
                        # control-plane Lambda does not write a repository's memory -- the
                        # container does, through `provisioner._execution_role_policy`'s
                        # `UseOwnMemory`), and none of the six destructive ones
                        # (`DeleteEvent`, `DeleteMemoryRecord`, `BatchDeleteMemoryRecords`,
                        # `CreateMemory`, `UpdateMemory`, `DeleteMemory`) nor `ListMemories`,
                        # which is how a caller finds a memory it was not given.
                        actions=[
                            "bedrock-agentcore:RetrieveMemoryRecords",
                            "bedrock-agentcore:ListMemoryRecords",
                        ],
                        # `memory/*`, and it CANNOT be a literal memory ARN the way the
                        # container's `UseOwnMemory` statement is. That statement is minted
                        # per configuration at run time, so it can name the one `memoryId` on
                        # the item; this role is minted once per deploy and serves every
                        # registered repository, so there is no single id to name -- the same
                        # shape as the boundary's own `MayUseOwnMemory`, which says `memory/*`
                        # because it is created before any memory exists. So the fence here is
                        # the two read-only actions plus the `memory/*` prefix (one account,
                        # one region), not one id; what stops this route reading a memory the
                        # caller did not ask about is the handler resolving the namespace from
                        # the configuration item it just read.
                        resources=[
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/*"
                        ],
                    )
                )
            else:  # "iam:mint-execution-roles"
                for statement in provisioner_role_statements(
                    app_name=self._app_name,
                    account=self.account,
                    boundary_arn=agent.role_boundary.managed_policy_arn,
                ):
                    fn.add_to_role_policy(statement)
