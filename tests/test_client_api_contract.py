"""The two *callers* of the HTTP API, reconciled against the route table that wires it.

`infra/stacks/api_stack.py::ROUTES` is the authority -- the union table
`tests/test_grant_contract.py` already pins against the handler modules' own `ROUTES`
tuples, so pinning the callers to it pins them to the handlers too. The callers are the
console (`web/js/api.js`, whose one private `request` carries every request the operator's
browser ever sends) and the MCP plugin (`mcp/src/rca_mcp/server.py`, whose six tools call
`client.get` / `client.post`).

Nothing else holds either of them. A path typed differently in either file is a **404 at
run time with nothing failing anywhere first**: the handler still exists, the route is
still wired, the suite is still green, and the console shows "the API answered 404 with no
explanation" -- or the model reads a tool error it will happily paraphrase as "that
repository does not exist".

For the console the check is an equality in both directions: it calls 17 of the 19 wired
routes today, and the two it does not are declared in `CONSOLE_NOT_CALLED` below with the
reason -- so a route the server grows before the console learns it has to be written down.

For the MCP server the reverse direction is deliberately **not** an equality, and this
file states the reason rather than deleting the check. `ApiClient.ALLOWED_METHODS` is
`frozenset({"GET", "POST"})` on purpose (`mcp/src/rca_mcp/client.py:326-330`): teardown
takes the AgentCore runtime, the execution role and the GitHub secret with it (invariant
22), and that is a human at a terminal, not something a chat window can be talked into. So
every `PATCH` / `PUT` / `DELETE` route is unreachable from there by construction, and what
is asserted is the rule -- plus a declared list of the GET/POST routes the server does not
call, each with a reason.
"""

from __future__ import annotations

import ast
import re

from conftest import REPO_ROOT
from rca_mcp.client import ApiClient
from stacks import api_stack

API_JS = REPO_ROOT / "web/js/api.js"
MCP_SERVER = REPO_ROOT / "mcp/src/rca_mcp/server.py"

# The authority, as `(method, path)`. Imported rather than parsed: `pytest.ini`'s
# `pythonpath` includes `infra` and `tests/test_web_stack.py` already imports this module
# -- only *constructing* `ApiStack` needs Docker.
WIRED: frozenset[tuple[str, str]] = frozenset((m, p) for m, p, _module, _fn in api_stack.ROUTES)

# The two argument forms `web/js/api.js` uses: a double-quoted literal and a template
# literal. It deliberately does not match `request(method, path, {...})` -- the 401-retry
# recursion at `web/js/api.js:83` -- nor the `async function request(` definition, because
# neither quotes a method.
_REQUEST = re.compile(r'request\(\s*"(?P<method>[A-Z]+)"\s*,\s*(?P<path>"[^"]*"|`[^`]*`)')

# The one interpolation shape the console uses. `seg` is `web/js/api.js:101`, "One escaper
# for every path parameter", so a `${...}` that does not go through it is an unescaped path
# parameter and must fail loudly here rather than be normalised away.
_SEG = re.compile(r"\$\{seg\(\s*(?P<name>[A-Za-z_$][\w$]*)\s*\)\}")

# Anti-vacuity guards. Both scans read source text, so a reformat that stopped matching
# would make every other assertion in this file pass over an empty set.
_CONSOLE_CALL_COUNT = 17
_MCP_CALL_COUNT = 6

# Two entries today: the console is the operator's whole surface on this API and calls 17 of
# the 19 wired routes. This is the declared place to record a route the server grows before
# the console learns it, and `test_no_stale_console_exception` keeps it from becoming a
# silent allowlist -- an entry for a route a wrapper does start calling fails the suite.
CONSOLE_NOT_CALLED: dict[tuple[str, str], str] = {
    ("GET", "/configurations/{configId}/memory"): (
        "Wired ahead of its view. The route is the operator's read of what a repository "
        "remembers and `web/` is owned by another card in this wave, so the console has no "
        "wrapper for it yet -- `web/js/views/register.js` prints the `memoryId` and nothing "
        "behind it. Delete this entry with the wrapper that replaces it."
    ),
    ("POST", "/tasks/{taskId}/cancel"): (
        "Wired ahead of its control. `web/` is owned by another card, and the button is not "
        "one line: it is a confirm panel over a destructive action (`ui.CONFIRM_ORDER`) plus "
        "the run view's two timers. Shipping the route without it is what lets an operator "
        "stop a run started by mistake today -- from the plugin or a signed request -- rather "
        "than waiting for the console. Delete this entry with the wrapper that replaces it."
    ),
}

