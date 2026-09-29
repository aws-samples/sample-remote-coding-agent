"""CDK entry point: the five stacks and the deploy-time context they read.

`main()` runs only under `__main__`. A test imports this module to prove the entry
point is importable, and an `aws_cdk.App()` built at import time is a side effect
that writes `cdk.out/` into whatever directory the suite happens to run from --
`tests/test_layout_infra.py` asserts exactly that, so keep every construct inside
`main()`.

The split into five stacks is by **lifecycle**, not by tidiness -- with one exception,
which is `AccountStack`:

* `DataStack` holds what must outlive everything else (three `RETAIN` tables), so
  `cdk destroy` of any other stack cannot take a fleet's history with it.
* `AgentStack` holds what has to be standing before a runtime can be created -- the
  ARM64 image, the VPC, the permissions boundary -- and creates no runtimes at all,
  because a runtime is created per API call by `provisioner._create`.
* `ApiStack` is the control plane, redeployed whenever a handler changes.
* `WebStack` is the console, and depends on `ApiStack` through **ARN strings** only.
* `AccountStack` is split off by **region**, not by lifecycle: a CloudFront-scoped WAF web
  ACL is only creatable in `us-east-1` and AWS Budgets is global, so neither can live in
  the region the CDK CLI resolved for the other four. Its docstring is the one place that
  constraint is written down. It is additionally *instanced* per consuming region -- one
  stack per (fleet, region) pair, named `{prefix}Account{RegionSuffix}` -- so that no
  region's deploy submits a template deleting another region's CDK `ExportsWriter`.

Every knob below is `--context`, not an environment variable, because context is
recorded in `cdk.context.json`/the synthesized template and so shows up in a
`cdk diff`; an env var read at synth time makes the same deploy produce two
different templates depending on whose shell ran it. `-c stackPrefix=` is the knob
that renames all five stacks at once, which is what lets one account host a second
fleet.
"""

from __future__ import annotations

import re
from typing import NamedTuple

import aws_cdk
from stacks import account_stack
from stacks.account_stack import AccountStack
from stacks.agent_stack import AgentStack
from stacks.api_stack import ApiStack
from stacks.data_stack import DataStack
from stacks.web_stack import WebStack

# The prefix on the physical names and metric names a deployment owns, so one AWS account
# can host more than one of them. It is deliberately NOT changed by the stack rename
# below, and that is not an oversight: `api_stack.py` scopes what the control plane may
# mint to `role/{app_name}/{app_name}-*` and `secret:{app_name}/github/*`, while
# `handlers/provisioner.py` creates those roles at the hardcoded path `/rca/` with the
# name `rca-{configId}` and `handlers/configurations.py` writes the secret at
# `rca/github/{configId}`. The IAM grant and the API call have to spell the same prefix or
# every provision fails with AccessDenied on CreateRole, so moving this constant is a
# change to the handlers, not to this line. Stack names carry no such seam.
APP_NAME = "rca"

