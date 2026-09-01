"""`AccountStack` synthesizes, and the five things about it that are silent when wrong.

Every assertion here covers a failure that looks like success: a managed rule group left in
count mode still reads as "the common rule set is on"; a cost filter naming a service Cost
Explorer does not know produces a budget indistinguishable from being under budget; a web ACL
created outside `us-east-1` fails at `CreateWebACL` in front of an operator rather than at
synth; and a CloudWatch metric name that carries no consuming region makes two of these
stacks -- which now coexist in `us-east-1`, one per consuming region -- write into one
another's metrics with nothing anywhere reporting it. This stack bundles nothing through
Docker, so -- like `WebStack` and unlike `AgentStack` and `ApiStack` -- it can actually be
built here, and that is the whole offline check on it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import aws_cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template
from stacks import account_stack
from stacks.account_stack import AccountStack

_ALERT_EMAIL = "ops@example.com"

# Deliberately *not* `us-east-1`: the stack itself is built in the WAF's region, so a metric
# name that forgot the consuming-region discriminator would accidentally look right if the
# two were the same string.
_CONSOLE_REGION = "ap-southeast-1"

# WAF's own pattern for a metric name, from the `CreateWebACL` API reference: at least one
# character, at most 128, and only word characters and `#:.-/`. A name outside it is a
# `CreateWebACL` validation error in front of an operator, never a synth error.
_WAF_METRIC_NAME = re.compile(r"^[\w#:.\-/]+$")

# The three metric names this stack writes, one per `VisibilityConfig`: the ACL's own and one
# per rule. Spelled as the `kind=` values `console_metric_name` takes.
_METRIC_KINDS = ("web-acl", "common-rules", "rate-limit")


def _build(
    tmp_path: Path, *, alert_email: str, console_region: str = _CONSOLE_REGION
) -> AccountStack:
    """`AccountStack` in its own app, synthesized into `tmp_path`.

    `outdir=` is not optional. `Template.from_stack` synthesizes into the app's outdir, and
    a bare `aws_cdk.App()` writes `cdk.out/` into the current working directory -- the
    repository root under `make test` -- which fails
    `tests/test_layout_infra.py::test_the_cdk_entry_point_imports_without_building_an_app`
    in a file that has nothing to do with this one.
    """
    app = aws_cdk.App(outdir=str(tmp_path))
    return AccountStack(
        app,
        "CodingAgentAccount",
        app_name="rca",
        monthly_budget_usd="200",
        alert_email=alert_email,
        # Required, not defaulted, by the construct: this stack is one instance per consuming
        # region and two of them coexist in us-east-1, so a default here would silently give
        # both the same CloudWatch metric names.
        console_region=console_region,
        # Concrete, and in the WAF's region: `cross_region_references=True` refuses an
        # environment-agnostic stack outright, which is the same thing `make preflight`'s
        # `aws sts get-caller-identity` check guarantees before any real deploy.
        env=aws_cdk.Environment(account="123456789012", region=account_stack.WAF_REGION),
        cross_region_references=True,
    )


@pytest.fixture
def template(tmp_path: Path) -> Template:
    return Template.from_stack(_build(tmp_path, alert_email=_ALERT_EMAIL))


def _properties(template: Template, kind: str) -> list[dict[str, Any]]:
    return [resource.get("Properties", {}) for resource in template.find_resources(kind).values()]


def _rule(template: Template, name: str) -> dict[str, Any]:
    (web_acl,) = _properties(template, "AWS::WAFv2::WebACL")
    (rule,) = [entry for entry in web_acl["Rules"] if entry["Name"] == name]
    return rule


def _metric_names(template: Template) -> set[str]:
    """Every `VisibilityConfig.MetricName` in the template: the ACL's own, then each rule's.

    A set, because what matters about these names is that two regions share none of them --
    and a set that came back short is itself a collision, which is why the callers assert its
    size rather than trusting the count of rules.
    """
    (web_acl,) = _properties(template, "AWS::WAFv2::WebACL")
    return {web_acl["VisibilityConfig"]["MetricName"]} | {
        rule["VisibilityConfig"]["MetricName"] for rule in web_acl["Rules"]
    }


def test_the_waf_region_is_us_east_1() -> None:
    """A CloudFront-scoped web ACL is only creatable in `us-east-1`, so this stack cannot be
    folded back into the deployment region however tidy that would read: the failure is a
    `CreateWebACL` error during someone's deploy, not a synth error a test can catch."""
    assert account_stack.WAF_REGION == "us-east-1"


