"""The agent network, checked against the real synthesized template rather than the source.

`AgentStack` itself cannot be built here: `ecr_assets.DockerImageAsset` runs `docker build`
during synthesis and this container has no daemon. That is why `test_grant_contract.py`
reaches `execution_role_boundary_statements` as a pure function and compares constants.
The DNS Firewall cannot be checked that way and still mean anything: a `CfnFirewallRuleGroup`
that is never associated with the VPC filters nothing and reads perfectly in source, and
`action="ALERT"` writes the same log line as `BLOCK` while letting the query through. Those
are template assertions or they are nothing -- so `add_agent_network` takes the scope it
builds on, and every test below synthesizes it into a bare `aws_cdk.Stack`.

Every assertion here covers a failure that looks like success: a rule group with no
association deploys clean, a logging config with no association produces an empty log group
that reads as "no vendor lookups happened", a missing ECR endpoint half presents as a
runtime that never reaches `READY`, and an alarm with no action is the same failure as no
alarm.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import aws_cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template
from stacks import agent_stack

_ALERT_EMAIL = "ops@example.com"
_APP_NAME = "rca"

# The destinations Qualification 3 says are unavoidable. One of these in the deny list is an
# `NXDOMAIN` in the middle of a run, whose symptom is a `setupCommand` or `git` failure that
# reads like a flaky network -- so it is worth a test rather than a comment.
_UNAVOIDABLE = ("github.com", "pypi.org", "pythonhosted.org", "npmjs.org", "amazonaws.com")


def _stack(tmp_path: Path, *, alert_email: str) -> aws_cdk.Stack:
    """The network in a bare stack of its own, synthesized into `tmp_path`.

    `outdir=` is not optional, exactly as in `tests/test_account_stack.py::_build`:
    `Template.from_stack` synthesizes into the app's outdir, and a bare `aws_cdk.App()`
    writes `cdk.out/` into the current working directory -- the repository root under
    `make test` -- which fails
    `tests/test_layout_infra.py::test_the_cdk_entry_point_imports_without_building_an_app`
    in a file that has nothing to do with this one.
    """
    app = aws_cdk.App(outdir=str(tmp_path))
    # Concrete rather than environment-agnostic, so the interface endpoints render their
    # service names as plain strings instead of `Fn::Join`s over `AWS::Region`.
    stack = aws_cdk.Stack(
        app, "Net", env=aws_cdk.Environment(account="123456789012", region="ap-southeast-1")
    )
    agent_stack.add_agent_network(stack, app_name=_APP_NAME, alert_email=alert_email)
    return stack


def _template(tmp_path: Path, *, alert_email: str = _ALERT_EMAIL) -> Template:
    return Template.from_stack(_stack(tmp_path, alert_email=alert_email))


@pytest.fixture
def template(tmp_path: Path) -> Template:
    return _template(tmp_path)


def _properties(template: Template, kind: str) -> list[dict[str, Any]]:
    return [resource.get("Properties", {}) for resource in template.find_resources(kind).values()]


def _vpc_ref(template: Template) -> dict[str, str]:
    """The `Ref` shape the VPC's logical id renders as, whatever hash CDK appends to it."""
    (logical_id,) = template.find_resources("AWS::EC2::VPC")
    return {"Ref": logical_id}


def _service_name(value: Any) -> str:
    """A VPC endpoint's `ServiceName` as a string.

    Interface endpoints render it literally when the stack has a concrete region; the two
    gateway endpoints render an `Fn::Join` over `AWS::Region` regardless, so the joined
    parts are flattened and only the string pieces kept -- which is why the assertions match
    on suffixes rather than whole values.
    """
    if isinstance(value, str):
        return value
    _, parts = value["Fn::Join"]
    return "".join(part for part in parts if isinstance(part, str))


