"""IAM grants match the call graph, and the read routes stay read-only.

`ast` over `api/src/handlers/*.py`, resolving helper calls transitively -- within a
module and across the handler package -- so `configurations.create` is credited with
the `sqs:SendMessage` it reaches through `provisioner.enqueue` and `cards.move` with
the `dynamodb:PutItem` it reaches through `runs.queue_run`.

Two halves:

- the **call graph**: every AWS call an entry point can reach appears in `_GRANTS`
  below. The set is deliberately a declaration rather than a derivation -- when it
  fails, the fix is a grant in `infra/stacks/api_stack.py` (phase 7) *and* a line here.
- the **policy** the provisioner writes for each configuration's execution role, which
  is the one policy in this system that is built at run time and so cannot be reviewed
  in a CDK diff. Six properties of that half are pinned here, so a reader who wants to
  know what a compromised container reaches has one file to read: no `iam:*`, no AgentCore
  action but the six that read and add to its own memory (nothing naming `AgentRuntime`,
  `Invoke` or `Session`), Configurations is `GetItem` only, `dynamodb:LeadingKeys` fences
  **both** table statements, the image pull is scoped to the one ECR repository
  `AGENT_IMAGE_URI` names, and the permissions boundary is attached to every role the
  provisioner mints.

The last tests in the file cross back into `infra/`, deliberately: they check that the
boundary in `stacks/agent_stack.py` permits every action this policy grants. That
agreement is what actually failed in production, and neither half alone shows it. It is now
checked in **both** directions -- the boundary permits nothing the inline policy does not
grant either -- against the classification in `tests/execution_boundary.py`, which is where
a deliberately wider ceiling is declared with a reason rather than left as prose.
"""

from __future__ import annotations

import ast
import fnmatch
import json
from typing import Any, NamedTuple

import execution_boundary
import pytest
from conftest import REPO_ROOT
from stacks import agent_stack
from test_api_fakes import load_handlers as _load

HANDLERS = REPO_ROOT / "api/src/handlers"
_MODULES = (
    "_common",
    "observability",
    "runs",
    "configurations",
    "cards",
    "tasks",
    "provisioner",
    "dispatcher",
    "watchdog",
)

# The names `_common` exposes that reach AWS. `agentcore_control`/`agentcore_data` are
# accessors, so they appear as a call before the operation: `_common.agentcore_data()
# .invoke_agent_runtime(...)`.
_CLIENTS = frozenset(
    {
        "config_table",
        "tasks_table",
        "cards_table",
        "dynamodb",
        "sqs",
        "secrets",
        "iam",
        "agentcore_control",
        "agentcore_data",
    }
)

# Everything a read route may do. `scan` is here because `configurations.list_` has no
# partition key to query on; note 30 is about *reading* rather than about `query`.
_READ_ONLY_METHODS = frozenset({"get_item", "batch_get_item", "query", "scan"})

# The declared grant table. A subset check, not equality: an entry point that reaches
# something absent here needs a grant nobody has written yet.
_GRANTS: dict[tuple[str, str], set[str]] = {
    ("configurations", "create"): {
        "config_table.query",
        "config_table.put_item",
        # `_retire_failed`: a stale `FAILED` configuration is marked `DELETING` so
        # `provisioner._delete` can take its role and its secret. Already covered by the
        # `config:write` `create` holds in `infra/stacks/api_stack.py`.
        "config_table.update_item",
        "secrets.create_secret",
        "secrets.delete_secret",
        "sqs.send_message",
    },
    ("configurations", "list_"): {"config_table.scan"},
    ("configurations", "get"): {"config_table.get_item"},
    ("configurations", "patch"): {"config_table.get_item", "config_table.update_item"},
    ("configurations", "delete"): {
        "config_table.get_item",
        "config_table.update_item",
        "sqs.send_message",
    },
    ("configurations", "runtime_update"): {"config_table.get_item", "sqs.send_message"},
    # The two credential routes. Both read the configuration and then reach the token vault,
    # and neither writes the item: the vault is the only authority on whether a repository is
    # authorised, so a stored flag would be a second and staler answer. `github_status`
    # additionally reaches GitHub through `github_probe`, which is not a boto3 client and so
    # is not in this vocabulary.
    ("configurations", "github_authorize"): {
        "config_table.get_item",
        "agentcore_data.get_workload_access_token_for_user_id",
        "agentcore_data.get_resource_oauth2_token",
    },
    ("configurations", "github_status"): {
        "config_table.get_item",
        "agentcore_data.get_workload_access_token_for_user_id",
        "agentcore_data.get_resource_oauth2_token",
    },
    ("configurations", "memory"): {
        "config_table.get_item",
        # The two read-only data-plane operations, granted by `memory:read` in
        # infra/stacks/api_stack.py. Nothing that writes or deletes a record.
        "agentcore_data.retrieve_memory_records",
        "agentcore_data.list_memory_records",
    },
    ("cards", "create"): {"config_table.get_item", "cards_table.put_item"},
    ("cards", "list_"): {"cards_table.query", "tasks_table.get_item", "tasks_table.query"},
    ("cards", "get"): {"cards_table.get_item", "tasks_table.get_item"},
    # `patch` re-renders the derived view, which reads the linked run.
    ("cards", "patch"): {"cards_table.get_item", "cards_table.update_item", "tasks_table.get_item"},
    ("cards", "delete"): {
        "cards_table.get_item",
        "tasks_table.get_item",
        "cards_table.delete_item",
    },
    ("cards", "move"): {
        "cards_table.get_item",
        "cards_table.update_item",
        "config_table.get_item",
        "tasks_table.get_item",
        "tasks_table.put_item",
        "sqs.send_message",
    },
    # Every run has a card: `create_claimed` puts it, `store_task_id`/`release` update it.
    ("tasks", "create"): {
        "config_table.get_item",
        "tasks_table.put_item",
        "sqs.send_message",
        "cards_table.put_item",
        "cards_table.update_item",
    },
    ("tasks", "list_"): {"tasks_table.query"},
    ("tasks", "get"): {"tasks_table.get_item", "config_table.get_item"},
    ("tasks", "activity"): {"tasks_table.get_item"},
    # `cancel` writes the mark and then reuses the sweep's own stop path
    # (`watchdog.stop_session`), which is where the `stop_runtime_session` and the
    # `stoppedAt` update it is credited with come from.
    ("tasks", "cancel"): {
        "tasks_table.get_item",
        "tasks_table.update_item",
        "agentcore_data.stop_runtime_session",
    },
    ("provisioner", "lambda_handler"): {
        "config_table.get_item",
        "config_table.update_item",
        "config_table.delete_item",
        "sqs.send_message",
        "secrets.delete_secret",
        "iam.create_role",
        "iam.put_role_policy",
        "iam.delete_role_policy",
        "iam.delete_role",
        "iam.tag_role",
        "agentcore_control.create_agent_runtime",
        "agentcore_control.get_agent_runtime",
        "agentcore_control.update_agent_runtime",
        "agentcore_control.delete_agent_runtime",
        # One AgentCore Memory per registered repository, minted by `_create` and torn down
        # by `_delete`.
        "agentcore_control.create_memory",
        "agentcore_control.delete_memory",
    },
    ("dispatcher", "lambda_handler"): {
        "tasks_table.get_item",
        "tasks_table.update_item",
        "config_table.get_item",
        "agentcore_data.invoke_agent_runtime",
    },
    ("dispatcher", "dlq_handler"): {"tasks_table.update_item"},
    # `config_table.scan` and `config_table.update_item` are the sweep's configuration half:
    # it lists the configurations whose provisioning chain has not finished, records a nudge
    # on one it re-drives, and resolves one that stayed stalled. `sqs.send_message` covers
    # both queues it writes to -- the task queue for a resume and the provision queue for a
    # nudge -- which is why `_ENTRY_POINT_GRANTS` names the two queues separately and this
    # table cannot.
    ("watchdog", "lambda_handler"): {
        "tasks_table.scan",
        "tasks_table.get_item",
        "tasks_table.update_item",
        "config_table.get_item",
        "config_table.scan",
        "config_table.update_item",
        "sqs.send_message",
        "agentcore_data.stop_runtime_session",
    },
}