# The five stack names are one prefix and five suffixes, and `-c stackPrefix=` moves all
# five together: `make deploy EMAIL=... STACK_PREFIX=Foo` deploys `FooData`/`FooRuntime`/
# `FooApi`/`FooConsole`/`FooAccount`, which is how one account hosts a second fleet
# (`data_stack.py` leaves every physical name to CloudFormation precisely so the stack name
# is the only thing separating two of them). The plugin side of that rename is
# `RCA_STACK_NAME` / the optional `stack_name` userConfig in `plugins/sample-rca`.
#
# The **account** suffix carries one thing more than the prefix: the consuming region, as
# `FooAccountApSoutheast1`. That is one stack per *(fleet, region)* pair rather than per
# account, and it is the fix for a measured cost, not tidiness. CDK's cross-region
# reference machinery writes an `ExportsWriter` custom resource *per consuming region* into
# the producing stack, and a synth only knows the one region it is deploying -- so while
# every region shared one `{prefix}Account`, deploying from a second region submitted a
# template that *deleted* the first region's `ExportsWriter`. CloudFormation reported
# `DELETE_FAILED (skipped)` after ~6 minutes of retry on every deploy in both directions,
# and left the stack in `UPDATE_COMPLETE_CLEANUP_IN_PROGRESS`, where the next deploy was
# refused outright. Per-region ownership means each producing stack has exactly one
# consuming region, so its `ExportsWriter` is never absent from the template that replaces
# it. The other four suffixes are untouched, because those stacks already live in the
# consuming region.
#
# The five constants below are the **defaults**, i.e. `stack_names()` with no argument, and
# `API_STACK_NAME` is a contract rather than a preference: `mcp/src/rca_mcp/client.py` pins
# `DEFAULT_STACK_NAME` and looks the `ApiUrl` output up on a stack of that name, so changing
# the default breaks the MCP plugin's discovery with an error about a missing stack rather
# than about a rename. `tests/test_grant_contract.py` asserts the two halves still agree.
# Deriving the constants from `stack_names()` instead of spelling five literals is what
# makes it structurally impossible for the pinned default and the name
# `-c stackPrefix=SampleCodingAgent` produces to disagree.
#
# The `Sample` qualifier in the default is load-bearing, not decoration. A stack name is
# unique per account and region, and `CodingAgent` is a name any number of things could
# reasonably pick: deploy an unqualified prefix into an account that already runs something
# of that name and `cdk deploy` does not create a second fleet, it **updates theirs** --
# pushing these templates onto their stacks, `…Data` among them, whose tables are `RETAIN`
# precisely because they hold history nothing can reconstruct. A qualifier no unrelated
# project would choose is the whole defence, which is also why `-c stackPrefix=` exists.
DEFAULT_STACK_PREFIX = "SampleCodingAgent"


class StackNames(NamedTuple):
    """The five stack names one deployment owns."""

    account: str
    data: str
    agent: str
    api: str
    web: str


_REGION_WORD = re.compile(r"[A-Za-z0-9]+")


def region_suffix(region: str | None) -> str:
    """`ap-southeast-1` -> `ApSoutheast1`: the consuming region as a stack-name token."""
    if not region:
        return ""
    return "".join(
        word[:1].upper() + word[1:] for word in _REGION_WORD.findall(str(region).strip().lower())
    )


def stack_names(prefix: str = DEFAULT_STACK_PREFIX, region: str | None = None) -> StackNames:
    """The five names for `prefix`, which is what `-c stackPrefix=` sets.

    `region` is the **consuming** region -- the one the other four stacks deploy into --
    and it only reaches the `account` entry, which is one stack per *(fleet, region)* pair.
    Passing no region yields the unsuffixed `{prefix}Account`, which is deliberate and
    load-bearing twice over: the five module constants below derive from `stack_names()`
    with no argument, and that unsuffixed name is exactly the name of the shared account
    stack any deploy from before the split left behind, which `make deploy` reports as
    orphaned.
    """
    return StackNames(
        account=f"{prefix}Account{region_suffix(region)}",
        data=f"{prefix}Data",
        agent=f"{prefix}Runtime",
        api=f"{prefix}Api",
        web=f"{prefix}Console",
    )


_DEFAULT_NAMES = stack_names()
ACCOUNT_STACK_NAME = _DEFAULT_NAMES.account
DATA_STACK_NAME = _DEFAULT_NAMES.data
AGENT_STACK_NAME = _DEFAULT_NAMES.agent
API_STACK_NAME = _DEFAULT_NAMES.api
WEB_STACK_NAME = _DEFAULT_NAMES.web

# `global.` on all three, which is the only prefix that works from every commercial region:
# a `global.` profile fans the call out to whichever region has capacity, which is what keeps
# Opus available from ap-southeast-1 at all -- and it is also why the foundation-model ARN in
# both IAM policies must NOT be region-scoped (invariant 21).
#
# Haiku was `apac.` here, on the reasoning that cheap local work (title generation,
# classification) wants the closest regional profile. That reasoning assumed the deployer was
# in APAC: `apac.` profiles exist ONLY in APAC, so from anywhere else the default was a
# `preflight: FAILED` at best -- and, for a deploy that skipped preflight, an id that passes
# `config.MODEL_RE` (a PATTERN, never a GEOGRAPHY) and then fails at invocation time. A default
# that only works in one geography is not a default. Pin a geo-scoped profile per deployment
# with `-c haikuModelId=...` when residency or latency calls for it; that is a deployment
# decision, and `make preflight` checks whichever ids the deploy actually forwards.
_DEFAULT_PLAN_MODEL = "global.anthropic.claude-opus-4-5-20251101-v1:0"
_DEFAULT_CODE_MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
_DEFAULT_HAIKU_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def _context(app: aws_cdk.App, key: str, default: str) -> str:
    """`--context key=value`, or `default`.

    `try_get_context` returns `None` for an absent key **and** for `-c key=`, so the
    `or` collapses both to the default: forwarding an empty model id would reach the
    container as a present-but-empty `ANTHROPIC_*_MODEL`, which defeats the container's
    own default and fails `validate_model` at start-up.
    """
    value = app.node.try_get_context(key)
    return str(value) if value else default