def test_the_only_egress_the_agent_security_group_allows_is_443(template: Template) -> None:
    """The whole point of `allow_all_outbound=False`: anything that is not TLS has no route
    out, so the NAT path is `github.com`, PyPI and npm and nothing else. A second rule here,
    or a widened one, is invisible in a deploy log.

    The group is selected by its `GroupDescription` and never as "the only one": each of the
    six interface endpoints gets a security group of its own, so the template carries seven.
    """
    groups = _properties(template, "AWS::EC2::SecurityGroup")
    assert len(groups) == 6, (
        f"{len(groups)} security groups: the interface endpoints bring their own, so a "
        f"test that assumes one silently asserts against an endpoint's group instead"
    )
    (agent,) = [
        group
        for group in groups
        if group["GroupDescription"] == f"{_APP_NAME} agent runtime egress: 443 only"
    ]
    # No UDP/53 rule, and none is wanted: the VPC User Guide is explicit that traffic to and
    # from the Amazon DNS server cannot be filtered with security groups at all, so
    # resolution works regardless of what this group says -- which is exactly why the DNS
    # Firewall below, and not this group, is what can refuse a lookup.
    assert agent["SecurityGroupEgress"] == [
        {
            "CidrIp": "0.0.0.0/0",
            "Description": "443 only",
            "FromPort": 443,
            "IpProtocol": "tcp",
            "ToPort": 443,
        }
    ]
    assert "SecurityGroupIngress" not in agent, (
        "nothing dials in to a runtime -- AgentCore invokes it through its own control "
        "plane -- so any ingress rule here is surface nobody asked for"
    )


def test_every_aws_call_that_carries_content_has_a_vpc_endpoint(template: Template) -> None:
    """The endpoints are what make "inside your boundary" demonstrable rather than asserted:
    without them the same traffic works fine over the NAT gateway and nothing says so. Both
    halves of ECR are load-bearing -- `ecr.api` hands out the auth token and `ecr.dkr`
    serves the layers, and one without the other fails the image pull, which presents as a
    runtime that never reaches `READY`."""
    # Seven endpoints, with no Secrets Manager one. The container's GitHub
    # credential comes from the AgentCore data plane, which `.bedrock-agentcore` below
    # already covers, and AgentCore Identity's own Secrets Manager write happens service-side
    # from a control-plane Lambda outside this VPC -- so an endpoint for it would be an ENI in
    # every AZ with no client.
    template.resource_count_is("AWS::EC2::VPCEndpoint", 7)
    endpoints = _properties(template, "AWS::EC2::VPCEndpoint")
    names = [_service_name(endpoint["ServiceName"]) for endpoint in endpoints]
    for suffix in (
        ".bedrock-runtime",
        ".ecr.api",
        ".ecr.dkr",
        ".logs",
        # The AgentCore **data plane**, which is what `agentcore_memory.py` calls. The exact
        # suffix matters twice: `.bedrock-agentcore-control` is the control plane, whose
        # Lambdas are deliberately not in this VPC, and `.bedrock-agentcore.gateway` is a
        # third service -- so a test that matched either would pass on the wrong endpoint.
        ".bedrock-agentcore",
    ):
        assert any(name.endswith(suffix) for name in names), f"no endpoint for {suffix}: {names}"
    # Gateway endpoints, and free: an ECR layer *is* an S3 object, which is why S3 is here.
    for suffix in (".dynamodb", ".s3"):
        assert any(name.endswith(suffix) for name in names), f"no endpoint for {suffix}: {names}"


def test_the_deny_list_names_every_vendor_domain_and_none_of_the_four_it_must_not(
    template: Template,
) -> None:
    """Wrong in either direction and silent both times: a name missing is a call that leaves
    the VPC with nothing to say so, and one too many takes every run down at `setupCommand`
    time with what looks like a flaky network."""
    (deny_list,) = _properties(template, "AWS::Route53Resolver::FirewallDomainList")
    assert deny_list["Domains"] == list(agent_stack.BLOCKED_VENDOR_DOMAINS)
    # The `*.` prefix is DNS Firewall's wildcard form and not decoration: a bare
    # `claudeusercontent.com` matches that exact name and no subdomain of it, and the
    # subdomains are the whole set of names actually used.
    assert {"*.claudeusercontent.com", "*.datadoghq.com"} <= set(deny_list["Domains"])
    for domain in deny_list["Domains"]:
        for unavoidable in _UNAVOIDABLE:
            assert unavoidable not in domain, (
                f"{domain!r} covers {unavoidable!r}, which Qualification 3 names as "
                f"unavoidable: denying it is an NXDOMAIN mid-run, not a security control"
            )