_READ_ROUTES = (
    ("configurations", "get"),
    ("configurations", "list_"),
    ("cards", "get"),
    ("cards", "list_"),
    ("tasks", "get"),
    ("tasks", "list_"),
    ("tasks", "activity"),
)


def _trees() -> dict[str, ast.Module]:
    return {module: ast.parse((HANDLERS / f"{module}.py").read_text()) for module in _MODULES}


def _aws_call(node: ast.Call) -> str | None:
    """`_common.tasks_table.get_item(...)` -> `tasks_table.get_item`;
    `_common.agentcore_data().invoke_agent_runtime(...)` -> `agentcore_data.invoke_agent_runtime`."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    owner = func.value
    if isinstance(owner, ast.Call):  # the lru_cached accessors
        owner = owner.func
    if isinstance(owner, ast.Attribute) and owner.attr in _CLIENTS:
        return f"{owner.attr}.{func.attr}"
    if isinstance(owner, ast.Name) and owner.id in _CLIENTS:
        # A bare `tasks_table.get_item(...)` means someone aliased the handle out of
        # `_common`, which breaks the reload contract as well as this test.
        return f"{owner.id}.{func.attr}"
    return None


def _graph() -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str], set[tuple[str, str]]]]:
    """`(direct AWS calls, callees)`, both keyed by `(module, function)`."""
    trees = _trees()
    functions = {
        (module, node.name): node
        for module, tree in trees.items()
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    direct: dict[tuple[str, str], set[str]] = {key: set() for key in functions}
    callees: dict[tuple[str, str], set[tuple[str, str]]] = {key: set() for key in functions}
    for (module, name), node in functions.items():
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            reached = _aws_call(call)
            if reached:
                direct[(module, name)].add(reached)
            func = call.func
            if isinstance(func, ast.Name) and (module, func.id) in functions:
                callees[(module, name)].add((module, func.id))
            elif (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and (func.value.id, func.attr) in functions
            ):
                callees[(module, name)].add((func.value.id, func.attr))
    return direct, callees


def _reachable(entry: tuple[str, str]) -> set[str]:
    direct, callees = _graph()
    seen: set[tuple[str, str]] = set()
    stack = [entry]
    reached: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        reached |= direct.get(current, set())
        stack.extend(callees.get(current, ()))
    return reached


@pytest.mark.parametrize("entry", sorted(_GRANTS), ids=lambda entry: f"{entry[0]}.{entry[1]}")
def test_every_aws_call_an_entry_point_reaches_is_a_declared_grant(entry: tuple[str, str]) -> None:
    undeclared = _reachable(entry) - _GRANTS[entry]
    assert not undeclared, (
        f"{entry[0]}.{entry[1]} reaches {sorted(undeclared)}, which no grant covers. Add the "
        f"matching grant to the Lambda's role in infra/stacks/api_stack.py (phase 7) and the "
        f"call to _GRANTS here -- an entry point that reaches an ungranted API fails at run "
        f"time with an AccessDenied that reads like a service problem."
    )


@pytest.mark.parametrize("entry", _READ_ROUTES, ids=lambda entry: f"{entry[0]}.{entry[1]}")
def test_read_routes_touch_only_get_item_and_query(entry: tuple[str, str]) -> None:
    """Note 30: a read handler never gains a `logs:*` or `bedrock-agentcore:*`
    permission to look something up live. Log locations are *derived* from stored
    attributes, so a naming-scheme change is a code change rather than a data
    migration, and a compromised read route cannot enumerate the fleet.

    The rule is narrowed for exactly one route, `configurations.memory`, which is deliberately
    absent from `_READ_ROUTES` above and bounded instead by
    `test_the_memory_route_reaches_two_read_only_agentcore_operations_and_nothing_else` below
    -- so the exemption is written down rather than implied by an absence from a tuple."""
    for reached in _reachable(entry):
        target, method = reached.split(".", 1)
        assert target in ("config_table", "tasks_table", "cards_table", "dynamodb"), (
            f"{entry[0]}.{entry[1]} reaches {reached}; a read route talks to DynamoDB and "
            f"nothing else"
        )
        assert method in _READ_ONLY_METHODS, f"{entry[0]}.{entry[1]} writes: {reached}"


_MEMORY_ROUTE = ("configurations", "memory")
_MEMORY_ROUTE_CALLS = frozenset(
    {
        "config_table.get_item",
        "agentcore_data.retrieve_memory_records",
        "agentcore_data.list_memory_records",
    }
)


def test_the_memory_route_reaches_two_read_only_agentcore_operations_and_nothing_else() -> None:
    """The one narrowing of note 30 in this repository, and what still bounds it.

    Note 30 -- "a read handler never gains a `logs:*` or `bedrock-agentcore:*` permission to
    look something up live" -- exists so that a compromised read route cannot enumerate the
    fleet, and `test_read_routes_touch_only_get_item_and_query` above holds the other seven
    read routes to DynamoDB alone. `GET /configurations/{configId}/memory` is the exception,
    because the thing an operator is asking about is not stored in DynamoDB at all: the
    memory data plane is the only place a repository's records exist, so a route over stored
    attributes could only ever answer with the `memoryId` the console already prints.

    What still bounds it is asserted here as an EQUALITY, not a subset: one `GetItem` on
    Configurations and the two read-only memory operations. No `create_event` (writing a
    repository's memory belongs to the container, under
    `provisioner._execution_role_policy`'s `UseOwnMemory`), no `delete_*`, no
    `list_memories`, and no table write -- and `infra/stacks/api_stack.py`'s `memory:read`
    arm grants exactly the two actions named here, on `memory/*`.
    """
    assert _reachable(_MEMORY_ROUTE) == set(_MEMORY_ROUTE_CALLS)
    assert _MEMORY_ROUTE not in _READ_ROUTES, (
        "the DynamoDB-only rule above is deliberately left intact; this route is the "
        "declared exception and this test is its fence"
    )
    for forbidden in (
        "create_event",
        "delete_event",
        "delete_memory_record",
        "batch_delete_memory_records",
        "create_memory",
        "update_memory",
        "delete_memory",
        "list_memories",
        "get_memory_record",
        "put_item",
        "update_item",
        "delete_item",
    ):
        assert not any(
            reached.endswith(f".{forbidden}") for reached in _reachable(_MEMORY_ROUTE)
        ), f"configurations.memory reaches {forbidden}"


def test_observability_imports_no_boto3() -> None:
    tree = ast.parse((HANDLERS / "observability.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "boto3" not in imported and "botocore" not in imported, (
        "observability.py is pure derivation. A boto3 import here is the first step "
        "towards a read route that looks a log location up live."
    )
    assert not [call for call in ast.walk(tree) if isinstance(call, ast.Call) and _aws_call(call)]


# --- the policy half: the per-configuration execution role -----------------------

_CONFIG = {
    "configId": "cfg_0123456789ab",
    "repository": "owner/repo",
    # The one AgentCore Memory this repository owns, spelled the way the service does:
    # `<name>-<10 service-generated characters>`, which is botocore's own `memoryId` pattern.
    # This fixture is what closes the boundary's second set difference -- without a
    # `memoryId` the inline policy carries no `UseOwnMemory` statement, and
    # `test_the_boundary_permits_no_action_the_inline_policy_does_not_grant` reports the six
    # actions `MayUseOwnMemory` allows as dead surface.
    "memoryId": "rca_mem_cfg_0123456789ab-AbCdEfGhIj",
}

# What `ApiStack` forwards as `AGENT_IMAGE_URI`: the CDK assets repository, whose name
# carries the account and region a second time and whose tag is the asset hash.
_FLEET_IMAGE_URI = (
    "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/"
    "cdk-hnb659fds-container-assets-123456789012-ap-southeast-1:abc123"
)
_ASSET_REPOSITORY_ARN = (
    "arn:aws:ecr:ap-southeast-1:123456789012:repository/"
    "cdk-hnb659fds-container-assets-123456789012-ap-southeast-1"
)


@pytest.fixture
def policy(aws: Any) -> dict[str, Any]:
    _common, provisioner = _load("provisioner")
    return provisioner._execution_role_policy(_CONFIG)


@pytest.fixture
def fleet_policy(aws: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`policy`, plus the fleet's image URI. Set before `_load`, because the module binds
    `AGENT_IMAGE_URI` at import -- which is also how a real deploy changes it."""
    monkeypatch.setenv("AGENT_IMAGE_URI", _FLEET_IMAGE_URI)
    _common, provisioner = _load("provisioner")
    return provisioner._execution_role_policy(_CONFIG)


class _Agreement(NamedTuple):
    """The inline policy and the boundary it is the grant half of."""

    policy: dict[str, Any]
    boundary: list[dict[str, Any]]


@pytest.fixture
def agreement(aws: Any, monkeypatch: pytest.MonkeyPatch) -> _Agreement:
    """`fleet_policy` and the matching boundary, built from one image URI."""
    # Set before `_load`, because the module binds its environment at import -- which is also
    # how a real deploy changes it.
    monkeypatch.setenv("AGENT_IMAGE_URI", _FLEET_IMAGE_URI)
    _common, provisioner = _load("provisioner")
    return _Agreement(
        policy=provisioner._execution_role_policy(_CONFIG),
        boundary=execution_boundary.build(
            agent_stack,
            # Derived from the same URI the grant is derived from, so the two halves name one
            # repository from one source.
            provisioner._asset_repository_arn(_FLEET_IMAGE_URI),
        ),
    )


def _statements(policy: dict[str, Any], sid: str) -> list[dict[str, Any]]:
    return [statement for statement in policy["Statement"] if statement.get("Sid") == sid]


def _actions(statement: dict[str, Any]) -> list[str]:
    action = statement["Action"]
    return action if isinstance(action, list) else [action]


def _resources(statement: dict[str, Any]) -> list[str]:
    """IAM accepts `Resource` as a string or a list and means the same thing by both, so
    the assertions below read it through here rather than pinning one spelling."""
    resource = statement["Resource"]
    return resource if isinstance(resource, list) else [resource]


def test_the_execution_role_can_neither_mint_a_role_nor_dispatch_a_runtime(
    policy: dict[str, Any],
) -> None:
    """The blanket "no `bedrock-agentcore:` anywhere" this test used to assert is false by
    design now that `UseOwnMemory` grants six data-plane actions on the repository's own
    memory. What that substring was really protecting is stated here as itself: the role may
    not mint an IAM role, may not reach S3, and may not dispatch a run -- no `*AgentRuntime`,
    no `Invoke*`, no session action -- because a container that can call the AgentCore
    control plane can invoke its own runtime and turn one compromised task into an unbounded
    fan-out."""
    text = json.dumps(policy)
    for forbidden in ("iam:", "s3:"):
        assert forbidden not in text, (
            f"the per-configuration execution role must carry no {forbidden}* permission: the "
            f"permissions boundary is what stops the provisioner minting an admin role, and a "
            f"container that can reach S3 reads every deploy asset in the account"
        )

    agentcore = {
        action
        for statement in policy["Statement"]
        for action in _actions(statement)
        if action.startswith("bedrock-agentcore:")
    }
    assert agentcore == {
        # The six `UseOwnMemory` data-plane actions, on this repository's own memory's literal
        # ARN.
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:GetEvent",
        "bedrock-agentcore:RetrieveMemoryRecords",
        "bedrock-agentcore:ListMemoryRecords",
        "bedrock-agentcore:GetMemoryRecord",
        # Plus the two `UseGitHubCredential` actions, which are the container's whole
        # credential path and cannot be resource-fenced -- see
        # `test_the_github_credential_statement_is_as_narrow_as_the_service_permits`. Neither
        # reaches the Identity *control* plane: creating, updating or deleting a credential
        # provider or a workload identity is absent here and denied by name in the boundary's
        # `MayNotEscalate`, because a container that could reconfigure the provider every
        # repository shares could point it at an OAuth app of its own choosing.
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:GetResourceOauth2Token",
    }, (
        f"this role may carry the six `UseOwnMemory` data-plane actions and the two "
        f"`UseGitHubCredential` ones, and nothing else on AgentCore; got {sorted(agentcore)}"
    )
    for action in agentcore:
        for reachable in ("AgentRuntime", "Invoke", "Session"):
            assert reachable not in action, (
                f"{action} lets a container reach the runtime dispatch path; the fan-out fence "
                f"is that no granted AgentCore action names {reachable}"
            )


def test_the_memory_grant_names_one_memory_and_cannot_destroy_it(policy: dict[str, Any]) -> None:
    """Invariant 31's division of labour, for the one AgentCore Memory a repository owns:
    the boundary's `MayUseOwnMemory` is minted once per deploy, before any memory exists, so
    it can only say `memory/*` and the **literal** ARN in this statement is the whole of
    what stops one repository's container reading another repository's records.

    The seven absent actions are the other half. A prompt-injected session must be able to
    add to and read this repository's shared memory, and never to destroy it or to enumerate
    anybody else's -- so `DeleteEvent`, `DeleteMemoryRecord`, `BatchDeleteMemoryRecords`,
    `CreateMemory`, `UpdateMemory`, `DeleteMemory` and `ListMemories` are asserted absent by
    name rather than left to the six-action equality above, because the equality reads as an
    accident of the current list and this reads as the decision it is.
    """
    (statement,) = _statements(policy, "UseOwnMemory")
    assert _resources(statement) == [
        "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:memory/"
        "rca_mem_cfg_0123456789ab-AbCdEfGhIj"
    ], (
        f"one memory, named literally and derived from the configuration's own `memoryId` -- "
        f"a `memory/*` here would hand every container the whole fleet's memory, since the "
        f"boundary already permits that much. Got {_resources(statement)}"
    )
    assert set(_actions(statement)) == {
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:GetEvent",
        "bedrock-agentcore:RetrieveMemoryRecords",
        "bedrock-agentcore:ListMemoryRecords",
        "bedrock-agentcore:GetMemoryRecord",
    }, f"exactly the six data-plane actions, got {sorted(_actions(statement))}"

    text = json.dumps(policy)
    for absent in (
        "bedrock-agentcore:DeleteEvent",
        "bedrock-agentcore:DeleteMemoryRecord",
        "bedrock-agentcore:BatchDeleteMemoryRecords",
        "bedrock-agentcore:CreateMemory",
        "bedrock-agentcore:UpdateMemory",
        "bedrock-agentcore:DeleteMemory",
        "bedrock-agentcore:ListMemories",
    ):
        assert absent not in text, (
            f"{absent} is deliberately absent: a prompt-injected session that can delete "
            f"events, delete records or drop the memory itself destroys what every later task "
            f"in this repository was going to read, and `ListMemories` is how it finds another "
            f"repository's memory to try it on. The matching Deny is "
            f"`agent_stack.execution_role_boundary_statements`' `MayNotEscalate`, which names "
            f"all seven so that adding one here fails a test instead of landing"
        )


def test_both_table_statements_are_fenced_by_leading_keys(policy: dict[str, Any]) -> None:
    """Invariant 31 / Tier 1 #7-#8: without `LeadingKeys` a compromised container reads
    -- and on Tasks writes -- every other repository's rows, and editing another
    config's `verifyCommand` is remote code execution in that repository's container."""
    table_statements = [
        statement
        for statement in policy["Statement"]
        if any(action.startswith("dynamodb:") for action in _actions(statement))
    ]
    assert len(table_statements) == 2, "one statement for Configurations, one for Tasks"
    for statement in table_statements:
        condition = statement["Condition"]["ForAllValues:StringEquals"]
        assert condition["dynamodb:LeadingKeys"] == ["cfg_0123456789ab"]


def test_configurations_access_is_get_item_only(policy: dict[str, Any]) -> None:
    (statement,) = _statements(policy, "ReadOwnConfiguration")
    assert _actions(statement) == ["dynamodb:GetItem"], (
        "the container writes no configuration item (Tier 1 #7)"
    )


def test_no_statement_reaches_secrets_manager_at_all(policy: dict[str, Any]) -> None:
    """There is no secret, so there is no `secretsmanager:` anywhere in a container's role.

    This replaced a `ReadOwnSecret` statement naming one literal secret ARN, and the swap is
    not a tightening -- see `test_the_github_credential_statement_is_as_narrow_as_the_service_permits`
    below, which states what was given up. What is asserted here is only the half that did
    improve: the container can no longer reach Secrets Manager under any name.
    """
    assert "secretsmanager:" not in json.dumps(policy)


def test_the_github_credential_statement_is_as_narrow_as_the_service_permits(
    policy: dict[str, Any],
) -> None:
    """Two actions, and **neither resource is a fence** -- which is the point of the test.

    Measured against a throwaway role rather than assumed:
    `GetWorkloadAccessTokenForUserId` is denied on an individual `workload-identity/<name>`
    ARN and on `workload-identity-directory/default*`, and allowed only on
    `workload-identity-directory/*`; `GetResourceOauth2Token` is denied on the credential
    provider's own ARN, on `token-vault/default/*` and on `token-vault/*`, and allowed only on
    `*`. There is no condition key for the 3LO user id.

    So this statement cannot name one repository's credential the way `UseOwnMemory` names one
    memory, and the test asserts the `*` deliberately: a future edit that *narrows* it will
    fail here and send the reader to this docstring rather than shipping a role that cannot
    fetch a token. What bounds the exposure instead is that a container can read exactly one
    configuration item (invariant 31) and therefore knows exactly one random `configId`, plus
    the GitHub App's own installation list -- stated in CLAUDE.md's "What remains exposed".
    """
    (statement,) = _statements(policy, "UseGitHubCredential")
    assert _actions(statement) == [
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:GetResourceOauth2Token",
    ]
    assert _resources(statement) == [
        "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:workload-identity-directory/*",
        "*",
    ]


def test_log_creation_is_scoped_to_the_runtime_log_groups(policy: dict[str, Any]) -> None:
    (statement,) = _statements(policy, "WriteRuntimeLogs")
    assert "logs:CreateLogGroup" in _actions(statement)
    for resource in _resources(statement):
        assert "/aws/bedrock-agentcore/runtimes" in resource, (
            "Tier 1 #9: `logs:CreateLogGroup` on `arn:aws:logs:*:*:*` lets a container create "
            "a log group anywhere in the account"
        )


def test_the_bedrock_statement_is_not_region_scoped(policy: dict[str, Any]) -> None:
    """Invariant 21: a `global.` inference profile routes the call outside the calling
    region, so a region-scoped model ARN denies it with a message about the model."""
    (statement,) = _statements(policy, "InvokeClaude")
    assert set(_actions(statement)) == {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
    }
    resources = _resources(statement)
    assert any(
        resource.startswith("arn:aws:bedrock:*::foundation-model/") for resource in resources
    )
    assert any("inference-profile" in resource for resource in resources)
    for resource in resources:
        assert "ap-southeast-1" not in resource


def test_the_image_pull_grant_names_the_repository_the_image_lives_in(
    fleet_policy: dict[str, Any],
) -> None:
    """The *runtime* pulls `AGENT_IMAGE_URI` with this role -- not this Lambda, and before
    any container code runs -- and `CreateAgentRuntime` pre-validates that it can. Without
    these two statements the create fails `AccessDenied` with a **null** `errorMessage` in
    CloudTrail, `CreateWorkloadIdentity` is never reached and no runtime is ever created,
    which is exactly how the omission survived: the failure names nothing."""
    (pull,) = _statements(fleet_policy, "PullAgentImage")
    assert set(_actions(pull)) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }, "the pull actions the one working execution role in this account carries, and no more"
    assert _resources(pull) == [_ASSET_REPOSITORY_ARN], (
        f"the repository `AGENT_IMAGE_URI` names, derived from it rather than guessed or "
        f"widened to `ecr:*`, got {_resources(pull)}"
    )

    (authenticate,) = _statements(fleet_policy, "AuthenticateToEcr")
    assert _actions(authenticate) == ["ecr:GetAuthorizationToken"]
    assert _resources(authenticate) == ["*"], (
        "`ecr:GetAuthorizationToken` supports no resource types, so a scoped ARN there matches "
        "no authorization at all; the token it returns still authorizes against PullAgentImage, "
        "which names one repository"
    )