# The GET/POST routes the MCP server does not call. Each entry is a reason, not an
# allowlist: the reason is what the next reader needs.
MCP_NOT_CALLED: dict[tuple[str, str], str] = {
    ("GET", "/configurations/{configId}/memory"): (
        "An operator's read of the memory data plane, not a model's. The container already "
        "has `memory_tools` over its own repository's memory inside a run, so a seventh tool "
        "here would be a second, unscoped path to the same records -- reachable from a chat "
        "window, for any registered repository, with a free-prose query."
    ),
    ("POST", "/configurations/{configId}/runtime-update"): (
        "A fleet rollout onto a live AgentCore runtime. An operator action from the "
        "console, with the board in front of them -- not something a chat window should "
        "start on its own."
    ),
    ("POST", "/cards"): (
        "The board is the console's model. This server's vocabulary is repositories and "
        "tasks, and a card tool would have to carry `cards.BOARD_COLUMNS` into a "
        "description before a model could use it correctly."
    ),
    ("GET", "/cards"): (
        "The board is the console's model, and `list_tasks` already answers the question a "
        "chat window asks of it: what work is in flight for this repository."
    ),
    ("GET", "/cards/{cardId}"): (
        "Same reason as `GET /cards`: one card's column is a board fact, and `get_task` is "
        "the run-shaped view this server's tools are written in."
    ),
    ("POST", "/cards/{cardId}/move"): (
        "The only door to a run, so a tool over it is a second way to start work beside "
        "`start_task` -- with the column state machine (`cards.MOVE_TARGETS`) to explain "
        "in a description as well."
    ),
    ("GET", "/tasks/{taskId}/activity"): (
        "The polling route. `tests/test_mcp_server.py:35-37` already declares a seventh "
        "tool over it 'a decision, not an addition', and `get_task`'s own description "
        "tells the model not to poll it in a loop."
    ),
    ("POST", "/tasks/{taskId}/cancel"): (
        "Reachable by construction -- `POST` was chosen over `DELETE` for exactly that -- but "
        "a `cancel_task` tool is a decision about what a chat window may stop, and this card "
        "ships the route the console and the plugin will both call. The tool is the next "
        "decision, not part of this one."
    ),
}


def _console_calls() -> set[tuple[str, str]]:
    """Every `(method, path)` `web/js/api.js` sends, in the API's placeholder spelling."""
    source = API_JS.read_text(encoding="utf-8")
    calls: set[tuple[str, str]] = set()
    for match in _REQUEST.finditer(source):
        line = source.count("\n", 0, match.start()) + 1
        # One leading and one trailing quote or backtick, whichever form matched.
        path = _SEG.sub(lambda m: "{" + m["name"] + "}", match["path"][1:-1])
        assert "${" not in path and "`" not in path, (
            f"{API_JS.name}:{line} interpolates a path parameter without `seg(...)`: "
            f"{match['path']}. Every path parameter goes through `seg` (web/js/api.js:101) "
            "so an operator-pasted configId cannot break out of the path -- and so this "
            "contract can normalise it to the API's own placeholder spelling."
        )
        calls.add((match["method"], path))
    return calls


