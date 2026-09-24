"""The infra-side contracts, and specifically the ones nothing else can reach.

`ApiStack` bundles `../api` through Docker at construction time, so **no test in this
repository can synthesize it**, and `cdk synth` needs a daemon and credentials CI does
not have. Everything a test can check without either is therefore a module-level
constant or a pure function in `stacks/api_stack.py` and `stacks/agent_stack.py`, and
this file is what checks them: the route table, the grant table, the three-way fence on
`iam:CreateRole`, the boundary's model ARN -- and, last, every keyword `infra/` passes to
its own functions, because a keyword threaded wrongly from `app.py` is a synth-time
`TypeError` and there is otherwise nothing between it and a failed deploy.

`tests/test_api_grant_contract.py` is the *handler-side* half -- "every AWS call an entry
point reaches is declared" -- and stays a separate file: it reads `api/src`, this one
reads `infra/`, and a failure in one points at a different directory than a failure in
the other.
"""

from __future__ import annotations

import ast
import inspect
from types import ModuleType
from typing import Any

import app
import execution_boundary
import pytest
from conftest import REPO_ROOT
from rca_mcp import client as mcp_client
from stacks import account_stack, agent_stack, api_stack, data_stack, web_stack

HANDLERS = REPO_ROOT / "api/src/handlers"
INFRA = REPO_ROOT / "infra"

_BOUNDARY_ARN = "arn:aws:iam::123456789012:policy/rca-execution-boundary"

# What `AgentStack` passes as `self.image.repository.repository_arn`, spelled out: at synth
# time that attribute is a CDK token, and this is the ARN it resolves to -- the same
# repository `provisioner._asset_repository_arn` derives from `AGENT_IMAGE_URI` at run time.
_ASSET_REPOSITORY_ARN = (
    "arn:aws:ecr:ap-southeast-1:123456789012:repository/"
    "cdk-hnb659fds-container-assets-123456789012-ap-southeast-1"
)

_READ_ROUTES = (
    ("configurations", "get"),
    ("configurations", "list_"),
    # The read route that reaches a second data plane, and the reason the capability is
    # spelled `memory:read`: the assertion below is about capability *strings*, so it is not
    # what bounds this one. That is
    # `tests/test_api_grant_contract.py::test_the_memory_route_reaches_two_read_only_agentcore_operations_and_nothing_else`.
    ("configurations", "memory"),
    ("cards", "get"),
    ("cards", "list_"),
    ("tasks", "get"),
    ("tasks", "list_"),
    ("tasks", "activity"),
)


def _declared_routes(module: str) -> tuple[tuple[str, str, str], ...]:
    """The `ROUTES` tuple a handler module declares, by `ast` -- importing the module
    would construct `boto3.resource("dynamodb")` and raise `NoRegionError`."""
    tree = ast.parse((HANDLERS / f"{module}.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "ROUTES" for target in node.targets
        ):
            return tuple(tuple(entry) for entry in ast.literal_eval(node.value))
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ROUTES"
            and node.value is not None
        ):
            return tuple(tuple(entry) for entry in ast.literal_eval(node.value))
    return ()


def test_the_route_table_api_stack_wires_is_the_one_the_handlers_declare() -> None:
    """`api_stack.ROUTES` is spelled out by hand because `api/src` is not on the CDK
    app's import path. This is what keeps the copy honest: a route added to a module and
    not there is never wired up, and nothing at deploy time says so."""
    declared: dict[tuple[str, str], tuple[str, str]] = {}
    for module in ("configurations", "cards", "tasks"):
        for method, path, function in _declared_routes(module):
            declared[(method, path)] = (module, function)

    wired = {
        (method, path): (module, function) for method, path, module, function in api_stack.ROUTES
    }
    assert wired == declared, (
        f"api_stack.ROUTES and the handlers' own ROUTES disagree on "
        f"{sorted(set(wired) ^ set(declared)) or 'the module/function behind a path'}; a route a "
        f"module declares and ApiStack does not wire up answers 403 with no route, and one "
        f"ApiStack wires up with no handler is a 500 at the first request"
    )
    assert len(api_stack.ROUTES) == 19, "the 19 declared routes"


