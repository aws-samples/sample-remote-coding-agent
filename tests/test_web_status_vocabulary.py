"""The console's copy of the task-status vocabulary, reconciled with the container's.

`web/js/status.js` is pinned duplication: the agent is a container image and the console
is a static bundle, so neither can import the other's list and the vocabulary is written
twice on purpose. Nothing fails when the two drift -- the console draws a chip for a
status it has never heard of and the board keeps working -- which is exactly why the
agreement needs a test rather than a code-review rule.

Two of the assertions here are about a colour, and that is not decoration.
`SUCCEEDED_UNVERIFIED` means "a draft PR nobody verified" (invariant 6: the verification
veto is one-way). A console that renders it `good` -- or a stylesheet that quietly
resolves `--tone-warn` to `--tone-good` -- undoes the veto in the only place an operator
actually looks.

Everything is read as source text: `re` over the JS (its formatting is load-bearing, one
entry per line, so a line-by-line parse is the honest reader) and `ast` over
`api/src/handlers/*.py`, which must not be imported -- those modules build boto3 clients
at import and raise `NoRegionError`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, get_args

import pytest
from coding_agent.state import TERMINAL_STATUSES, TaskStatus
from conftest import REPO_ROOT

WEB = REPO_ROOT / "web"
STATUS_JS = WEB / "js/status.js"
TRANSITIONS_JS = WEB / "js/transitions.js"
STYLES_CSS = WEB / "styles.css"
CARDS_PY = REPO_ROOT / "api/src/handlers/cards.py"
RUNS_PY = REPO_ROOT / "api/src/handlers/runs.py"
WATCHDOG_PY = REPO_ROOT / "api/src/handlers/watchdog.py"

# One entry per line, `label` then `tone` then `terminal`. Anchored at exactly two
# spaces of indentation so a reformat that wraps an entry over two lines fails loudly
# here instead of silently shrinking the vocabulary this file believes in.
_ENTRY = re.compile(
    r'^\s{2}(?P<name>[A-Z][A-Z_]*):\s*\{\s*label:\s*"(?P<label>[^"]+)",'
    r'\s*tone:\s*"(?P<tone>[a-z]+)",\s*terminal:\s*(?P<terminal>true|false)\s*\},\s*$'
)
# `export const NAME = ["A", "B"];` -- the tone list and the four column tuples.
_JS_ARRAY = re.compile(
    r"^export const (?P<name>[A-Z][A-Z_]*) = \[(?P<items>[^\]]*)\];", re.MULTILINE
)
_JS_STRING = re.compile(r'"([^"]*)"')

# The four tuples `cards.py` owns, and the FOLLOW-UP it leaves for this test: JavaScript
# cannot import a Python tuple, so the agreement is held from here.
_COLUMN_TUPLES = ("BOARD_COLUMNS", "STORED_COLUMNS", "DERIVED_COLUMNS", "MOVE_TARGETS")


def _js_entries() -> dict[str, dict[str, Any]]:
    """`status.js`'s STATUS block, parsed line by line, in file order."""
    entries: dict[str, dict[str, Any]] = {}
    for line in STATUS_JS.read_text().splitlines():
        match = _ENTRY.match(line)
        if match:
            entries[match["name"]] = {
                "label": match["label"],
                "tone": match["tone"],
                "terminal": match["terminal"] == "true",
            }
    return entries


def _js_arrays(path: Path) -> dict[str, list[str]]:
    """Every `export const NAME = [...]` of strings in a module."""
    return {
        match["name"]: _JS_STRING.findall(match["items"])
        for match in _JS_ARRAY.finditer(path.read_text())
    }


def _python_names(path: Path) -> dict[str, Any]:
    """Module-level constants of a handler module, without importing it.

    `ast.literal_eval` alone is not enough: `cards.py` writes
    `BACKLOG, ACTIVE, DONE = STORED_COLUMNS` and then builds `BOARD_COLUMNS` out of
    those names, which is the right way to write it and unreadable to a literal parser.
    So names resolve against what has already been assigned above them.
    """
    env: dict[str, Any] = {}

    def value(node: ast.expr) -> Any:
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.Tuple):
            return tuple(value(element) for element in node.elts)
        # `watchdog.RESUMABLE_STATUSES` and `runs.TERMINAL_STATUSES` are
        # `frozenset({...})`, a call `ast.literal_eval` rejects -- so the loop's
        # `except ValueError` was silently skipping exactly the constants this file
        # needs to read, and a skipped constant is a `KeyError` in the test that reads
        # it rather than a false pass.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("frozenset", "set")
            and len(node.args) == 1
        ):
            return frozenset(value(node.args[0]))
        return ast.literal_eval(node)

    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        try:
            resolved = value(node.value)
        except (KeyError, ValueError):
            continue  # a call, an f-string, a comprehension: not a constant we read
        if isinstance(target, ast.Name):
            env[target.id] = resolved
        elif isinstance(target, ast.Tuple) and isinstance(resolved, tuple):
            for element, item in zip(target.elts, resolved, strict=True):
                if isinstance(element, ast.Name):
                    env[element.id] = item
    return env


