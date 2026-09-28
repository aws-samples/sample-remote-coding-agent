"""Contracts on the three documents: `README.md`'s size and shape, where the
reference material lives, and whether the documents still describe what shipped.

A separate file rather than one more test in `tests/test_repo_contracts.py`, whose
docstring enumerates exactly six contracts about the repository's own build files --
these are about prose, and nothing here reads the Makefile, a manifest or the image.

The shape is the contract because prose has no compiler. A README grows into a reference
manual -- an API surface, a model-routing table, board semantics, observability internals --
one reasonable-looking paragraph at a time, and nobody reads it after that. `CLAUDE.md` is where reference goes now, so a
README that grows is content that has landed in the wrong file, and only a test notices.
The table of AWS resources a `cdk destroy` deliberately retains moved out of the README for
exactly that reason and lives in `CLAUDE.md` now, under "What deliberately survives a
destroy"; the walkthrough's teardown step points at it.

Prose has no compiler for its *claims* either, which is what the second half of this file
is: a rename leaves behind a sentence that still reads as current and sends a reader to
code that is not there. Four shapes are recognised, and they are deliberately narrow --
a backticked path (`agent/src/coding_agent/nodes/code.py`), a path and a symbol joined by
`::`, a possessive (`nodes/code.py`'s `GOAL_CONDITION`), and a dotted `module.SYMBOL`.
Everything else is IGNORED rather than guessed at, because a check that fires on prose it
could not parse gets deleted by the next person who trips over it -- so `os.environ`,
`AWS::DynamoDB::GlobalTable`, `~/.rca/endpoint.json`, `github.com` and `conftest.py` are
all silently dropped by the rules in `_is_repo_path`, `_NOT_SYMBOLS` and the
one-candidate-only rule in `_claims`. The cost of that narrowness is real coverage --
`server._REQUIRED` and `activity.SUBAGENT_TOOL_NAMES` name ambiguous stems and go
unchecked -- so this is a floor on the documents' accuracy, not a proof of it.

Symbols are resolved by READING the named file's source text, never by importing it:
anything under `api/src/handlers/` builds boto3 clients at import and raises
`NoRegionError`, and three of the `::` claims point into that package.
"""

from __future__ import annotations

import ast
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

# `infra/app.py` owns the five stack names, and `pytest.ini` puts `infra` on the path, which is
# how `tests/test_layout_infra.py` imports it too. Imported rather than retyped here on purpose:
# the teardown step below has to name the stacks this app really composes, and a literal list of
# names in a test rots exactly the way the prose it is policing did.
import app
import pytest
from conftest import REPO_ROOT

README = REPO_ROOT / "README.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# ~180 lines is the target; 200 is the ceiling, so a step gained or a gap named does not
# fail the suite but a section does.
_README_MAX_LINES = 200

# Required navigation, not an exact editorial outline. Keep the entry page short.
_README_HEADINGS = ["## Quick start", "## Architecture", "## Costs and cleanup"]
_README_TRAILING_HEADINGS = ["## Security", "## License"]

# The two CDK enum classes `infra/stacks/agent_stack.py` names its endpoints through. Read out of
# the source as an AST walk and not as a regex, because `BEDROCK_AGENTCORE_GATEWAY` appears there
# only in a comment saying nothing speaks to it, and a regex would demand a word in the diagram for
# an endpoint the stack does not create.
_ENDPOINT_SERVICE_CLASSES = frozenset(
    {"InterfaceVpcEndpointAwsService", "GatewayVpcEndpointAwsService"}
)

# Enum member -> the word `README.md`'s diagram must show for it. A mapping and not a literal list
# because the diagram is prose: `ECR` and `ECR_DOCKER` are both "ECR" to a reader, and
# `BEDROCK_RUNTIME` is "Bedrock". An unmapped member is a failure and not a skip -- an endpoint
# added to the stack with no word here is exactly the staleness this test exists for, and it was
# real once: the AgentCore data-plane endpoint shipped and the diagram never gained it.
_ENDPOINT_DIAGRAM_WORDS = {
    "BEDROCK_RUNTIME": "Bedrock",
    "BEDROCK_AGENTCORE": "AgentCore",
    "SECRETS_MANAGER": "Secrets",
    "ECR": "ECR",
    "ECR_DOCKER": "ECR",
    "CLOUDWATCH_LOGS": "Logs",
    "DYNAMODB": "DynamoDB",
    "S3": "S3",
}

# The claim three documents carried for the whole life of the route that refutes it: a document
# that denies a shipped feature is worse than one that is silent, because a reader who trusts it
# concludes the code is the mistake.
_CANCELLATION_DISCLAIMED = "no cancellation api"

# The two labels the container cites by name -- `D6` is the `actorId`-is-the-configuration /
# `sessionId`-is-the-task keying rule, `D11` is degrade-to-a-warning-and-an-empty-result -- and
# that no document defined. They are written as bare references in `agentcore_memory.py`,
# `graph.py` and `memory_tools.py`, so a comment reading "(D6)" resolves nowhere unless a
# document spells the label out.
_MEMORY_LABELS = ("D6", "D11")

# The heading PREFIX `CLAUDE.md`'s reference section for memory is found by, not the whole
# heading: the rest of the line is free to be reworded. Two tests below slice on it, so renaming
# this prefix breaks both -- which is why each of their failure messages repeats it.
_MEMORY_SECTION_PREFIX = "## AgentCore Memory"

# The two namespace templates, verbatim, and both are cross-checked against
# `agent/src/coding_agent/agentcore_memory.py` before they are demanded of the prose -- so a
# template renamed in code fails here rather than leaving the document quietly describing a path
# nothing writes to. Retyped and not imported for the reason the module docstring gives: importing
# that module is not the hazard, but `api/src/handlers/provisioner.py` -- the other half of the
# same contract -- builds boto3 clients at import and raises `NoRegionError`.
_MEMORY_NAMESPACES = ("/rca/{actorId}/episodic", "/rca/{actorId}/session/{sessionId}")

# The two `memoryStrategies` names, cross-checked against the provisioner's source the same way.
# A reader who knows only that there are "two strategies" cannot find either one in the console.
_MEMORY_STRATEGIES = ("repository_episodic", "session_summary")

# The two tool names and the wildcard `subagents.build_options` adds to every role's tools, all
# three cross-checked against `agent/src/coding_agent/memory_tools.py`. The wildcard is included
# because it is the spelling an operator sees in a `GetAgentRuntime` response and in a role's
# allowlist, and neither tool name appears there.
_MEMORY_TOOL_NAMES = ("memory_search", "memory_record", "mcp__memory__*")

# The two numeric defaults a reader cannot guess and nothing else writes down for them: the
# per-node note cap (`graph._MEMORY_TEXT_LIMIT`) and the event expiry in days
# (`provisioner._MEMORY_EVENT_EXPIRY_DAYS`). Digits and not names, because the name is only useful
# to somebody already reading the code.
_MEMORY_NUMBERS = ("500", "90")

# How the IAM contract is derived rather than retyped: each `sid=` slice of
# `infra/stacks/agent_stack.py`, regexed for action names and then narrowed to the memory ones.
# `Memor`, not `Memory`, because `ListMemoryRecords` and `BatchDeleteMemoryRecords` are memory
# actions too; `Event` because `CreateEvent`, `ListEvents`, `GetEvent` and `DeleteEvent` carry no
# `Memor` at all.
_AGENTCORE_ACTION_RE = re.compile(r"bedrock-agentcore:([A-Za-z]+)")
_MEMORY_ACTION_MARKERS = ("Memor", "Event")

# The two statement Sids, and the sizes they hold today. The sizes are asserted so that a seventh
# granted or an eighth denied action is a failure a human reads rather than a silently smaller
# expectation: this test can only demand of the prose what it derived, so a derivation that
# quietly shrinks is the one way it could pass while covering less.
_MEMORY_GRANT_SID = "MayUseOwnMemory"
_MEMORY_DENY_SID = "MayNotEscalate"
_MEMORY_GRANTED_ACTIONS = 6
_MEMORY_DENIED_ACTIONS = 7