def test_every_entry_point_has_a_declared_grant_set() -> None:
    """One Lambda per entry point, and therefore one grant set per entry point: a
    function sharing a role with another gets the union of both, which is how a read
    route quietly acquires a write."""
    expected = {(module, function) for _, _, module, function in api_stack.ROUTES} | set(
        api_stack.QUEUE_ENTRY_POINTS
    )
    assert set(api_stack._ENTRY_POINT_GRANTS) == expected, (
        f"_ENTRY_POINT_GRANTS covers {sorted(set(api_stack._ENTRY_POINT_GRANTS) ^ expected)} "
        f"differently from the entry points ApiStack creates"
    )
    assert len(expected) == 23, (
        "19 routes plus the provisioner, dispatcher, DLQ handler and watchdog"
    )
    for entry, capabilities in api_stack._ENTRY_POINT_GRANTS.items():
        unknown = set(capabilities) - api_stack._CAPABILITIES
        assert not unknown, (
            f"{entry} declares {sorted(unknown)}, which _apply_grants cannot map to a grant -- "
            f"it raises at synth, which is the good case; the bad one is a typo that happens to "
            f"read like a real capability"
        )


@pytest.mark.parametrize("entry", _READ_ROUTES, ids=lambda entry: f"{entry[0]}.{entry[1]}")
def test_read_routes_get_no_write_capability(entry: tuple[str, str]) -> None:
    """Note 30: a read handler never gains a `logs:*` or `bedrock-agentcore:*` permission
    to look something up live. Log locations are *derived* from stored attributes, so a
    compromised read route cannot enumerate the fleet or dispatch a run."""
    for capability in api_stack._ENTRY_POINT_GRANTS[entry]:
        assert not capability.endswith((":write", ":send")), (
            f"{entry[0]}.{entry[1]} is a read route and holds {capability}"
        )
        for forbidden in ("agentcore", "iam", "secrets"):
            assert forbidden not in capability, (
                f"{entry[0]}.{entry[1]} is a read route and holds {capability}; a read route "
                f"talks to DynamoDB and nothing else"
            )


def _module_scope_environment_names(path: str) -> set[str]:
    """The environment-variable names a handler module reads **at import**, recovered as
    literals from `os.environ.get("NAME")` and `_csv_environment("NAME")`.

    Module scope only: a read inside a function body (`AWS_REGION`, which Lambda sets
    itself) is not something a stack has to forward.
    """
    tree = ast.parse((HANDLERS / path).read_text())
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            func = call.func
            reading = (isinstance(func, ast.Name) and func.id == "_csv_environment") or (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
            )
            if reading and isinstance(call.args[0], ast.Constant):
                names.add(call.args[0].value)
    return names


def test_provisioner_environment_names_every_variable_the_handler_reads() -> None:
    environment = api_stack._provisioner_environment(
        image_uri="123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/agent:abc",
        boundary_arn=_BOUNDARY_ARN,
        subnet_ids=["subnet-1", "subnet-2"],
        security_group_ids=["sg-1"],
        plan_model_id="global.anthropic.claude-opus-4-5-20251101-v1:0",
        code_model_id="global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        haiku_model_id="apac.anthropic.claude-haiku-4-5-20251001-v1:0",
        fallback_model_id="global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        goal_enabled=True,
        otel_metrics_enabled=False,
        platform_version="V1",
    )
    assert environment == {
        "AGENT_IMAGE_URI": "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/agent:abc",
        "EXECUTION_ROLE_BOUNDARY_ARN": _BOUNDARY_ARN,
        # Comma-joined here and split by `provisioner._csv_environment` there -- a Lambda
        # environment variable holds a string, and the list has to survive the round trip.
        "AGENT_SUBNET_IDS": "subnet-1,subnet-2",
        "AGENT_SECURITY_GROUP_IDS": "sg-1",
        "PLAN_MODEL_ID": "global.anthropic.claude-opus-4-5-20251101-v1:0",
        "CODE_MODEL_ID": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "HAIKU_MODEL_ID": "apac.anthropic.claude-haiku-4-5-20251001-v1:0",
        "FALLBACK_MODEL_ID": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "CODE_GOAL_ENABLED": "1",
        # Forwarded even when off, and that is the point: the provisioner's own constant reads
        # this name, `_fleet_environment` puts its value on every runtime as
        # `CLAUDE_CODE_ENABLE_TELEMETRY`, and a `GetAgentRuntime` diff showing "0" says
        # something where an absent variable says nothing.
        "OTEL_METRICS_ENABLED": "0",
        # Forwarded even at the default, for the same reason as the flag above and one more:
        # `provisioner.PLATFORM_VERSION` is sent on every create *and* update, because
        # omitting it on an update means "keep the current platform version" rather than
        # "use the default". An absent variable here would make the deploy-time key one-way.
        "AGENT_PLATFORM_VERSION": "V1",
    }

    # The names are the contract, and the two halves are written in different
    # directories by different people. A rename on either side fails here rather than at
    # run time, where an unset variable reads as a missing grant.
    read = _module_scope_environment_names("provisioner.py")
    assert read - set(environment) == set(), (
        f"provisioner.py reads {sorted(read - set(environment))} at import and ApiStack "
        f"forwards no such variable; add it to _provisioner_environment"
    )
    assert set(environment) - read == set(), (
        f"ApiStack forwards {sorted(set(environment) - read)}, which provisioner.py never "
        f"reads -- either the read was renamed or the variable is dead"
    )