def _camel(name: str) -> str:
    """`config_id` -> `configId`, `task_id` -> `taskId`.

    The whole of the placeholder-spelling bridge: `server.py` names the local it
    interpolates after the path parameter, so the camel-cased local *is* the API's
    placeholder.
    """
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _mcp_path(node: ast.expr) -> str | None:
    """The path a call site asks for, or `None` if this scan cannot recover it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Name):
                parts.append("{" + _camel(value.value.id) + "}")
            else:
                return None
        return "".join(parts)
    return None


def _mcp_calls() -> set[tuple[str, str]]:
    """Every `(method, path)` `mcp/src/rca_mcp/server.py` sends through its client."""
    tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"))
    calls: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        if func.value.id != "client":
            continue
        assert func.attr in ("get", "post"), (
            f"{MCP_SERVER.name}:{node.lineno} calls `client.{func.attr}`. This package "
            "sends GET and POST and nothing else -- ApiClient.ALLOWED_METHODS is what "
            "makes teardown unreachable from a chat window -- so this scan will not "
            "record the call under some other method and let it through."
        )
        path = _mcp_path(node.args[0]) if node.args else None
        assert path is not None, (
            f"{MCP_SERVER.name}:{node.lineno} builds its path in a shape this scan cannot "
            "read. Bind the segment to a local named after the path parameter "
            "(`config_id`, `task_id`) and interpolate that local, so the path the tool "
            "sends is checkable against the wired route table."
        )
        calls.add((func.attr.upper(), path))
    return calls


def test_the_console_scan_found_every_request_call() -> None:
    """The guard that stops a reformat emptying this file instead of failing it.

    `_REQUEST` is the only reader of a JavaScript file: if a refactor wraps a `request(`
    call so the method and the path land on different lines, or introduces a third argument
    spelling, the scan silently shrinks and every assertion below passes over what is left.
    """
    calls = _console_calls()
    assert len(calls) == _CONSOLE_CALL_COUNT, (
        f"web/js/api.js parsed to {len(calls)} calls, not {_CONSOLE_CALL_COUNT}: "
        f"{sorted(calls)}. Either a wrapper was added or removed -- in which case update "
        "_CONSOLE_CALL_COUNT and expect the equality tests below to move with it -- or the "
        '`request("METHOD", path` shape this file scans for has changed, in which case '
        "_REQUEST has to learn the new one before it can hold anything at all."
    )


def test_every_console_call_is_a_wired_route() -> None:
    """The direction that is a 404 in the browser and green everywhere else."""
    unwired = _console_calls() - WIRED
    assert not unwired, (
        f"{sorted(unwired)} is sent by web/js/api.js and wired by nothing in "
        "infra/stacks/api_stack.py::ROUTES. That is a 404 at run time -- the view shows "
        "the API's empty explanation -- and no other test in this repository looks at the "
        "console's paths at all."
    )


def test_every_wired_route_the_console_does_not_call_is_declared() -> None:
    """The other direction: a route the server grows and the console never learns about."""
    uncalled = WIRED - _console_calls()
    assert uncalled == set(CONSOLE_NOT_CALLED), (
        f"{sorted(uncalled - set(CONSOLE_NOT_CALLED))} is wired by "
        "infra/stacks/api_stack.py::ROUTES and called by no wrapper in web/js/api.js. The "
        "console is the operator's whole surface on this API, so either add the wrapper or "
        "declare the route in CONSOLE_NOT_CALLED with a reason."
    )


def test_no_stale_console_exception() -> None:
    """A stale entry is standing permission for whatever lands there next."""
    called = _console_calls()
    for route, reason in CONSOLE_NOT_CALLED.items():
        assert route in WIRED, f"{route} is not a wired route at all: {reason}"
        assert route not in called, (
            f"{route} is called by web/js/api.js now, so its CONSOLE_NOT_CALLED entry is a "
            "lie about the system and should go"
        )


def test_the_mcp_scan_found_every_client_call() -> None:
    """The same guard on the other scan: an empty walk would pass everything below."""
    calls = _mcp_calls()
    assert len(calls) == _MCP_CALL_COUNT, (
        f"mcp/src/rca_mcp/server.py parsed to {len(calls)} client calls, not "
        f"{_MCP_CALL_COUNT}: {sorted(calls)}. A tool added or removed means updating "
        "_MCP_CALL_COUNT and MCP_NOT_CALLED together; anything else means the calls no "
        "longer go through a local named `client`, and this scan finds nothing."
    )


def test_every_mcp_call_is_a_wired_route() -> None:
    """A path typed differently here reaches the model as a tool error it will paraphrase."""
    unwired = _mcp_calls() - WIRED
    assert not unwired, (
        f"{sorted(unwired)} is sent by mcp/src/rca_mcp/server.py and wired by nothing in "
        "infra/stacks/api_stack.py::ROUTES. The tool answers with whatever the API says "
        "about a route that does not exist, and the model reports that as an answer."
    )


def test_the_mcp_server_can_only_reach_allowed_methods() -> None:
    """Why this file does not assert an equality on the MCP side.

    Five wired routes are unreachable from this package by construction today: `PATCH` and
    `DELETE /configurations/{configId}`, `PUT /configurations/{configId}/github-token`, and
    `PATCH` and `DELETE /cards/{cardId}`. Asserting equality would force a bogus
    `MCP_NOT_CALLED` entry for each and bury the actual reason, which is
    `ApiClient.ALLOWED_METHODS`: teardown takes the runtime, the execution role and the
    GitHub secret with it (invariant 22), so it is a human at a terminal and no prompt
    injection can start it from a chat window. So the rule is what gets asserted.
    """
    assert set(ApiClient.ALLOWED_METHODS) == {"GET", "POST"}, (
        "ApiClient.ALLOWED_METHODS is the whole safety story of this package. Widening it "
        "is not a refactor, it is a change of threat model -- and it is what makes a broad, "
        "free-prose skill over these tools safe to ship."
    )

    used = {method for method, _path in _mcp_calls()}
    assert used <= set(ApiClient.ALLOWED_METHODS), (
        f"{sorted(used - set(ApiClient.ALLOWED_METHODS))} is sent from a tool, and "
        "`ApiClient.request` raises on it: the tool fails at run time with the "
        "ALLOWED_METHODS sentence, which is a broken tool rather than a guarded one."
    )

    unreachable = {route for route in WIRED if route[0] not in ApiClient.ALLOWED_METHODS}
    assert unreachable, (
        "no wired route uses a method outside ALLOWED_METHODS, so this claim says nothing. "
        "If the destructive routes were renamed or removed, this test is no longer the "
        "reason the MCP reverse direction is one-way and the file should say so instead."
    )
    assert unreachable.isdisjoint(_mcp_calls())


def test_every_get_or_post_route_the_mcp_server_does_not_call_is_declared() -> None:
    """The reachable half, held as an equality: not calling a route is a decision."""
    reachable = {route for route in WIRED if route[0] in ApiClient.ALLOWED_METHODS}
    uncalled = reachable - _mcp_calls()
    assert uncalled == set(MCP_NOT_CALLED), (
        f"{sorted(uncalled - set(MCP_NOT_CALLED))} is a GET or POST route the MCP server "
        f"could call and does not, and {sorted(set(MCP_NOT_CALLED) - uncalled)} is declared "
        "as uncalled while being called or unreachable. A tool is a decision about what a "
        "chat window may do, so record it in MCP_NOT_CALLED with the reason."
    )


def test_no_stale_mcp_exception() -> None:
    """The anti-dodge clause: the file is only as strong as the reasons in it."""
    called = _mcp_calls()
    for route, reason in MCP_NOT_CALLED.items():
        assert route in WIRED, f"{route} is not a wired route at all: {reason}"
        assert route[0] in ApiClient.ALLOWED_METHODS, (
            f"{route} is unreachable from this package by construction, so it needs no "
            "entry here -- test_the_mcp_server_can_only_reach_allowed_methods is its reason"
        )
        assert route not in called, (
            f"{route} is called by a tool now, so its MCP_NOT_CALLED entry is standing "
            "permission for whatever lands there next"
        )
