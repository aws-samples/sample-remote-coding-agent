"""Environment-variable *names* are the contract between the stack and the container.

`api/src/handlers/provisioner.py` decides what a runtime's `environmentVariables` map
contains; `agent/src/coding_agent/` decides what it reads out of it. Nothing links the
two but a string spelled twice, and a name that is read and not forwarded fails at
run time as a container that quietly uses a default nobody chose. This file parses every
environment read in the container package and fails when a name is neither forwarded by
the provisioner nor listed below as a deliberate default with a reason.

The direction is **one-way on purpose**: reads are a subset of forwarded plus declared,
and never the converse. A variable that is forwarded and not yet read is fine and is
expected -- `CLAUDE_CODE_USE_BEDROCK` is read by the Claude Code CLI rather than by our
own code, and `CONFIG_ID`/`SECRET_ARN` reach the graph through the invocation payload --
so asserting the converse would fail on all three and teach nobody anything.

Both sides are read with `ast` and neither is imported. `provisioner.py` cannot be
imported without a region at all (it builds `boto3.resource("dynamodb").Table(...)` at
module scope), and the other contracts in this repository -- `test_api_route_contract.py`,
`test_api_grant_contract.py` -- are deliberately contracts on the source text.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from conftest import REPO_ROOT

PROVISIONER = REPO_ROOT / "api/src/handlers/provisioner.py"
CONTAINER = REPO_ROOT / "agent/src/coding_agent"

# The enclosing-"function" name for a read at module scope. There is none today, and one
# that appeared would need declaring below like any other unrecoverable site.
_MODULE_SCOPE = "<module>"

# The phrase in a reason that switches call-site recovery on. A site declared without it
# contributes no names at all, which is why the two that carry it are held to the
# anti-dodge clause in `test_no_declared_dynamic_site_hides_a_name` and the one that does
# not has a narrow test of its own.
_FROM_CALL_SITES = "recovered from the call sites"

CONTAINER_DYNAMIC_SITES: dict[tuple[str, str], str] = {
    # `_table(env_var)` reads `os.environ.get(env_var)` for a name its callers pass as a
    # string literal, so CONFIG_TABLE and TASKS_TABLE are recovered from the call sites
    # rather than from the read itself.
    ("store", "_table"): "name comes from a caller's literal; recovered from the call sites",
    # `_env_flag(name, default=...)` is the same shape: CODE_GOAL_ENABLED is recovered
    # from `load_config`'s call site. This is what keeps the helper legitimate while the
    # anti-dodge clause below still holds.
    ("config", "_env_flag"): "name comes from a caller's literal; recovered from the call sites",
    # `_role_config` builds `ROLE_<ROLE>_{MODEL,EFFORT,TURNS}` from an f-string, so no
    # call site carries the whole name. These are operator overrides that are never
    # forwarded by the fleet -- setting one is a deliberate act on one runtime.
    (
        "config",
        "_role_config",
    ): "per-role override names are built from an f-string; never forwarded",
}

CONTAINER_DEFAULTS: dict[str, str] = {
    "AGENT_SKILL_PATHS": "baked into the image (`ENV AGENT_SKILL_PATHS=/app/skills`), "
    "with `config.skill_paths()` falling back to the in-tree copy for a local run. "
    "Forwarding it would let a runtime override the image's own skill roots.",
    "VENV_ROOT": "deliberately NOT forwarded: the /tmp/venv default keeps ~692 MB of "
    "perfectly reconstructible bytes OFF the 1 GB session-storage mount. A forwarded "
    "value is one edit away from filling the session and killing the task.",
    "GITHUB_TOKEN": "the developer-machine path in `local_run.py` and `nodes/submit.py`. "
    "In the container the token is read from Secrets Manager at submit time and must "
    "never enter the environment (invariant 4).",
    "PATH": "process environment the OS provides; `commands._ALLOWED_ENV` is a filter, "
    "not a configuration knob (invariant 32).",
    "HOME": "as PATH.",
    "LANG": "as PATH.",
    "LC_ALL": "as PATH.",
    "TERM": "as PATH.",
}

# The Claude Code CLI's suppression set, name -> expected value, and the contract that it
# cannot silently shrink.
#
# These are read by the **Claude Code CLI**, not by anything under `agent/src/coding_agent/`,
# which is why they are not in `CONTAINER_DEFAULTS` (`test_no_stale_declared_default` asserts
# every entry there is read by the container package) and why the one-way direction of this
# file does not cover them: reads are a subset of forwarded, and nothing here reads these, so
# the whole group could be deleted without a single other test noticing.
#
# It would also be invisible at **run time**. On Bedrock the vendor telemetry, error-reporting
# and feature-flag endpoints are never contacted at all -- that traffic exists only when the
# CLI talks to the Anthropic API directly -- so the group does not close a hole that is open
# today. It makes the posture explicit and auditable in a `GetAgentRuntime` response instead
# of a default a reader has to know about, and a test is the only thing that can tell the
# difference between "still set" and "quietly dropped".
#
# Three of the eleven are not merely belt-and-braces:
#   * `CLAUDE_CODE_DISABLE_AUTO_MEMORY` is the CROSS-TENANT one. Auto-memory loads REGARDLESS
#     of `setting_sources`, and one container serves many tasks over its life, so without it
#     one task's notes can surface in another's session. It is also a literal in
#     `subagents.build_options`' `env=` on purpose, so it holds on a local run and on a
#     runtime whose environment has not converged yet.
#   * the `WebFetch` removal, which is not here because it is a **tool** and not a variable:
#     before fetching a URL the CLI sends the hostname to the vendor for a safety check that
#     `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` explicitly does not suppress. That half of
#     the posture is owned by
#     `tests/test_subagents.py::test_no_role_can_reach_the_network`.
#   * `CLAUDE_CODE_ENABLE_TELEMETRY` is a different mechanism from the other ten rather than
#     more of the same, which is why it once had a table of its own here. The ten above govern
#     **Anthropic-bound** traffic, which on Bedrock does not exist; this is the only variable
#     that turns the CLI's **OTLP** exporter on, and nothing in this deployment listens for
#     OTLP. It carried the provisioner's `OTEL_METRICS_ENABLED` constant when an in-container
#     ADOT collector signed the CLI's metrics to CloudWatch for Coding Agent Insights; that
#     feature was removed (`CLAUDE.md` says why), so the value is now a literal `"0"` and the
#     five `OTEL_*` names that configured the exporter are gone with it. A literal `"1"` here
#     points the CLI at a port nothing is listening on; any endpoint it *did* reach would be an
#     egress destination invariant 33 has not sanctioned.
CLI_SUPPRESSIONS: dict[str, str] = {
    # The umbrella: metrics, error reports, feature flags, surveys.
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_BUG_COMMAND": "1",
    "DISABLE_FEEDBACK_COMMAND": "1",
    # The one ENABLE-shaped name in the group, so its value is "false" and not "1". A
    # copy-pasted "1" here would enable what the group exists to switch off and would still
    # read as compliant, which is why this file pins values and not just names.
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_ARTIFACT": "1",
    "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    # Not one of the ten: the OTLP exporter switch, pinned off. See the note above.
    "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
}

# -- reading the container side ----------------------------------------------------


@dataclass(frozen=True)
class _Read:
    """One environment read. `names` is empty when the scan cannot recover the name, in
    which case `(module, function)` must be a declared dynamic site."""

    module: str
    function: str
    lineno: int
    names: tuple[str, ...]


def _trees() -> list[tuple[str, ast.Module]]:
    """`(module stem, tree)` for the whole package, `nodes/` included -- that subpackage
    is where `GITHUB_TOKEN` is read, so a scan that stops at the top level misses it. A
    list rather than a dict because both `__init__.py` files share a stem."""
    return [
        (path.stem, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(CONTAINER.rglob("*.py"))
    ]


def _is_os_environ(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _read_name_expression(node: ast.AST) -> ast.expr | None:
    """The expression naming the variable this node reads, or `None` when it reads none.

    This is the one judgement call in the scan, so it is spelled out. A **read** is
    `os.environ.get(X, ...)`, `os.getenv(X, ...)` or `os.environ[X]` in a `Load` context.
    `os.environ.update(...)`, `os.environ.pop(...)`, `X in os.environ` and
    `os.environ[X] = ...` are **not** reads: they are writes and cleanups rather than
    configuration inputs, so no forwarded variable is owed for them. `git_ops._export`
    (which publishes and then prunes the `GIT_CONFIG_*` group) and `local_run.main`
    (which sets `WORK_ROOT` from a CLI flag) are the sites that make the distinction
    matter -- counting either as a read would demand the provisioner forward a variable
    the container itself produces.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if not isinstance(func, ast.Attribute) or not node.args:
            return None
        if func.attr == "get" and _is_os_environ(func.value):
            return node.args[0]
        if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
            return node.args[0]
        return None
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and _is_os_environ(node.value)
    ):
        return node.slice
    return None