def test_a_policy_built_with_no_fleet_image_omits_the_ecr_statements(
    aws: Any, policy: dict[str, Any]
) -> None:
    """No `AGENT_IMAGE_URI` here, as in every other fixture in this file, so the derivation
    has to degrade rather than raise: a raise inside `_execution_role_policy` would break
    `CREATE`, `UPDATE` and the re-provision path at once, while an empty ARN only returns
    the grant this file pinned before the image pull was on the list."""
    assert "ecr:" not in json.dumps(policy)

    _common, provisioner = _load("provisioner")
    assert provisioner._asset_repository_arn("") == ""
    # Not an ECR host: a Docker Hub image must yield no ARN rather than a nonsense one.
    assert provisioner._asset_repository_arn("docker.io/library/python:3.13") == ""
    assert provisioner._asset_repository_arn(_FLEET_IMAGE_URI) == _ASSET_REPOSITORY_ARN


def test_a_policy_for_a_configuration_with_no_memory_omits_the_memory_statement(
    aws: Any,
) -> None:
    """The mirror of `test_a_policy_built_with_no_fleet_image_omits_the_ecr_statements`, and
    deliverable D11's control-plane half: `_ensure_memory` swallows its own `ClientError` and
    returns `""`, so a configuration whose `CreateMemory` failed still gets its runtime and
    still reaches `READY` -- and what an operator has then must be a working repository that
    simply has no memory, not a role carrying an Allow on `memory/` with nothing after the
    slash. No `UseOwnMemory` Sid and no `bedrock-agentcore:` anywhere in the document, the
    same standard the ECR and CloudWatch mirrors hold their own degraded states to.

    Both spellings of "no memory" are built: the empty string is what the stamp writes when
    the create failed, and the missing key is what every configuration provisioned before
    this change carries (`_update` does not mint one, so they stay that way until they are
    re-registered).
    """
    _common, provisioner = _load("provisioner")
    without_key = {key: value for key, value in _CONFIG.items() if key != "memoryId"}
    for config in ({**_CONFIG, "memoryId": ""}, without_key):
        document = provisioner._execution_role_policy(config)
        assert not _statements(document, "UseOwnMemory"), (
            "a configuration with no memory gets no memory statement -- not an Allow with an "
            "empty resource, and not one with an empty action list"
        )
        assert "bedrock-agentcore:CreateEvent" not in json.dumps(document), (
            "with no `memoryId` the per-configuration execution role must carry no memory "
            "permission at all: an ARN ending at `memory/` matches nothing an operator "
            "intended, and this is the state a swallowed `CreateMemory` failure produces"
        )