def test_the_goal_flag_is_normalised_rather_than_forwarded_as_a_bool() -> None:
    """`config._env_flag` treats an empty value as *unset*, and a Lambda environment
    variable is a string either way, so the boolean has to become `"1"`/`"0"` somewhere.
    Doing it here means a `GetAgentRuntime` diff shows a value that says something."""
    off = api_stack._provisioner_environment(
        image_uri="i",
        boundary_arn=_BOUNDARY_ARN,
        subnet_ids=[],
        security_group_ids=[],
        plan_model_id="p",
        code_model_id="c",
        haiku_model_id="h",
        fallback_model_id="f",
        goal_enabled=False,
        otel_metrics_enabled=False,
        platform_version="V1",
    )
    assert off["CODE_GOAL_ENABLED"] == "0"


def _metrics_environment(*, otel_metrics_enabled: bool) -> dict[str, str]:
    """`_provisioner_environment` with everything but the metrics flag held constant."""
    return api_stack._provisioner_environment(
        image_uri="i",
        boundary_arn=_BOUNDARY_ARN,
        subnet_ids=[],
        security_group_ids=[],
        plan_model_id="p",
        code_model_id="c",
        haiku_model_id="h",
        fallback_model_id="f",
        goal_enabled=True,
        otel_metrics_enabled=otel_metrics_enabled,
        platform_version="V1",
    )


def test_the_metrics_flag_is_normalised_rather_than_forwarded_as_a_bool() -> None:
    """The same normalisation the goal flag gets, for the same reason -- a Lambda environment
    variable is a string either way -- and then one difference that is the whole point of this
    knob: the **direction of the default**.

    `provisioner.CODE_GOAL_ENABLED` is an opt-out blacklist, so an empty or unrecognised value
    there means *on*. `provisioner.OTEL_METRICS_ENABLED` is an opt-in whitelist, so an empty
    or unrecognised value means *off*. That asymmetry is deliberate: a `/goal` loop that comes
    on by accident is a feature, while an exporter that comes on by accident is a
    `cloudwatch:PutMetricData` grant on `*`, a widened permissions boundary and a CloudWatch
    bill nobody asked for. So this test pins both the stack side's "1"/"0" and the handler
    side's direction; a handler that flipped to `.get(...) != "0"` would pass every other test
    in this file.
    """
    assert _metrics_environment(otel_metrics_enabled=True)["OTEL_METRICS_ENABLED"] == "1"
    assert _metrics_environment(otel_metrics_enabled=False)["OTEL_METRICS_ENABLED"] == "0"

    # The handler's own reader, by source rather than by import: `provisioner.py` builds
    # `boto3.resource("dynamodb").Table(...)` at module scope and cannot be imported without a
    # region. The three spellings that mean on are the whole vocabulary, so every other value
    # -- unset, empty, "TRUE " with a space, "on", a typo -- lands on "0".
    source = (HANDLERS / "provisioner.py").read_text(encoding="utf-8")
    assert (
        'os.environ.get("OTEL_METRICS_ENABLED", "").strip().lower() in ("1", "true", "yes")'
        in source
    ), (
        "OTEL_METRICS_ENABLED must be read as an opt-in whitelist -- membership of a closed "
        "set of on-values -- and not as an opt-out blacklist like CODE_GOAL_ENABLED. Inverting "
        "it makes an unrecognised value turn the exporter, the grant and the boundary widening "
        "on, which is the one failure mode this flag exists to make impossible."
    )