# The subsection the auto-memory bullet lives in, and the variable that names it. Sliced from this
# `###` to the next one because the bullet's argument -- that memory shared across sessions is a
# cross-tenant hazard -- is what has to be reconciled, and the reconciliation is only useful
# standing next to it.
_AUTO_MEMORY_SUBSECTION = "### What makes it enforced rather than asserted"
_AUTO_MEMORY_VARIABLE = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"

# Any one of these is enough to say the two mechanisms are NOT the same thing. A word list and not
# a sentence match, for `_PRECEDENCE_WORDS`' reason: the prose is free to be rewritten and only
# the distinction is the contract.
_DISTINCTION_WORDS = ("different", "distinct", "not the same", "another mechanism")

# Invariant 10b reads "No MCP tool may call an AWS API" and the `memory` server ships two that do.
# Any one of these phrases is the qualification: what makes the exception mechanical is that the
# model cannot name the memory, because the ids are closed over and no parameter exists for them.
_QUALIFICATION_WORDS = ("cannot name", "closed over", "no parameter")
_INVARIANT_10B = "10b."
_INVARIANT_10C = "10c."

# The walkthrough section, sliced by this heading rather than by line number, is where the two
# claims below live. Both were sentences that read as finished and were not, and a clean-clone
# walkthrough paid for both: the plugin silently registered a repository into an older
# deployment in another region because a leftover `RCA_API_URL` outranked discovery, and a
# teardown step that said `destroy --all` would have deleted a shared account stack out from
# under a live console in another region.
_WALKTHROUGH_HEADING = "## Quick start"

# The three optional `userConfig` fields of `plugins/sample-rca/.claude-plugin/plugin.json`
# reach the server as these three variables (`plugins/sample-rca/.mcp.json`), and
# `mcp/src/rca_mcp/client.py::resolve_endpoint` gives `RCA_API_URL` precedence over both the
# `~/.rca/endpoint.json` cache and stack discovery.
_ENDPOINT_OVERRIDES = ("RCA_API_URL", "AWS_REGION", "RCA_STACK_NAME")

# Any one of these words, in the step that names the overrides, is enough to say an override
# BEATS discovery rather than merely existing. A word list and not a sentence match: the prose is
# free to be rewritten, and only the direction of precedence is the contract.
_PRECEDENCE_WORDS = ("outrank", "precedence", "beat", "override", "win", "ahead of")

# The three Makefile targets `README.md`'s deploy step fences, in the order it runs them.
# Repeated here rather than read out of the Makefile, because this file polices prose and nothing
# in it opens a build file; the closed set the Makefile declares is pinned by
# `tests/test_repo_contracts.py::test_the_makefile_declares_exactly_the_five_targets`, which is
# where that guard moved when the plugin stopped wrapping these targets in a skill.
_DEPLOY_STEP_TARGETS = ("make setup", "make preflight", "make deploy")

# `CLAUDE.md` carries 35 invariants, several with sub-numbered entries (6b, 10c, 32b), so
# the floor is 35 rather than an exact count -- the point is that they are all here, not
# how they are numbered.
_MINIMUM_INVARIANTS = 35

# The directories a documented path can live in. Several of them carry generated or
# vendored trees on a developer machine that are not this repository's code; `_index`
# excludes those by name.
_SEARCH_DIRS = ("agent/src", "api/src", "mcp/src", "infra", "tests", "web", "plugins")

# Directory names `_index` refuses to walk into, and the reason the list has to be this
# long: `_resolve` matches a documented shorthand by UNIQUE path suffix, so a generated
# *copy of first-party source* is the one thing that turns a correct claim into a failing
# one. `make deploy` writes the agent's own `src/coding_agent/` tree into
# `infra/cdk.out/asset.<hex>/`, and with those copies indexed there are three
# `nodes/code.py` files, the suffix stops being unique, `_resolve` returns `None`, and both
# document tests fail as though the file had been renamed or deleted -- on a machine where
# the prose and the tree are both correct. Every name here is one `.gitignore` already
# carries, so nothing tracked can be swept up and no real regression can hide behind the
# exclusion. `tests/test_plugin_manifest.py`'s `_SKIP` carries the same list for the same
# reason; the duplication is deliberate, as `tests/` has no shared tree-walking helper.
# `.git` is absent because it cannot appear under `_SEARCH_DIRS`.
_EXCLUDED_DIRS = frozenset(
    {
        "node_modules",
        "cdk.out",
        "__pycache__",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        ".agent",
        ".agent.repo",
    }
)

# `.gitignore` writes `*.egg-info/` -- a glob, not a name -- so it needs a suffix test
# rather than set membership. Not hypothetical: this repository's own documented setup
# (`pip install -e agent -e mcp`) writes `agent/src/coding_agent.egg-info/` and
# `mcp/src/rca_mcp.egg-info/` directly under two `_SEARCH_DIRS` entries.
_EXCLUDED_DIR_SUFFIX = ".egg-info"

# What makes a backticked token containing a `/` a path claim rather than a URL fragment,
# an ARN or a shell snippet: a last segment ending in one of these.
_PATH_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".json",
        ".md",
        ".ts",
        ".yml",
        ".yaml",
        ".toml",
        ".ini",
        ".sh",
        ".html",
        ".css",
        ".txt",
        ".cfg",
        ".mjs",
    }
)

# Two claims assert a file's ABSENCE -- "there is no `mcp/__init__.py`", "there is no
# `infra/stacks/__init__.py`" -- and are true precisely because the path does not exist,
# so an exists-check fails on prose that is correct. The exemption is itself a contract:
# `test_every_path_a_document_names_exists` asserts both really are missing, so this set
# cannot rot into a place to hide a path that got deleted by accident.
_ABSENT_ON_PURPOSE = frozenset({"mcp/__init__.py", "infra/stacks/__init__.py"})

# `github.com` parses as module `github` (there is a real `github.py`) with symbol `com`.
# Four TLDs is the whole denylist these two documents need.
_NOT_SYMBOLS = frozenset({"com", "org", "net", "io"})

# The root files no `_SEARCH_DIRS` walk reaches. They are indexed as basenames only, so a
# dotted token like `CLAUDE.md` or `ruff.toml` is recognised as a filename and not read as
# module `PLAN` with symbol `md`.
_ROOT_FILES = (
    "README.md",
    "CLAUDE.md",
    "Makefile",
    "pytest.ini",
    "ruff.toml",
    "requirements-dev.txt",
)

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

# Every backticked span. Narrowed to path claims by `_is_repo_path`.
_PATH_RE = re.compile(r"`([^`\n]+)`")
# `api/src/handlers/configurations.py::_require_supported_token`.
_COLON_RE = re.compile(rf"`([^`\n]+?)::({_IDENT})`")
# `nodes/code.py`'s `GOAL_CONDITION`.
_POSSESSIVE_RE = re.compile(rf"`([^`\n]+?)`'s\s+`({_IDENT})`")
# `store._ddb_safe`, `graph._instrument`, `git_ops.has_origin_head`.
_DOTTED_RE = re.compile(rf"`({_IDENT})\.({_IDENT})`")


def _is_generated(relative: Path) -> bool:
    """Whether a REPO_ROOT-RELATIVE path sits inside a generated or gitignored directory.

    Relative and not absolute on purpose: an absolute path carries the checkout prefix, so
    a repository living under a directory named `cdk.out` or `.agent` -- and `.agent/` is
    exactly where this agent's own working directory lives -- would match on the prefix and
    exclude the entire tree, leaving every document test passing vacuously.

    Only the PARENT parts are tested, so a *file* named `something.egg-info` is not swept up
    by the suffix rule. Directories are what is being excluded.
    """
    return any(
        part in _EXCLUDED_DIRS or part.endswith(_EXCLUDED_DIR_SUFFIX)
        for part in relative.parent.parts
    )


def _source(relative: str) -> str:
    """The source text of a first-party file, read and never imported.

    The same rule the symbol resolution below follows, for the same reason: the checks that
    derive their expectations from code reach into `api/src/handlers/` and `infra/stacks/`,
    and both build boto3 clients or CDK constructs at import time.
    """
    return (REPO_ROOT / relative).read_text()


