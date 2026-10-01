"""The HTTP surface, asserted mechanically rather than described.

`ast` over `api/src/handlers/*.py`, so it costs nothing to run and fails on exactly
the change that is otherwise invisible: a route added to a module but not to `ROUTES`
(and therefore never wired up in `ApiStack`), an entry point that is neither HTTP nor
queue/schedule driven, or a log line that carries a raw event.

Nothing here imports a handler module -- these are contracts on the source text, and
`boto3.resource("dynamodb")` raises `NoRegionError` at construction anyway.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conftest import REPO_ROOT

HANDLERS = REPO_ROOT / "api/src/handlers"

# The modules that answer HTTP, and the ones the queue and the schedule drive. Adding
# a module means adding it to one of these two lists, which is the point.
HTTP_MODULES = ("configurations", "cards", "tasks", "github_app")
EVENT_MODULES = ("provisioner", "dispatcher", "watchdog")
SUPPORT_MODULES = ("_common", "observability", "runs", "github_probe")

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})
# A name rooted at one of these is a whole request or a whole message, and a request
# body can carry a GitHub token.
_RAW_EVENT_NAMES = frozenset({"event", "record", "body", "payload"})


def _tree(module: str) -> ast.Module:
    return ast.parse((HANDLERS / f"{module}.py").read_text())


def _is_handler_decorator(node: ast.expr) -> bool:
    """`@handler` or `@_common.handler`, with or without the module prefix."""
    if isinstance(node, ast.Name):
        return node.id == "handler"
    return isinstance(node, ast.Attribute) and node.attr == "handler"


def _decorated_functions(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(_is_handler_decorator(decorator) for decorator in node.decorator_list)
    }


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _routes(tree: ast.Module) -> tuple[tuple[str, str, str], ...]:
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


def test_every_handler_module_is_accounted_for() -> None:
    """A new module in `api/src/handlers/` must be classified here, or the two
    contracts below silently stop covering it."""
    on_disk = {path.stem for path in HANDLERS.glob("*.py") if path.stem != "__init__"}
    assert on_disk == set(HTTP_MODULES) | set(EVENT_MODULES) | set(SUPPORT_MODULES), (
        "a handler module is neither HTTP nor queue/schedule driven nor shared plumbing; "
        "classify it in this test so the route and grant contracts cover it"
    )


@pytest.mark.parametrize("module", HTTP_MODULES)
def test_every_decorated_function_is_a_declared_route(module: str) -> None:
    tree = _tree(module)
    decorated = _decorated_functions(tree)
    declared = {name for _, _, name in _routes(tree)}
    assert decorated == declared, (
        f"handlers/{module}.py: {decorated ^ declared} is decorated with @handler but not in "
        f"ROUTES, or in ROUTES but not decorated. A route that is not in ROUTES is never wired "
        f"up by ApiStack, and a ROUTES entry with no handler is a 500 at the first request."
    )


@pytest.mark.parametrize("module", HTTP_MODULES)
def test_every_route_names_a_function_that_exists(module: str) -> None:
    tree = _tree(module)
    functions = _module_functions(tree)
    for method, path, name in _routes(tree):
        assert method in _METHODS, f"handlers/{module}.py: {method} is not an HTTP method"
        assert path.startswith("/"), f"handlers/{module}.py: {path} is not a path"
        assert name in functions, f"handlers/{module}.py: ROUTES names {name}, which does not exist"


def test_no_two_modules_claim_the_same_method_and_path() -> None:
    seen: dict[tuple[str, str], str] = {}
    for module in HTTP_MODULES:
        for method, path, _ in _routes(_tree(module)):
            key = (method, path)
            assert key not in seen, f"{method} {path} is claimed by both {seen[key]} and {module}"
            seen[key] = module
    # The nine routes `CLAUDE.md`'s control-plane description names plus the tenth
    # configurations route -- the memory read -- and the five task routes and the six card
    # routes.
    assert len(seen) == 24, f"expected the 24 declared routes, got {sorted(seen)}"


def test_the_declared_route_table_is_the_one_plan_md_names() -> None:
    declared = {
        (method, path) for module in HTTP_MODULES for method, path, _ in _routes(_tree(module))
    }
    assert declared == {
        ("POST", "/configurations"),
        ("GET", "/configurations"),
        ("GET", "/configurations/{configId}"),
        ("PATCH", "/configurations/{configId}"),
        ("DELETE", "/configurations/{configId}"),
        ("POST", "/configurations/{configId}/runtime-update"),
        # The two per-repository credential routes that replaced
        # `PUT /configurations/{configId}/github-token`. There is no token to rotate: one
        # GitHub App serves the deployment and AgentCore Identity holds its credentials, so
        # what a repository needs is an authorisation it can start and a verdict it can read.
        ("POST", "/configurations/{configId}/github-authorize"),
        ("GET", "/configurations/{configId}/github-status"),
        ("GET", "/configurations/{configId}/memory"),
        # The deployment-wide GitHub App, in `github_app.py`. Four routes rather than one
        # because connecting is a two-leg flow -- mint the provider, then exchange the code
        # GitHub redirects back with -- and disconnecting has to be reachable on its own.
        ("GET", "/github-app"),
        ("POST", "/github-app"),
        ("POST", "/github-app/conversions"),
        ("DELETE", "/github-app"),
        ("POST", "/cards"),
        ("GET", "/cards"),
        ("GET", "/cards/{cardId}"),
        ("PATCH", "/cards/{cardId}"),
        ("DELETE", "/cards/{cardId}"),
        ("POST", "/cards/{cardId}/move"),
        ("POST", "/tasks"),
        ("GET", "/tasks"),
        ("GET", "/tasks/{taskId}"),
        ("GET", "/tasks/{taskId}/activity"),
        ("POST", "/tasks/{taskId}/cancel"),
    }


def test_every_lambda_entry_point_is_http_or_queue_or_schedule_driven() -> None:
    """`lambda_handler`/`dlq_handler` exist only in the queue- and schedule-driven
    modules, and are never `@handler`-decorated -- an SQS or EventBridge event has no
    `requestContext`, so a decorated one would answer HTTP to nobody."""
    for module in HTTP_MODULES + SUPPORT_MODULES:
        functions = _module_functions(_tree(module))
        assert not {"lambda_handler", "dlq_handler"} & set(functions), (
            f"handlers/{module}.py declares a bare Lambda entry point. A new HTTP entry point "
            f"belongs in ROUTES behind @handler; a new Lambda belongs to a queue or a schedule, "
            f"in provisioner.py / dispatcher.py / watchdog.py."
        )
    for module in EVENT_MODULES:
        tree = _tree(module)
        functions = _module_functions(tree)
        assert "lambda_handler" in functions, f"handlers/{module}.py has no lambda_handler"
        assert not _decorated_functions(tree), (
            f"handlers/{module}.py is queue- or schedule-driven and must declare no @handler: "
            f"it answers no HTTP request."
        )
        assert not _routes(tree), f"handlers/{module}.py declares ROUTES but serves no HTTP route"
        for name in ("lambda_handler", "dlq_handler"):
            if name in functions:
                assert not functions[name].decorator_list, (
                    f"handlers/{module}.py: {name} is decorated"
                )
    assert "dlq_handler" in _module_functions(_tree("dispatcher")), (
        "the DLQ handler is the terminal owner that sets FAILED; without it a run's failure "
        "stops nowhere"
    )


def _scrubbed_nodes(arg: ast.expr) -> set[int]:
    """Every node inside a `scrub(...)` call within `arg`, by identity."""
    safe: set[int] = set()
    for node in ast.walk(arg):
        if isinstance(node, ast.Call):
            func = node.func
            scrubbing = (isinstance(func, ast.Name) and func.id == "scrub") or (
                isinstance(func, ast.Attribute) and func.attr == "scrub"
            )
            if scrubbing:
                safe.update(id(inner) for inner in ast.walk(node))
    return safe


def _log_calls(tree: ast.Module) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_LEVELS
        ):
            root = node.func.value
            if (isinstance(root, ast.Name) and root.id == "LOG") or (
                isinstance(root, ast.Attribute) and root.attr == "LOG"
            ):
                calls.append(node)
    return calls


@pytest.mark.parametrize("path", sorted(HANDLERS.glob("*.py")), ids=lambda path: Path(path).name)
def test_no_handler_logs_a_raw_event(path: Path) -> None:
    """Tier 1 #15: no handler ever logs a raw event. A request body holds the GitHub
    token, and CloudWatch is not a place a credential should be recoverable from --
    which is why this is a test and not a code-review rule. Covers f-strings too: a
    `FormattedValue` is where a raw event usually gets in."""
    tree = ast.parse(path.read_text())
    for call in _log_calls(tree):
        for arg in call.args + [keyword.value for keyword in call.keywords]:
            safe = _scrubbed_nodes(arg)
            for node in ast.walk(arg):
                if (
                    isinstance(node, ast.Name)
                    and node.id in _RAW_EVENT_NAMES
                    and id(node) not in safe
                ):
                    pytest.fail(
                        f"{path.name}:{node.lineno} logs `{node.id}` raw. Log the fields you need "
                        f"by name, or wrap the value in scrub()."
                    )