def test_the_boundary_widens_only_when_metrics_are_enabled() -> None:
    """The ceiling half of the switch, built both ways.

    `provisioner._execution_role_policy` grants `cloudwatch:PutMetricData` only when its own
    `OTEL_METRICS_ENABLED` is "1", and a boundary is a whitelist: a grant the boundary does not
    cover is *silently denied at run time*, with nothing naming the missing action -- the
    container would simply export no metric and report nothing. The converse costs review
    rather than function, which is why both directions are asserted here.
    """

    def actions_of(otel_metrics_enabled: bool) -> set[str]:
        return {
            action
            for statement in execution_boundary.allows(
                execution_boundary.build(
                    agent_stack,
                    _ASSET_REPOSITORY_ARN,
                    otel_metrics_enabled=otel_metrics_enabled,
                )
            )
            for action in execution_boundary.actions(statement)
        }

    on, off = actions_of(True), actions_of(False)
    assert "cloudwatch:PutMetricData" in on, (
        "with otelMetricsEnabled on, the boundary must permit the one action the inline policy "
        "then grants, or every metric export is an AccessDenied the container cannot report"
    )
    assert "cloudwatch:PutMetricData" not in off, (
        "with otelMetricsEnabled off the inline policy grants nothing on CloudWatch, so a "
        "ceiling that allows it anyway is dead surface a future inline policy could take "
        "without review -- and it is the shipped state of this branch"
    )
    assert on - off == {"cloudwatch:PutMetricData"}, (
        f"the flag may widen the ceiling by exactly that one write-only action, and it widened "
        f"it by {sorted(on - off)}"
    )


def _statement_with(statements: list[Any], action: str) -> dict[str, Any]:
    matching = [
        statement.to_json()
        for statement in statements
        if action
        in (
            statement.to_json()["Action"]
            if isinstance(statement.to_json()["Action"], list)
            else [statement.to_json()["Action"]]
        )
    ]
    assert len(matching) == 1, (
        f"expected exactly one statement carrying {action}, got {len(matching)}"
    )
    return matching[0]


def _resources(statement: dict[str, Any]) -> list[str]:
    resource = statement["Resource"]
    return resource if isinstance(resource, list) else [resource]


@pytest.fixture
def provisioner_statements() -> list[Any]:
    return api_stack.provisioner_role_statements(
        app_name="rca", account="123456789012", boundary_arn=_BOUNDARY_ARN
    )


def test_create_role_is_fenced_three_ways(provisioner_statements: list[Any]) -> None:
    """Invariant 17. `iam:CreateRole` on a Lambda is one condition away from an
    administrator role, so it is fenced by the role *path*, by a required permissions
    boundary, and by what the resulting role may be passed to -- and all three have to
    hold, because each one alone is escapable."""
    create = _statement_with(provisioner_statements, "iam:CreateRole")
    assert all(":role/rca/" in resource for resource in _resources(create)), (
        f"fence one of three: the minted roles must be path-scoped, got {_resources(create)}"
    )
    assert create["Condition"]["StringEquals"]["iam:PermissionsBoundary"] == _BOUNDARY_ARN, (
        "fence two of three: without the boundary condition this Lambda can mint a role "
        "carrying any policy at all"
    )

    passrole = _statement_with(provisioner_statements, "iam:PassRole")
    assert (
        passrole["Condition"]["StringEquals"]["iam:PassedToService"]
        == "bedrock-agentcore.amazonaws.com"
    ), (
        "fence three of three: a minted role is only useful once it can be handed to "
        "something, so limit what it may be handed to"
    )


def test_tag_role_carries_no_permissions_boundary_condition(
    provisioner_statements: list[Any],
) -> None:
    """Invariant 16, and the reason `iam:TagRole` is its own statement.

    `provisioner._role_kwargs` passes `Tags`, and a `CreateRole` carrying tags triggers a
    *separate* `TagRole` authorization whose request context has no
    `iam:PermissionsBoundary` key. A conditioned statement therefore never matches that
    authorization and the whole `CreateRole` is denied -- with an error naming TagRole,
    which reads like a tagging problem rather than the condition it actually is.
    Path-scoping is the only control available here.
    """
    tag = _statement_with(provisioner_statements, "iam:TagRole")
    assert "Condition" not in tag, (
        "iam:TagRole must carry NO condition: the TagRole authorization context lacks "
        "iam:PermissionsBoundary, so a conditioned statement never matches and every "
        "CreateRole is denied"
    )
    assert all(":role/rca/" in resource for resource in _resources(tag)), (
        "the path scope is the only fence TagRole has; it may not be dropped along with the "
        "condition"
    )