def test_the_web_acl_is_cloudfront_scoped_and_allows_by_default(template: Template) -> None:
    (web_acl,) = _properties(template, "AWS::WAFv2::WebACL")
    assert web_acl["Scope"] == "CLOUDFRONT", (
        f"scope is {web_acl['Scope']!r}: a REGIONAL ACL cannot be attached to a "
        f"distribution at all, and the attachment fails at deploy time rather than here"
    )
    assert web_acl["DefaultAction"] == {"Allow": {}}, (
        "the default action must allow and the rules must block; a default block takes the "
        "console down with it the moment a rule stops matching"
    )


def test_the_managed_rule_group_blocks_rather_than_counts(template: Template) -> None:
    """The silent one. `override_action=count={}` leaves the common rule set in
    logging-only mode while the template still names it, and `action` on a managed group is
    a `CreateWebACL` validation error rather than a synth error."""
    rule = _rule(template, "AWSManagedRulesCommonRuleSet")
    assert rule["OverrideAction"] == {"None": {}}, (
        f"OverrideAction is {rule.get('OverrideAction')!r}; `Count` means the group logs "
        f"and blocks nothing while still reading as 'the common rule set is on'"
    )
    assert "Action" not in rule, (
        "a managed rule group carries its own per-rule actions, so `Action` here is a "
        "CreateWebACL validation error"
    )
    group = rule["Statement"]["ManagedRuleGroupStatement"]
    assert (group["VendorName"], group["Name"]) == ("AWS", "AWSManagedRulesCommonRuleSet")


def test_the_rate_based_rule_blocks_at_the_declared_limit(template: Template) -> None:
    rule = _rule(template, "RateLimit")
    assert rule["Action"] == {"Block": {}}, (
        f"Action is {rule.get('Action')!r}; a rate-based rule is not a managed group, so it "
        f"takes `Action` and an `OverrideAction` is rejected"
    )
    assert "OverrideAction" not in rule
    statement = rule["Statement"]["RateBasedStatement"]
    assert statement["Limit"] == account_stack.RATE_LIMIT_PER_FIVE_MINUTES
    assert statement["AggregateKeyType"] == "IP", (
        "aggregating on anything but the source IP makes the cap global, so one busy "
        "operator rate-limits everybody else"
    )


def test_both_rules_sample_requests(template: Template) -> None:
    """Sampling is what makes a blocked request diagnosable. Without it the only evidence a
    rule ever fired is a metric count, and "the console is broken for one person" has no
    request to look at."""
    (web_acl,) = _properties(template, "AWS::WAFv2::WebACL")
    assert len(web_acl["Rules"]) == 2
    for rule in web_acl["Rules"]:
        visibility = rule["VisibilityConfig"]
        assert visibility["SampledRequestsEnabled"] is True, rule["Name"]
        assert visibility["CloudWatchMetricsEnabled"] is True, rule["Name"]