def test_no_handler_names_a_cloudwatch_action_at_all() -> None:
    """The mechanical form of "nothing under `api/src/handlers/` grants itself CloudWatch".

    This was once "no handler *but the provisioner*", because the provisioner minted a
    `cloudwatch:PutMetricData` statement onto the container's execution role for Coding Agent
    Insights. That feature is gone -- see `CLAUDE.md`'s note on why -- so the exception went
    with it and the property is now the stronger one: no module here names a CloudWatch action
    in any position, neither for its own role nor in a policy it writes for somebody else's.

    Worth a test rather than a sentence because it is a security property, and the cheapest way
    to break it is a helpful `put_metric_data` call in a handler that already has an IAM
    statement nearby to extend. Reintroducing an exporter means reintroducing this grant, the
    boundary widening that ceilings it, and an egress destination -- so it should cost a failing
    test and a deliberate edit, not a line.
    """
    naming: dict[str, list[str]] = {}
    for module in _MODULES:
        tree = ast.parse((HANDLERS / f"{module}.py").read_text(encoding="utf-8"))
        found = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "cloudwatch:" in node.value
        ]
        if found:
            naming[module] = found

    assert naming == {}, (
        f"{sorted(naming)} name a cloudwatch: action ({naming}). No handler under api/ reads or "
        f"writes a metric, and none mints a policy that lets the container do so either -- the "
        f"execution role's four reachable things are this repository's secret, this "
        f"configuration's task items, Bedrock and a read-only ECR pull. A grant here makes it "
        f"five and widens the permissions boundary to match"
    )