def test_the_service_linked_role_grant_is_fenced_to_agentcore_by_a_wildcard_match(
    provisioner_statements: list[Any],
) -> None:
    """A `networkMode: VPC` runtime (invariant 30) needs a service-linked role AgentCore
    creates for itself during `CreateAgentRuntime`, and without
    `iam:CreateServiceLinkedRole` every `CreateAgentRuntime` in a live eu-west-1 deployment
    failed `Failed creating service linked role. Please verify that the calling role has
    sufficient permissions to create a service linked role.` The only provisioner in this
    account that succeeds uses `networkMode: PUBLIC`, which never reaches this role at all
    -- the network mode is the whole difference.

    The operator is `StringLike`, deliberately: the AgentCore service-linked role already
    in the account is `AWSServiceRoleForBedrockAgentCoreRuntimeIdentity` for
    `runtime-identity.bedrock-agentcore.amazonaws.com`, so the names are
    subdomain-qualified, and the VPC-mode one's name is documented nowhere reachable from
    here. Both patterns are anchored on the literal suffix, so the grant still cannot
    reach `bedrock.amazonaws.com` or any other service.
    """
    slr = _statement_with(provisioner_statements, "iam:CreateServiceLinkedRole")
    assert "StringEquals" not in slr["Condition"], (
        "a StringEquals on one guessed service name is the failure mode being fixed: the "
        "VPC-mode role's service name cannot be looked up from here, and the sibling role "
        "already in the account proves the names are subdomain-qualified, so an exact match "
        "on a guess just fails the deploy again"
    )

    patterns = slr["Condition"]["StringLike"]["iam:AWSServiceName"]
    patterns = patterns if isinstance(patterns, list) else [patterns]
    for pattern in patterns:
        assert pattern.endswith("bedrock-agentcore.amazonaws.com"), (
            f"{pattern} is not anchored on AgentCore's own suffix, so this grant can create a "
            f"service-linked role for some other service entirely"
        )
    assert any(pattern.startswith("*") for pattern in patterns), (
        f"got {patterns}: a subdomain-qualified name like "
        f"runtime-identity.bedrock-agentcore.amazonaws.com has to be covered, which is what "
        f"the wildcard label is for"
    )

    create = _statement_with(provisioner_statements, "iam:CreateRole")
    create_actions = create["Action"] if isinstance(create["Action"], list) else [create["Action"]]
    assert "iam:CreateServiceLinkedRole" not in create_actions, (
        "this action must stay in its own statement: the iam:CreateRole statement requires "
        "iam:PermissionsBoundary, a service-linked role never carries one, so the condition "
        "would never match and the call would be denied -- and relaxing that condition to "
        "make it match would loosen invariant 17's three-way fence"
    )


@pytest.fixture
def boundary_statements() -> list[Any]:
    return agent_stack.execution_role_boundary_statements(
        app_name="rca",
        account="123456789012",
        region="ap-southeast-1",
        # The boundary cannot be the union of what the inline policy grants without naming
        # the repository the agent image lives in.
        asset_repository_arn=_ASSET_REPOSITORY_ARN,
        # The shipped state of the `otelMetricsEnabled` context key, so every test taking this
        # fixture asserts the ceiling a deploy from this branch actually creates. The other
        # state has its own test, `test_the_boundary_widens_only_when_metrics_are_enabled`.
        otel_metrics_enabled=False,
    )


def test_the_boundary_model_arn_is_not_region_scoped(boundary_statements: list[Any]) -> None:
    """Invariant 21: a `global.` inference profile fans the call out to whichever region
    serves the request, so a region in the foundation-model ARN denies the call from
    every other one -- and the denial names the *model*, which reads as a bad model id or
    an unentitled account rather than as a policy written one field too narrow."""
    invoke = _statement_with(boundary_statements, "bedrock:InvokeModel")
    resources = _resources(invoke)
    assert any(
        resource.startswith("arn:aws:bedrock:*::foundation-model/") for resource in resources
    ), f"the foundation-model ARN must carry no region, got {resources}"
    for resource in resources:
        assert "ap-southeast-1" not in resource, (
            f"{resource} is region-scoped; a global. inference profile routes the call outside "
            f"the calling region"
        )