def _section(text: str, prefix: str) -> str:
    """A document section, from the line starting with `prefix` to the next heading as deep.

    `""` when the heading is absent, so a caller can say what a missing section means rather
    than failing on an `IndexError`. Depth is taken from the prefix itself: `## Known gaps`
    ends at the next `## ` and is not cut short by a `### ` inside it, which is how
    `_walkthrough` already slices and why this reads headings rather than line numbers.
    """
    marker = prefix.split(" ", 1)[0] + " "
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith(prefix)), None)
    if start is None:
        return ""
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith(marker)),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _memory_actions(source: str, sid: str) -> set[str]:
    """The memory-related `bedrock-agentcore:` actions one `sid=`-named IAM statement carries.

    Derived from `infra/stacks/agent_stack.py`'s own source rather than retyped, which is the
    whole point of the check it feeds: a list of action names in a test rots exactly the way the
    prose it polices does. The slice runs from this statement's `sid=` to the next one, or to the
    end of the file for the last statement in the list -- which is where `MayNotEscalate` sits, so
    that arm is exercised on every run and not only after a reorder.

    Comments inside the slice are read too, deliberately: they are how the file explains why a
    name is present or absent, and every occurrence there is spelled `bedrock-agentcore:` exactly
    as the action list is. What that costs is nothing measurable -- the names in those comments are
    the names in the list beside them -- and what it buys is that a name moved into a comment
    while an edit is in progress does not silently shrink the expectation.
    """
    start = source.index(f'sid="{sid}"')
    following = source.find('sid="', start + 1)
    statement = source[start : following if following != -1 else len(source)]
    return {
        action
        for action in _AGENTCORE_ACTION_RE.findall(statement)
        if any(marker in action for marker in _MEMORY_ACTION_MARKERS)
    }


def _index() -> tuple[list[str], set[str], dict[str, list[str]]]:
    """Index the tree once: source files, every real basename, and stem -> candidates.

    `by_stem` is what makes the dotted shape safe. A dotted token is only read as a claim
    when its left half names exactly one first-party source file, which drops
    `RemovalPolicy.RETAIN`, `os.environ`, `safe.directory` and `scripts.setup` for free --
    they have no module here -- and drops `server.*`, `activity.*` and `app.*`, whose
    stems each name two or more files, because a guess that fires wrongly costs more than
    a claim that goes unchecked.

    Generated and gitignored directories are excluded by `_is_generated`, because a
    generated copy of first-party source is what makes a unique path suffix ambiguous: with
    `infra/cdk.out/asset.<hex>/src/coding_agent/` indexed, the documented `nodes/code.py`
    matches three files and resolves to none of them.
    """
    source_files: list[str] = []
    basenames: set[str] = set(_ROOT_FILES)
    by_stem: dict[str, list[str]] = {}
    for search_dir in _SEARCH_DIRS:
        for path in sorted((REPO_ROOT / search_dir).rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(REPO_ROOT)
            # Above `basenames.add` so `by_stem` is cleaned too, not just `source_files`:
            # its "exactly one file with this stem" rule breaks on the generated copies the
            # same way `_resolve` does, and it fails by silently SKIPPING a dotted claim,
            # which is the quieter version of the same bug.
            if _is_generated(relative):
                continue
            basenames.add(path.name)
            if path.suffix not in {".py", ".js"}:
                continue
            rel = relative.as_posix()
            source_files.append(rel)
            by_stem.setdefault(path.stem, []).append(rel)
    return source_files, basenames, by_stem


_SOURCE_FILES, _BASENAMES, _BY_STEM = _index()


def _is_repo_path(token: str) -> bool:
    # A token starting with `/` is the one that bites: `REPO_ROOT / "/root/.claude/
    # settings.json"` is an ABSOLUTE path, and stat-ing it raised `PermissionError` while
    # this check was being written. `~` is a home path, `http` a URL, a space means the
    # token is a command line or an expression, and no `/` at all means it is not a path
    # claim -- a bare `code.py` is prose about a file, not a pointer to one.
    if token.startswith(("/", "~", "http")) or " " in token or "/" not in token:
        return False
    return Path(token).suffix in _PATH_EXTENSIONS


def _resolve(token: str, source_files: list[str]) -> str | None:
    """The repo-relative path a claim names, or `None` when nothing matches uniquely.

    `CLAUDE.md` writes `nodes/code.py` for a file that lives under
    `agent/src/coding_agent/`, and the shorthand is how those documents are written, so a
    token that does not exist at the root is resolved by unique path-suffix match.
    Ambiguous suffixes resolve to nothing and fail, rather than picking one.
    """
    if (REPO_ROOT / token).exists():
        return token
    matches = [rel for rel in source_files if rel.endswith("/" + token)]
    return matches[0] if len(matches) == 1 else None


def _symbols(rel: str) -> set[str] | None:
    """Every name a Python file defines, assigns, imports or reaches for as an attribute.

    `None` for anything that is not Python, so the caller falls back to a substring test
    -- `web/js/*.js` has no parser here and does not need one. A `SyntaxError` is `None`
    too: this test exists to police prose, and failing it because a source file is mid-edit
    would blame the wrong file.
    """
    if not rel.endswith(".py"):
        return None
    try:
        tree = ast.parse((REPO_ROOT / rel).read_text())
    except SyntaxError:
        return None
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            # `store._ddb_safe` is documented from the caller's side, so the attribute
            # reached for counts as much as the name defined.
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
    return names


def _claims(text: str) -> list[tuple[str, int, str, str, str | None]]:
    """`(kind, line, claim, path_token, symbol)` for every claim one document makes.

    Order is by shape, not by position; the line number comes from counting newlines
    before the match so a failure names the line a reader can go and edit.
    """
    found: list[tuple[str, int, str, str, str | None]] = []

    def line_of(match: re.Match[str]) -> int:
        return text.count("\n", 0, match.start()) + 1

    for match in _PATH_RE.finditer(text):
        token = match.group(1)
        # `::` spans are the symbol shape below, and would resolve as a path here only by
        # accident.
        if "::" not in token and _is_repo_path(token):
            found.append(("path", line_of(match), match.group(0), token, None))

    for match in _COLON_RE.finditer(text):
        left, symbol = match.group(1), match.group(2)
        # `AWS::DynamoDB::GlobalTable` and `arn:aws:bedrock:*::foundation-model/...` both
        # contain `::` and neither names a file, so the left side has to look like a path.
        if _is_repo_path(left):
            found.append(("symbol", line_of(match), match.group(0), left, symbol))

    for match in _POSSESSIVE_RE.finditer(text):
        left, symbol = match.group(1), match.group(2)
        if _is_repo_path(left):
            found.append(("symbol", line_of(match), match.group(0), left, symbol))

    for match in _DOTTED_RE.finditer(text):
        module, symbol = match.group(1), match.group(2)
        # `conftest.py` is a filename, not module `conftest` with symbol `py`; `github.com`
        # is a hostname; and a module stem that names two files is ambiguous. All three are
        # skipped rather than guessed at.
        if match.group(1) + "." + match.group(2) in _BASENAMES:
            continue
        if symbol in _NOT_SYMBOLS or len(_BY_STEM.get(module, ())) != 1:
            continue
        found.append(("symbol", line_of(match), match.group(0), _BY_STEM[module][0], symbol))

    return found


def test_the_readme_stays_short() -> None:
    lines = README.read_text().splitlines()
    assert len(lines) <= _README_MAX_LINES, (
        f"README.md is {len(lines)} lines, over the {_README_MAX_LINES}-line ceiling. "
        "A README that grows into a reference manual stops being read; this one answers "
        "only what this is, how it is shaped, and how to run it "
        "from nothing to nothing. The reference -- the layout, the commands, the SDK "
        "probe results, the invariants, the boundary claim -- lives in `CLAUDE.md`, so a "
        "README that grows is almost always content that belongs in the other file."
    )


def test_the_readme_has_navigation_and_media() -> None:
    text = README.read_text()
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert all(heading in headings for heading in _README_HEADINGS + _README_TRAILING_HEADINGS)
    assert text.index("assets/console-preview.gif") < text.index("## Quick start")
    assert "not a live AWS run" in text
    assert "not an enforced domain allowlist" in text
    for path in (
        "assets/architecture.drawio",
        "assets/architecture.png",
        "assets/console-preview.gif",
        "assets/console-preview.png",
    ):
        assert path in text
        assert (REPO_ROOT / path).is_file()
    assert headings[-2:] == _README_TRAILING_HEADINGS
    cleanup = _section(text, "## Costs and cleanup")
    assert cleanup.index("Deregister") < cleanup.index("destroy")
    assert "draft" in text.lower() and "verification command" in text
    for document in (README, REPO_ROOT / "assets/README.md"):
        for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
            if "://" in target:
                continue
            path, _, anchor = target.partition("#")
            destination = document.parent / path if path else document
            assert destination.is_file(), target
            if anchor:
                headings = re.findall(r"^#+ (.+)$", destination.read_text(), re.MULTILINE)
                slugs = {
                    re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                    for heading in headings
                }
                assert anchor in slugs, target


def test_every_document_names_agentcore_memory() -> None:
    """All three documents name the one feature that shipped without any of them saying so.

    This is the check that would have caught the original bug, and it is deliberately the crudest
    one in this file: `README.md` contained the word "memory" **zero times** while every
    registered repository was being given an AgentCore Memory resource, `CLAUDE.md` mentioned only
    `CLAUDE_CODE_DISABLE_AUTO_MEMORY` -- Claude Code's own auto-memory, a different feature -- and
    and the documents listed Memory among the surfaces the repository does not use. A feature no document
    mentions is invisible to the next reader, who then designs around its absence.
    """
    silent = [
        document.name
        for document in (README, CLAUDE_MD)
        if "agentcore memory" not in document.read_text().lower()
    ]
    assert not silent, (
        "these documents never name AgentCore Memory: " + ", ".join(silent) + ". One memory "
        "resource per registered repository is created by "
        "`api/src/handlers/provisioner.py::_ensure_memory` and reached from the container by "
        "`agent/src/coding_agent/agentcore_memory.py`, and each document owes the reader a "
        "different depth of it: `README.md` two or three sentences plus a `## Known gaps` bullet, "
        f"`CLAUDE.md` the reference under {_MEMORY_SECTION_PREFIX!r}, and the "
        "reasoning. Write the words `AgentCore Memory` -- the string is what this checks, because "
        'a document that says only "memory" is ambiguous with the filesystem `.agent/memory/` '
        "digest and with Claude Code's auto-memory, and both of those already appear here."
    )


def test_the_readme_diagram_names_every_vpc_endpoint() -> None:
    """The diagram lists every VPC endpoint the stack creates, derived from the stack.

    It was stale for a whole feature: `infra/stacks/agent_stack.py` gained the
    `BEDROCK_AGENTCORE` interface endpoint -- the data plane a task's own transcript now goes
    to instead of the NAT gateway -- and the diagram, the only place the endpoint set is drawn
    for a reader, kept listing the six it had before. The expected set is walked out of the
    stack's own source so the next endpoint added there fails this until the picture catches up.
    """
    tree = ast.parse(_source("infra/stacks/agent_stack.py"))
    members = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr in _ENDPOINT_SERVICE_CLASSES
    }
    assert members, (
        "no `InterfaceVpcEndpointAwsService` / `GatewayVpcEndpointAwsService` member is reached "
        "in `infra/stacks/agent_stack.py`, so this test has nothing to compare the diagram "
        "against and passes vacuously. Either the endpoints moved to another stack -- point "
        "`_source` at it -- or they are named some other way now."
    )
    unmapped = sorted(member for member in members if member not in _ENDPOINT_DIAGRAM_WORDS)
    assert not unmapped, (
        "`infra/stacks/agent_stack.py` creates VPC endpoints this test has no diagram word "
        "for: " + ", ".join(unmapped) + ". That is deliberately a failure and not a skip: an "
        "endpoint the stack creates and the picture omits is exactly the staleness here. Add "
        "the endpoint to `README.md`'s diagram AND its member to `_ENDPOINT_DIAGRAM_WORDS`."
    )
    diagram = ET.parse(REPO_ROOT / "assets/architecture.drawio")
    endpoint_ids = {"bedrock", "memory", "secrets", "dynamodb", "ecr", "s3", "logs"}
    cells = {cell.get("id"): cell for cell in diagram.findall(".//mxCell")}
    for name in endpoint_ids:
        assert cells[name].get("parent") == "endpoints"
        assert "shape=" in cells[name].get("style", "")
    labels = " ".join(
        cell.get("value", "") for cell in cells.values() if cell.get("parent") == "endpoints"
    )
    assert all(_ENDPOINT_DIAGRAM_WORDS[member] in labels for member in members)
    assert all(name in cells for name in ("claude", "github", "runtime"))
    for cell in cells.values():
        if cell.get("edge") == "1":
            assert cell.get("source") in cells and cell.get("target") in cells
            assert cell.find("mxGeometry").get("relative") == "1"