def _string_sequences(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """Module-level assignments whose value is a literal tuple or list of strings.

    The recovery rule that keeps `CONTAINER_DYNAMIC_SITES` at three entries.
    `commands._base_env` and `git_ops._base_env_for_git` both read
    `{name: os.environ[name] for name in <allow-list> if name in os.environ}`, so the
    subscript is not a constant -- but the allow-list is spelled out in the source, which
    makes the names as knowable as a literal. Declaring those two as dynamic sites
    instead would hide PATH, HOME, LANG, LC_ALL and TERM behind an escape hatch.
    """
    sequences: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        try:
            literal = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            continue
        if not isinstance(literal, tuple | list) or not all(
            isinstance(item, str) for item in literal
        ):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                sequences[target.id] = tuple(literal)
    return sequences


def _comprehension_bindings(
    node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    sequences: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    """Comprehension targets that iterate one of `sequences`, so a `Name` subscript
    inside the comprehension expands to those literal strings."""
    bindings: dict[str, tuple[str, ...]] = {}
    for generator in node.generators:
        if (
            isinstance(generator.target, ast.Name)
            and isinstance(generator.iter, ast.Name)
            and generator.iter.id in sequences
        ):
            bindings[generator.target.id] = sequences[generator.iter.id]
    return bindings


def _reads_in(module: str, tree: ast.Module) -> list[_Read]:
    sequences = _string_sequences(tree)
    reads: list[_Read] = []

    def visit(node: ast.AST, function: str, bound: dict[str, tuple[str, ...]]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                # A nested function is its own scope, and a comprehension binding does
                # not cross into it.
                visit(child, child.name, {})
                continue
            inner = bound
            if isinstance(child, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
                inner = {**bound, **_comprehension_bindings(child, sequences)}
            expression = _read_name_expression(child)
            if expression is not None:
                reads.append(_Read(module, function, child.lineno, _names_of(expression, inner)))
            visit(child, function, inner)

    visit(tree, _MODULE_SCOPE, {})
    return reads


def _names_of(expression: ast.expr, bound: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return (expression.value,)
    if isinstance(expression, ast.Name) and expression.id in bound:
        return bound[expression.id]
    return ()


def _read_sites() -> list[_Read]:
    return [read for module, tree in _trees() for read in _reads_in(module, tree)]


def _names_from_call_sites() -> dict[tuple[str, str], set[str]]:
    """The literal names the callers of each call-site-recovered site pass in.

    The mechanism behind two of the three declared sites: `_table("CONFIG_TABLE")` and
    `_env_flag("CODE_GOAL_ENABLED", default=True)` name the variable at the call rather
    than at the read, so the name is still in the source and still recoverable.
    """
    wanted = {
        site for site, reason in CONTAINER_DYNAMIC_SITES.items() if _FROM_CALL_SITES in reason
    }
    by_function = {site[1]: site for site in wanted}
    recovered: dict[tuple[str, str], set[str]] = {site: set() for site in wanted}
    for _module, tree in _trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            site = by_function.get(called)
            if site is None:
                continue
            # The first positional argument is the name in both shapes; keywords are
            # swept too so a `_table(env_var="CONFIG_TABLE")` spelling still resolves.
            arguments = [*node.args[:1], *(keyword.value for keyword in node.keywords)]
            recovered[site] |= {
                argument.value
                for argument in arguments
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            }
    return recovered


def _container_reads() -> set[str]:
    names = {name for read in _read_sites() for name in read.names}
    for from_calls in _names_from_call_sites().values():
        names |= from_calls
    return names


# -- reading the provisioner side --------------------------------------------------


def _return_dict(tree: ast.Module, function: str) -> ast.Dict:
    """The literal dict `function` returns. One walk, two readers: `_returned_keys` wants
    every constant key whatever its value is, and `_literal_items` wants the pairs whose
    value is a string constant too."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Dict):
                    return inner.value
    raise AssertionError(
        f"provisioner.py no longer returns a literal dict from {function}(); this whole file "
        f"reads the two environment groups out of that dict, so the contract is unreadable "
        f"rather than broken -- restore the literal or teach this helper the new shape"
    )


def _returned_keys(tree: ast.Module, function: str) -> set[str]:
    """Every constant key of the returned dict, whatever the value is -- the routing keys
    are `ast.Name` values (`PLAN_MODEL_ID` and friends) and must keep being seen here."""
    return {
        key.value
        for key in _return_dict(tree, function).keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _literal_items(tree: ast.Module, function: str) -> dict[str, str]:
    """Only the `"NAME": "value"` pairs -- both sides string constants. A `**splat` entry
    has `key is None` and is skipped, here and in `_returned_keys`, which is why the
    suppression group has to be spelled as literal pairs to be visible at all."""
    returned = _return_dict(tree, function)
    return {
        key.value: value.value
        for key, value in zip(returned.keys, returned.values, strict=True)
        if isinstance(key, ast.Constant)
        and isinstance(key.value, str)
        and isinstance(value, ast.Constant)
        and isinstance(value.value, str)
    }


def _forwarded() -> tuple[set[str], set[str]]:
    """`(fleet, per_configuration)`: the keys `_fleet_environment` converges on every
    update, and the create-time defaults `_configuration_environment` supplies."""
    tree = ast.parse(PROVISIONER.read_text(encoding="utf-8"))
    return _returned_keys(tree, "_fleet_environment"), _returned_keys(
        tree, "_configuration_environment"
    )


# -- the contracts -----------------------------------------------------------------


def test_every_container_environment_read_is_forwarded_or_a_declared_default() -> None:
    fleet, per_configuration = _forwarded()
    for read in _read_sites():
        if read.names:
            continue
        site = (read.module, read.function)
        assert site in CONTAINER_DYNAMIC_SITES, (
            f"{read.module}.py:{read.lineno} reads an environment variable whose name this scan "
            f"cannot recover, and {site} is not in CONTAINER_DYNAMIC_SITES. Read a literal name, "
            f"or declare the site here with the reason a reviewer needs -- an unrecoverable read "
            f"is a variable nobody can prove is forwarded."
        )
    undeclared = sorted(_container_reads() - fleet - per_configuration - set(CONTAINER_DEFAULTS))
    assert not undeclared, (
        f"the container reads {undeclared}, which the provisioner does not forward and this file "
        f"does not declare as a deliberate default. A new container knob is three edits: the key "
        f"in `_fleet_environment` (or `_configuration_environment`) in "
        f"api/src/handlers/provisioner.py, the forwarding chain from infra/app.py's context "
        f"through `ApiStack._provisioner_environment` into the provisioner Lambda's environment, "
        f"and a line in CONTAINER_DEFAULTS here when the answer is that it is deliberately not "
        f"forwarded at all."
    )


def test_the_two_fleet_policy_knobs_are_classified_as_fleet() -> None:
    """Invariant 24, and the failure this file exists to catch. `_desired_runtime` merges
    `{**create-time defaults, **live, **fleet}`, so a key in the per-configuration group
    is *carried forward* from whatever the live runtime was born with while a key in the
    fleet group **converges**. Put a fleet-wide knob in the wrong group and every other
    test still passes while a rollout quietly fails to take -- invisible until someone
    diffs a live runtime with `GetAgentRuntime`.
    """
    fleet, per_configuration = _forwarded()
    # The two this phase adds, plus the four routing values they sit beside.
    for name in (
        "ANTHROPIC_FALLBACK_MODEL",
        "CODE_GOAL_ENABLED",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        assert name in fleet, (
            f"{name} is fleet policy and must be a key of `_fleet_environment`: one value for "
            f"the whole deployment, converged onto every live runtime by an UPDATE"
        )
        assert name not in per_configuration, (
            f"{name} is in `_configuration_environment`, so `UpdateAgentRuntime` would preserve "
            f"the live runtime's own value instead of converging it (invariant 24)"
        )
    # And the two that genuinely vary per repository, asserted the other way round so the
    # classification cannot be satisfied by putting everything in one group.
    for name in ("CONFIG_ID", "SECRET_ARN"):
        assert name in per_configuration and name not in fleet, (
            f"{name} names one configuration's own runtime, so it is a create-time default that "
            f"loses to the live value, not fleet policy"
        )


def test_the_cli_suppression_set_cannot_silently_shrink() -> None:
    """The eleven names in `CLI_SUPPRESSIONS`, each with its value, in the fleet group.

    Nothing under `agent/src/coding_agent/` reads these, so the one-way contract above
    cannot see them, and on Bedrock the endpoints they suppress are never contacted anyway
    -- a deletion is invisible both to the rest of this file and at run time. This test is
    the whole guard, which is why it pins values and not only names.
    """
    tree = ast.parse(PROVISIONER.read_text(encoding="utf-8"))
    fleet_items = _literal_items(tree, "_fleet_environment")
    per_configuration = _returned_keys(tree, "_configuration_environment")
    for name, expected in CLI_SUPPRESSIONS.items():
        assert name in fleet_items, (
            f'{name} is no longer forwarded as a literal `"{name}": "{expected}"` pair by '
            f"`_fleet_environment` in api/src/handlers/provisioner.py. If it was dropped "
            f"deliberately, delete it from CLI_SUPPRESSIONS here AND say why in CLAUDE.md's "
            f'"The boundary claim, and where it is qualified" -- that is what an auditor '
            f"reads, and this group is the only place the posture is written down. If it is "
            f"still meant to be "
            f"set, note that a `**splat` is invisible to this reader: literal pairs only."
        )
        assert fleet_items[name] == expected, (
            f"{name} is forwarded as {fleet_items[name]!r} and this contract expects "
            f"{expected!r}. A wrong value reads as compliant while doing the opposite: "
            f"`ENABLE_CLAUDEAI_MCP_SERVERS` is the one ENABLE-shaped name in the group, so "
            f'"1" there enables what the group exists to switch off, and a "0" is not a '
            f"suppression anywhere in the set."
        )
        assert name not in per_configuration, (
            f"{name} is in `_configuration_environment`, so it is a create-time default that "
            f"loses to the live runtime's value: `_desired_runtime` merges "
            f"`{{**create-time, **live, **fleet}}` and only the fleet group converges "
            f"(invariant 24). A runtime provisioned before this group existed would keep "
            f"talking to whatever it was going to talk to, forever, and nothing short of "
            f"diffing it with `GetAgentRuntime` would say so."
        )


def test_no_declared_dynamic_site_hides_a_name() -> None:
    """The anti-dodge clause. `CONTAINER_DYNAMIC_SITES` is an escape hatch, so adding a
    site is the cheapest way to make an unforwarded read invisible: a site that recovers
    nothing exempts every read inside it forever."""
    unrecoverable = {(read.module, read.function) for read in _read_sites() if not read.names}
    from_call_sites = _names_from_call_sites()
    for site, reason in CONTAINER_DYNAMIC_SITES.items():
        assert site in unrecoverable, (
            f"{site} is declared as a dynamic environment read and no longer is one; drop the "
            f"entry. A stale escape hatch is standing permission for whatever lands there next."
        )
        if _FROM_CALL_SITES in reason:
            assert from_call_sites[site], (
                f"{site} is declared as {reason!r} and yet no call site passes a literal name, "
                f"so every read it makes is exempt from this file. Either the callers stopped "
                f"naming the variable -- in which case the reason is now false -- or the helper "
                f"has become a way to read the environment without declaring what it reads."
            )


def test_no_stale_declared_default() -> None:
    read_names = _container_reads()
    for name in CONTAINER_DEFAULTS:
        assert name in read_names, (
            f"{name} is declared here as a deliberate container default and nothing under "
            f"agent/src/coding_agent/ reads it any more. A stale entry is a licence nobody "
            f"needs, and it makes the list read as bigger than the contract it describes."
        )


def test_the_role_override_reads_are_the_three_this_file_knows_about() -> None:
    """`config._role_config` is the one site whose names no call site carries, so its
    exemption is bounded by this test rather than by its own declaration: a fourth
    per-role knob has to come past this file."""
    tree = ast.parse((CONTAINER / "config.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_role_config"
    )
    suffixes: set[str] = set()
    for node in ast.walk(function):
        expression = _read_name_expression(node)
        if isinstance(expression, ast.JoinedStr):
            suffixes |= {
                part.value
                for part in expression.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            }
    assert suffixes == {"MODEL", "EFFORT", "TURNS"}, (
        f"config._role_config reads ROLE_<ROLE>_{sorted(suffixes)}; the declared exemption in "
        f"CONTAINER_DYNAMIC_SITES covers MODEL, EFFORT and TURNS. A new per-role override is "
        f"either a deliberate never-forwarded operator knob -- say so here -- or a fleet value "
        f"that belongs in `_fleet_environment`."
    )