def test_the_boundary_permits_the_image_pull(boundary_statements: list[Any]) -> None:
    """The ceiling over `provisioner._execution_role_policy`'s ECR grant, and the reason
    this function's own docstring says an action the inline policy allows and this list
    omits is **silently denied at runtime**: the *runtime* pulls `AGENT_IMAGE_URI` with the
    execution role before any container code runs, so an omission here is a
    `CreateAgentRuntime` that fails `AccessDenied` with a null `errorMessage` and no
    runtime ever created -- nothing names the missing permission."""
    pull = _statement_with(boundary_statements, "ecr:BatchGetImage")
    assert pull["Effect"] == "Allow"
    assert set(pull["Action"]) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }, "the four actions the one working execution role in this account carries, and no more"
    assert _resources(pull) == [_ASSET_REPOSITORY_ARN], (
        f"the pull is scoped to the repository the asset lives in, which is the one "
        f"`provisioner._asset_repository_arn` derives from `AGENT_IMAGE_URI`, got "
        f"{_resources(pull)}"
    )

    authenticate = _statement_with(boundary_statements, "ecr:GetAuthorizationToken")
    assert authenticate["Effect"] == "Allow"
    assert _resources(authenticate) == ["*"], (
        "`ecr:GetAuthorizationToken` supports no resource types, so a scoped ARN here matches "
        "no authorization at all and every pull fails"
    )


def test_the_boundary_denies_escalation(boundary_statements: list[Any]) -> None:
    """A boundary is a ceiling, and the explicit deny is why it is worth attaching: the
    condition on the provisioner's `iam:CreateRole` only checks that *some* boundary is
    present, so this list is what makes attaching one mean something."""
    denies = [
        statement.to_json()
        for statement in boundary_statements
        if statement.to_json()["Effect"] == "Deny"
    ]
    assert denies, "the boundary must deny escalation outright, not merely omit it"
    denied = {action for statement in denies for action in statement["Action"]}
    assert "iam:*" in denied, (
        "without iam:* the provisioner's own CreateRole grant is enough to mint an "
        "administrator role"
    )
    assert "bedrock-agentcore:InvokeAgentRuntime" in denied, (
        "without it a container can invoke its own runtime, turning one compromised task into "
        "an unbounded fan-out -- the self-dispatch fence, named exactly rather than by a "
        "wildcard now that MayUseOwnMemory allows six data-plane memory actions"
    )
    # The `ecr:` clause below, in the dimension the same mistake is most likely to be made
    # next: a `bedrock-agentcore:*` here -- or `List*`, or `Create*` -- shadows
    # `MayUseOwnMemory`'s `ListEvents`, `ListMemoryRecords` and `CreateEvent`, and the symptom
    # is a container that can write no memory event at all with nothing naming the cause. The
    # destructive memory actions are denied here one literal name at a time for exactly that
    # reason.
    assert not any(
        action.startswith("bedrock-agentcore:") and "*" in action for action in denied
    ), (
        "a bedrock-agentcore wildcard deny shadows MayUseOwnMemory and the UseOwnMemory grant "
        "it ceilings; spell the actions this boundary denies out, as the twelve entries in "
        "MayNotEscalate do. Every other AgentCore action is denied by not being listed among "
        "the Allows -- a boundary is a whitelist"
    )
    # An explicit Deny beats the Allow above it, so an `ecr:*` entry here reinstates the
    # original bug -- no image pull, `CreateAgentRuntime` denied -- while the diff reads as
    # fixed. A boundary is a whitelist, so `ecr:PutImage` and friends are denied by not
    # being listed among the Allows rather than by being denied here.
    assert not any(action.startswith("ecr:") for action in denied), (
        "the ECR pull is granted by MayAuthenticateToEcr/MayPullAgentImage above; a deny here "
        "nullifies both, and the failure is a null errorMessage on CreateAgentRuntime"
    )
    # Same shape as the `ecr:` clause above, and the reason it is worth writing twice: with
    # `otelMetricsEnabled` on, the boundary carries a `MayPublishAgentMetrics` Allow, and an
    # explicit Deny beats every Allow in either document. A `cloudwatch:*` here would make
    # that Allow -- and the grant it is the ceiling over -- a dead letter whose only symptom is
    # a fleet that exports no metric, which is indistinguishable from a collector that never
    # started. `MayNotEscalate` denies IAM, STS, Organizations, AgentCore, S3 and SQS, and
    # CloudWatch is deliberately not among them.
    assert not any(action.startswith("cloudwatch") for action in denied), (
        "a cloudwatch deny here silently shadows MayPublishAgentMetrics and the "
        "PublishAgentMetrics grant it ceilings; PutMetricData is write-only, so the escalation "
        "this list guards against is not reachable through it. Every other CloudWatch action "
        "is denied by not being listed among the Allows -- a boundary is a whitelist"
    )
    assert all("*" in _resources(statement) for statement in denies)