def _css_rules() -> list[tuple[str, str]]:
    """`(selector, body)` for every innermost rule in the stylesheet.

    Comments are stripped first, and that matters: this stylesheet's own header explains
    the two tone rules in prose, so a reader that kept comments would find `.tone-warn`
    described and count it as styled.
    """
    css = re.sub(r"/\*.*?\*/", "", STYLES_CSS.read_text(), flags=re.DOTALL)
    return [(match[1].strip(), match[2]) for match in re.finditer(r"([^{}]*)\{([^{}]*)\}", css)]


def test_the_status_block_lists_every_container_status_in_order() -> None:
    names = list(_js_entries())
    assert names == list(get_args(TaskStatus)), (
        "web/js/status.js and coding_agent.state.TaskStatus have drifted. The agent "
        "writes a status this console has never heard of, and nothing fails -- the chip "
        "just renders a raw name and the board's column projection degrades to PLANNING. "
        "Same members and the same order: the order is the board's reading order and the "
        f"parse below is line by line. Console has {names}."
    )


def test_the_status_block_parsed_at_all() -> None:
    """A reformat that breaks `_ENTRY` would empty every other test in this file."""
    entries = _js_entries()
    assert len(entries) == len(get_args(TaskStatus)), (
        "no entry in web/js/status.js matched the line-by-line parse. The formatting of "
        "that block is load-bearing -- one entry per line, `label` then `tone` then "
        "`terminal` -- because a JS object cannot be imported from Python. A wrapped "
        "line here makes every vocabulary assertion vacuous rather than failing."
    )


def test_terminal_flags_agree_with_the_containers_terminal_set() -> None:
    for name, entry in _js_entries().items():
        assert entry["terminal"] == (name in TERMINAL_STATUSES), (
            f"web/js/status.js calls {name} terminal={entry['terminal']}, the container "
            f"disagrees. `terminal` is the stop condition for the activity pulse and the "
            f"run poller: wrong in one direction the console polls a finished run for "
            f"ever, wrong in the other it goes quiet mid-run and the operator watches a "
            f"frozen feed."
        )


def test_succeeded_unverified_renders_warn_and_only_succeeded_renders_good() -> None:
    entries = _js_entries()
    assert entries["SUCCEEDED_UNVERIFIED"]["tone"] == "warn", (
        "invariant 6: the verification veto is ONE-WAY. SUCCEEDED_UNVERIFIED is a draft "
        "PR that nobody verified, and a console that paints it `good` undoes the veto in "
        "the one place an operator actually looks -- the review it was meant to force "
        "never happens."
    )
    assert entries["SUCCEEDED_UNVERIFIED"]["label"] == "Draft, not verified", (
        "the label carries the meaning for anyone who cannot see the tone -- colour is "
        "never the only signal. 'Unverified' as a bare adjective reads as a detail; "
        "'Draft, not verified' reads as the state it is."
    )
    good = [name for name, entry in entries.items() if entry["tone"] == "good"]
    assert good == ["SUCCEEDED"], (
        f"only SUCCEEDED may render `good`; {good} do. Every other terminal status is a "
        f"draft, a veto or a failure, and green is the console's single claim that a run "
        f"needs no further judgement."
    )


def test_every_tone_used_is_declared_and_styled() -> None:
    tones = _js_arrays(STATUS_JS)["TONES"]
    used = {entry["tone"] for entry in _js_entries().values()}
    assert used <= set(tones), (
        f"web/js/status.js uses tone(s) {sorted(used - set(tones))} that TONES does not "
        f"declare; `ui.chip` emits `.tone-<name>` and an undeclared tone is an unstyled "
        f"chip -- text with no state on it at all."
    )
    rules = _css_rules()
    for tone in tones:
        # `(?![\w-])` so `.tone-warning` is not read as a rule for `warn`.
        used = re.compile(rf"\.tone-{tone}(?![\w-])")
        painted = [
            selector
            for selector, body in rules
            if used.search(selector) and ("color:" in body or "background:" in body)
        ]
        assert painted, (
            f"web/styles.css has no `.tone-{tone}` rule that paints anything. The chip "
            f"renders, carries the class, and looks identical to every other state: "
            f"nothing fails, and the board stops distinguishing a failure from a success."
        )