def test_a_document_admits_what_is_unmeasured_about_memory() -> None:
    """Somewhere still says the memory feature is implemented and undemonstrated.

    This used to read `README.md`'s `## Known gaps`. That section was removed -- thirteen
    bullets of mostly "unmeasured" had stopped being specifics and become a disclaimer -- so the
    assertion moved to `CLAUDE.md`, beside `provisioner._ensure_memory`, which is the mechanism
    it qualifies. What is asserted did not change: a feature documented only in the register
    that describes what *works* reads as measured, and this one is not.

    Three things: no live run has shown a memory written by one task being recalled by another,
    the two strategies' extraction quality is a default nobody has judged, and so is the 90-day
    `eventExpiryDuration` -- a memory silently empties after a quarter and no run has lived long
    enough to see it.
    """
    text = CLAUDE_MD.read_text()
    assert re.search(r"unmeasured", text, re.IGNORECASE), (
        "`CLAUDE.md` no longer admits anything is unmeasured. The `## Known gaps` section that "
        "used to carry these admissions is gone, so this file is where they live now."
    )
    lowered = text.lower()
    assert "memory" in lowered and "recall" in lowered, (
        "`CLAUDE.md` does not admit that no live run has shown a memory written by one task "
        "being recalled by another. Each registered repository gets its own AgentCore Memory, "
        "the feature is implemented, and nothing has demonstrated it end to end. Say so next to "
        "`provisioner._ensure_memory`, and do not hedge it into sounding measured."
    )
    assert "90" in text, (
        "`CLAUDE.md` does not name the 90-day `eventExpiryDuration` "
        "(`api/src/handlers/provisioner.py::_ensure_memory` sets it, and nothing has tested "
        "it). The number is the gap: a reader who is not told it exists cannot know a memory "
        "silently empties after a quarter."
    )


def test_no_document_still_claims_there_is_no_cancellation_api() -> None:
    """No document lists a cancellation API among the things this repository does not have.

    Both documents carried it once. A document that asserts the opposite of the tree is worse
    than a silent one -- the next reader concludes the route is the mistake and deletes it, or
    spends an afternoon looking for the design that says why an operator cannot stop a run they
    started by mistake.
    """
    claiming = [
        document.name
        for document in (README, CLAUDE_MD)
        if _CANCELLATION_DISCLAIMED in document.read_text().lower()
    ]
    assert not claiming, (
        f"these documents still say {_CANCELLATION_DISCLAIMED!r}: " + ", ".join(claiming) + ". "
        "`POST /tasks/{taskId}/cancel` shipped -- `api/src/handlers/tasks.py::cancel` writes "
        "`cancelledAt` and then stops the AgentCore session through `watchdog.stop_session`, and "
        "the watchdog resolves the marked task on its next tick (invariant 28). Rewrite the "
        "sentence rather than deleting it where the surrounding clauses are still true."
    )


def _memory_section() -> str:
    """`CLAUDE.md`'s memory reference section, found by heading prefix and never by line number.

    The prefix is fixed and the rest of the heading is free, so the section can be renamed to say
    more without breaking the two tests that read it. `""` when it is absent, which each caller
    reports in its own words.
    """
    return _section(CLAUDE_MD.read_text(), _MEMORY_SECTION_PREFIX)