def test_the_firewall_blocks_rather_than_alerts(template: Template) -> None:
    """`ALERT` is the silent alternative: it writes the *same* log line and then lets the
    query through, so the query log reads identically while the control does nothing."""
    (firewall,) = _properties(template, "AWS::Route53Resolver::FirewallRuleGroup")
    (rule,) = firewall["FirewallRules"]
    assert rule["Action"] == "BLOCK", (
        f"Action is {rule['Action']!r}: ALERT logs and forwards, so the evidence looks the "
        f"same and the lookup succeeds anyway"
    )
    # So the container sees "no such host", which is diagnosable in a stack trace, rather
    # than an empty answer that reads as a network glitch.
    assert rule["BlockResponse"] == "NXDOMAIN"


def test_the_rule_group_and_the_query_log_are_both_associated_with_the_vpc(
    template: Template,
) -> None:
    """The two resources that deploy clean and do nothing. A rule group with no association
    filters nothing and reports success; a logging config with no association produces an
    empty log group, which reads exactly like "no vendor lookups happened"."""
    vpc = _vpc_ref(template)
    (firewall_association,) = _properties(
        template, "AWS::Route53Resolver::FirewallRuleGroupAssociation"
    )
    assert firewall_association["VpcId"] == vpc
    # DNS Firewall rejects anything outside this window at `AssociateFirewallRuleGroup`.
    assert 100 <= firewall_association["Priority"] <= 9900

    (logging_association,) = _properties(
        template, "AWS::Route53Resolver::ResolverQueryLoggingConfigAssociation"
    )
    assert logging_association["ResourceId"] == vpc


def test_a_single_blocked_lookup_alarms(template: Template) -> None:
    """One blocked lookup is the whole signal -- the alarm exists to say that a CLI upgrade
    introduced a new call -- so `threshold=0` over one period is deliberate.
    `TreatMissingData` is the load-bearing one: no blocked lookup means no log line means no
    datapoint, and the default parks the alarm in `INSUFFICIENT_DATA` forever, which on the
    console is indistinguishable from an alarm that was never wired up."""
    (metric_filter,) = _properties(template, "AWS::Logs::MetricFilter")
    assert metric_filter["FilterPattern"] == agent_stack.DNS_BLOCK_FILTER_PATTERN
    assert "$.firewall_rule_action" in metric_filter["FilterPattern"], (
        "the field is what makes this pattern mean 'a deny-list hit'; it is documented as "
        "populated only when DNS Firewall matched a rule with action ALERT or BLOCK"
    )
    (transformation,) = metric_filter["MetricTransformations"]
    assert transformation["MetricName"] == agent_stack.DNS_BLOCK_METRIC_NAME
    assert transformation["DefaultValue"] == 0, (
        "without a default the metric reports no datapoints between hits, which is the "
        "same INSUFFICIENT_DATA trap from the other end"
    )

    (alarm,) = _properties(template, "AWS::CloudWatch::Alarm")
    assert alarm["Threshold"] == 0
    assert alarm["EvaluationPeriods"] == 1
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
    assert alarm["Statistic"] == "Sum"
    # Lower-case `n`: that is what CDK renders, and CloudWatch is case-sensitive here.
    assert alarm["TreatMissingData"] == "notBreaching"
    assert alarm["MetricName"] == agent_stack.DNS_BLOCK_METRIC_NAME


def test_the_alarm_has_somewhere_to_go_and_says_so_when_it_does_not(tmp_path: Path) -> None:
    """An alarm nobody hears is the same failure as no alarm. `alert_email` is threaded from
    the existing `-c budgetAlertEmail` knob, so it can be empty on a hand-rolled deploy --
    and the annotation is what stops that being silent in the synth output of the deploy
    that caused it."""
    with_address = _template(tmp_path / "with", alert_email=_ALERT_EMAIL)
    (topic,) = _properties(with_address, "AWS::SNS::Topic")
    assert topic["DisplayName"] == f"{_APP_NAME} blocked vendor DNS lookups"
    (subscription,) = _properties(with_address, "AWS::SNS::Subscription")
    assert subscription["Protocol"] == "email"
    assert subscription["Endpoint"] == _ALERT_EMAIL
    (alarm,) = _properties(with_address, "AWS::CloudWatch::Alarm")
    assert len(alarm["AlarmActions"]) == 1

    without = _stack(tmp_path / "without", alert_email="")
    template = Template.from_stack(without)
    template.resource_count_is("AWS::SNS::Topic", 0)
    (actionless,) = _properties(template, "AWS::CloudWatch::Alarm")
    assert "AlarmActions" not in actionless, (
        "the alarm is still created without an address -- a blocked lookup is worth "
        "recording either way -- so the absence of an action is what the annotation names"
    )
    Annotations.from_stack(without).has_warning(
        "*", Match.string_like_regexp("no alert address was given")
    )