def test_every_metric_name_carries_the_consuming_region(template: Template) -> None:
    """The ACL's metric name and both rules' carry the region whose console they protect.

    This stack is one instance per consuming region now, so two of them live in `us-east-1`
    at once and CloudWatch metric names are per-account. An undiscriminated name therefore
    makes two consoles write into one metric: the rate-limit graph an operator opens after a
    blocked request counts both fleets' regions and cannot be attributed back to either, and
    nothing about it reads as broken.
    """
    expected = {
        kind: account_stack.console_metric_name(
            app_name="rca", kind=kind, console_region=_CONSOLE_REGION
        )
        for kind in _METRIC_KINDS
    }
    (web_acl,) = _properties(template, "AWS::WAFv2::WebACL")
    actual = {
        "web-acl": web_acl["VisibilityConfig"]["MetricName"],
        "common-rules": _rule(template, "AWSManagedRulesCommonRuleSet")["VisibilityConfig"][
            "MetricName"
        ],
        "rate-limit": _rule(template, "RateLimit")["VisibilityConfig"]["MetricName"],
    }
    assert actual == expected, (
        f"the metric names are {actual} but `console_metric_name` derives {expected}. The "
        "template and the helper must agree, or the helper stops being the one place the "
        "discriminator is written down."
    )
    for kind, name in actual.items():
        assert name.endswith(f"-{_CONSOLE_REGION}"), (
            f"the {kind} metric name is {name!r}, which does not end with the consuming "
            f"region. Two account stacks coexist in us-east-1, so both consoles would write "
            f"into this one metric."
        )


def test_two_consuming_regions_share_no_metric_name(tmp_path: Path) -> None:
    """The collision itself, built twice: two consuming regions, no metric name in common.

    Two apps and two outdirs rather than two stacks in one app, because the point is two
    *deploys* -- the shape that put an `ExportsWriter` for one region into the other region's
    template, which is what made a shared account stack unworkable in the first place.
    """
    templates = {}
    for region in ("ap-southeast-1", "us-east-1"):
        outdir = tmp_path / region
        outdir.mkdir()
        templates[region] = _metric_names(
            Template.from_stack(_build(outdir, alert_email=_ALERT_EMAIL, console_region=region))
        )

    for region, names in templates.items():
        assert len(names) == len(_METRIC_KINDS), (
            f"{region} produced {sorted(names)}: {len(_METRIC_KINDS)} distinct metric names "
            f"are expected, so a short set means two of this stack's own visibility configs "
            f"already share one."
        )
    first, second = templates["ap-southeast-1"], templates["us-east-1"]
    assert first.isdisjoint(second), (
        f"ap-southeast-1 and us-east-1 share the metric names {sorted(first & second)}. Both "
        "stacks live in us-east-1 and CloudWatch metric names are per-account, so a shared "
        "name silently merges two regions' WAF counters."
    )


def test_metric_names_stay_within_wafs_pattern(template: Template) -> None:
    """`rca-console-web-acl-ap-southeast-1` is 34 characters and legal; the discriminator has
    ample room against WAF's 128. Asserted because a metric name outside
    `^[\\w#:.\\-/]+$` is a `CreateWebACL` validation error in front of an operator halfway
    through a deploy, and synth accepts any string at all."""
    for name in _metric_names(template):
        assert _WAF_METRIC_NAME.match(name), (
            f"{name!r} is outside WAF's metric-name pattern. The region is appended verbatim "
            "because hyphens are legal here -- anything else about it has to be transliterated."
        )
        assert 1 <= len(name) <= 128, f"{name!r} is {len(name)} characters; WAF allows 1-128"


def test_the_console_region_is_required(tmp_path: Path) -> None:
    """`console_region` has no default, and that is the contract rather than an oversight: a
    default would give every instance of this stack the same metric names again, silently, in
    exactly the configuration -- two account stacks in `us-east-1` -- this argument exists to
    tell apart."""
    app = aws_cdk.App(outdir=str(tmp_path))
    with pytest.raises(TypeError):
        AccountStack(
            app,
            "CodingAgentAccount",
            app_name="rca",
            monthly_budget_usd="200",
            alert_email=_ALERT_EMAIL,
            env=aws_cdk.Environment(account="123456789012", region=account_stack.WAF_REGION),
            cross_region_references=True,
        )