def test_claude_md_carries_the_memory_keying_rule() -> None:
    """The reference section states the one thing about memory that cannot be inferred from the API.

    The keying rule is it: `actorId` is the configuration id and `sessionId` is the task id, which
    is what makes `/rca/<configId>/episodic` **one** namespace shared by every task the repository
    ever runs. Get it backwards and nothing raises -- both templates still resolve, to somewhere no
    reader looks -- so a document is the only place the pairing is checkable at all. Everything
    else asserted here is a literal a reader cannot guess and that a rename in code would silently
    invalidate, which is why each one is cross-checked against the source that owns it.
    """
    section = _memory_section()
    assert section, (
        f"`CLAUDE.md` has no section whose heading starts with {_MEMORY_SECTION_PREFIX!r}. That "
        "exact prefix is how this test and "
        "`test_claude_md_names_every_memory_action_the_boundary_moves` both find it -- the rest of "
        "the heading is free. `CLAUDE.md` is where reference goes (see "
        "`test_the_readme_stays_short`'s message), so the memory design belongs here at reference "
        "depth and in `README.md` only as two or three sentences."
    )

    memory_source = _source("agent/src/coding_agent/agentcore_memory.py")
    for template in _MEMORY_NAMESPACES:
        assert template in memory_source, (
            f"`agentcore_memory.py` no longer spells the namespace template `{template}`, so this "
            "check is holding `CLAUDE.md` to a path the code has stopped using. Update "
            "`_MEMORY_NAMESPACES` and the section together -- and check "
            "`api/src/handlers/provisioner.py`, which configures the same two strings on the "
            "memory resource and whose copy is the one that decides what is actually written."
        )
        assert template in section, (
            f"`CLAUDE.md`'s memory section does not name the namespace `{template}`, and "
            "`agentcore_memory.py` resolves records through it. Both templates have to be written "
            "out verbatim: a mismatch between the container's spelling and the provisioner's does "
            "not fail anything -- `retrieve_memory_records` just returns nothing, forever, with no "
            "error and no warning -- so the only way a reader can diff the two halves is by "
            "reading them, and this section is one of the places they are written down."
        )

    provisioner_source = _source("api/src/handlers/provisioner.py")
    for strategy in _MEMORY_STRATEGIES:
        assert strategy in provisioner_source, (
            f"`provisioner.py` no longer names the memory strategy `{strategy}`. The strategies "
            "are what extract a written event into the two namespaces, so a rename here changes "
            "what the memory resource actually does; update `_MEMORY_STRATEGIES` and the section."
        )
        assert strategy in section, (
            f"`CLAUDE.md`'s memory section does not name the strategy `{strategy}`, which "
            "`provisioner._ensure_memory` configures on every memory it creates. Both names have "
            "to be here: they are what an operator sees in a `GetMemory` response, and a reader "
            'told only that there are "two strategies" cannot match either one to a namespace.'
        )

    assert re.search(r"actorId[^\n]*configuration", section), (
        "`CLAUDE.md`'s memory section never says on one line that `actorId` is the "
        "**configuration** id. Write the pairing on a single line -- this check is one line wide "
        "on purpose, because a sentence-window regex over prose either fires on text it could not "
        "parse or matches almost anything. The pairing is the whole design: it is what makes the "
        "episodic namespace one namespace shared by every task the repository runs, and a document "
        "that gets it backwards describes a namespace nobody reads, because both templates still "
        "resolve and nothing raises."
    )
    assert re.search(r"sessionId[^\n]*task", section), (
        "`CLAUDE.md`'s memory section never says on one line that `sessionId` is the **task** id. "
        "Write the pairing on a single line, and in the same order the code does "
        "(`TaskMemory.record` passes `actorId=self.config_id, sessionId=self.task_id`). Reversed, "
        "the summary strategy would key a namespace per repository and the episodic one per task, "
        "nothing would raise, and the memory would silently stop being a repository's memory."
    )

    tools_source = _source("agent/src/coding_agent/memory_tools.py")
    for name in _MEMORY_TOOL_NAMES:
        assert name in tools_source, (
            f"`memory_tools.py` no longer contains `{name}`, so this check is holding `CLAUDE.md` "
            "to a tool surface the code has changed. Update `_MEMORY_TOOL_NAMES` and the section "
            "-- and if a tool was ADDED rather than renamed, invariant 10b's qualification is "
            "about two fixed operations and now needs rereading."
        )
        assert name in section, (
            f"`CLAUDE.md`'s memory section does not name `{name}`. All three belong here: the two "
            "tool names are the whole SDK surface a model can reach memory through, and "
            "`mcp__memory__*` is the spelling that appears in a role's allowlist, where neither "
            "tool name does. The section is also where invariant 10b's exception is argued, and "
            "that argument is about exactly these two tools and nothing else."
        )

    for label in _MEMORY_LABELS:
        assert label in memory_source, (
            f"`agentcore_memory.py` no longer cites `{label}`, so this check is comparing "
            "`CLAUDE.md` against a label the code has stopped using. Either it was renamed -- "
            "update `_MEMORY_LABELS` and both documents -- or the rule it named is gone."
        )
        assert label in section, (
            f"`CLAUDE.md`'s memory section does not define `{label}`. `agentcore_memory.py`, "
            "`graph.py` and `memory_tools.py` all cite it as a bare label -- `D6` is the keying "
            "rule and `D11` is that every AWS call degrades to a warning and an empty result "
            "rather than raising -- and a pointer whose referent is written down nowhere is worse "
            "than no pointer, because a reader spends time looking for the document that has it."
        )

    for phrase in ("data plane", "bedrock-agentcore-control"):
        assert phrase in section, (
            f"`CLAUDE.md`'s memory section does not say {phrase!r}. The `BEDROCK_AGENTCORE` VPC "
            "endpoint is the **data plane** only, and the control plane "
            "`bedrock-agentcore-control` is deliberately not endpointed because the provisioner "
            "Lambdas that call it are outside this VPC. Without both words the next reader adds a "
            "second endpoint that is an ENI in every AZ with no client, or -- worse -- concludes "
            "the memory calls still leave over the NAT gateway."
        )

    for number in _MEMORY_NUMBERS:
        assert number in section, (
            f"`CLAUDE.md`'s memory section does not name `{number}`. The two numeric defaults are "
            "the per-node note cap (500 characters, because a memory record is prompt surface a "
            "future task pastes into a role's context) and the event expiry (90 days, so a memory "
            "silently empties after a quarter). Neither is guessable from the API, and a reader "
            "who is not told the second one cannot know the first thing to check when an old "
            "lesson stops being recalled."
        )


def test_claude_md_names_every_memory_action_the_boundary_moves() -> None:
    """Every memory IAM action the boundary grants or denies is written down where a reader looks.

    Derived from `infra/stacks/agent_stack.py`, not retyped: the six granted and the seven denied
    names come out of its own two statements, so a seventh grant added tomorrow fails this until
    the section names it. The direction that matters is spelled out in that file's own comment --
    an explicit Deny beats every Allow, so a `bedrock-agentcore:*` in the deny group would turn
    every `CreateEvent` from a container into an `AccessDenied` with nothing naming the cause. A
    deny list written only in code is a trap the next widening walks into.
    """
    section = _memory_section()
    assert section, (
        f"`CLAUDE.md` has no section whose heading starts with {_MEMORY_SECTION_PREFIX!r}, so the "
        "IAM contract has nowhere to be checked. That exact prefix is how this test finds it."
    )
    stack_source = _source("infra/stacks/agent_stack.py")
    granted = _memory_actions(stack_source, _MEMORY_GRANT_SID)
    denied = _memory_actions(stack_source, _MEMORY_DENY_SID)
    assert len(granted) == _MEMORY_GRANTED_ACTIONS and len(denied) == _MEMORY_DENIED_ACTIONS, (
        f"`infra/stacks/agent_stack.py` now carries {len(granted)} memory actions under "
        f"`{_MEMORY_GRANT_SID}` ({', '.join(sorted(granted))}) and {len(denied)} under "
        f"`{_MEMORY_DENY_SID}` ({', '.join(sorted(denied))}), where this expected "
        f"{_MEMORY_GRANTED_ACTIONS} and {_MEMORY_DENIED_ACTIONS}. The sizes are asserted and not "
        "merely derived because a derivation that quietly shrinks is the one way this test could "
        "keep passing while covering less: it can only demand of the prose what it found. A "
        "change in either number is the thing a human should look at -- a widened grant needs the "
        "matching statement in `provisioner._execution_role_policy` and a line in the section, and "
        "a new deny needs checking against the six allows it could shadow."
    )
    unnamed = sorted(action for action in granted | denied if action not in section)
    assert not unnamed, (
        "`CLAUDE.md`'s memory section does not name these memory actions the permissions boundary "
        "moves: " + ", ".join(unnamed) + ". `infra/stacks/agent_stack.py`'s own comment is why "
        "this matters more than completeness: an explicit Deny beats every Allow, so a wildcard "
        f"in `{_MEMORY_DENY_SID}` -- `bedrock-agentcore:*`, or even `List*`, or `Create*` -- would "
        "shadow the `CreateEvent` allow and break every memory write from a container with nothing "
        "in either document naming the cause. The deny list is only useful as a warning if it is "
        "written down where a reader looks, and this is that place. Write the granted six and the "
        "denied seven out by name; do not summarise them as a count."
    )
    assert "memory/*" in section, (
        f"`CLAUDE.md`'s memory section does not name `memory/*`, which is the resource "
        f"`{_MEMORY_GRANT_SID}` takes. It is wider than the grant on purpose -- the boundary is "
        "minted once per deploy, before any memory exists -- and saying so is what stops a reader "
        "mistaking the ceiling for the fence. The fence is the literal ARN "
        "`provisioner._execution_role_policy` names."
    )
    assert "boundary" in section.lower(), (
        "`CLAUDE.md`'s memory section never uses the word `boundary`, so the three-way split reads "
        "as one policy. It is three: the grant on the literal memory ARN, the permissions boundary "
        f"ceiling `{_MEMORY_GRANT_SID}` on `memory/*`, and the `{_MEMORY_DENY_SID}` deny list by "
        "literal name. Which of the three is the cross-repository fence -- the grant -- is the "
        "sentence a reader needs, and it cannot be written without naming the other two."
    )