# A shuffled account, the shape that broke provisioning: its first two names are `a` and `b`,
# and `a` maps to an ID AgentCore refuses. `max_azs=2` placed a subnet there.
_SHUFFLED_ZONES = (
    ("us-east-1a", "use1-az6"),
    ("us-east-1b", "use1-az1"),
    ("us-east-1c", "use1-az2"),
    ("us-east-1d", "use1-az4"),
    ("us-east-1e", "use1-az3"),
)


def test_the_agent_zones_skip_every_id_agentcore_refuses() -> None:
    assert agent_stack.select_agent_zones("us-east-1", _SHUFFLED_ZONES) == [
        "us-east-1b",
        "us-east-1c",
    ]


def test_a_fleet_already_in_supported_zones_keeps_them() -> None:
    """Name order is what `max_azs=2` took from, so where the first two names were already
    supported the answer is the same two -- and no subnet of a working fleet moves."""
    zones = (("eu-west-1a", "euw1-az3"), ("eu-west-1b", "euw1-az1"), ("eu-west-1c", "euw1-az2"))
    assert agent_stack.select_agent_zones("eu-west-1", reversed(zones)) == [
        "eu-west-1a",
        "eu-west-1b",
    ]


def test_no_supported_zone_is_a_synth_failure_not_an_empty_list() -> None:
    """An empty answer would hand placement back to `max_azs`, which is the bug."""
    with pytest.raises(ValueError, match="none of this account's Availability Zones"):
        agent_stack.select_agent_zones("us-east-1", (("us-east-1a", "use1-az6"),))
    with pytest.raises(ValueError, match="does not list VPC-mode Availability Zones"):
        agent_stack.select_agent_zones("mars-north-1", (("mars-north-1a", "marn1-az1"),))


def test_every_region_this_fleet_deploys_to_has_a_zone_table() -> None:
    """README names the regions AgentCore Runtime V2 limits a deploy to. One missing from the
    table fails every synth there with "does not list", which is right only if it is true."""
    readme = Path(__file__).resolve().parents[1].joinpath("README.md").read_text("utf-8")
    (line,) = [chunk for chunk in readme.split("\n\n") if "AgentCore Runtime V2 only" in chunk]
    regions = re.findall(r"`([a-z]{2}-[a-z]+-\d)`", line)
    assert regions, "README's V2 region list moved; this test reads it"
    assert set(regions) <= set(agent_stack.AGENTCORE_VPC_ZONE_IDS)


def test_named_zones_are_the_ones_every_agent_subnet_lands_in(tmp_path: Path) -> None:
    app = aws_cdk.App(
        outdir=str(tmp_path),
        # What CDK's own AZ lookup would have cached, so the bare stack knows the account's
        # names; `AgentStack` overrides `availability_zones` instead, for the reason its
        # docstring gives.
        context={
            "availability-zones:account=123456789012:region=us-east-1": [
                name for name, _ in _SHUFFLED_ZONES
            ]
        },
    )
    stack = aws_cdk.Stack(
        app, "Net", env=aws_cdk.Environment(account="123456789012", region="us-east-1")
    )
    zones = agent_stack.select_agent_zones("us-east-1", _SHUFFLED_ZONES)
    agent_stack.add_agent_network(
        stack, app_name=_APP_NAME, alert_email=_ALERT_EMAIL, availability_zones=zones
    )
    subnets = _properties(Template.from_stack(stack), "AWS::EC2::Subnet")
    # Public and agent subnets alike: the NAT gateway's public subnet has to share an AZ with
    # the private one it routes, and `Vpc` lays both tiers over the same list.
    assert sorted(subnet["AvailabilityZone"] for subnet in subnets) == sorted(zones * 2)