def _flag_context(app: aws_cdk.App, key: str, *, default: bool) -> bool:
    """A boolean knob from context, where the value arrives as a string.

    `-c codeGoalEnabled=false` is a string, and a non-empty string is truthy in Python,
    so reading a flag with `_context` would make every spelling of "off" mean on.
    """
    value = app.node.try_get_context(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def main() -> None:
    app = aws_cdk.App()

    # One environment for the four in-region stacks, from the ambient CDK CLI credentials.
    # It is spelled out rather than left implicit because `WebStack`'s CloudFront + Cognito
    # pair and `ApiStack`'s account-scoped IAM ARNs are both region- and
    # account-dependent, and an environment-agnostic stack resolves those to
    # pseudo-parameters that no test and no reviewer can read.
    #
    # The two reads are hoisted into locals because `AccountStack` needs the same *account*
    # with a different region, and re-reading the context there would let the two drift.
    account_id = app.node.try_get_context("account")
    region = app.node.try_get_context("region")
    env = aws_cdk.Environment(account=account_id, region=region)

    # Read once and threaded through, for the same reason as the two above: five separate
    # context reads would let one stack keep the default prefix while the other four moved.
    # `_context` collapses `-c stackPrefix=` (empty) back to the default, which is wanted --
    # an empty prefix would otherwise deploy a fleet named `Data`, `Api`, `Console`.
    # `region` reaches the names too, and only the account one: that stack is per consuming
    # region, and the region it is *named* after is the region of the other four, not the
    # `us-east-1` it deploys into.
    names = stack_names(_context(app, "stackPrefix", DEFAULT_STACK_PREFIX), region)

    data = DataStack(app, names.data, app_name=APP_NAME, env=env)
    agent = AgentStack(
        app,
        names.agent,
        app_name=APP_NAME,
        # The same address the budget uses, and deliberately not a second knob: `make deploy`
        # already requires `EMAIL=`, and an address nobody remembers to pass is an alarm
        # nobody hears. Empty leaves the DNS Firewall alarm actionless and annotates the
        # synth to say so.
        alert_email=_context(app, "budgetAlertEmail", ""),
        env=env,
    )

    api = ApiStack(
        app,
        names.api,
        app_name=APP_NAME,
        data=data,
        agent=agent,
        plan_model_id=_context(app, "planModelId", _DEFAULT_PLAN_MODEL),
        code_model_id=_context(app, "codeModelId", _DEFAULT_CODE_MODEL),
        haiku_model_id=_context(app, "haikuModelId", _DEFAULT_HAIKU_MODEL),
        # Defaults to the *code* model rather than to a fourth id: the fallback is what a
        # run degrades to when the planning model is throttled, so "the model we already
        # trust for coding" is the answer that needs no separate entitlement check.
        fallback_model_id=_context(app, "fallbackModelId", _DEFAULT_CODE_MODEL),
        # Both of these are forwarded on to the container as *fleet* policy
        # (`provisioner._fleet_environment`), so a deploy converges them across every
        # existing runtime rather than leaving each one with whatever it was born with
        # (invariant 24).
        goal_enabled=_flag_context(app, "codeGoalEnabled", default=True),
        # The identity every run's commit carries, and the reason it is a context key rather
        # than a constant: the container's default, `agent@localhost`, matches no GitHub
        # account, so GitHub resolves no author and any check that gates on commit
        # attribution refuses the pull request -- a Vercel preview answers `Deployment was
        # blocked` and never builds. Set both to an address GitHub can resolve, e.g.
        #   make deploy EMAIL=... CDK_CONTEXT='-c gitAuthorEmail=<id>+<user>@users.noreply.github.com'
        # A dedicated bot account is the honest choice over a human's address: the commits
        # were not written by a person, and the audit trail should not claim they were.
        # Empty is passed through deliberately -- `git_ops._identity` reads "" as unset and
        # uses its own default, so clearing the key restores the default rather than setting
        # an empty identity git would reject.
        git_author_name=_context(app, "gitAuthorName", ""),
        git_author_email=_context(app, "gitAuthorEmail", ""),
        env=env,
    )

    # The one stack pinned to another region, because a CloudFront-scoped WAF web ACL is
    # only creatable in us-east-1 and Budgets is global (`account_stack`'s docstring has
    # the whole reason). It is also one *instance* per consuming region -- `names.account`
    # carries `region` -- and that is what makes CDK's per-consumer `ExportsWriter` a
    # create-or-keep rather than a delete: a stack with exactly one consuming region never
    # has another region's writer to omit from the template that replaces it. While the
    # stack was shared, every deploy from a second region deleted the first region's writer,
    # which CDK's in-use tag turned into `DELETE_FAILED (skipped)` after ~6 minutes of retry.
    # `cross_region_references=True` has to be set on **both** the
    # producing and the consuming stack -- CDK writes a custom resource on each side, and
    # setting it here only makes `WebStack` fail at synth with an unresolvable reference.
    # It also refuses an environment-agnostic stack, which is why `account_id` and `region`
    # above have to be concrete -- and they are concrete only because `make deploy` resolves
    # both and passes them as `-c account=` and `-c region=`. Nothing else does: the CDK CLI
    # sets `CDK_DEFAULT_ACCOUNT`/`CDK_DEFAULT_REGION` as *environment variables* and injects
    # no `account`/`region` context, so `try_get_context` returns `None` for both and every
    # command fails at synth with `CrossRegionReferencesRequireExplicitRegion`. It is the
    # deploy target and not `preflight` that makes this work: preflight *prints* the account
    # it resolved, which reads like it settles the question and does not, because printing a
    # value is not passing it. A bare `cdk deploy` in this directory therefore cannot synth,
    # which is the trap `make deploy` exists to keep an operator out of.
    account = AccountStack(
        app,
        names.account,
        app_name=APP_NAME,
        monthly_budget_usd=_context(
            app, "monthlyBudgetUsd", account_stack.DEFAULT_MONTHLY_BUDGET_USD
        ),
        # Defaults to empty rather than to an address: a budget whose alarm mails a
        # placeholder is worse than one that annotates the synth to say it has no alarm.
        # `make deploy` passes `-c budgetAlertEmail=$(EMAIL)`, which it already requires.
        alert_email=_context(app, "budgetAlertEmail", ""),
        # The consuming region, as the discriminator on this stack's three CloudWatch metric
        # names. Two of these stacks live in us-east-1 at once now, so an undiscriminated
        # metric name would have two consoles writing into one metric.
        console_region=region or "",
        env=aws_cdk.Environment(account=account_id, region=account_stack.WAF_REGION),
        cross_region_references=True,
    )

    WebStack(
        app,
        names.web,
        app_name=APP_NAME,
        # ARNs, never the `Function` objects. Passing the objects makes the console's
        # integration write a Lambda permission naming the *web* API's id, so the two
        # stacks then reference each other and `cdk synth` fails with a cyclic reference.
        handler_arns=api.handler_arns,
        # The Cognito hosted-UI prefix, which must be globally unique within the region --
        # hence a context knob rather than a constant. A second deployment in the same
        # region fails at `CreateUserPoolDomain` without one.
        domain_prefix=_context(app, "consoleDomainPrefix", f"{APP_NAME}-console"),
        # The ARN, and required: `cloudfront.Distribution`'s `web_acl_id` takes the ARN for
        # a CloudFront-scoped ACL, and the cross-region hand-off is what the flag below
        # carries.
        web_acl_arn=account.web_acl_arn,
        env=env,
        cross_region_references=True,
    )

    app.synth()


if __name__ == "__main__":
    main()