def test_the_auto_memory_qualification_distinguishes_the_two_mechanisms() -> None:
    """The subsection arguing auto-memory is a cross-tenant hazard says AgentCore Memory is not it.

    Left alone, that subsection reads as a flat argument against the feature that shipped: it says
    one container serves many tasks over its life, so without `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`
    one task's notes can surface in another's session -- and one shared, repository-scoped memory
    is exactly what this repository now provisions. A reader who finds only half of that concludes
    the feature violates the boundary claim, and the reconciliation is real rather than rhetorical:
    auto-memory is keyed by container and carries whatever any session wrote, AgentCore Memory is
    keyed to one configuration id and fenced by IAM to that repository's own memory ARN.
    """
    section = _section(CLAUDE_MD.read_text(), _AUTO_MEMORY_SUBSECTION)
    assert section, (
        f"`CLAUDE.md` has no {_AUTO_MEMORY_SUBSECTION!r} subsection, so the auto-memory argument "
        "moved somewhere this check cannot see it. It is sliced from that `###` to the next one, "
        "because the reconciliation is only useful standing next to the claim it qualifies."
    )
    assert _AUTO_MEMORY_VARIABLE in section, (
        f"`{_AUTO_MEMORY_VARIABLE}` is no longer named in {_AUTO_MEMORY_SUBSECTION!r}. It is one "
        "of the ten suppression variables every runtime carries and the only one of them that is "
        "cross-tenant, so if the bullet moved, this check and its reconciliation must move with it."
    )
    lowered = section.lower()
    assert "agentcore memory" in lowered, (
        f"{_AUTO_MEMORY_SUBSECTION!r} argues that memory shared across sessions is a cross-tenant "
        "hazard -- one container serves many tasks over its life, so one task's notes can surface "
        "in another's session -- and never mentions AgentCore Memory, which is one shared, "
        "repository-scoped memory and is exactly what shipped. A reader who finds only half of "
        "that concludes the feature violates the boundary claim this whole section exists to make. "
        "Name it here, say the two are different mechanisms, and give the reason: AgentCore Memory "
        "is keyed to one configuration id, fenced by IAM to that repository's own literal memory "
        f"ARN, and reached only through two closed-over tools -- see {_MEMORY_SECTION_PREFIX!r}."
    )
    assert any(word in lowered for word in _DISTINCTION_WORDS), (
        f"{_AUTO_MEMORY_SUBSECTION!r} names AgentCore Memory but never says it is a DIFFERENT "
        f"mechanism from auto-memory (looked for any of {', '.join(_DISTINCTION_WORDS)}). Naming "
        "both in one subsection without separating them is worse than naming neither: it reads as "
        "though the cross-tenant argument applies to both, i.e. as though this repository ships a "
        "hazard it documented and then built anyway. Auto-memory is keyed by container, loads "
        "regardless of `setting_sources`, and carries whatever any session wrote; AgentCore Memory "
        "is keyed by configuration id and has no id-shaped parameter in either tool's schema."
    )