def test_the_good_and_warn_palettes_are_different_colours() -> None:
    css = STYLES_CSS.read_text()
    good = set(re.findall(r"--tone-good:\s*([^;]+);", css))
    warn = set(re.findall(r"--tone-warn:\s*([^;]+);", css))
    assert good and warn, "web/styles.css defines no --tone-good/--tone-warn custom property"
    assert not (good & warn), (
        f"--tone-good and --tone-warn resolve to the same colour ({sorted(good & warn)}). "
        f"invariant 6 again, one layer down: the tone is asserted in status.js and then "
        f"undone by a stylesheet, and a draft PR looks green anyway. Every colour scheme "
        f"in this file has to keep them apart, dark mode included."
    )
    for selector, body in _css_rules():
        if ".tone-warn" in selector:
            assert "var(--tone-good)" not in body, (
                f"`{selector}` paints a warn tone with var(--tone-good). See above: the "
                f"veto is one-way and this is the cheapest way to hide that."
            )


def test_the_api_status_tuple_is_the_console_vocabulary() -> None:
    """Equality, both directions and in order -- was
    `test_the_api_status_tuple_is_a_subset_of_the_console_vocabulary`, one-directional only
    because `runs.TASK_STATUSES` was itself missing FAILED_VERIFICATION and CANCELLED. It is
    not any more, so all three copies (the container's `TaskStatus`, `web/js/status.js` and
    `runs.TASK_STATUSES`) are now pinned to one list in one order: a status the API invents
    and the console cannot draw fails here, and so does a status the container writes and the
    API has never heard of -- which is the direction that left a finished run billing a
    session nobody stopped.
    """
    api_statuses = _python_names(RUNS_PY)["TASK_STATUSES"]
    console = list(_js_entries())
    assert list(api_statuses) == console, (
        f"api/src/handlers/runs.py and web/js/status.js have drifted. Only in runs.py: "
        f"{sorted(set(api_statuses) - set(console))}; only in the console: "
        f"{sorted(set(console) - set(api_statuses))}. Order matters too -- the console's "
        f"order is the container's, and `runs.TASK_STATUSES` is pinned to the same list so "
        f"the three copies can be read side by side. Read by `ast`, never imported: those "
        f"modules build boto3 clients at import and raise NoRegionError."
    )


def test_every_non_terminal_container_status_is_resumable_by_the_watchdog() -> None:
    """The one direction of invariant 13 that nothing held.

    `watchdog.RESUMABLE_STATUSES`' only other authority is `runs.TASK_STATUSES`
    (`tests/test_api_agentcore_figures.py::test_the_watchdog_and_the_task_vocabulary_cannot_drift`
    pins it equal to the non-terminal half of that tuple), and `runs.TASK_STATUSES` is now
    pinned equal to the console vocabulary above -- so this test is what independently pins
    `RESUMABLE_STATUSES` to the container's own non-terminal half, without going through
    either. Otherwise a new node status in `coding_agent.state.TaskStatus` reaches the
    console and the graph under existing tests and reaches the watchdog under none of them:
    the sweep's filter never names it, a live run looks unknown rather than resumable, and
    nothing ever rescues it.
    """
    resumable = set(_python_names(WATCHDOG_PY)["RESUMABLE_STATUSES"])
    live = set(get_args(TaskStatus)) - TERMINAL_STATUSES
    assert resumable == live, (
        f"api/src/handlers/watchdog.py RESUMABLE_STATUSES and the container's "
        f"non-terminal statuses have drifted. Missing from RESUMABLE_STATUSES: "
        f"{sorted(live - resumable)} -- the watchdog's scan filter never matches a run "
        f"in that state, so a quiet or cancelled one is never resumed or stopped. "
        f"Resumable but unknown to the container: {sorted(resumable - live)} -- either a "
        f"status the agent no longer writes, or a terminal one the sweep would resume "
        f"for ever. Read by `ast`, never imported: that module builds boto3 clients at "
        f"import and raises NoRegionError."
    )


@pytest.mark.parametrize("name", _COLUMN_TUPLES)
def test_the_consoles_column_tuples_are_the_servers(name: str) -> None:
    """`cards.py` asks for exactly this ("FOLLOW-UP (phase 10)"), and a test is the only
    way to discharge it: JavaScript cannot import a Python tuple."""
    server = _python_names(CARDS_PY)[name]
    client = _js_arrays(TRANSITIONS_JS)[name]
    assert client == list(server), (
        f"web/js/transitions.js {name} = {client}, api/src/handlers/cards.py has "
        f"{list(server)}. Order matters as much as membership: BOARD_COLUMNS is the "
        f"board's left-to-right layout, and a column added on the server and forgotten "
        f"here is a card that renders in no column at all -- it vanishes off the board, "
        f"and nothing raises."
    )
