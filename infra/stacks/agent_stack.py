"""`AgentStack`: everything a per-configuration runtime needs to exist *before* it can be
created, and deliberately **no runtime at all**.

The image asset, the VPC and the permissions boundary are grouped by *lifecycle*, not by
tidiness: none of the three is created per API call. A runtime is. `provisioner._create`
mints one on a `POST /configurations` and `provisioner._delete` tears it down on a
`DELETE`, so a runtime declared here would give one resource two owners -- `cdk destroy`
would delete what the provisioner is still tracking in DynamoDB, and a `CreateAgentRuntime`
would collide with a CloudFormation-managed name. A resource created per API call belongs
to the data plane; this stack is the part of the plane that has to be standing first.

`ApiStack`'s Lambdas stay **outside** this VPC, deliberately. They speak to DynamoDB, SQS,
Secrets Manager, IAM and the AgentCore control plane, and not one of those calls carries a
prompt or a line of a repository -- so attaching them buys no part of the in-boundary
claim, while costing VPC cold starts and a second set of endpoints.

Public attributes, because `ApiStack` reads exactly these four and nothing else:
`image`, `role_boundary`, `vpc`, `security_group`.

The Route 53 Resolver DNS Firewall deny list is here, with its query log and an alarm on
any single hit, so the vendor domains this design says it never contacts are refused by
the network rather than by a flag -- and the log is the evidence. What that does *not*
close is 443 to an arbitrary host: a TLS-inspecting egress allowlist (an AWS Network
Firewall restricted to `github.com`, `pypi.org`, `files.pythonhosted.org` and
`registry.npmjs.org`) is what would, and it is a project rather than a config change, so
it is roadmap rather than MVP and README's "Known gaps" names its absence.

The whole network lives in the module-level `add_agent_network` rather than inline in
`__init__` for one reason: the image asset above makes this stack unsynthesizable without
a Docker daemon, and a function taking the scope it builds on is what
`tests/test_agent_network.py` can reach. A `FirewallRuleGroup` with no association, and
`action="ALERT"` where `BLOCK` was meant, both read perfectly in source and do nothing --
those are template assertions or they are nothing. Every construct id is created on the
scope it is handed, so no logical id moves.

The WAF web ACL in front of the console and the Bedrock spend budget alarm are no longer
deferred, and are deliberately **not** here: both are account-wide and one of them has to
be `us-east-1` to be CloudFront-scoped at all, so they live in
`infra/stacks/account_stack.py` with `tests/test_account_stack.py` on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53resolver as route53resolver
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as sns_subscriptions
from constructs import Construct

# Spelled out rather than imported, for the same reason `api_stack.ROUTES` is a literal:
# `api/src` is not on the CDK app's import path. It must stay identical to
# `api/src/handlers/observability.py::LOG_GROUP_PREFIX`, because the per-configuration
# policy scopes the container's `logs:*` grants to this prefix and a boundary that does not
# cover it denies every log line the runtime writes -- which surfaces as a runtime with no
# logs, i.e. as the one symptom that makes the cause unfindable.
RUNTIME_LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes"

# The names this design claims it never contacts, turned into something the network
# enforces. Every entry is silent when it is wrong in either direction: one missing is a
# call that leaves the VPC and nothing says so, and one too many is an `NXDOMAIN` in the
# middle of a run whose symptom is a `setupCommand` or `git` failure that reads like a
# flaky network.
#
# Enumerated rather than a blanket `*.anthropic.com`, deliberately: the point of the list
# is that each name is one this design says it does not use, and a blanket rule would also
# be a claim nobody wrote down -- it would deny names nobody checked, which is how the
# paragraph above stops being true of the code.
#
# The `*.` prefix is DNS Firewall's wildcard form and it is not decoration. A bare
# `claudeusercontent.com` matches that exact name and **no subdomain of it**, and the
# subdomains are the whole set of names actually used.
#
# `github.com`, `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org` and
# `docs.aws.amazon.com` must never appear here: those are the destinations Qualification 3
# says are unavoidable, and denying one of them takes every run down at `setupCommand`
# time. `tests/test_agent_network.py` asserts their absence.
BLOCKED_VENDOR_DOMAINS: tuple[str, ...] = (
    "api.anthropic.com",
    "downloads.claude.ai",
    "mcp-proxy.anthropic.com",
    "*.claudeusercontent.com",
    "*.datadoghq.com",
    "storage.googleapis.com",
    "code.claude.com",
)

# A month is long enough to answer "did this ever happen?" after an alarm and short enough
# that nobody pays to store DNS queries for a year.
DNS_QUERY_LOG_RETENTION = logs.RetentionDays.ONE_MONTH
DNS_BLOCK_METRIC_NAME = "BlockedVendorLookups"
# The field is documented as populated only when DNS Firewall matched a rule with
# action ALERT or BLOCK, so this pattern is exactly "a deny-list hit". It is also the one
# thing here checked against documentation rather than against a real log record: if the
# alarm never fires on a live deploy, compare this against an actual line in the query log
# group before believing the docs a second time.
DNS_BLOCK_FILTER_PATTERN = '{ $.firewall_rule_action = "BLOCK" }'


@dataclass(frozen=True)
class AgentNetwork:
    """What `add_agent_network` hands back. `ApiStack` reads `vpc` and `security_group`."""

    vpc: ec2.Vpc
    security_group: ec2.SecurityGroup
    dns_query_log_group: logs.LogGroup


def add_agent_network(scope: Construct, *, app_name: str, alert_email: str) -> AgentNetwork:
    """The VPC, its endpoints, the 443-only security group, and the DNS Firewall (Tier 2 #16).

    A module-level function taking the scope it builds on, rather than a `Construct`
    subclass or a method: `AgentStack` cannot be synthesized by any test here (the image
    asset needs a Docker daemon), and a subclass would prefix every child's logical id and
    so replace the VPC, the NAT gateway and all eight endpoints on the next deploy. Called
    with `self` from `AgentStack.__init__`, every id below is exactly what it was before
    this function existed.
    """
    vpc = ec2.Vpc(
        scope,
        "AgentVpc",
        ip_addresses=ec2.IpAddresses.cidr("10.42.0.0/16"),
        max_azs=2,
        # One NAT gateway, not one per AZ: the runtime's egress is `git clone`, PyPI and
        # npm, and an hour of downtime in one AZ costs a retry rather than an incident.
        nat_gateways=1,
        subnet_configuration=[
            # The public subnets exist only to hold the NAT gateway, hence the /24.
            ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
            ec2.SubnetConfiguration(
                name="Agent", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=20
            ),
        ],
    )

    # `allow_all_outbound=False` and then exactly one rule. The NAT path exists for
    # `github.com`, PyPI and npm and for nothing else, so anything that is not TLS
    # leaves no route out -- which is the difference between "the container talks to
    # the internet" and "the container makes HTTPS requests we can enumerate".
    #
    # No UDP/53 rule, and adding one would be dead surface that also invites widening
    # egress: the VPC User Guide is explicit that traffic to and from the Amazon DNS server
    # cannot be filtered with security groups or network ACLs at all, so resolution works
    # regardless of what this group says. That is *why* the DNS Firewall below -- which is
    # not a security group -- is the only thing that can see and refuse a lookup.
    security_group = ec2.SecurityGroup(
        scope,
        "AgentSecurityGroup",
        vpc=vpc,
        description=f"{app_name} agent runtime egress: 443 only",
        allow_all_outbound=False,
    )
    security_group.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "443 only")

    # The endpoints are what make the in-boundary claim *demonstrable* rather than
    # asserted: the calls carrying prompts and repository content -- Bedrock, the GitHub
    # credential out of the AgentCore token vault, the task rows, the log lines, the memory a
    # later task reads back -- resolve to a private address in these subnets and never
    # traverse the internet. Without them the same traffic works fine over the NAT
    # gateway, and the claim becomes a sentence in a document that nothing enforces.
    #
    # `SECRETS_MANAGER` used to be in this list and is **gone with the secret**: the
    # container's GitHub credential now comes from the AgentCore data plane, which the
    # `AgentCore` endpoint below already covers. AgentCore Identity does keep the GitHub
    # App's client secret in a Secrets Manager secret of its own, but that is written and
    # read *service-side* from a control-plane Lambda outside this VPC, so an endpoint here
    # would be an ENI in every AZ with no client -- the same reasoning the AgentCore comment
    # below applies to `bedrock-agentcore-control`.
    private_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
    for name, service in (
        ("BedrockRuntime", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
        # Both halves of an image pull: `ECR` is the API that hands out the auth token,
        # `ECR_DOCKER` is the registry that serves the layers. One without the other
        # fails the pull, which presents as a runtime that never reaches `READY`.
        ("Ecr", ec2.InterfaceVpcEndpointAwsService.ECR),
        ("EcrDocker", ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER),
        ("CloudWatchLogs", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
        # Two content-carrying call groups ride this one endpoint, and both would otherwise
        # leave the VPC over the NAT gateway to reach a service in the same account:
        # `agentcore_memory.py`'s `create_event`/`retrieve_memory_records` -- a task's own
        # transcript -- and `store.github_token`'s two-call credential exchange. It needs no
        # change in the container: `private_dns_enabled` defaults on, so the regional name
        # `boto3.client("bedrock-agentcore")` already resolves now answers privately.
        #
        # The **data plane** only. The control plane is `bedrock-agentcore-control`, which
        # `provisioner.py` calls for `CreateMemory`/`DeleteMemory` from Lambdas this module's
        # docstring keeps deliberately outside this VPC -- an endpoint for it here would be an
        # ENI in every AZ with no client. `BEDROCK_AGENTCORE_GATEWAY` is a third service
        # (`bedrock-agentcore.gateway`) and nothing here speaks to a gateway either.
        ("AgentCore", ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE),
    ):
        vpc.add_interface_endpoint(f"{name}Endpoint", service=service, subnets=private_subnets)

    # Gateway endpoints are route-table entries rather than ENIs, so they are free and
    # carry no security group. S3 is here because an ECR layer *is* an S3 object.
    for name, gateway_service in (
        ("DynamoDb", ec2.GatewayVpcEndpointAwsService.DYNAMODB),
        ("S3", ec2.GatewayVpcEndpointAwsService.S3),
    ):
        vpc.add_gateway_endpoint(f"{name}Endpoint", service=gateway_service)

    # -- the DNS Firewall deny list (Tier 2 #16) ------------------------------
    deny_list = route53resolver.CfnFirewallDomainList(
        scope,
        "VendorDenyList",
        name=f"{app_name}-agent-vendor-deny",
        domains=list(BLOCKED_VENDOR_DOMAINS),
    )

    firewall = route53resolver.CfnFirewallRuleGroup(
        scope,
        "DnsFirewall",
        name=f"{app_name}-agent-dns-firewall",
        firewall_rules=[
            route53resolver.CfnFirewallRuleGroup.FirewallRuleProperty(
                # `BLOCK`, and `ALERT` is the silent alternative: it writes the *same* log
                # line and then lets the query through, so the log reads identically while
                # the control does nothing at all.
                action="BLOCK",
                priority=10,
                firewall_domain_list_id=deny_list.attr_id,
                # So the container sees "no such host", which is diagnosable in a stack
                # trace, rather than an empty answer that reads as a network glitch.
                block_response="NXDOMAIN",
            )
        ],
    )

    # The association is what makes the rule group apply to anything, and it is the failure
    # mode this whole construct has: a rule group with no association synthesizes, deploys,
    # reports success and filters nothing. `priority` must be between 100 and 9900.
    #
    # `mutation_protection` is left at its default (`DISABLED`) on purpose: `ENABLED`
    # disallows *removal* of the association, so `cdk destroy` of this stack fails on the
    # association rather than tearing down. The deny list is guarded by a test in this
    # repository instead of by a flag that breaks teardown.
    route53resolver.CfnFirewallRuleGroupAssociation(
        scope,
        "DnsFirewallAssociation",
        firewall_rule_group_id=firewall.attr_id,
        priority=101,
        vpc_id=vpc.vpc_id,
    )

    # An explicit name, under `/aws/route53/`, so an operator chasing the alarm finds the
    # log without a console hunt and so it matches the convention Route 53's other
    # query-logging destinations use. A fixed name means a second deployment sharing
    # `app_name` in one account collides loudly at `CreateLogGroup` -- the same trade
    # `account_stack.py` takes for the budget name, and the right one for the same reason.
    log_group = logs.LogGroup(
        scope,
        "DnsQueryLogs",
        log_group_name=f"/aws/route53/{app_name}-agent-dns",
        retention=DNS_QUERY_LOG_RETENTION,
        removal_policy=cdk.RemovalPolicy.DESTROY,
    )

    logging_config = route53resolver.CfnResolverQueryLoggingConfig(
        scope,
        "DnsQueryLogging",
        name=f"{app_name}-agent-dns",
        destination_arn=log_group.log_group_arn,
    )

    # The second association, and the second thing that is invisible when it is missing: a
    # logging config with nothing associated produces an empty log group, which reads
    # exactly like "no vendor lookups happened".
    route53resolver.CfnResolverQueryLoggingConfigAssociation(
        scope,
        "DnsQueryLoggingAssociation",
        resolver_query_log_config_id=logging_config.attr_id,
        resource_id=vpc.vpc_id,
    )

    blocked_filter = logs.MetricFilter(
        scope,
        "DnsBlockedFilter",
        log_group=log_group,
        filter_pattern=logs.FilterPattern.literal(DNS_BLOCK_FILTER_PATTERN),
        metric_namespace=f"{app_name}/dns",
        metric_name=DNS_BLOCK_METRIC_NAME,
        metric_value="1",
        default_value=0,
    )

    alarm = cloudwatch.Alarm(
        scope,
        "DnsBlockedAlarm",
        metric=blocked_filter.metric(statistic="Sum", period=cdk.Duration.minutes(5)),
        # `threshold=0` with `GREATER_THAN_THRESHOLD` over one period is "any hit at all",
        # which is the point: the alarm exists to tell you that a CLI upgrade introduced a
        # new call, and one such call is the whole signal.
        threshold=0,
        evaluation_periods=1,
        comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        # Load-bearing. No blocked lookup means no log line means no datapoint, and the
        # default (`MISSING`) parks this alarm in `INSUFFICIENT_DATA` forever -- which on
        # the console is indistinguishable from an alarm that was never wired up.
        treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        alarm_description=(
            f"A {app_name} agent runtime looked up a domain on the vendor deny list, so "
            "something in the container is trying to contact a destination this design "
            "says it never contacts -- most likely a CLI upgrade that introduced a new "
            f"call. The blocked query is in {log_group.log_group_name}."
        ),
    )

    # Same shape as `account_stack.AccountStack._notifications`, and for the same reason: an
    # alarm nobody hears is the same failure as no alarm, and the annotation is what stops
    # that being silent in the synth output of the deploy that caused it.
    address = alert_email.strip()
    if address:
        topic = sns.Topic(
            scope, "DnsBlockedAlerts", display_name=f"{app_name} blocked vendor DNS lookups"
        )
        topic.add_subscription(sns_subscriptions.EmailSubscription(address))
        alarm.add_alarm_action(cloudwatch_actions.SnsAction(topic))
    else:
        cdk.Annotations.of(scope).add_warning(
            "no alert address was given, so the blocked-vendor-lookup alarm was created "
            "with no action and nobody will hear it. Deploy with "
            "'make deploy EMAIL=you@example.com' (or -c budgetAlertEmail=...) to wire it."
        )

    return AgentNetwork(vpc=vpc, security_group=security_group, dns_query_log_group=log_group)


def execution_role_boundary_statements(
    *,
    account: str,
    region: str,
    asset_repository_arn: str,
) -> list[iam.PolicyStatement]:
    """The permissions boundary every per-configuration execution role is minted under.

    A boundary is a **ceiling**, not a grant: nothing here gives a role anything. It is
    therefore the union of what `provisioner._execution_role_policy` grants and no wider --
    an action the inline policy allows and this list omits is silently denied at runtime,
    and an action this list allows and the inline policy omits is dead surface a future
    inline policy could take without review. Both halves of that sentence are now
    enforced rather than promised:
    `tests/test_api_grant_contract.py::test_the_boundary_permits_every_action_the_inline_policy_grants`
    for the first, and `::test_the_boundary_permits_no_action_the_inline_policy_does_not_grant`
    reading the classification in `tests/execution_boundary.py` for the second.

    The *resource* dimension is deliberately wider in four statements -- `MayTouchOwnRows`,
    `MayUseGitHubCredential`, `MayWriteRuntimeLogs` and `MayUseOwnMemory`, each for the reason
    its own comment below gives -- and each of the four is declared in
    `execution_boundary.WIDER_RESOURCES`, so widening a fifth costs a line in that table with
    a reason rather than passing as a silent change. Same idiom as
    `provisioner._execution_role_policy`'s docstring pointing back at this ceiling.

    `app_name` used to be a parameter and is gone: the only statement that needed it was
    `MayReadOneSecret`, naming the `rca/github/*` secret prefix, and there is no secret any
    more. A boundary parameter that no statement reads is a signature a reader has to check
    against the body.

    `asset_repository_arn` is the one parameter that is not a name: the union above cannot
    be written without naming the repository the agent image lives in, and this function is
    stack-free (below), so it cannot reach the asset itself. It is an ARN rather than an
    image URI because at synth time `ecr_assets.DockerImageAsset.image_uri` is an
    unresolved CDK token -- there is no `<account>.dkr.ecr...` string here to parse, only a
    `${Token[...]}` placeholder -- whereas `.repository.repository_arn` is a token that
    resolves to exactly the repository the provisioner derives from `AGENT_IMAGE_URI` at run
    time. Required rather than defaulted, for the reason `provisioner._fleet_network`'s
    docstring gives about `live`: a default is how a future caller loses the grant without
    noticing, and a ceiling one action too narrow is silent at run time.

    Pure and stack-free on purpose: `iam.PolicyStatement.to_json()` needs no `Stack`, so
    `tests/test_grant_contract.py` reads these statements directly. That test is the only
    thing standing between invariant 21 and a fleet that cannot invoke a model, because
    this stack cannot be synthesized in CI (the image asset needs a Docker daemon).
    """
    return [
        iam.PolicyStatement(
            sid="MayInvokeClaude",
            effect=iam.Effect.ALLOW,
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                # Invariant 21, and the one line here that is silent when wrong: **no
                # region**. A `global.` inference profile fans the call out to whichever
                # commercial region serves the request, so a region in this ARN denies the
                # call from every other one -- and the denial names the *model*, which
                # reads as a bad model id or an unentitled account rather than as a policy
                # written one field too narrow. `_execution_role_policy` spells the same
                # ARN the same way; if either moves, both move.
                "arn:aws:bedrock:*::foundation-model/anthropic.*",
                f"arn:aws:bedrock:*:{account}:inference-profile/*",
            ],
        ),
        iam.PolicyStatement(
            sid="MayTouchOwnRows",
            effect=iam.Effect.ALLOW,
            # The four actions the container performs, and no `DeleteItem`, no `Scan`, no
            # `BatchWriteItem`: `configurations.list_` scans, but that is a control-plane
            # route, not a thing any runtime does.
            actions=[
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:Query",
            ],
            # `table/*` rather than the two table ARNs: `DataStack` owns the names and is a
            # sibling stack, so naming them here would couple two deploys for no security.
            # The `dynamodb:LeadingKeys` fence that stops one repository reading another's
            # rows lives in the per-configuration policy (invariant 31); a boundary's job
            # is to stop a *wider action*, not to restate a condition -- restating it here
            # would mean every future fence had to be written twice to work at all.
            resources=[f"arn:aws:dynamodb:{region}:{account}:table/*"],
        ),
        iam.PolicyStatement(
            sid="MayUseGitHubCredential",
            effect=iam.Effect.ALLOW,
            # The ceiling over `provisioner._execution_role_policy`'s `UseGitHubCredential`:
            # the two calls that exchange this configuration's id for the GitHub token
            # AgentCore Identity holds. There is no `secretsmanager:GetSecretValue` in this
            # boundary any more, because there is no secret -- the container's whole
            # credential path is these two actions.
            actions=[
                "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                "bedrock-agentcore:GetResourceOauth2Token",
            ],
            # Declared in `execution_boundary.WIDER_RESOURCES`, and the widest entry in this
            # list by some distance. It is not a choice: measured against a throwaway role,
            # `GetWorkloadAccessTokenForUserId` is denied on an individual
            # `workload-identity/<name>` ARN and allowed only on
            # `workload-identity-directory/*`, and `GetResourceOauth2Token` is denied on the
            # credential provider's own ARN, on `token-vault/default/*` and on
            # `token-vault/*`, and allowed only on `*`. No condition key exists for the user
            # id either. So neither this ceiling nor the grant under it can name one
            # repository's credential, and `CLAUDE.md`'s "What remains exposed" states the
            # consequence rather than letting the shape of this list imply a fence.
            resources=["*"],
        ),
        iam.PolicyStatement(
            sid="MayWriteRuntimeLogs",
            effect=iam.Effect.ALLOW,
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            # Tier 1 #9: the runtime log-group namespace, not the account's. The trailing
            # `*` sits directly after the prefix with no `/`, which is what makes one
            # resource cover both the log-group ARN and the `...:log-stream:*` form the
            # inline policy spells separately. Region and account are wildcards because the
            # name prefix is the fence.
            resources=[f"arn:aws:logs:*:*:log-group:{RUNTIME_LOG_GROUP_PREFIX}*"],
        ),
        iam.PolicyStatement(
            sid="MayAuthenticateToEcr",
            effect=iam.Effect.ALLOW,
            actions=["ecr:GetAuthorizationToken"],
            # `*` is what the action *requires*, not laziness: `ecr:GetAuthorizationToken`
            # supports no resource types, so a scoped ARN here matches no authorization at
            # all. The token it hands back grants nothing on its own -- it still authorizes
            # against `MayPullAgentImage`, which names one repository.
            resources=["*"],
        ),
        iam.PolicyStatement(
            sid="MayPullAgentImage",
            effect=iam.Effect.ALLOW,
            # This pair is what makes `CreateAgentRuntime` succeed at all: the **runtime**
            # pulls `AGENT_IMAGE_URI` with the execution role before any container code
            # runs, and without these four actions the create fails `AccessDenied` with a
            # null `errorMessage` and no runtime is ever created.
            actions=[
                "ecr:BatchCheckLayerAvailability",
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
            ],
            # The repository the asset lives in, which is the one
            # `provisioner._asset_repository_arn` derives from `AGENT_IMAGE_URI`: one
            # repository named from one source, so the ceiling cannot drift off the grant.
            resources=[asset_repository_arn],
        ),
        iam.PolicyStatement(
            sid="MayUseOwnMemory",
            effect=iam.Effect.ALLOW,
            # The ceiling over `provisioner._execution_role_policy`'s `UseOwnMemory`: exactly
            # the six data-plane actions it grants, so a container may add to and read the
            # memory its own repository owns. `DeleteEvent`, `DeleteMemoryRecord`,
            # `BatchDeleteMemoryRecords`, `CreateMemory`, `UpdateMemory`, `DeleteMemory` and
            # `ListMemories` are absent here and *denied by name* below, so a later
            # "helpful" widening of the grant fails a test rather than landing.
            #
            # Gated on nothing: the grant side degrades per configuration rather than per
            # fleet -- a configuration whose `CreateMemory` failed simply carries no
            # `memoryId` and gets no statement -- so both of `execution_boundary`'s set
            # differences stay closed either way.
            actions=[
                "bedrock-agentcore:CreateEvent",
                "bedrock-agentcore:ListEvents",
                "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:RetrieveMemoryRecords",
                "bedrock-agentcore:ListMemoryRecords",
                "bedrock-agentcore:GetMemoryRecord",
            ],
            # `memory/*` rather than one memory's ARN, and declared in
            # `execution_boundary.WIDER_RESOURCES` as the fourth widening: the boundary is
            # minted **once per deploy**, before any memory exists, so there is nothing
            # narrower to name here. The trailing `*` also absorbs the ten-character suffix
            # AgentCore appends to every memory id -- botocore's own `memoryId` pattern is
            # `[a-zA-Z][a-zA-Z0-9-_]{0,99}-[a-zA-Z0-9]{10}` -- so an ARN ending at the name
            # would match nothing at all, the trap `MayReadOneSecret` already documents. The
            # fence that stops one repository reaching another's memory is the literal ARN
            # `provisioner._execution_role_policy` names (invariant 31's division of labour).
            resources=[f"arn:aws:bedrock-agentcore:{region}:{account}:memory/*"],
        ),
        iam.PolicyStatement(
            sid="MayNotEscalate",
            # The explicit deny is why the boundary exists at all. Without it the
            # provisioner's own `iam:CreateRole` grant is enough to mint an administrator
            # role -- the boundary condition on that grant only checks that *some* boundary
            # is attached, so this list is what makes attaching one mean something. And the
            # first two AgentCore entries are the self-dispatch fence: a container that can
            # call the AgentCore control plane can invoke its own runtime, which turns one
            # compromised task into an unbounded fan-out that bills like a bug.
            #
            # `bedrock-agentcore:*` used to stand for that fence and is now **twelve literal
            # action names**, for the `ecr:*` reason below: `MayUseOwnMemory` above allows the
            # six data-plane memory actions, and an explicit Deny beats every Allow in either
            # document, so a wildcard here would make every `CreateEvent` from a container an
            # `AccessDenied` with nothing naming the pattern. **No wildcard may enter this
            # group** -- `List*` shadows `ListEvents` and `ListMemoryRecords`, `Create*`
            # shadows `CreateEvent`. What the literal list buys is that the seven destructive
            # memory actions `provisioner._execution_role_policy` calls deliberately absent
            # are denied *by name*, so a later "helpful" widening of the grant fails a test
            # instead of landing. It will go stale -- a thirteenth destructive action AWS
            # ships tomorrow is denied by the whitelist but not named here, and that is the
            # correct default: this list is a named denial of the actions a future edit is
            # most likely to add, not an enumeration of the service.
            #
            # `ecr:*` used to be on this list and is deliberately **not** any more: an
            # explicit Deny beats the two Allow statements above it, so leaving it here makes
            # the image-pull grant a dead letter -- the diff reads as fixed and the runtime
            # still cannot pull. Nothing widens: a boundary is a whitelist, so `ecr:PutImage`,
            # `ecr:DeleteRepository`, `bedrock-agentcore:ListAgentRuntimes` and every other
            # unlisted action are denied by not being Allowed above. `s3:*` **stays**: an ECR
            # layer download is a presigned S3 URL, which needs no `s3:` permission on the
            # caller -- the S3 gateway endpoint `tests/test_agent_network.py` pins is network
            # reachability, not IAM.
            effect=iam.Effect.DENY,
            actions=[
                "iam:*",
                "sts:AssumeRole",
                "organizations:*",
                "bedrock-agentcore:InvokeAgentRuntime",
                "bedrock-agentcore:StopRuntimeSession",
                "bedrock-agentcore:CreateAgentRuntime",
                "bedrock-agentcore:UpdateAgentRuntime",
                "bedrock-agentcore:DeleteAgentRuntime",
                "bedrock-agentcore:CreateMemory",
                "bedrock-agentcore:UpdateMemory",
                "bedrock-agentcore:DeleteMemory",
                "bedrock-agentcore:ListMemories",
                "bedrock-agentcore:DeleteEvent",
                "bedrock-agentcore:DeleteMemoryRecord",
                "bedrock-agentcore:BatchDeleteMemoryRecords",
                # The Identity control plane, denied by name for exactly the reason the seven
                # memory actions above are. `MayUseGitHubCredential` has to allow
                # `GetResourceOauth2Token` on `*`, so a container is one widening away from
                # reconfiguring the credential provider every repository in the deployment
                # shares -- which would let it point the provider at an attacker's OAuth app,
                # or delete it and take the whole fleet's access with it. `SetTokenVaultCMK`
                # is here for the same reason: re-keying the vault is a denial of service on
                # every stored authorisation. Still no wildcard: `Get*` would shadow
                # `GetResourceOauth2Token` and `GetWorkloadAccessTokenForUserId` and break
                # every task.
                "bedrock-agentcore:CreateOauth2CredentialProvider",
                "bedrock-agentcore:UpdateOauth2CredentialProvider",
                "bedrock-agentcore:DeleteOauth2CredentialProvider",
                "bedrock-agentcore:ListOauth2CredentialProviders",
                "bedrock-agentcore:CreateApiKeyCredentialProvider",
                "bedrock-agentcore:UpdateApiKeyCredentialProvider",
                "bedrock-agentcore:DeleteApiKeyCredentialProvider",
                "bedrock-agentcore:GetResourceApiKey",
                "bedrock-agentcore:CreateWorkloadIdentity",
                "bedrock-agentcore:UpdateWorkloadIdentity",
                "bedrock-agentcore:DeleteWorkloadIdentity",
                "bedrock-agentcore:ListWorkloadIdentities",
                "bedrock-agentcore:CreateTokenVault",
                "bedrock-agentcore:SetTokenVaultCMK",
                # The container must never reach Secrets Manager at all now: nothing it does
                # needs it, so the whole service is denied rather than one prefix allowed.
                "secretsmanager:*",
                "s3:*",
                "sqs:*",
            ],
            resources=["*"],
        ),
    ]


class AgentStack(cdk.Stack):
    """The agent runtime's prerequisites: the ARM64 image, the VPC, and the boundary."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        app_name: str,
        # Defaulted so nothing that constructs this stack without an address breaks; empty
        # means the DNS alarm is created with no action and `add_agent_network` annotates
        # the synth to say so.
        alert_email: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -- the image ------------------------------------------------------------
        #
        # `agent/Dockerfile` exists, and four of its properties are this stack's business
        # rather than that file's taste. `tests/test_repo_contracts.py::
        # test_the_agent_image_*` is what holds them, because nothing in CI can synthesize
        # this stack or build the asset below -- both need a Docker daemon:
        #   * every `FROM` carries `--platform=linux/arm64`, matching `platform` below;
        #   * `@anthropic-ai/claude-code` is pinned to an exact version -- an unpinned CLI
        #     makes an image rebuild a silent behaviour change -- and the build-time
        #     `claude --version` check sits in the SAME `RUN` as the install, so a broken
        #     install fails the build instead of the first task;
        #   * `COPY skills /app/skills` with `ENV AGENT_SKILL_PATHS=/app/skills`. The build
        #     context is `agent/` itself (see `directory=` below), so that source path takes
        #     no `agent/` prefix; with one, the build dies on `"/agent/skills": not found`;
        #   * **no** `ENV WORK_ROOT` (invariant 26): a Dockerfile `ENV` cannot be *unset* by
        #     a runtime's `environmentVariables`, only overridden, which is why the work
        #     root is forwarded by the provisioner instead.
        self.image = ecr_assets.DockerImageAsset(
            self,
            "AgentImage",
            directory=str(Path(__file__).resolve().parents[2] / "agent"),
            # Invariant 2, and silent when wrong: CloudFormation accepts an x86 image
            # happily and AgentCore then fails to start it with no signal anywhere -- no
            # exec-format error in the runtime's logs, because there are no logs. The only
            # evidence is a runtime stuck out of `READY`.
            #
            # The built image is ~1.3 GB against AgentCore's 2 GB cap. Weigh new tooling in
            # the Dockerfile against that headroom: the limit is enforced at
            # `CreateAgentRuntime`, so overshooting it fails the provisioning of every
            # configuration at once rather than one build.
            platform=ecr_assets.Platform.LINUX_ARM64,
        )

        # -- the VPC and the DNS Firewall (Tier 2 #16) ----------------------------
        #
        # Built on `self`, so every logical id is what it would be if the body of
        # `add_agent_network` were still written out here.
        network = add_agent_network(self, app_name=app_name, alert_email=alert_email)
        self.vpc = network.vpc
        self.security_group = network.security_group

        # Nothing reads this -- `make deploy` reads outputs from the API stack only
        # (`SampleCodingAgentApi` by default; `-c stackPrefix=` renames it). It is
        # here so a demo can point at the log the deny list writes into.
        cdk.CfnOutput(self, "DnsQueryLogGroup", value=network.dns_query_log_group.log_group_name)

        # -- the permissions boundary --------------------------------------------
        #
        # The one physical name this repository sets on purpose. Everything else is left to
        # CloudFormation so an accidental replacement is its problem, but
        # `provisioner.EXECUTION_ROLE_BOUNDARY_ARN` is compared as a *string* inside the
        # `iam:PermissionsBoundary` condition on the provisioner's `iam:CreateRole` grant,
        # and a generated name makes that condition unreadable in the console at exactly
        # the moment someone is trying to work out why a create was denied.
        #
        # It carries `self.region` because an IAM managed policy name is **account-global**
        # while every other stack in this app is per-region: without the suffix a second
        # deployment in a second region dies at `CreateManagedPolicy` with
        # `EntityAlreadyExists`, and it dies AFTER the ~1.3 GB image build and push, which is
        # the most expensive step of the whole deploy. Nothing compares the literal --
        # `api_stack` takes `role_boundary.managed_policy_arn` by reference and the
        # provisioner reads the ARN out of its environment -- so the suffix costs nothing but
        # the readability above, and `rca-execution-boundary-us-west-2` still names the region
        # whose roles it bounds.
        self.role_boundary = iam.ManagedPolicy(
            self,
            "ExecutionRoleBoundary",
            managed_policy_name=f"{app_name}-execution-boundary-{self.region}",
            description=(
                f"Ceiling on every {app_name} per-configuration AgentCore execution role "
                "(invariant 17)"
            ),
            statements=execution_role_boundary_statements(
                account=self.account,
                region=self.region,
                # The asset's own repository, as a token that resolves to a real ARN.
                # `self.image.image_uri` cannot be used: at synth time it is an unresolved
                # token with no repository name in it to parse. This is the same repository
                # the provisioner derives from `AGENT_IMAGE_URI` at run time, so grant and
                # ceiling name one repository from one source.
                asset_repository_arn=self.image.repository.repository_arn,
            ),
        )
