"""The single classification of the execution-role permissions boundary's Allow statements.

`infra/stacks/agent_stack.py::execution_role_boundary_statements` says it is "the union of
what `provisioner._execution_role_policy` grants and no wider", and neither half can see the
other: the boundary is written at synth time in `infra/`, the inline policy is built at run
time in `api/src/handlers/provisioner.py`, and the only thing linking them is that sentence.
This is what the gap cost: `ecr:*` sat in `MayNotEscalate`'s DENY list while the grant needed
the image pull, and because a role's effective permission under a boundary is
`identity ∩ boundary` -- with an explicit Deny in **either** document winning outright -- the
pull was a dead letter. `CreateAgentRuntime` failed `AccessDenied` with a null
`errorMessage`, so nothing named the missing permission and a full debugging cycle went into
finding it.

`tests/test_api_grant_contract.py` now checks both directions of that sentence and this
module holds the part both sides need to agree on. Every Allow **action** pattern is either
exercised by an action the inline policy grants or declared in `SURPLUS_ACTIONS`; every Allow
**resource** pattern is either literally one of the grant's resources or declared in
`WIDER_RESOURCES`. So the next Allow added to the boundary either matches the grant or costs
a line with a reason, rather than becoming dead surface a future inline policy could take
without review.

One rule here is a policy choice rather than IAM semantics, and is deliberate: an Allow
action pattern that *contains a wildcard* never counts as exercised, even though IAM would
happily read `logs:*` as covering `logs:CreateLogGroup`. By construction such a pattern
admits actions the inline policy does not spell, which is precisely "wider", so a
legitimately-wildcard Allow costs a `SURPLUS_ACTIONS` line. That friction is the deliverable,
not a bug.

Deliberately a module beside the tests rather than a `tests/conftest.py` fixture, for the
reason `tests/invoke_payload.py` gives: `conftest.py` is jointly owned, and the two halves of
this seam live in two files -- building the inline policy needs the `aws` fixture and
`test_api_fakes.load_handlers`, so it can only happen in `tests/test_api_grant_contract.py`,
while "is this declaration still true of the boundary?" needs only `stacks.agent_stack` and
belongs in `tests/test_grant_contract.py`. Deliberately importing nothing but `__future__`,
`fnmatch` and `typing`, so any test module can import it at collection time; even
`stacks.agent_stack` is *handed* to `build` rather than imported, so importing this file costs
no `aws_cdk` import.
"""

from __future__ import annotations

import fnmatch
from typing import Any

# The build parameters, here rather than spelled a third time, because `WIDER_RESOURCES`'
# keys interpolate them. They match the literals `tests/test_api_grant_contract.py` and
# `tests/test_grant_contract.py` already use, so a divergence surfaces as a failure of
# `test_every_boundary_declaration_names_something_the_boundary_still_allows` naming the
# pattern rather than as a test that passes vacuously.
APP_NAME = "rca"
ACCOUNT = "123456789012"
REGION = "ap-southeast-1"


def build(agent_stack: Any, asset_repository_arn: str) -> list[dict[str, Any]]:
    """The boundary as JSON, built with the parameters above.

    `agent_stack` is the module, handed in rather than imported, so importing this file
    costs no `aws_cdk` import at collection time.
    """
    return [
        statement.to_json()
        for statement in agent_stack.execution_role_boundary_statements(
            account=ACCOUNT,
            region=REGION,
            asset_repository_arn=asset_repository_arn,
        )
    ]