def test_no_deny_in_the_boundary_shadows_an_allow_in_it() -> None:
    """The `ecr:*` failure stated once, mechanically, rather than one clause per service
    prefix.

    `test_the_boundary_denies_escalation` above spells that property out for `ecr:`, for
    `cloudwatch` and for a `bedrock-agentcore` wildcard, because each of those messages names
    its own incident and is worth reading at the point of failure. This is the general form:
    an explicit Deny beats every Allow in the same document, so a Deny pattern that matches
    an action this boundary Allows makes that Allow -- and the inline-policy grant it is the
    ceiling over -- a dead letter whose only symptom is an `AccessDenied` nothing reports.
    Whatever service the next Allow names, this catches it without waiting for a fourth
    clause.

    The intra-document generalisation of the cross-document check
    `tests/test_api_grant_contract.py::test_the_boundary_permits_every_action_the_inline_policy_grants`
    already performs against the grant; it lives here because it needs only
    `stacks.agent_stack` -- the same reason
    `test_every_boundary_declaration_names_something_the_boundary_still_allows` below does.

    Both states of `otelMetricsEnabled`, because the flag adds an Allow (and only an Allow):
    the state that is off cannot see a Deny shadowing `MayPublishAgentMetrics`.
    """
    for otel_metrics_enabled in (False, True):
        boundary = execution_boundary.build(
            agent_stack, _ASSET_REPOSITORY_ARN, otel_metrics_enabled=otel_metrics_enabled
        )
        for allow in execution_boundary.allows(boundary):
            for allowed in execution_boundary.actions(allow):
                # Literal Allow patterns only. A wildcard Allow is not an action, so matching
                # a Deny pattern against it would compare two patterns and mean nothing --
                # `test_the_boundary_permits_no_action_the_inline_policy_does_not_grant` is
                # what makes a wildcard Allow cost a `SURPLUS_ACTIONS` line in the first place.
                if execution_boundary.is_wildcard(allowed):
                    continue
                for deny in execution_boundary.denies(boundary):
                    for pattern in execution_boundary.actions(deny):
                        assert not execution_boundary.permits(pattern, allowed), (
                            f"the boundary's {deny['Sid']} denies {pattern}, which matches "
                            f"{allowed} allowed by {allow['Sid']} (otelMetricsEnabled="
                            f"{otel_metrics_enabled}). An explicit Deny beats every Allow in "
                            f"the same document, so that Allow and the inline-policy grant it "
                            f"ceilings are both dead letters while the diff reads as correct. "
                            f"Spell the denial out one literal action at a time, as "
                            f"MayNotEscalate does -- every action this boundary does not "
                            f"Allow is already denied by omission, because a boundary is a "
                            f"whitelist"
                        )


def test_every_boundary_declaration_names_something_the_boundary_still_allows(
    boundary_statements: list[Any],
) -> None:
    """The direction that stops a declaration outliving the Allow it excuses.

    `tests/execution_boundary.py` is read from both sides of the seam, and the grant side
    checks that every Allow is exercised or declared. This is the other way round: a
    `SURPLUS_ACTIONS` or `WIDER_RESOURCES` entry for a statement that has since been
    narrowed or deleted still *reads* as an explanation of the ceiling, and the next reader
    trusts it. Needing no inline policy, it belongs here -- `stacks.agent_stack` is the
    only thing it touches.

    It is also what catches a divergence in `execution_boundary`'s own build parameters:
    `WIDER_RESOURCES`' keys interpolate `ACCOUNT`/`REGION`/`APP_NAME`, so a fixture value
    that moves on one side fails here naming the pattern rather than passing vacuously.
    """
    allows = execution_boundary.allows([statement.to_json() for statement in boundary_statements])
    allowed = {action for allow in allows for action in execution_boundary.actions(allow)}
    by_sid = {allow["Sid"]: allow for allow in allows}

    for pattern, reason in execution_boundary.SURPLUS_ACTIONS.items():
        assert pattern in allowed, (
            f"execution_boundary.SURPLUS_ACTIONS declares {pattern!r} as surplus, but no Allow "
            f"in execution_role_boundary_statements carries it any more -- delete the line, "
            f"because the reason it gives is now untrue of the boundary: {reason}"
        )

    for (sid, pattern), reason in execution_boundary.WIDER_RESOURCES.items():
        assert sid in by_sid, (
            f"execution_boundary.WIDER_RESOURCES declares a widening on {sid!r}, which is no "
            f"longer an Allow statement in execution_role_boundary_statements -- delete the "
            f"line: {reason}"
        )
        assert pattern in execution_boundary.resources(by_sid[sid]), (
            f"execution_boundary.WIDER_RESOURCES declares {pattern!r} on {sid}, which now names "
            f"{execution_boundary.resources(by_sid[sid])} -- the ceiling moved and the "
            f"declaration did not, so either update the pattern or delete the line: {reason}"
        )