def test_the_budget_measures_bedrock_monthly_spend(template: Template) -> None:
    """A prompt-injected agent that cannot exfiltrate can still burn tokens, and this is the
    only control for that. `CostFilters` is asserted against the module constant rather than
    a literal because the value is a Cost Explorer *display name*: a typo matches no line
    item and the budget then never fires, which looks exactly like being under budget."""
    (budget,) = _properties(template, "AWS::Budgets::Budget")
    data = budget["Budget"]
    assert data["BudgetType"] == "COST"
    assert data["TimeUnit"] == "MONTHLY"
    assert data["CostFilters"] == account_stack.BEDROCK_COST_FILTER
    assert data["BudgetLimit"] == {"Amount": 200, "Unit": "USD"}


def test_the_budget_has_no_explicit_name(template: Template) -> None:
    """`BudgetName` is left to CloudFormation, and this is a regression test for a deploy
    that could not be re-run.

    AWS Budgets is global, and several `CfnBudget` properties replace rather than update, so
    a replacement creates the new budget before deleting the old one. With an explicit name
    the new one collides with the name the old one still holds -- `A budget or resource with
    the same name but a different internalId already exists` -- and because `AccountStack` is
    part of `cdk deploy --all`, that fails the entire deploy until a human deletes the budget
    by hand. It was observed on the first `make deploy` that passed `EMAIL` to a stack created
    without one, which adds `NotificationsWithSubscribers` for the first time.
    """
    (budget,) = _properties(template, "AWS::Budgets::Budget")
    assert "BudgetName" not in budget["Budget"], (
        "the budget carries an explicit BudgetName again. A replacement of an explicitly "
        "named global resource collides with itself and bricks `cdk deploy --all` until "
        "someone deletes the budget out of band; see the comment in account_stack.py."
    )


def test_the_budget_notifies_on_actual_and_forecast(template: Template) -> None:
    """80% of *actual* is the "there is still time to look" alarm; 100% of *forecast* is the
    one that fires early on a runaway loop, because actual spend crossing the limit is news
    that arrives after the money is gone."""
    (budget,) = _properties(template, "AWS::Budgets::Budget")
    notifications = budget["NotificationsWithSubscribers"]
    assert len(notifications) == 2
    by_type = {entry["Notification"]["NotificationType"]: entry for entry in notifications}
    assert set(by_type) == {"ACTUAL", "FORECASTED"}
    assert by_type["ACTUAL"]["Notification"]["Threshold"] == 80
    assert by_type["FORECASTED"]["Notification"]["Threshold"] == 100
    for entry in notifications:
        assert entry["Notification"]["ComparisonOperator"] == "GREATER_THAN"
        assert entry["Subscribers"] == [{"SubscriptionType": "EMAIL", "Address": _ALERT_EMAIL}], (
            entry
        )


def test_an_empty_alert_address_leaves_the_budget_with_no_notifications(tmp_path: Path) -> None:
    """`CfnBudget` requires at least one subscriber per notification, so an empty address
    cannot produce an empty notification block -- it has to produce none at all, or
    `CreateBudget` fails. The budget itself still exists, because the spend is worth
    recording even when nobody is subscribed; `AccountStack` annotates the synth so a budget
    with no alarm is visible rather than silent."""
    stack = _build(tmp_path, alert_email="")
    (budget,) = _properties(Template.from_stack(stack), "AWS::Budgets::Budget")
    assert "NotificationsWithSubscribers" not in budget, (
        "a notification with no subscriber is a CreateBudget failure, so the whole block "
        "has to be absent when there is no address -- and an empty list still renders the "
        "property, so the construct has to be given None rather than []"
    )
    Annotations.from_stack(stack).has_warning(
        "*", Match.string_like_regexp("no budget alert address")
    )


def test_the_web_acl_arn_is_the_one_output(template: Template) -> None:
    """`make deploy` reads no output from this stack -- `tests/test_web_stack.py` pins the
    set `web/config.js` is written from, and this stack must not grow a parallel contract.
    The ARN is here for the human chasing a blocked request to the ACL's metrics."""
    assert set(template.to_json()["Outputs"]) == {"ConsoleWebAclArn"}