def _walkthrough() -> str:
    """`README.md`'s walkthrough, from its `##` heading to the next one.

    Sliced by heading text rather than by line number because the two shape tests above
    deliberately allow a step to be gained and a `###` subsection to be renamed or reordered;
    a check that pinned line numbers would fail on an edit it does not care about.
    """
    text = README.read_text()
    start = text.index(_WALKTHROUGH_HEADING)
    rest = text[start + len(_WALKTHROUGH_HEADING) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _numbered_steps() -> list[str]:
    """The walkthrough's numbered steps, each cut at the next `###`.

    The cut matters in the other direction: `### Tear it down -- in this order` truncates the
    step before it, so the warning under that heading -- "Doing step 9 first orphans the
    runtime, the IAM role and the secret" -- is not read as part of step 7 and cannot satisfy
    a check on its own. The same rule cuts `### If something goes wrong` off the teardown step's
    chunk, and that too is deliberate: the conditional branches parked in that subsection --
    a re-run of `make deploy EMAIL=...` after an empty console bucket, a leftover `RCA_API_URL`
    -- name the same literals the checks below look for, and must not satisfy a numbered-step
    check.
    """
    return [chunk.split("\n#")[0] for chunk in re.split(r"\n(?=\d+\. )", _walkthrough())]


def test_the_walkthrough_says_an_override_outranks_endpoint_discovery() -> None:
    steps = [step for step in _numbered_steps() if _ENDPOINT_OVERRIDES[0] in step]
    assert len(steps) == 1, (
        f"expected exactly one walkthrough step to name `{_ENDPOINT_OVERRIDES[0]}`, found "
        f"{len(steps)}. The install step is where a reader learns how the plugin finds the API, "
        "and it has to say that discovery is the default AND that an override beats it -- "
        "`plugins/sample-rca/.claude-plugin/plugin.json` declares three optional `userConfig` "
        "fields, and a reader told only about discovery has no reason to look for one."
    )
    step = steps[0]
    missing = [name for name in _ENDPOINT_OVERRIDES if name not in step]
    assert not missing, (
        "the install step does not name " + ", ".join(missing) + ", which "
        "`plugins/sample-rca/.mcp.json` forwards from the plugin's optional `userConfig`. All "
        "three have to be named there: a leftover one is invisible in the config UI, and the "
        "failure it produces is a complete-success run against another deployment -- a repository "
        "registered into an older fleet in another region while the one just deployed answers "
        "`GET /configurations` with zero items. Nothing in the output says which endpoint was "
        "used, so the prose is the only warning there is."
    )
    lowered = step.lower()
    assert any(word in lowered for word in _PRECEDENCE_WORDS), (
        "the install step names the three override variables but never says an override "
        f"outranks discovery (looked for any of {', '.join(_PRECEDENCE_WORDS)}). That direction "
        "is the whole point: `mcp/src/rca_mcp/client.py::resolve_endpoint` returns an "
        "`RCA_API_URL` before it reads either `~/.rca/endpoint.json` or the stack's `ApiUrl` "
        "output, so a step that merely mentions the fields reads as though discovery always "
        "wins. Reword it; do not delete the sentence."
    )


def test_the_deploy_step_names_every_target_it_runs() -> None:
    """The deploy step names all three targets it runs, in the order it runs them.

    A fresh-clone reader who follows only this README and never runs `make setup` has no
    virtualenv, so the SECOND thing they do fails: preflight's synth check imports `aws_cdk`
    from this checkout's `.venv` and stops with `preflight: FAILED -- this checkout's
    virtualenv has no aws-cdk-lib, so nothing can synth.` and a `fix:` line naming the target
    the walkthrough never mentioned. This step named two targets for as long as the plugin's
    deleted `setup` skill ran three and nothing noticed -- and that skill's closed set was the
    only thing that had checked it, over in `tests/test_plugin_manifest.py`, which never reads
    this document. The step is the last place the three are written down in order now.
    """
    # Keyed on the `EMAIL=` argument and not on `make deploy` alone: two later steps mention the
    # target in passing ("what `make deploy` just wrote", "the console URL `make deploy` printed"),
    # and the step that RUNS it is the one that passes the address the first console operator
    # becomes.
    steps = [step for step in _numbered_steps() if "make deploy EMAIL=" in step]
    assert len(steps) == 1, (
        f"expected exactly one walkthrough step in README.md to run `make deploy EMAIL=...`, "
        f"found {len(steps)}. It is found by the target it runs and not by its number, so "
        "renumbering the list is fine -- none means the deploy step moved somewhere this check "
        "cannot see it."
    )
    step = steps[0]
    missing = [target for target in _DEPLOY_STEP_TARGETS if target not in step]
    assert not missing, (
        "README.md's deploy step does not name "
        + ", ".join(f"`{t}`" for t in missing)
        + ". All three run in that order, so a step that names fewer is a walkthrough a reader "
        "cannot follow: without `make setup` there is no `.venv`, and `make preflight` stops at "
        "its synth check on a fresh clone."
    )
    positions = [step.index(target) for target in _DEPLOY_STEP_TARGETS]
    assert positions == sorted(positions), (
        "README.md's deploy step names "
        + ", ".join(f"`{t}`" for t in _DEPLOY_STEP_TARGETS)
        + " out of order. The order is the instruction: `make setup` builds the virtualenv "
        "`make preflight` synthesizes from, and the confirmation `make deploy` waits for has to "
        "quote the account and region preflight printed."
    )


def test_the_teardown_step_names_the_stacks_it_destroys() -> None:
    """The `cdk destroy` command names all five stacks, wherever it is documented.

    It moved from `README.md` to `CLAUDE.md` when the walkthrough was cut back to the happy
    path: teardown is reference an operator reads once, and `CLAUDE.md` already owned its other
    half ("What deliberately survives a destroy"), so the two halves now sit together. The
    README keeps the part that is a *decision* -- deregister first, or orphan the runtime -- and
    points here for the commands.

    What is asserted did not change, only where it is read from. The blast radius of `--all` is
    the reason: it destroys every stack the CDK app composes, so an account hosting two fleets
    loses more than the reader meant.
    """
    text = CLAUDE_MD.read_text()
    heading = "### Tearing a fleet down"
    assert heading in text, (
        f"`CLAUDE.md` has no {heading!r} section, so the teardown moved somewhere this check "
        "cannot see it. README points here for the commands."
    )
    # The whole section, not the fenced block alone: the `--all` caveat is in the prose around
    # the command, and it is as load-bearing as the command itself.
    start = text.index(heading)
    step = text[start : text.index("\n### ", start + 1)]
    assert step.count("aws-cdk@") == 1, (
        f"expected exactly one pinned `aws-cdk@` destroy command in that section, found "
        f"{step.count('aws-cdk@')}: two commands leave a reader no way to tell which is safe."
    )
    assert "destroy" in step
    # `stack_names()` with no argument, so the account entry is the UNSUFFIXED name: any
    # per-region spelling (`...AccountEuWest1`) contains it, and the step is free to show a
    # region without this test having to pick one.
    missing = [name for name in app.stack_names() if name not in step]
    assert not missing, (
        "the teardown step does not name " + ", ".join(missing) + ". It has to name the stacks "
        "it destroys, because `destroy --all` destroys every stack the CDK app composes: an "
        "account hosting two fleets (`-c stackPrefix=`, `infra/app.py::stack_names`), or one "
        "carrying an unsuffixed account stack left by a deploy from before the per-region split, "
        "loses more than the reader meant -- including a shared `us-east-1` account stack under "
        "a live console in another region. Naming the five is what makes the step's blast "
        "radius readable."
    )
    if "--all" in step:
        assert "only" in step, (
            "the teardown step mentions `--all` without saying when it is safe. `--all` is "
            "equivalent to naming the five stacks ONLY where this account and region hold "
            "exactly one fleet and no leftover unsuffixed account stack; unqualified, it is an "
            "instruction to delete another fleet's stacks. Either drop it or keep the "
            "condition attached to it."
        )


def test_claude_md_carries_the_invariants() -> None:
    text = CLAUDE_MD.read_text()
    assert "## Invariants" in text, (
        "`CLAUDE.md` must carry the invariants: each one cost a real incident, and "
        "several fail SILENTLY, so an agent about to change this code cannot tell it has "
        "reintroduced one. A design document nobody rereads once the code "
        "exists; `CLAUDE.md` is what gets read."
    )
    # `12.` and `6b.` at the start of a line, i.e. one entry of a numbered list. Counted
    # over the whole file rather than sliced out of the section: the floor is what
    # matters, and a slice would break on the next heading that gets added.
    entries = [
        line
        for line in text.splitlines()
        if line.strip() and line.split(".", 1)[0].rstrip("abc").isdigit() and ". " in line
    ]
    assert len(entries) >= _MINIMUM_INVARIANTS, (
        f"`CLAUDE.md` holds {len(entries)} numbered entries, fewer than the "
        f"{_MINIMUM_INVARIANTS} invariants this repository has earned. "
        '"Invariants to carry over verbatim". Compressing the list is how a silent '
        "failure mode stops being written down anywhere."
    )


def test_invariant_10b_is_qualified_for_the_memory_server() -> None:
    """Invariant 10b names its one exception, because the repository ships two of them.

    As written it read "No MCP tool may call an AWS API" while
    `agent/src/coding_agent/memory_tools.py` shipped `memory_search` and `memory_record`, which
    reach `RetrieveMemoryRecords` and `CreateEvent`. An invariant contradicted by the tree is
    worse than a missing one: the next reader either deletes the tools or stops trusting the list.
    The qualification is mechanical rather than asserted -- exactly two fixed operations exist in
    that module and the model names neither the memory, the actor, the session nor the namespace --
    and that argument belongs in the invariant, not only in the module's own docstring.
    """
    # From the `10b.` line to the `10c.` line, the same heading-relative slicing `_section` does:
    # numbering is what a reader navigates the list by, and `10c.` is the entry that follows.
    lines = CLAUDE_MD.read_text().splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.startswith(_INVARIANT_10B)), None
    )
    assert start is not None, (
        f"`CLAUDE.md` has no invariant starting `{_INVARIANT_10B}`. The sub-numbering is the "
        "contract here -- `test_claude_md_carries_the_invariants` counts these line starts and "
        "needs at least "
        f"{_MINIMUM_INVARIANTS} of them -- so do not renumber or collapse the entry; amend it."
    )
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith(_INVARIANT_10C)
        ),
        len(lines),
    )
    entry = "\n".join(lines[start:end])
    tools_source = _source("agent/src/coding_agent/memory_tools.py")
    assert _INVARIANT_10B.rstrip(".") in tools_source, (
        f"`memory_tools.py` no longer cites `{_INVARIANT_10B.rstrip('.')}`, so the code and the "
        "invariant have stopped pointing at each other and either can now drift alone. That module "
        "IS the exception the invariant names; its docstring carrying the argument in full is what "
        "makes the qualification checkable from the code side."
    )
    assert "memory" in entry.lower(), (
        f"invariant {_INVARIANT_10B} does not mention memory. As written it says no MCP tool may "
        "call an AWS API, and `memory_tools.py` ships two that reach `CreateEvent` and "
        "`RetrieveMemoryRecords` -- so the invariant is contradicted by the tree, and an agent "
        "reading the list either deletes a shipped feature or stops trusting the other 34 entries."
    )
    assert any(phrase in entry.lower() for phrase in _QUALIFICATION_WORDS), (
        f"invariant {_INVARIANT_10B} mentions memory but does not say what makes the exception "
        f"mechanical (looked for any of {', '.join(_QUALIFICATION_WORDS)}). The invariant was "
        "written against a GENERAL-PURPOSE AWS client handed to a prompt-injected agent, and the "
        "`memory` server is the opposite shape: exactly two fixed operations exist in the whole "
        "module, and the model cannot name the memory, the actor, the session or the namespace "
        "because no such parameter exists in either schema -- all four are closed over from the "
        "state. Cite the enforcement "
        "(`tests/test_agent_memory.py::test_no_tool_lets_a_model_name_the_memory_it_reads`) rather "
        "than restating it, and keep the general rule for anything general-purpose. An exception "
        "recorded only as prose is the kind an approving reviewer widens by one parameter."
    )