def test_the_boundary_declaration_tables_carry_a_reason() -> None:
    """The mirror of `test_invoke_payload_contract.py::test_the_declaration_tables_carry_a_reason`,
    and the only thing keeping either table from decaying into a silent allowlist: a
    declaration whose reason is a shrug excuses the widening just as effectively as one that
    explains it."""
    tables = (
        (execution_boundary.SURPLUS_ACTIONS, "SURPLUS_ACTIONS"),
        (execution_boundary.WIDER_RESOURCES, "WIDER_RESOURCES"),
    )
    for table, name in tables:
        for key, reason in table.items():
            assert isinstance(reason, str)
            assert len(reason.strip()) >= execution_boundary.MIN_REASON, (
                f"execution_boundary.{name}[{key!r}] needs a reason a reader can act on, "
                f"naming what makes the ceiling deliberately wider there"
            )


def test_the_api_stack_name_and_output_key_are_the_ones_the_mcp_client_looks_up() -> None:
    """The plugin's whole "nobody types the API URL" story is a `describe_stacks` against
    that name for that output, so both halves are a contract between two directories."""
    assert app.API_STACK_NAME == mcp_client.DEFAULT_STACK_NAME
    assert api_stack.API_URL_OUTPUT_KEY == mcp_client.API_URL_OUTPUT_KEY
    source = (INFRA / "stacks/api_stack.py").read_text()
    assert "CfnOutput(self, API_URL_OUTPUT_KEY" in source, (
        "the output's logical id IS the constant -- writing the literal instead lets the two "
        "drift without any test noticing"
    )


def _callables(module: ModuleType) -> dict[str, Any]:
    return {
        name: value
        for name, value in vars(module).items()
        if (inspect.isfunction(value) or inspect.isclass(value))
        and getattr(value, "__module__", None) == module.__name__
    }


def test_every_keyword_infra_passes_is_one_the_callee_accepts() -> None:
    """The guard for the one failure this suite otherwise cannot see.

    `ApiStack` bundles `../api` through Docker at construction time, so nothing here can
    build it, and `cdk synth` needs a daemon and deploy credentials CI does not have. A
    keyword threaded wrongly from `app.py` into `ApiStack.__init__` -- or from there into
    `_provisioner_environment` -- is therefore a `TypeError` that first appears in front
    of a human running a deploy. This walks every call `infra/` makes to its own
    functions and checks the keywords against the real signature.
    """
    registry: dict[str, Any] = {}
    # The tuple is spelled out rather than discovered, so a new `stacks/*.py` module has to
    # be added here: a constructor the registry does not know resolves to no entry at all
    # and its keywords are then checked by nothing, which is exactly the blind spot this
    # test closes rather than a test that quietly passes.
    for module in (app, account_stack, data_stack, agent_stack, api_stack, web_stack):
        registry.update(_callables(module))

    checked = 0
    for path in [INFRA / "app.py", *sorted((INFRA / "stacks").glob("*.py"))]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            target = registry.get(node.func.id)
            if target is None:
                continue
            signature = inspect.signature(target)
            accepted = set(signature.parameters)
            takes_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            for keyword in node.keywords:
                if keyword.arg is None:  # `**something`, unresolvable statically
                    continue
                checked += 1
                assert takes_kwargs or keyword.arg in accepted, (
                    f"{path.name}:{node.lineno} passes {keyword.arg}= to {node.func.id}, which "
                    f"accepts {sorted(accepted)}. This is a TypeError at `cdk synth` time and "
                    f"nothing else in this suite can see it: ApiStack bundles ../api through "
                    f"Docker, so no test can construct it."
                )
    assert checked > 20, (
        f"only {checked} keywords were checked, so this test is not reaching the stack "
        f"constructions in app.py -- the registry or the walk is wrong, not the code"
    )