def test_the_boundary_is_attached_to_every_minted_role(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlap with
    `test_api_provisioner.py::test_the_role_is_created_under_one_path_behind_a_boundary`
    is deliberate: that test also asserts the path and the trust policy, this one is here
    so all five properties of the run-time policy read in one file."""
    boundary = "arn:aws:iam::123456789012:policy/rca-execution-boundary"
    # Set before `_load`, because the module binds the environment at import -- which is
    # also how a real deploy changes it.
    monkeypatch.setenv("EXECUTION_ROLE_BOUNDARY_ARN", boundary)
    _common, provisioner = _load("provisioner")
    config_id = "cfg_0123456789ab"

    kwargs = provisioner._role_kwargs(
        config_id, {"configId": config_id, "repository": "owner/repo"}
    )

    assert kwargs["PermissionsBoundary"] == boundary, (
        "the tests above describe the *inline* policy, and an inline policy is not a ceiling: "
        "without the boundary the provisioner's own `iam:CreateRole` grant is enough to mint a "
        "role wider than the one it was asked for. Invariant 17 -- and "
        "`agent_stack.execution_role_boundary_statements`'s `MayNotEscalate` deny is what makes "
        "attaching one mean something"
    )


def test_creating_a_runtime_also_grants_the_endpoint_actions() -> None:
    """`agentcore:control` grants the four `*AgentRuntimeEndpoint` actions as well as the
    four `*AgentRuntime` ones, even though no code in this repository calls them.

    `CreateAgentRuntime` creates the DEFAULT endpoint for you (invariant 14 is why we never
    create a custom one) and authorizes that as a *separate* action. Without the grant the
    first provision of a fresh deployment fails with `not authorized to perform:
    bedrock-agentcore:CreateAgentRuntimeEndpoint ... because no identity-based policy
    allows` -- and it failed for 26 minutes before saying so, because the message contains
    "not authorized" and `provisioner._is_role_propagation_error` read that as IAM
    propagation. `ApiStack` bundles Docker so no test synthesizes it; this reads the source,
    in the idiom `tests/test_plugin_manifest.py` uses for the Makefile.
    """
    source = (REPO_ROOT / "infra/stacks/api_stack.py").read_text(encoding="utf-8")
    for verb in ("Create", "Get", "Update", "Delete"):
        assert f"bedrock-agentcore:{verb}AgentRuntime" in source, (
            f"the agentcore:control grant no longer names {verb}AgentRuntime"
        )
        assert "AgentRuntimeEndpoint" in source, (
            "the agentcore:control grant no longer names the *AgentRuntimeEndpoint actions. "
            "CreateAgentRuntime authorizes endpoint creation separately, so dropping these "
            "breaks the first provision of every new deployment -- and breaks it as a "
            "26-minute fake IAM-propagation loop, not as a clear denial."
        )
    assert "WorkloadIdentity" in source, (
        "the agentcore:control grant no longer names the *WorkloadIdentity actions. "
        "CreateAgentRuntime creates a workload identity under the covers and authorizes it "
        "as its own action, so without these the first provision of every new deployment "
        "fails AccessDenied with a null message in CloudTrail."
    )
    assert 'resources=["*"]' in source, (
        "the agentcore:control grant is resource-scoped again. It cannot be: "
        "CreateAgentRuntime is authorized before the runtime exists, so there is no ARN for "
        "a `runtime/*` pattern to match, and CreateWorkloadIdentity acts on a different "
        "resource type entirely. simulate-principal-policy returns `implicitDeny` for "
        "CreateAgentRuntime and `allowed` for CreateAgentRuntimeEndpoint from the same "
        "statement. The fence is the action list plus the iam:PassRole condition."
    )


def test_the_provisioner_may_create_agentcores_own_service_linked_roles() -> None:
    """A `networkMode: VPC` runtime (invariant 30) needs a service-linked role AgentCore
    creates for itself during `CreateAgentRuntime`.

    Without `iam:CreateServiceLinkedRole` every `CreateAgentRuntime` in a live eu-west-1
    deployment failed `Failed creating service linked role. Please verify that the calling
    role has sufficient permissions to create a service linked role.` -- and because that
    message contains the word "role", `provisioner._is_role_propagation_error` read it as
    IAM propagation and retried it ten times over 26 minutes before reporting that the
    execution role "was still not assumable", a problem the execution role never had. The
    only provisioner in this account that creates runtimes successfully uses
    `networkMode: PUBLIC`, which never needs this role.
    """
    source = (REPO_ROOT / "infra/stacks/api_stack.py").read_text(encoding="utf-8")
    assert "iam:CreateServiceLinkedRole" in source, (
        "the provisioner's grant no longer carries iam:CreateServiceLinkedRole, so every "
        "VPC-mode CreateAgentRuntime fails `Failed creating service linked role` -- and fails "
        "as a 26-minute fake IAM-propagation loop reporting an execution-role problem that "
        "does not exist, not as a clear denial"
    )
    assert "iam:AWSServiceName" in source, (
        "iam:CreateServiceLinkedRole must stay fenced to AgentCore's own service principals "
        "by an iam:AWSServiceName condition; "
        "tests/test_grant_contract.py::"
        "test_the_service_linked_role_grant_is_fenced_to_agentcore_by_a_wildcard_match "
        "checks the operator and the patterns at statement level"
    )


def test_the_boundary_permits_every_action_the_inline_policy_grants(
    agreement: _Agreement,
) -> None:
    """Their agreement is the property that actually failed here: the boundary denied
    `ecr:*` while the inline policy granted no `ecr:` action at all, and either half reads
    as correct on its own. The effective permission of a role under a boundary is
    `identity ∩ boundary`, with an explicit Deny in either document winning outright, so:

    * check 1 catches a ceiling one action too narrow -- omitted from the boundary is
      **silently denied at runtime**, with nothing naming the missing permission;
    * check 2 catches an Allow shadowed by a Deny in the same document, which is what an
      `ecr:*` entry in `MayNotEscalate` would be now that the pull is granted above it.

    `infra` is on `pytest.ini`'s `pythonpath`, so this file can read the boundary; it lives
    here rather than in `tests/test_grant_contract.py` because building the inline policy
    at all needs the `aws` fixture and `test_api_fakes.load_handlers`.

    """
    boundary = agreement.boundary
    allows = [statement for statement in boundary if statement["Effect"] == "Allow"]
    denies = [statement for statement in boundary if statement["Effect"] == "Deny"]

    for statement in agreement.policy["Statement"]:
        for action in _actions(statement):
            # `fnmatchcase`, because the boundary spells wildcards (`iam:*`) and IAM matches
            # them case-sensitively.
            assert any(
                fnmatch.fnmatchcase(action, pattern)
                for allow in allows
                for pattern in _actions(allow)
            ), (
                f"{statement['Sid']} grants {action}, which no Allow in the boundary permits. "
                f"A boundary is a whitelist, so this is denied at run time with nothing naming "
                f"the missing action -- add it to "
                f"agent_stack.execution_role_boundary_statements"
            )
            for deny in denies:
                for pattern in _actions(deny):
                    assert not fnmatch.fnmatchcase(action, pattern), (
                        f"{statement['Sid']} grants {action}, which the boundary's "
                        f"{deny['Sid']} denies as {pattern}. An explicit Deny beats every "
                        f"Allow, in either document, so the grant is dead letter"
                    )

    (pull,) = _statements(agreement.policy, "PullAgentImage")
    (may_pull,) = [
        statement for statement in boundary if statement.get("Sid") == "MayPullAgentImage"
    ]
    assert _resources(may_pull) == _resources(pull), (
        "the ceiling and the grant must name the same repository: the boundary gets it as "
        "`self.image.repository.repository_arn` at synth time and the provisioner derives it "
        "from `AGENT_IMAGE_URI` at run time, and those are one repository or the pull fails"
    )


def test_the_boundary_permits_no_action_the_inline_policy_does_not_grant(
    agreement: _Agreement,
) -> None:
    """The other half of the docstring's claim -- "and no wider" -- and the half that guards
    against dead surface a future inline policy could take without review.

    The pair with `test_the_boundary_permits_every_action_the_inline_policy_grants` above: a
    ceiling one action too narrow is a silent denial at run time, and a ceiling one action too
    wide is permission nobody reviewed. A wildcard Allow fails here even when a granted action
    matches it, because by construction it admits actions the inline policy does not spell --
    the classification and that rule both live in `tests/execution_boundary.py`.
    """
    boundary = agreement.boundary
    granted = execution_boundary.granted_actions(agreement.policy["Statement"])

    for allow in execution_boundary.allows(boundary):
        for pattern in execution_boundary.actions(allow):
            if execution_boundary.is_wildcard(pattern):
                assert pattern in execution_boundary.SURPLUS_ACTIONS, (
                    f"the boundary's {allow['Sid']} allows {pattern}, a wildcard: it permits "
                    f"actions the inline policy does not spell, so it is surplus by "
                    f"construction however many granted actions happen to match it. Declare "
                    f"it in execution_boundary.SURPLUS_ACTIONS with a reason, or spell the "
                    f"actions out"
                )
                continue
            assert pattern in granted or pattern in execution_boundary.SURPLUS_ACTIONS, (
                f"the boundary's {allow['Sid']} allows {pattern}, which "
                f"provisioner._execution_role_policy grants to nobody -- dead surface a "
                f"future inline policy could take without review. Either delete it from "
                f"agent_stack.execution_role_boundary_statements (never widen the grant to "
                f"match a ceiling), or declare it in execution_boundary.SURPLUS_ACTIONS with "
                f"a one-line reason"
            )


def test_every_boundary_resource_is_the_grants_or_a_declared_widening(
    agreement: _Agreement,
) -> None:
    """The dimension the ceiling really is traded away in: three of the boundary's Allow
    statements name a wider resource than the grant, each for a reason
    `execution_boundary.WIDER_RESOURCES` writes down. Resources are compared only inside a
    pair -- an Allow and the grant statements whose actions it permits -- because a resource
    means nothing without the action it is attached to.
    """
    boundary = agreement.boundary

    for allow in execution_boundary.allows(boundary):
        paired = execution_boundary.grants_for(allow, agreement.policy["Statement"])
        # An Allow no grant statement pairs with is already the failure the test above
        # reports, and reporting it twice would say nothing new.
        if not paired:
            continue
        granted = {
            resource for statement in paired for resource in execution_boundary.resources(statement)
        }
        for pattern in execution_boundary.resources(allow):
            assert any(execution_boundary.permits(pattern, resource) for resource in granted), (
                f"the boundary's {allow['Sid']} allows {pattern}, which none of the grant's "
                f"{sorted(granted)} falls under -- the silent-runtime-denial failure in the "
                f"resource dimension: the action is permitted and the resource is not, so the "
                f"call is denied with nothing naming the pattern that is too narrow"
            )
            if pattern not in granted:
                assert (allow["Sid"], pattern) in execution_boundary.WIDER_RESOURCES, (
                    f"the boundary's {allow['Sid']} allows {pattern}, which is wider than the "
                    f"{sorted(granted)} the grant names. Narrow it to what the grant names, or "
                    f"declare ({allow['Sid']!r}, {pattern!r}) in "
                    f"execution_boundary.WIDER_RESOURCES with a one-line reason for why the "
                    f"ceiling is deliberately wider there"
                )


def test_no_boundary_declaration_is_stale(agreement: _Agreement) -> None:
    """A declaration that has become untrue is a lie about the system, exactly as
    `test_every_field_the_container_reads_is_sent_or_declared` treats `READ_NOT_SENT`: once
    the grant spells an action the boundary was excused for, or names a resource as wide as
    the ceiling, the entry stops describing a trade-off and starts hiding that there is none.
    """
    boundary = agreement.boundary
    granted = execution_boundary.granted_actions(agreement.policy["Statement"])

    for pattern, reason in execution_boundary.SURPLUS_ACTIONS.items():
        assert pattern not in granted, (
            f"execution_boundary.SURPLUS_ACTIONS declares {pattern} as surplus, but the "
            f"inline policy grants it now -- delete the line. Its reason was: {reason}"
        )
        if not execution_boundary.is_wildcard(pattern):
            assert not any(execution_boundary.permits(pattern, action) for action in granted), (
                f"execution_boundary.SURPLUS_ACTIONS declares {pattern} as surplus, but it "
                f"matches an action the inline policy grants now -- delete the line"
            )

    for (sid, pattern), reason in execution_boundary.WIDER_RESOURCES.items():
        for allow in execution_boundary.allows(boundary):
            if allow.get("Sid") != sid:
                continue
            granted_resources = {
                resource
                for statement in execution_boundary.grants_for(allow, agreement.policy["Statement"])
                for resource in execution_boundary.resources(statement)
            }
            assert pattern not in granted_resources, (
                f"execution_boundary.WIDER_RESOURCES declares {sid}'s {pattern} as a widening, "
                f"but the grant names that resource literally now, so the ceiling is no wider "
                f"than the grant -- delete the line. Its reason was: {reason}"
            )