def test_every_path_a_document_names_exists() -> None:
    broken = []
    for document in (README, CLAUDE_MD):
        text = document.read_text()
        for kind, line, claim, token, _ in _claims(text):
            if kind != "path" or token in _ABSENT_ON_PURPOSE:
                continue
            if _resolve(token, _SOURCE_FILES) is None:
                broken.append(f"{document.name}:{line}: {claim} -> no such file `{token}`")
    assert not broken, (
        "these documents name paths that are not in the tree:\n  "
        + "\n  ".join(broken)
        + "\nA renamed or moved file leaves a sentence that still reads as current and "
        "sends its reader to code that is not there -- which is worse than no sentence, "
        "because the reader trusts it and then doubts themselves. Fix the prose to match "
        "the tree; never move a file to match the prose. A path written as shorthand "
        "(`nodes/code.py` for a file under `agent/src/coding_agent/`) resolves by unique "
        "suffix, so this failure means the file is gone, renamed, or now ambiguous."
    )
    # The exemption above is a contract of its own, not a hole: both claims say the file
    # is absent, so a file appearing at either path makes a true sentence false.
    for absent in sorted(_ABSENT_ON_PURPOSE):
        assert not (REPO_ROOT / absent).exists(), (
            f"`{absent}` now exists, and both documents claim it does not. That claim is "
            "load-bearing -- the namespace-package layout is what keeps `mcp/src` "
            "importable without shadowing the `mcp` distribution, and `infra/stacks` is a "
            "plain directory of CDK stacks on purpose. Either delete the file or rewrite "
            f"the prose and drop `{absent}` from `_ABSENT_ON_PURPOSE`."
        )


def test_every_symbol_a_document_names_is_in_the_file_it_names() -> None:
    broken = []
    for document in (README, CLAUDE_MD):
        text = document.read_text()
        for kind, line, claim, token, symbol in _claims(text):
            if kind != "symbol":
                continue
            rel = _resolve(token, _SOURCE_FILES)
            if rel is None:
                broken.append(f"{document.name}:{line}: {claim} -> no such file `{token}`")
                continue
            names = _symbols(rel)
            # `None` is a non-Python file, where a substring is all the resolution there is.
            found = (
                symbol in names if names is not None else symbol in (REPO_ROOT / rel).read_text()
            )
            if not found:
                broken.append(f"{document.name}:{line}: {claim} -> `{symbol}` is not in {rel}")
    assert not broken, (
        "these documents name symbols that are not in the files they name:\n  "
        + "\n  ".join(broken)
        + "\nThis is the failure this check exists for: a rename lands in the code, the "
        "tests stay green, and the sentence describing the old name keeps reading as "
        "current until somebody goes looking for a function that is not there. Restate "
        "the claim to match what the code does today -- do not rename code to match "
        "prose. Symbols are resolved from the file's source text, so a name that lives "
        "only in a comment or a docstring does not count; if that is the case here, the "
        "honest fix is to reword the claim."
    )


def test_no_docs_directory() -> None:
    assert not (REPO_ROOT / "docs").exists(), (
        "there is no `docs/` directory in this repository, on purpose: a reference "
        "nobody reads is worse than a code comment somebody does. Reference material "
        "goes to `CLAUDE.md` or stays as the comment on the code that owns it."
    )


@pytest.fixture
def deployed_asset_tree() -> Path:
    """A fake `cdk synth` asset tree, in-tree, holding copies of two documented files.

    Materialised for real rather than mocked, because the bug it reproduces is about what
    `rglob` actually finds. Creating it under `infra/cdk.out/` is safe because `cdk.out/` is
    gitignored, so `git status` stays clean -- but it must NOT survive the test, or the next
    run's clean-tree case is a lie that nothing would notice.

    Deletes exactly what it created: `infra/cdk.out/` may already hold a developer's real
    synth output, so teardown always removes the `asset.deadbeefdeadbeef` subtree and
    removes `infra/cdk.out` itself only when this fixture is what created it. The teardown
    sits after `yield` so it runs when the assertions fail too.
    """
    cdk_out = REPO_ROOT / "infra" / "cdk.out"
    created_cdk_out = not cdk_out.exists()
    asset = cdk_out / "asset.deadbeefdeadbeef"
    nodes = asset / "src" / "coding_agent" / "nodes"
    nodes.mkdir(parents=True, exist_ok=True)
    for name in ("code.py", "submit.py"):
        shutil.copy2(REPO_ROOT / "agent" / "src" / "coding_agent" / "nodes" / name, nodes / name)
    yield asset
    shutil.rmtree(asset)
    if created_cdk_out:
        shutil.rmtree(cdk_out)


def test_a_deployed_tree_does_not_make_a_documented_path_ambiguous(
    deployed_asset_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nodes = deployed_asset_tree / "src" / "coding_agent" / "nodes"
    # Sanity first: without this the test could pass because the fixture quietly did nothing.
    for name in ("code.py", "submit.py"):
        assert (nodes / name).is_file(), (
            f"the fixture did not put `{name}` on disk, so this test proves nothing. It has "
            "to reproduce what `make deploy` really leaves behind before it can show the "
            "index survives it."
        )

    # And the hazard has to be genuinely present while the assertions below run, or this is
    # a duplicate green run rather than a regression test: an UNFILTERED walk must find more
    # than one `nodes/code.py`, which is precisely what breaks `_resolve`'s uniqueness rule.
    unfiltered = [
        path
        for search_dir in ("agent/src", "infra")
        for path in (REPO_ROOT / search_dir).rglob("code.py")
        if path.as_posix().endswith("nodes/code.py")
    ]
    assert len(unfiltered) > 1, (
        "a walk with no exclusions should now find at least two `nodes/code.py` files -- the "
        f"real one and the fixture's copy -- but found {len(unfiltered)}. The ambiguity this "
        "test defends against is not present, so a green run below means nothing."
    )

    # `_SOURCE_FILES`, `_BASENAMES` and `_BY_STEM` are computed once at module import, long
    # before any fixture runs, so they predate the asset tree above and cannot see it.
    # Re-calling `_index()` here is the option chosen over restructuring the two document
    # tests to take their index from a callable: it leaves their bodies -- and their two long
    # failure messages, which explain a real hazard -- completely untouched.
    source_files, basenames, by_stem = _index()

    assert not [rel for rel in source_files if "cdk.out" in rel], (
        "`_index` walked into `infra/cdk.out/`. Everything under it is generated and "
        "gitignored, and a copy of first-party source there is exactly what makes a "
        "documented shorthand ambiguous; a deploy in the tree must not turn this suite red."
    )
    for shorthand, real in (
        ("nodes/code.py", "agent/src/coding_agent/nodes/code.py"),
        ("nodes/submit.py", "agent/src/coding_agent/nodes/submit.py"),
    ):
        assert _resolve(shorthand, source_files) == real, (
            f"`{shorthand}` no longer resolves uniquely to `{real}` with a synth asset tree "
            "on disk. This is the bug: the prose and the tree are both correct, and both "
            "document tests fail on any machine that has run `make deploy` as though the "
            "file had been renamed or deleted. The search scope is what must be fixed, "
            "never the claim."
        )
    for stem in ("code", "submit"):
        assert len(by_stem.get(stem, ())) == 1, (
            f"stem `{stem}` names {len(by_stem.get(stem, ()))} files, so a dotted claim "
            f"like `{stem}.GOAL_CONDITION` is silently SKIPPED rather than checked -- the "
            "quiet version of the same failure. Generated copies must not reach `by_stem`."
        )

    # Finally run the two document tests themselves against an index built while the fake
    # asset tree was on disk, so what is asserted is the real check and not a paraphrase of
    # it. `monkeypatch` restores the three globals afterwards.
    monkeypatch.setattr(f"{__name__}._SOURCE_FILES", source_files)
    monkeypatch.setattr(f"{__name__}._BASENAMES", basenames)
    monkeypatch.setattr(f"{__name__}._BY_STEM", by_stem)
    test_every_path_a_document_names_exists()
    test_every_symbol_a_document_names_is_in_the_file_it_names()