def allows(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [statement for statement in statements if statement["Effect"] == "Allow"]


def denies(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [statement for statement in statements if statement["Effect"] == "Deny"]


def actions(statement: dict[str, Any]) -> list[str]:
    action = statement["Action"]
    return action if isinstance(action, list) else [action]


def resources(statement: dict[str, Any]) -> list[str]:
    """IAM accepts `Resource` as a string or a list and means the same thing by both, and
    `iam.PolicyStatement.to_json()` uses both spellings in the one document -- a bare string
    for `MayTouchOwnRows`, a list for `MayInvokeClaude`. So nothing may index it; read it
    through here, as `_resources` in `tests/test_api_grant_contract.py` does."""
    resource = statement["Resource"]
    return resource if isinstance(resource, list) else [resource]


def permits(pattern: str, value: str) -> bool:
    """`fnmatchcase`, because IAM matches its wildcards case-sensitively."""
    return fnmatch.fnmatchcase(value, pattern)


def is_wildcard(pattern: str) -> bool:
    """`[` counts too: IAM supports only `*` and `?`, but `fnmatchcase` also honours a
    character class, so a pattern carrying one must not slip through unclassified."""
    return any(char in pattern for char in "*?[")


def granted_actions(grant_statements: list[dict[str, Any]]) -> set[str]:
    return {action for statement in grant_statements for action in actions(statement)}


def grants_for(
    allow: dict[str, Any], grant_statements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The grant statements with at least one action this Allow's patterns permit -- the
    pair within which comparing resources means anything."""
    return [
        statement
        for statement in grant_statements
        if any(
            permits(pattern, action) for pattern in actions(allow) for action in actions(statement)
        )
    ]


# Allow **action** patterns the boundary may carry with no matching grant, each mapped to a
# one-line reason.
#
# Empty, and the emptiness is the measured finding rather than an omission: built with the
# parameters above, the set of actions the boundary's Allow statements permit is *exactly*
# the 20 actions `provisioner._execution_role_policy` grants, no wildcard among them, both
# set differences empty. So there is nothing to declare and nothing to narrow, and the value
# of the test reading this table is that the next Allow fails it unless it is exercised or
# written down here.
#
# A legitimate future entry is a permission the AgentCore service itself needs on the role
# that the inline policy does not spell, or a wildcard Allow whose extra actions are
# deliberate. A wildcard pattern always belongs here: it can never count as exercised,
# because by construction it admits actions the inline policy does not spell.
SURPLUS_ACTIONS: dict[str, str] = {}

# Allow **resource** patterns that are wider than the resources the paired grant statements
# name, keyed by `(Sid, pattern)`. This is the dimension where the ceiling really is traded
# away, and each reason is the one already written into `agent_stack.py` beside the statement.
WIDER_RESOURCES: dict[tuple[str, str], str] = {
    (
        "MayRefreshGitHubCredential",
        f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:bedrock-agentcore-identity!*",
    ): (
        "The boundary is minted once per deploy, before any GitHub App is connected, so it can "
        "only name AgentCore Identity's service-linked prefix; the grant names the one literal "
        "client-secret ARN. The trailing `*` also absorbs the random suffix every secret ARN "
        "carries, so an ARN ending at the name would match nothing."
    ),
    ("MayTouchOwnRows", f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/*"): (
        "`DataStack` owns the table names and is a sibling stack, so naming them here would "
        "couple two deploys for no security. The per-configuration `dynamodb:LeadingKeys` "
        "fence in the inline policy (invariant 31) is what stops one repository reading "
        "another's rows, and a boundary's job is to stop a wider *action*, not to restate a "
        "condition -- restating it would mean every future fence had to be written twice to "
        "work at all."
    ),
    # `MayUseGitHubCredential` is NOT declared here, and its absence is the point.
    # It allows `*`, which looks like the widest entry in the boundary -- but the grant
    # under it allows `*` too, so the ceiling is no wider than the grant and there is no
    # widening to declare. That is the service's shape rather than a choice: measured
    # against a throwaway role, `GetWorkloadAccessTokenForUserId` is denied on an
    # individual `workload-identity/<name>` ARN and allowed only on
    # `workload-identity-directory/*`, and `GetResourceOauth2Token` is denied on the
    # credential provider's own ARN, on `token-vault/default/*` and on `token-vault/*`,
    # and allowed only on `*`. No condition key exists for the 3LO user id either, so
    # neither document can name one repository's credential. CLAUDE.md's "What remains
    # exposed" states that consequence; this table is only about ceilings that are wider
    # than their grants, and this one is not.
    #
    # `/aws/bedrock-agentcore/runtimes` spelled out rather than imported from
    # `agent_stack.RUNTIME_LOG_GROUP_PREFIX`: this module imports nothing.
    ("MayWriteRuntimeLogs", "arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/runtimes*"): (
        "The `*` sits directly after the prefix with no `/`, which is what makes one pattern "
        "cover both the log-group ARN and the `...:log-stream:*` form the inline policy "
        "grants in a second statement. Region and account are wildcards because the name "
        "prefix is the fence (Tier 1 #9)."
    ),
    ("MayUseOwnMemory", f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:memory/*"): (
        "One ceiling spans every configuration's memory because the boundary is minted once "
        "per deploy, before any memory exists, while the grant names one literal ARN per "
        "role. The trailing `*` also absorbs the ten-character suffix AgentCore appends to "
        "every memory id -- botocore's own `memoryId` pattern is "
        "`[a-zA-Z][a-zA-Z0-9-_]{0,99}-[a-zA-Z0-9]{10}` -- so an ARN ending at the name "
        "matches nothing at all. The per-configuration literal ARN in the inline policy is "
        "what stops one repository reaching another's memory (invariant 31's division of "
        "labour, the same one `MayTouchOwnRows` states for `dynamodb:LeadingKeys`)."
    ),
}

# The same guard `tests/test_invoke_payload_contract.py` puts on its two declaration tables:
# below this, a "reason" is a shrug and the table has become a silent allowlist.
MIN_REASON = 60
