"""The two controls that cannot live in the deployment region: the WAF and the budget.

This stack exists because of an AWS constraint, not because five stacks read better than
four. Both of its resources are account-wide rather than region-scoped in any useful
sense, and both are only creatable from `us-east-1`:

* A `wafv2` web ACL with `Scope: CLOUDFRONT` is a global resource whose control plane
  lives in `us-east-1` only. Declaring it inside `WebStack` synthesizes perfectly and
  then fails at `CreateWebACL` for every operator whose CDK CLI resolved any other region
  -- and `infra/app.py` builds every other stack in exactly that resolved region. A
  `REGIONAL`-scoped ACL is not an escape: it cannot be attached to a distribution at all.
* AWS Budgets is a global service. Its canonical endpoint is `us-east-1`, and a budget
  created anywhere else is the same budget seen through a different door.

So `infra/app.py` gives this one stack an explicit `us-east-1` environment and hands the
ACL's ARN to `WebStack` through `cross_region_references=True`, which has to be set on
*both* the producing and the consuming stack. `WAF_REGION` below is the single place that
region is written down, and `tests/test_account_stack.py` asserts it -- so a later "let us
keep everything in one region" edit fails in a test rather than in front of an operator.

The stack lives in one region and is *instanced* per consuming region: `infra/app.py`'s
`stack_names(prefix, region)` gives the account entry a per-region suffix, so the ACL and
the budget a region uses are owned by a stack no other region rewrites. That is why
`console_region` is a required argument here rather than a nicety. CDK's cross-region
reference machinery writes an `ExportsWriter` custom resource per *consuming* region into
the producing stack, and a synth only knows the one region it is deploying -- so while this
stack was shared, deploying from a second region submitted a template that deleted the
first region's writer, which CloudFormation reported as `DELETE_FAILED (skipped)` after
minutes of retry and left the stack in `UPDATE_COMPLETE_CLEANUP_IN_PROGRESS`, where the
next deploy was refused outright. One instance per consuming region makes that writer a
create-or-keep instead. The cost of it is that two of these stacks now coexist in
`us-east-1`, so all three CloudWatch metric names carry the consuming region as a
discriminator (`console_metric_name`) -- without it two consoles write into one metric and
a rate-limit graph nobody can attribute back to a region.

Nothing here is bundled through Docker, so unlike `AgentStack` and `ApiStack` this stack
can be synthesized by a test. `console_web_acl_rules` is additionally pure, in the shape
of `agent_stack.execution_role_boundary_statements`, so the rule list can be read without
synthesizing at all.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

# A CloudFront requirement, not a preference. See the module docstring: a CLOUDFRONT-scoped
# web ACL can only be created in us-east-1, and a REGIONAL-scoped one cannot be attached to
# a distribution at all -- so there is no version of this that lives in the deploy region.
WAF_REGION = "us-east-1"

# A string, because `app.py::_context` returns strings (a `--context` value always arrives
# as one) and the default has to be the same type as the override. The stack casts with
# `float()`, which is where a non-numeric `-c monthlyBudgetUsd=` fails -- at synth, loudly.
DEFAULT_MONTHLY_BUDGET_USD = "200"

# Requests per five minutes from one IP, which is the only window WAFv2 offers; the floor
# it accepts is 100. 2000 is ~6 req/s sustained, far above a human driving a kanban board
# that polls, and far below a scripted sweep of the console's paths.
RATE_LIMIT_PER_FIVE_MINUTES = 2000

# A Cost Explorer *display name*, not an ARN or a service principal, and that is the whole
# reason it is a module constant a test can read: a typo here produces a budget that
# matches no line item, never fires, and is indistinguishable from being under budget.
BEDROCK_COST_FILTER = {"Service": ["Amazon Bedrock"]}


def console_metric_name(*, app_name: str, kind: str, console_region: str) -> str:
    """`rca-console-web-acl-ap-southeast-1`: a metric name no other region's ACL shares.

    Lowercase and hyphenated rather than CamelCase like the stack names, because that is the
    idiom the metric names already used and WAF's own pattern (`^[\\w#:.\\-/]+$`, up to 128
    characters) allows a region verbatim. An empty `console_region` -- which only happens
    when nothing resolved a region -- falls back to the pre-split, undiscriminated name.
    """
    suffix = f"-{console_region.strip()}" if console_region.strip() else ""
    return f"{app_name}-console-{kind}{suffix}"


def console_web_acl_rules(
    *, app_name: str, console_region: str
) -> list[wafv2.CfnWebACL.RuleProperty]:
    """The two rules on the console's web ACL: the managed common rule set, then a rate cap.

    Pure and stack-free so `tests/test_account_stack.py` can read the list directly, in the
    shape of `agent_stack.execution_role_boundary_statements`. The metric names carry
    `app_name` because CloudWatch metric names are per-account: two deployments sharing one
    account would otherwise write into one another's metrics. They carry `console_region` for
    the same reason one step further in -- this stack is one instance per consuming region,
    so two of them live in `us-east-1` at once.
    """
    return [
        wafv2.CfnWebACL.RuleProperty(
            name="AWSManagedRulesCommonRuleSet",
            priority=0,
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS",
                    name="AWSManagedRulesCommonRuleSet",
                )
            ),
            # `override_action`, and never `action`: a managed rule group carries its own
            # per-rule actions, so supplying `action` here is a `CreateWebACL` validation
            # error. The subtler half is the value -- `override_action=count={}` leaves the
            # whole group in logging-only mode while still reading, in a diff and in the
            # console, as "the common rule set is on". `none={}` is what makes it block.
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=console_metric_name(
                    app_name=app_name, kind="common-rules", console_region=console_region
                ),
                sampled_requests_enabled=True,
            ),
        ),
        wafv2.CfnWebACL.RuleProperty(
            name="RateLimit",
            priority=1,
            statement=wafv2.CfnWebACL.StatementProperty(
                rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                    limit=RATE_LIMIT_PER_FIVE_MINUTES,
                    aggregate_key_type="IP",
                )
            ),
            # The inverse of the gotcha above: a rate-based rule is not a managed group, it
            # has no rules of its own to override, so it takes `action` and `override_action`
            # is rejected outright.
            action=wafv2.CfnWebACL.RuleActionProperty(block={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=console_metric_name(
                    app_name=app_name, kind="rate-limit", console_region=console_region
                ),
                sampled_requests_enabled=True,
            ),
        ),
    ]


class AccountStack(cdk.Stack):
    """The us-east-1 stack, one instance per consuming region. Exposes `web_acl_arn`, which
    is the only thing it hands out.

    `console_region` is the region whose console this instance serves, and it is required
    rather than defaulted on purpose: a default would silently reproduce the metric-name
    collision between two of these stacks in `us-east-1` that it exists to remove.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        app_name: str,
        monthly_budget_usd: str,
        alert_email: str,
        console_region: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # As in `data_stack.py`: nothing here carries a physical name derived from
        # `app_name`, so it lands as a tag -- which is what separates two deployments
        # sharing one account in a cost report.
        cdk.Tags.of(self).add("Application", app_name)

        # -- the web ACL (Tier 1 #12) ---------------------------------------------
        # Defence in depth and nothing more: the console is already behind Cognito with
        # required MFA, so this is the cheap layer in front of the login page rather than
        # the control that matters.
        web_acl = wafv2.CfnWebACL(
            self,
            "ConsoleWebAcl",
            scope="CLOUDFRONT",
            # Allow by default, block by rule. A default block would take the console down
            # with it the moment a rule stopped matching.
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            rules=console_web_acl_rules(app_name=app_name, console_region=console_region),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=console_metric_name(
                    app_name=app_name, kind="web-acl", console_region=console_region
                ),
                sampled_requests_enabled=True,
            ),
        )
        # `attr_arn`, **not** `ref`. `cloudfront.Distribution`'s property is spelled
        # `web_acl_id`, and for a CloudFront-scoped ACL it wants the full ARN; `ref` returns
        # the bare id. Passing the id deploys cleanly, attaches nothing, and leaves a
        # distribution that reads as protected in the template and is not.
        self.web_acl_arn: str = web_acl.attr_arn

        # -- the Bedrock spend budget (Tier 1 #14) --------------------------------
        # A prompt-injected agent that cannot exfiltrate anything can still burn tokens,
        # and this is the only control for that. It is an alarm, not a cap: AWS Budgets
        # notifies, it does not stop the spend.
        budgets.CfnBudget(
            self,
            "BedrockSpend",
            budget=budgets.CfnBudget.BudgetDataProperty(
                # NO explicit `budget_name`. AWS Budgets is global and several `CfnBudget`
                # properties replace rather than update, so any update that replaces the
                # budget creates the new one before deleting the old -- against a name the
                # old one still holds:
                #
                #   A budget or resource with the same name but a different internalId
                #   already exists. (Service: Budgets, Status Code: 400)
                #
                # A generated name is the only thing that survives a replacement without
                # requiring manual budget deletion.
                #
                # What that costs, stated rather than hidden: two deployments in one account
                # now each get a budget, and because `BEDROCK_COST_FILTER` carries no
                # per-deployment dimension, both measure the account's *whole* Bedrock spend.
                # The fixed name never fixed that -- it only stopped the second deploy -- and
                # `-c stackPrefix=` makes two fleets in one account a supported configuration,
                # so refusing the second deploy is no longer the behaviour we want.
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=float(monthly_budget_usd), unit="USD"
                ),
                cost_filters=BEDROCK_COST_FILTER,
            ),
            notifications_with_subscribers=self._notifications(alert_email),
        )

        # One output, and only because a human chasing a blocked request needs the ARN to
        # find the ACL's metrics. `make deploy` reads nothing from this stack, and
        # `tests/test_web_stack.py::_EXPECTED_OUTPUTS` pins `WebStack`'s output set as a
        # contract -- this stack deliberately does not grow a parallel one.
        cdk.CfnOutput(self, "ConsoleWebAclArn", value=self.web_acl_arn)

    def _notifications(
        self, alert_email: str
    ) -> list[budgets.CfnBudget.NotificationWithSubscribersProperty] | None:
        """Two notifications, or none at all when there is no address to send them to.

        `CfnBudget` requires at least one subscriber per notification, so an empty address
        cannot produce an empty notification block. `None` and not `[]`: an empty list
        renders as `NotificationsWithSubscribers: []`, which CloudFormation rejects, while
        `None` leaves the property out of the template entirely. The annotation is what
        stops that being silent: a budget with no alarm is a budget nobody hears, and the
        warning says so in the synth output of the deploy that created it.
        """
        address = alert_email.strip()
        if not address:
            cdk.Annotations.of(self).add_warning(
                "no budget alert address was given, so the Bedrock spend budget was created "
                "with no notifications and nobody will hear it. Deploy with "
                "'make deploy EMAIL=you@example.com' (or -c budgetAlertEmail=...) to wire it."
            )
            return None

        subscribers = [
            budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=address)
        ]
        return [
            # 80% of actual spend: the month is not over and there is still time to look.
            budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    notification_type="ACTUAL",
                    comparison_operator="GREATER_THAN",
                    threshold=80,
                    threshold_type="PERCENTAGE",
                ),
                subscribers=subscribers,
            ),
            # 100% of *forecast*, which is the one that fires early on a runaway loop:
            # actual spend crossing the limit is news that arrives after the money is gone.
            budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    notification_type="FORECASTED",
                    comparison_operator="GREATER_THAN",
                    threshold=100,
                    threshold_type="PERCENTAGE",
                ),
                subscribers=subscribers,
            ),
        ]
