"""The Claude Code surface: the marketplace, the plugin, and the two pinned copies.

Everything here is a contract on files a *model* or the CLI reads, which is why it is
tested rather than reviewed. Each failure mode is silent: a required `userConfig` field
makes the install two-phase again, a `@main` reference installs whatever was pushed last
onto an operator's workstation, a colon in an unquoted frontmatter description is a skill
that simply does not load, and the bare `mcp__remote-coding-agent__` spelling inside the
*plugin* copy denies every tool with no diagnostic at all.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from rca_mcp.server import SERVER_NAME, TOOLS
from stacks.api_stack import API_URL_OUTPUT_KEY

_MARKETPLACE_NAME = "sample-remote-coding-agent"
# The `sample` qualifier is load-bearing, for the reason
# `test_the_plugin_name_cannot_collide_with_another_plugin` spells out: a bare `rca` is a
# name another product already installs under, and the directory below is derived from this
# constant so the two cannot drift apart.
_PLUGIN_NAME = "sample-rca"

# The prefix the collision produced, as a literal. Every other check below derives from
# `_PLUGIN_NAME`, so a future edit moves them all together; this one does not move, which is
# what makes it the backstop.
_COLLIDING_PREFIX = "mcp__plugin_rca_remote-coding-agent__"

# Bare, unqualified names another product is likely to have taken. Spelled out rather
# than derived, because the point is that each one is *plausible* and therefore dangerous.
_GENERIC_NAMES = frozenset(
    {"rca", "agent", "mcp", "remote", "claude", "coding-agent", "remote-coding-agent"}
)

_MARKETPLACE = REPO_ROOT / ".claude-plugin/marketplace.json"
_PLUGIN_ROOT = REPO_ROOT / "plugins" / _PLUGIN_NAME
_PLUGIN_MANIFEST = _PLUGIN_ROOT / ".claude-plugin/plugin.json"
_PLUGIN_MCP = _PLUGIN_ROOT / ".mcp.json"
_PROJECT_MCP = REPO_ROOT / ".mcp.json"
_SKILLS = _PLUGIN_ROOT / "skills"
_COMMANDS = REPO_ROOT / ".claude/commands"
_MAKEFILE = REPO_ROOT / "Makefile"
_WEB_STACK = REPO_ROOT / "infra/stacks/web_stack.py"
_INFRA_APP = REPO_ROOT / "infra/app.py"

# A fenced block, opener and closer included. `DOTALL` with a lazy body is what stops the
# first fence from swallowing every later one.
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)

# The shape of `~/.rca/endpoint.json`, spelled out in
# `mcp/src/rca_mcp/client.py::_write_cache`'s docstring as a contract shared with the
# deploy. `_read_cache` discards a document whose stack or region disagrees with the one in
# force, so a key the deploy misspells is not an error -- it is a cache that is silently
# never used, and one `describe_stacks` on every MCP server start.
_CACHE_KEYS = ("apiUrl", "stackName", "region", "stackLastUpdated", "writtenBy")

# The `endpoint used:` line, by the symbols that produce it, as (path, `def`-line) pairs so a
# failure can report which one the pinned commit is missing. A plugin `userConfig` override
# (`api_url`, `aws_region`, `stack_name`) silently aims every tool at a different deployment;
# this line is the only way an operator can tell which one answered, and
# `mcp/src/rca_mcp/server.py::_endpoint_line` appends it in `call_tool` so all six tools carry
# it, error path included. This tuple is a *secondary* content check now: the primary guard in
# `test_the_pinned_reference_is_not_older_than_the_fix_it_ships` is a currency range over `mcp/`,
# because an enumerated symbol list only ever catches the one regression someone already had.
_PROVENANCE_SYMBOLS = (
    ("mcp/src/rca_mcp/format.py", "def endpoint_provenance"),
    ("mcp/src/rca_mcp/server.py", "def _endpoint_line"),
)

# The pair table. `remote` exists twice on purpose: the project-scoped copy is what gives a
# checkout of this repository the un-namespaced `/remote`. `scaffold` is intentionally one-way
# -- it turns an EMPTY repository into one the agent can run tasks against, and nobody
# scaffolds this repository from inside itself, so a project-scoped mirror would be a command
# that can only refuse.
_PAIRS = (("remote", _COMMANDS / "remote.md", _SKILLS / "remote/SKILL.md"),)
_ONE_WAY_SKILLS = ("scaffold",)

_SCAFFOLD_SKILL = _SKILLS / "scaffold/SKILL.md"

# The one documented substitution table, applied to the PLUGIN copy to recover the
# project command. Order matters only in that neither pattern occurs inside the other.
_SUBSTITUTIONS = (
    (f"mcp__plugin_{_PLUGIN_NAME}_{SERVER_NAME}__", f"mcp__{SERVER_NAME}__"),
    (f"/{_PLUGIN_NAME}:remote", "/remote"),
)

_GUARDRAILS = (
    # Was "never pass or echo a github token". The tool takes none now, so the stronger
    # instruction is to refuse one: a model that asks for a credential sends the operator to
    # create something the control plane answers `400 TokenNotAccepted` to.
    "never ask for or accept a github token",
    "never poll get_task in a loop",
    "never call register_repository without explicit human intent",
    # Registering is half the setup, and authorising is a browser step no tool here can take.
    # Without this a model reports success and the operator's first task is refused with
    # `409 GitHubNotAuthorized` for a reason nothing mentioned.
    "never report a registration as finished",
    "a draft offered for review, not working code",
)

# The scaffold skill's own sentinels. Each one is a way it could do damage in a single tool
# call -- and it holds `Write`, `Bash` and a push, which is a wider blast radius than
# `/sample-rca:remote` has.
_SCAFFOLD_GUARDRAILS = (
    "never overwrite a file that already exists",
    "never ask for or accept a github token",
    "never force-push",
    "never scaffold a repository that already has source in it",
)

# language -> (setup command, verify command) `nodes/checkout.py` must detect in the tree the
# templates produce. Python declares a Makefile; TypeScript declares package.json scripts, so
# the two exercise both detection branches.
_SCAFFOLD_EXPECTED = {
    "Python": ("make setup", "make test"),
    "TypeScript": ("npm run setup", "npm test"),
}

# `.venv` is a symlink onto /tmp in the agent's container, and `.git` is large; neither
# holds a manifest this repository ships.
_SKIP = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "cdk.out"}


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """`(fields, body)` for a document that opens with a `---` fence.

    Parsed by hand, as `tests/test_skills.py` does: this repository pins every dependency
    and adding pyyaml for four lines of splitting is the worse trade. `partition` keeps
    everything after the FIRST colon, so a value containing one arrives intact -- which
    is what makes the no-colon assertion below real rather than an artefact of the parser.
    """
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} does not open with a `---` frontmatter fence"
    block, fence, body = text[4:].partition("\n---")
    assert fence, f"{path} never closes its frontmatter fence"

    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, colon, value = line.partition(":")
        assert colon, f"{path}: frontmatter line {line!r} is not `key: value`"
        fields[key.strip()] = value.strip()
    return fields, body


def _mcp_configs(root: Path = REPO_ROOT) -> list[Path]:
    """Every `.mcp.json` this repository ships, found without following a symlink."""
    found: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.name in _SKIP or entry.is_symlink():
            continue
        if entry.is_dir():
            found.extend(_mcp_configs(entry))
        elif entry.name == ".mcp.json":
            found.append(entry)
    return found


def _pinned_reference() -> str:
    """The `@<ref>` the plugin's `.mcp.json` installs from, as committed."""
    argument = next(
        arg
        for arg in json.loads(_PLUGIN_MCP.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME][
            "args"
        ]
        if "git+" in arg
    )
    match = re.search(r"git\+https://[^@\s]+@(?P<ref>[^#\s]+)", argument)
    assert match, f"plugins/{_PLUGIN_NAME}/.mcp.json installs from {argument!r} with no `@<ref>`"
    return match.group("ref")


def _git_env() -> dict[str, str]:
    """This process's environment with the `safe.directory` exemption for this checkout.

    Passed explicitly in git's own environment channel, as `tests/test_web_console.py` does:
    the checkout can be owned by another uid (a mounted workspace), and git then refuses
    every command with `dubious ownership` (exit 128). The ambient exemption travels in
    `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>`, which
    `agent/src/coding_agent/git_ops._export()` overwrites with a group of its own at import
    -- so under `pytest -n auto` a test sharing a worker with the git tests inherits an
    environment where the exemption is simply gone. Relying on it would make the pin check
    below skip for a reason that has nothing to do with the pin. The whole group is rewritten
    as a unit, count included, because a partial one is `fatal: missing config key`.
    """
    return os.environ | {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(REPO_ROOT),
    }


def _tool_mentions(text: str) -> list[str]:
    return re.findall(r"mcp__[A-Za-z0-9_-]+__[a-z_]+", text)


def _declared_targets() -> set[str]:
    """Every target the Makefile declares, by the regex `tests/test_repo_contracts.py` uses."""
    return set(
        re.findall(
            r"^([A-Za-z][A-Za-z0-9_-]*):", _MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE
        )
    )


def _recipe(target: str) -> str:
    """The recipe lines of one Makefile target, tabs included."""
    lines = _MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("\t"):
            break
        body.append(line)
    return "\n".join(body)


def _fenced_lines(path: Path) -> list[str]:
    """The non-blank lines inside every fenced block, the fences themselves dropped."""
    lines: list[str] = []
    for block in _FENCE.findall(path.read_text(encoding="utf-8")):
        lines.extend(line.strip() for line in block.splitlines()[1:-1] if line.strip())
    return lines


def _scaffold_templates() -> dict[str, dict[str, str]]:
    """`{language: {relative path: file content}}` from `scaffold/SKILL.md`'s fenced blocks.

    The two `## ` template sections are the source of truth for what the skill writes, and
    they are parsed rather than duplicated here so a template edited in one place is
    checked in the other.

    The shape the parse depends on is narrow, and deliberately so: one `## <Language>`
    heading per template section, one `### ` heading per file holding exactly one
    backticked path, and exactly one fenced block under each. A block on disk instead
    would be simpler to read and worse in two ways -- installing a plugin copies its
    directory into a cache, so a skill that says "copy `templates/python/`" depends on the
    model resolving its own install path at runtime, and a template `.py` under `plugins/`
    enters `tests/test_docs.py`'s `_BY_STEM` index, where it can make a correct documented
    path ambiguous.
    """
    _, body = _frontmatter(_SCAFFOLD_SKILL)
    templates: dict[str, dict[str, str]] = {}
    for section in body.split("\n## ")[1:]:
        heading, _, rest = section.partition("\n")
        words = heading.split()
        if not words or words[0] not in _SCAFFOLD_EXPECTED:
            continue
        files: dict[str, str] = {}
        for subsection in rest.split("\n### ")[1:]:
            sub_heading, _, remainder = subsection.partition("\n")
            named = re.findall(r"`([^`]+)`", sub_heading)
            assert len(named) == 1, (
                f"scaffold/SKILL.md's `{heading}` section has the subsection heading "
                f"{sub_heading!r}, which names {named}. Each one names exactly one "
                "backticked path -- it is what tells the model where to write the block."
            )
            block = _FENCE.search(remainder)
            assert block, (
                f"scaffold/SKILL.md's `{named[0]}` subsection carries no fenced block, so "
                "there is nothing for the model to copy"
            )
            files[named[0]] = "\n".join(block.group(0).splitlines()[1:-1]) + "\n"
        templates[words[0]] = files
    return templates


def test_the_plugin_needs_no_configuration_to_install() -> None:
    """No entry in `userConfig` is required, and each says so explicitly.

    This is the whole two-step install. The API URL is a CloudFormation output, so a
    required field pins the operator to a value only a deploy can produce and puts the
    install back to *deploy, read an output, install, paste* -- with a plugin installed
    before the deploy left unable to work at all.
    """
    manifest = json.loads(_PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["name"] == _PLUGIN_NAME
    assert manifest["version"]
    assert manifest["description"].strip()

    entries = manifest["userConfig"]
    assert set(entries) == {"api_url", "aws_region", "stack_name"}, (
        f"three optional overrides, for someone running two deployments; got {sorted(entries)}"
    )
    for name, entry in entries.items():
        assert "required" in entry, (
            f"userConfig.{name} omits `required`. Spell it out: an omitted flag is a "
            "reader guessing at the most important property of this manifest."
        )
        assert entry["required"] is False, (
            f"userConfig.{name} is required, which makes the install two-phase again and "
            "pins the operator to a value only a deploy can produce. The endpoint is "
            "discovered from the stack instead; these three are overrides only."
        )
        assert entry["description"].strip()


def test_every_override_carries_the_title_the_installer_demands() -> None:
    """Every `userConfig` entry declares a non-empty `title`.

    Claude Code validates this manifest *before* it copies anything into place, and its
    schema requires `title` on every entry. Without it `/plugin install` fails outright
    with one `userConfig.<name>.title: Invalid input: expected string, received
    undefined` per entry -- so the plugin cannot be installed on any machine, however the
    deployment is configured.

    `title` is the label the configuration UI puts on the field, and the only text an
    operator sees before opening `description` -- which is why it must not just be the
    description again.
    """
    entries = json.loads(_PLUGIN_MANIFEST.read_text(encoding="utf-8"))["userConfig"]
    for name, entry in entries.items():
        assert "title" in entry, (
            f"userConfig.{name} omits `title`, which Claude Code's manifest schema "
            "requires. The install fails validation before it copies anything, so no "
            "deploy, credential or endpoint state can work around it."
        )
        assert isinstance(entry["title"], str) and entry["title"].strip(), (
            f"userConfig.{name}.title is {entry['title']!r}; the schema wants a non-empty "
            "string and the UI has nothing to label the field with."
        )
        assert entry["title"] != entry["description"], (
            f"userConfig.{name}.title repeats `description` verbatim. The title is a "
            "label shown before the description is opened, so duplicating it leaves the "
            "operator with a paragraph where a label goes."
        )


def test_every_operator_command_is_a_makefile_target() -> None:
    """Every `make <target>` a skill names is one the Makefile declares.

    The Makefile declares exactly five (`tests/test_repo_contracts.py`), which bounds what
    a skill may tell a model to run: a named target that does not exist is a model
    inventing a deploy command from first principles instead.
    """
    declared = set(
        re.findall(
            r"^([A-Za-z][A-Za-z0-9_-]*):",
            (REPO_ROOT / "Makefile").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )
    # The scaffold skill is deliberately absent: the `make` targets in its fenced blocks belong
    # to the repository it scaffolds, not to this one, so checking them against this Makefile
    # would fail for a reason about the wrong tree.
    documents = [_SKILLS / "remote/SKILL.md", _COMMANDS / "remote.md"]
    for path in documents:
        for target in re.findall(
            r"\bmake\s+([A-Za-z][A-Za-z0-9_-]*)", path.read_text(encoding="utf-8")
        ):
            assert target in declared, (
                f"{path.relative_to(REPO_ROOT)} tells a model to run `make {target}`, which "
                f"the Makefile does not declare; it declares {sorted(declared)}"
            )


def test_nothing_is_installed_from_a_moving_reference() -> None:
    """Invariant 34: a tag or a sha, never `@main`.

    This line executes code on every operator's workstation, with their own AWS
    credentials, from a URL they never inspect. A moving reference means whatever was
    pushed last, and it also makes the plugin depend on a registry being up.
    """
    configs = _mcp_configs()
    assert len(configs) >= 2, f"the project and plugin servers are both configured; got {configs}"

    pinned = 0
    for path in configs:
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        for name, entry in servers.items():
            for argument in entry.get("args", []):
                if "git+" not in argument:
                    continue
                pinned += 1
                match = re.search(r"git\+https://[^@\s]+@(?P<ref>[^#\s]+)", argument)
                assert match, (
                    f"{path.relative_to(REPO_ROOT)}: {name} installs from {argument!r} with no "
                    "`@<ref>` at all, which resolves to the default branch"
                )
                reference = match.group("ref")
                assert re.fullmatch(r"v\d+\.\d+\.\d+", reference) or re.fullmatch(
                    r"[0-9a-f]{40}", reference
                ), (
                    f"{path.relative_to(REPO_ROOT)}: {name} installs from {reference!r}. "
                    "Invariant 34 wants a tag (v1.2.3) or a 40-character sha, never main, "
                    "master or HEAD: this runs on an operator's workstation with their own "
                    "AWS credentials."
                )
    assert pinned == 1, (
        f"exactly one server is installed from git -- the plugin's; got {pinned} that are"
    )


def test_the_project_server_runs_the_in_tree_package() -> None:
    """A checkout uses the tree it is in; the plugin uses a published reference."""
    project = json.loads(_PROJECT_MCP.read_text(encoding="utf-8"))["mcpServers"]
    assert set(project) == {SERVER_NAME}
    entry = project[SERVER_NAME]
    assert entry["command"] == "uv"
    assert entry["args"] == ["run", "--directory", "mcp", "rca-mcp"], (
        "`uv run --directory mcp` resolves mcp/pyproject.toml alone, which is exactly why "
        f"mcp/ may not import agent/ or api/; got {entry['args']}"
    )
    assert "env" not in entry, (
        "the project-scoped .mcp.json runs the in-tree package and has no userConfig to "
        f"interpolate, so it needs no env block; got {sorted(entry['env'])}"
    )

    plugin = json.loads(_PLUGIN_MCP.read_text(encoding="utf-8"))["mcpServers"]
    assert set(plugin) == {SERVER_NAME}, (
        "the same server name in both files is what fixes the two tool-name spellings the "
        f"pinned copies use; got {sorted(plugin)}"
    )
    assert plugin[SERVER_NAME]["command"] == "uvx"
    assert plugin[SERVER_NAME]["args"][-1] == "rca-mcp"

    # Both files name a console script, so `mcp/pyproject.toml` has to declare one: without
    # it there is no command to run and both servers are dead.
    assert 'rca-mcp = "rca_mcp.server:main"' in (REPO_ROOT / "mcp/pyproject.toml").read_text(
        encoding="utf-8"
    )

    # The plugin passes the three overrides through, and interpolates only names the
    # manifest declares -- an undeclared one would interpolate to nothing forever.
    #
    # The `user_config.` prefix is load-bearing. A plugin `.mcp.json` addresses a
    # userConfig field as `${user_config.<key>}`; the bare `${<key>}` form is not resolved
    # by Claude Code, and the failure surfaces at the first tool call as `'NoneType' object
    # has no attribute 'split'` -- a traceback that names neither this file nor the field.
    # The placeholders are parsed generically below and then judged: a regex that only
    # matches the correct form cannot see the incorrect one.
    declared = set(json.loads(_PLUGIN_MANIFEST.read_text(encoding="utf-8"))["userConfig"])
    env = plugin[SERVER_NAME]["env"]
    placeholders = [
        match.group(1) for value in env.values() for match in re.finditer(r"\$\{([^}]+)\}", value)
    ]
    bare = sorted(name for name in placeholders if not name.startswith("user_config."))
    assert not bare, (
        f"plugins/{_PLUGIN_NAME}/.mcp.json interpolates {bare} with no `user_config.` "
        "prefix. A plugin .mcp.json addresses a userConfig field as "
        "`${user_config.<key>}`; nothing resolves the bare `${<key>}` form, and the "
        "failure surfaces at the first tool call as `'NoneType' object has no attribute "
        "'split'`, which names neither this file nor the field. Write "
        f"`${{user_config.{bare[0]}}}`."
    )
    interpolated = {name.removeprefix("user_config.") for name in placeholders}
    assert interpolated == declared, (
        f"the env block interpolates {sorted(interpolated)} but the manifest declares "
        f"{sorted(declared)}; an unset override arrives as the empty string, which "
        "client._env() treats as unset on purpose"
    )


def test_no_server_config_overwrites_the_ambient_aws_region() -> None:
    """Neither `.mcp.json` may set `AWS_REGION`.

    An unset `AWS_REGION` in an `.mcp.json` env block arrives as `""` and blanks the
    region the operator's shell exported; a set one overwrites it for every boto3 call the
    server makes. The server's own region pin is `RCA_REGION` (`mcp/src/rca_mcp/client.py`);
    `AWS_REGION` is the shell's to set.
    """
    for path in (REPO_ROOT / ".mcp.json", REPO_ROOT / f"plugins/{_PLUGIN_NAME}/.mcp.json"):
        for name, server in json.loads(path.read_text())["mcpServers"].items():
            env = server.get("env") or {}
            assert "AWS_REGION" not in env, (
                f"{path.relative_to(REPO_ROOT)} server {name!r} sets AWS_REGION; export "
                "the region pin as RCA_REGION and leave AWS_REGION ambient"
            )
    plugin_env = json.loads((REPO_ROOT / f"plugins/{_PLUGIN_NAME}/.mcp.json").read_text())[
        "mcpServers"
    ][SERVER_NAME]["env"]
    assert plugin_env.get("RCA_REGION") == "${user_config.aws_region}"


def test_the_marketplace_lists_the_plugin_it_ships() -> None:
    """The repository is its own marketplace, which is what makes `sample-rca@…` guessable."""
    marketplace = json.loads(_MARKETPLACE.read_text(encoding="utf-8"))
    assert marketplace["name"] == _MARKETPLACE_NAME, (
        "`/plugin install sample-rca@sample-remote-coding-agent` is the second of the two "
        "install steps, so the marketplace name is the repository name; got "
        f"{marketplace['name']!r}"
    )
    assert marketplace["owner"]["url"] == "https://github.com/aws-samples"

    plugins = marketplace["plugins"]
    assert len(plugins) == 1
    entry = plugins[0]
    assert entry["name"] == _PLUGIN_NAME
    assert entry["description"].strip()

    # A relative `source` is resolved from the marketplace's repository root, not from the
    # `.claude-plugin` directory the manifest sits in.
    source = (REPO_ROOT / entry["source"]).resolve()
    assert source == _PLUGIN_ROOT, f"{entry['source']} should be the shipped plugin directory"
    assert (source / ".claude-plugin/plugin.json").is_file()
    manifest = json.loads((source / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == entry["name"]


def test_the_plugin_name_cannot_collide_with_another_plugin() -> None:
    """The plugin name is qualified, and the three places that spell it agree.

    Claude Code derives MCP tool names as `mcp__plugin_<pluginName>_<serverName>__<tool>`,
    so two plugins installed under the same name collapse onto the same tool names and only
    one resolves -- silently. A tool call that succeeds against a different deployment's
    handler gives no indication that it reached the wrong place.

    The name carries a qualifier for the same reason `infra/app.py` sets
    `DEFAULT_STACK_PREFIX = "SampleCodingAgent"`: a plugin name is unique per installation
    the way a stack name is unique per account and region. A bare acronym is a name another
    product may already have taken, and the collision is undetectable from inside a session.
    """
    manifest = json.loads(_PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    marketplace = json.loads(_MARKETPLACE.read_text(encoding="utf-8"))

    # 1. A half-finished rename is a failure, not a plugin whose directory and id disagree.
    assert manifest["name"] == marketplace["plugins"][0]["name"] == _PLUGIN_ROOT.name, (
        f"the manifest says {manifest['name']!r}, the marketplace entry says "
        f"{marketplace['plugins'][0]['name']!r} and the directory is {_PLUGIN_ROOT.name!r}. "
        "All three are the plugin id an operator types, and a rename that lands in some of "
        "them installs a plugin whose tools resolve under a name nothing else spells."
    )
    assert manifest["name"] == _PLUGIN_NAME

    # 2. Not a name another product is already installed under.
    assert _PLUGIN_NAME not in _GENERIC_NAMES, (
        f"{_PLUGIN_NAME!r} is a bare generic name, and a bare acronym is exactly what "
        "another product is likely to have taken already. Each name in "
        f"{sorted(_GENERIC_NAMES)} is plausible enough that somebody else has shipped it."
    )

    # 3. Not a bare acronym: a qualified name has a qualifier and a separator.
    assert len(_PLUGIN_NAME) >= 8 and "-" in _PLUGIN_NAME, (
        f"{_PLUGIN_NAME!r} reads like an unqualified acronym -- `rca`, `mcp` and `cca` all "
        "fail this the same way. The plugin id shares one flat namespace with every other "
        "plugin an operator has installed, so it needs a qualifier and a separator."
    )

    # 4. The qualifier is this repository's own, read from the file that explains why.
    prefix = re.search(
        r'^DEFAULT_STACK_PREFIX = "(?P<prefix>[^"]+)"',
        _INFRA_APP.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert prefix, (
        "infra/app.py no longer spells DEFAULT_STACK_PREFIX as a plain assignment, so the "
        "convention this name follows cannot be read back from the file that owns it"
    )
    assert "sample" in _PLUGIN_NAME, (
        f"{_PLUGIN_NAME!r} drops the `sample` qualifier. It is the same one "
        f"infra/app.py's DEFAULT_STACK_PREFIX = {prefix.group('prefix')!r} carries, for the "
        "same reason: this tree ships next to a differently-built product, and the "
        "qualifier is what keeps the two apart in a namespace neither of them owns."
    )

    # 5. The actual failure, refused by literal. This is the one assertion that survives
    # somebody loosening 2, 3 and 4.
    assert f"mcp__plugin_{_PLUGIN_NAME}_{SERVER_NAME}__" != _COLLIDING_PREFIX, (
        f"the plugin's tools resolve under {_COLLIDING_PREFIX}, which is the prefix the "
        "other product already owns on an operator's machine. Only one of the two ever "
        "resolves, and Claude Code reports the loser as nothing at all."
    )

    # 6. And no file under the plugin root still spells the old name -- the shape a partial
    # rename takes is a manifest that moved and a skill that did not.
    stale: list[str] = []
    for path in sorted(_PLUGIN_ROOT.rglob("*")):
        if not path.is_file() or _SKIP & set(path.relative_to(_PLUGIN_ROOT).parts):
            continue
        text = path.read_text(encoding="utf-8")
        for spelling in (_COLLIDING_PREFIX, "/rca:"):
            if spelling in text:
                stale.append(f"{path.relative_to(REPO_ROOT)} still carries {spelling!r}")
    assert not stale, (
        "the rename stopped short:\n  "
        + "\n  ".join(stale)
        + "\nA skill left on the old spelling names tools no runtime serves under "
        f"{_PLUGIN_NAME!r} -- or worse, names the other product's, which is how this "
        "started. The slash commands are `<pluginName>:<skillName>`, so they move with the "
        "name too."
    )

    # 7. The version is what a marketplace compares, and it is the only channel a rename has.
    version = manifest["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f"the manifest version is {version!r}, which a marketplace cannot compare"
    )
    assert tuple(int(part) for part in version.split(".")) > (0, 1, 0), (
        f"the plugin was renamed but is still at {version}, the version that shipped as "
        "`rca`. A version comparison is what a marketplace has to announce a change with, "
        "and a rename is as breaking as a 0.x plugin gets: the id an operator installed no "
        "longer exists, and nothing else in this repository can tell their "
        "installed_plugins.json entry that it is gone."
    )


@pytest.mark.parametrize(("name", "command", "skill"), _PAIRS)
def test_the_plugin_skill_is_a_pinned_copy_of_the_project_command(
    name: str, command: Path, skill: Path
) -> None:
    """The two bodies are equal after one documented substitution table."""
    _, command_body = _frontmatter(command)
    _, skill_body = _frontmatter(skill)

    substituted = skill_body
    for plugin_spelling, project_spelling in _SUBSTITUTIONS:
        substituted = substituted.replace(plugin_spelling, project_spelling)

    assert substituted == command_body, (
        f"plugins/{_PLUGIN_NAME}/skills/{name}/SKILL.md has drifted from "
        f".claude/commands/{name}.md. "
        "It is a copy on purpose: installing a plugin copies its directory to a cache, so "
        "the plugin can reference neither `../mcp` nor the sibling command file. Edit the "
        "two together -- and note that deleting the project-scoped copy removes the "
        f"un-namespaced /{name} from this checkout, it does not fall back to the plugin."
    )


def test_every_project_command_has_a_plugin_skill() -> None:
    """A new command cannot ship without its pinned copy."""
    commands = sorted(path.stem for path in _COMMANDS.glob("*.md"))
    assert commands, f"{_COMMANDS} holds the un-namespaced commands for this checkout"
    for stem in commands:
        assert (_SKILLS / stem / "SKILL.md").is_file(), (
            f"/{stem} exists in this checkout but not in the plugin, so an operator who "
            "installed the plugin never gets it"
        )

    shipped = sorted(path.name for path in _SKILLS.iterdir() if path.is_dir())
    assert shipped == sorted({*commands, *_ONE_WAY_SKILLS})


@pytest.mark.parametrize(("name", "command", "skill"), _PAIRS)
def test_each_copy_uses_its_own_tool_name_prefix(name: str, command: Path, skill: Path) -> None:
    """Each copy spells the tools the way its own runtime does.

    Frontmatter included, because `allowed-tools` is where a wrong spelling costs the most:
    in the plugin copy the bare name denies every tool, and Claude Code reports that as
    nothing at all -- no error, just a skill that never calls anything.
    """
    tool_names = {spec.name for spec in TOOLS}
    plugin_prefix = f"mcp__plugin_{_PLUGIN_NAME}_{SERVER_NAME}__"
    project_prefix = f"mcp__{SERVER_NAME}__"

    plugin_mentions = _tool_mentions(skill.read_text(encoding="utf-8"))
    assert len(plugin_mentions) >= len(tool_names)
    for mention in plugin_mentions:
        assert mention.startswith(plugin_prefix), (
            f"{skill.relative_to(REPO_ROOT)} names {mention!r}. A plugin-provided server is "
            "namespaced, and the bare spelling denies every tool with no diagnostic."
        )
        assert mention.removeprefix(plugin_prefix) in tool_names

    command_mentions = _tool_mentions(command.read_text(encoding="utf-8"))
    assert len(command_mentions) >= len(tool_names)
    for mention in command_mentions:
        assert not mention.startswith("mcp__plugin_"), (
            f"{command.relative_to(REPO_ROOT)} names {mention!r}, which is the plugin's "
            "spelling; a project-scoped server is not namespaced"
        )
        assert mention.startswith(project_prefix)
        assert mention.removeprefix(project_prefix) in tool_names

    # Every tool is reachable from each copy, so `allowed-tools` cannot silently omit one.
    for spec in TOOLS:
        assert f"{plugin_prefix}{spec.name}" in skill.read_text(encoding="utf-8")
        assert f"{project_prefix}{spec.name}" in command.read_text(encoding="utf-8")


def test_the_remote_command_keeps_its_guardrails() -> None:
    """The four sentinels, in both copies, and frontmatter that parses.

    They are ported unchanged because they are what keeps a free-prose command safe: the
    command reads a repository's own text, and a prompt in an issue is an untrusted input
    that would otherwise be arguing with nothing.
    """
    for path in (_COMMANDS / "remote.md", _SKILLS / "remote/SKILL.md"):
        text = path.read_text(encoding="utf-8").lower().replace("`", "")
        for guardrail in _GUARDRAILS:
            assert guardrail in text, (
                f"{path.relative_to(REPO_ROOT)} dropped the guardrail {guardrail!r}"
            )
        assert "revised" in text, "a second `plan` step is a revision, not a retry"
        assert "409 configurationnotready" in text, "a PENDING repository refuses tasks"

    for name in sorted({*(pair[0] for pair in _PAIRS), *_ONE_WAY_SKILLS}):
        path = _SKILLS / name / "SKILL.md"
        fields, _ = _frontmatter(path)
        assert fields.get("name") == name, (
            "the frontmatter `name` is what `plugin:skill` matches on, so one that "
            f"disagrees with its directory is a skill nothing can enable; got {fields.get('name')!r}"
        )
        description = fields.get("description", "")
        assert description, "the description is the whole of the trigger"
        assert ":" not in description, (
            "a colon in an unquoted YAML scalar is a parse error, and the CLI reports a "
            f"skill whose frontmatter failed to parse as one that does not load; got {description!r}"
        )

    command_fields, _ = _frontmatter(_COMMANDS / "remote.md")
    assert command_fields["description"]
    assert ":" not in command_fields["description"]
    assert ":" not in command_fields["argument-hint"]


def test_the_scaffold_skill_carries_both_language_templates() -> None:
    """Both languages, each with the three files a scaffolded tree needs regardless of it.

    `CLAUDE.md` is not decoration: `nodes/code.py`'s `CODE_PROMPT` tells the coder to read
    it before writing anything, so it is the only channel this repository has for telling a
    future run where the source and the tests live.

    The paths are checked as well as the contents, because the skill writes them with
    `Write` and a `..` segment or a leading `/` puts a file outside the repository the
    operator pointed at. A top-level `.agent/` is refused for a different reason:
    `checkout._quarantine_repo_agent_dir` renames a clone-provided one to `.agent.repo/`,
    and that rename is untracked, satisfies the invariant-5 no-op guards, and gets
    committed into the first pull request by `git add -A`.
    """
    templates = _scaffold_templates()
    assert set(templates) == set(_SCAFFOLD_EXPECTED), (
        "scaffold/SKILL.md is the only place the two language templates exist, and the "
        f"parse found {sorted(templates)} rather than {sorted(_SCAFFOLD_EXPECTED)}. An "
        "empty or partial parse is a skill that writes nothing, which reads as a skill "
        "that ran."
    )
    for language, files in templates.items():
        assert files, f"the {language} template section parsed to no files at all"
        assert {"CLAUDE.md", "README.md", ".gitignore"} <= set(files), (
            f"the {language} template omits one of CLAUDE.md, README.md or .gitignore; it "
            f"writes {sorted(files)}. `.gitignore` is what keeps a build directory or a "
            "virtualenv out of the agent's first `git add -A`."
        )
        for path, content in files.items():
            parts = Path(path).parts
            assert not path.startswith("/") and not Path(path).is_absolute(), (
                f"the {language} template writes {path!r}, an absolute path"
            )
            assert ".." not in parts, (
                f"the {language} template writes {path!r}, which escapes the repository"
            )
            assert parts[0] != ".agent", (
                f"the {language} template writes {path!r}. A top-level `.agent/` is this "
                "task's own state directory: the agent renames a clone-provided one to "
                "`.agent.repo/` and commits the rename into the pull request."
            )
            assert content.strip(), (
                f"the {language} template's {path} block is empty, so the skill writes an "
                "empty file and the tree it produces declares nothing"
            )


def test_the_scaffolded_repo_declares_the_entry_points_the_agent_sniffs(tmp_path: Path) -> None:
    """The tree each template produces is one `nodes/checkout.py` really detects.

    Asserted against the agent's own functions rather than against a retyped copy of what
    they look for, because the whole point of the skill is agreement with them. Detection
    reads a root `Makefile`'s targets or a root `package.json`'s `scripts` and nothing else
    -- no `pyproject.toml`, no `tox.ini`, no bare `pytest` fallback -- so a template that
    declares its entry points anywhere else declares them nowhere.

    What a failure here costs is a whole class of run: with no verification command
    `nodes/verify.py` returns `exitCode: None`, `review._compose` yields `verified: False`,
    and `nodes/submit.py` opens a **draft** pull request marked `SUCCEEDED_UNVERIFIED` --
    every time, on every task, with nothing in the run saying the suite was never found.
    """
    # Imported inside the test on purpose: `coding_agent.git_ops` rewrites the ambient
    # `GIT_CONFIG_*` group at import, and `_git_env` above exists because of it. Keeping the
    # import off module scope keeps that side effect inside the one test that needs it.
    from coding_agent.nodes import checkout

    templates = _scaffold_templates()
    assert templates, "nothing parsed, so the assertions below would pass vacuously"
    for language, (expected_setup, expected_verify) in _SCAFFOLD_EXPECTED.items():
        root = tmp_path / language
        for path, content in templates[language].items():
            written = root / path
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_text(content, encoding="utf-8")

        assert checkout._detect_setup_command(root) == expected_setup, (
            f"the {language} template produces a tree whose setup command detects as "
            f"{checkout._detect_setup_command(root)!r}, not {expected_setup!r}. Only a "
            "`setup:` or `bootstrap:` Makefile target or a `scripts.setup` counts, and a "
            "target line has to start at column 0 for `checkout._MAKE_TARGET` to see it."
        )
        assert checkout._detect_verify_command(root) == expected_verify, (
            f"the {language} template produces a tree whose verify command detects as "
            f"{checkout._detect_verify_command(root)!r}, not {expected_verify!r}. Nothing "
            "downstream recovers from that: the run verifies nothing and every pull "
            "request it opens is a draft marked SUCCEEDED_UNVERIFIED."
        )


def test_the_scaffolded_templates_parse() -> None:
    """Every JSON block is JSON, every Python block is Python, and the `pwd -P` survives.

    Shape only. `make test` never runs `npm install` or `pip install`, so a `typescript`
    version that does not exist still passes here -- which is why step 4 of the skill runs
    the scaffolded repository's own setup and test before it offers to push. That check
    belongs in the skill and not in this suite, because putting it here would put the
    network on the critical path of `make test`.
    """
    templates = _scaffold_templates()
    assert templates, "nothing parsed, so the loops below would pass vacuously"
    for language, files in templates.items():
        for path, content in files.items():
            if path.endswith(".json"):
                try:
                    json.loads(content)
                except json.JSONDecodeError as error:
                    pytest.fail(
                        f"the {language} template's {path} block is not valid JSON "
                        f"({error}). A manifest with a trailing comma or a comment in it is "
                        "a repository whose entry points detect as nothing at all: "
                        "`checkout._package_scripts` logs the parse failure and returns {}."
                    )
            if path.endswith(".py"):
                try:
                    ast.parse(content)
                except SyntaxError as error:
                    pytest.fail(
                        f"the {language} template's {path} block does not parse as Python "
                        f"({error}), so the scaffolded repository's own `make test` is red "
                        "from the first commit"
                    )

    makefile = templates["Python"]["Makefile"]
    assert "pwd -P" in makefile, (
        "the Python template's Makefile dropped the `pwd -P` that resolves `.venv`. "
        "`checkout._link_venv` creates `.venv` as a SYMLINK before the setup command runs, "
        "and `python -m venv` refuses to build into one -- `EnvBuilder.ensure_directories` "
        "checks `os.path.islink` and `--clear` does not help. It fails as "
        "`ValueError: Unable to create directory` inside the runtime and nowhere else, so "
        "no local run of `make setup` ever reproduces it."
    )
    lines = makefile.splitlines()
    # The recipe line, not a comment that merely mentions it -- otherwise a template whose
    # only `pwd -P` is in prose satisfies the assertion below with itself.
    index = next(
        number
        for number, line in enumerate(lines)
        if "pwd -P" in line and not line.lstrip().startswith("#")
    )
    assert any(line.lstrip().startswith("#") for line in lines[max(0, index - 8) : index]), (
        "the Python template's `pwd -P` carries no comment above it. It reads like an "
        "over-complicated `python -m venv .venv` and the next person to touch this template "
        "simplifies it away; the note is what stops them."
    )


def test_the_scaffold_skill_confirms_before_it_pushes() -> None:
    """An explicit wait for a yes before the first `git push`, and never a force.

    Same shape the removed `setup` skill used before `make deploy`, and for the same
    reason: the push is the one step here that changes something outside the operator's
    own working tree. It is also the step that has to happen -- until the default branch
    exists on the remote the registration write probe hits GitHub's 409
    `Git Repository is empty` and reaches no verdict, `git_ops.default_branch` assumes
    `main` unchecked, the pull request has no base, and `/security-review` is skipped -- so
    the skill argues for it and then waits, rather than skipping either half.
    """
    text = _SCAFFOLD_SKILL.read_text(encoding="utf-8")
    before = text[: text.index("git push")].lower()
    assert re.search(r"wait[^.\n]{0,80}yes", before), (
        "nothing before the first `git push` in scaffold/SKILL.md tells the model to WAIT "
        "for an explicit yes. The push creates a branch on a remote the operator owns, and "
        "a skill that pushes because a repository was mentioned is the wrong default."
    )

    for line in text.splitlines():
        if "git push" not in line:
            continue
        for word in line.split():
            assert word != "-f" and not word.startswith("--force"), (
                f"scaffold/SKILL.md offers {line.strip()!r}. There is nothing on the remote "
                "to overwrite -- a push that needs force is a repository that was not "
                "empty, which is a step 1 that was wrong, not a flag to add."
            )

    lowered = text.lower().replace("`", "")
    for guardrail in _SCAFFOLD_GUARDRAILS:
        assert guardrail in lowered, f"scaffold/SKILL.md dropped the guardrail {guardrail!r}"

    python = text.index("\n## Python")
    typescript = text.index("\n## TypeScript")
    assert python < typescript, (
        "scaffold/SKILL.md puts the TypeScript template before the Python one. The order is "
        "where the default is written down, and a model reading top to bottom takes the "
        "first one it finds."
    )
    heading = text[python + 1 : text.index("\n", python + 1)]
    assert "default" in heading.lower(), (
        f"the Python template's heading is {heading!r} and does not say `default`. `Python "
        "unless the operator asked for TypeScript by name` is a decision the skill has to "
        "carry in the heading a model skims, not only in the prose above it."
    )


def test_the_preflight_target_checks_every_prerequisite() -> None:
    """The ten checks are all still there, and the bootstrap command is printed, not run.

    Each dropped check costs the same thing: a failure that arrives minutes later out of
    the CDK CLI or CloudFormation, in their wording, instead of one sentence and a `fix:`.
    The last two checks are worth an AWS call each because both failures otherwise land
    only AFTER the ~1.3 GB ARM64 image has been built and pushed -- and the profile one is
    worse than late: `MODEL_RE` in `agent/src/coding_agent/config.py` validates an id's
    PATTERN and not its GEOGRAPHY, so a wrong-region profile passes the container's
    start-up `validate_model` and fails at invocation time instead, a silent runtime failure
    rather than a deploy-time one. The three `infra/app.py` defaults are `global.` to avoid
    this; only an explicit geo-scoped override reaches that hazard now. The assertions below
    are the sharp ones -- a `fix:` that is shell rather than a printf argument is a fix
    nobody reads, a `bootstrap aws://` line NOT inside a printf is preflight running
    bootstrap on the operator's behalf (bootstrap is a different privilege level from a
    deploy, and it is needed once per account and region rather than on every run), a
    constant renamed in `infra/app.py` turns the profile check into a silent no-op, and an
    `aws` verb that is not read-only makes a target documented as safe at any time unsafe.

    The AgentCore check is also held to what it can honestly claim. It may only make
    read-only calls -- this target is documented safe at any time and the read-only-verb
    loop below enforces it -- and no read-only call distinguishes "the endpoint resolves in
    this region" from "these credentials may create a runtime here". So the check reports the
    endpoint resolving and disclaims `CreateAgentRuntime` by name rather than implying
    availability; the `CreateAgentRuntime` probe is what keeps that disclaimer in the recipe.

    The ECR check is a login and not an inspection, because `docker info` answering is not
    the same capability: a `credHelpers` entry mapping this account's registry to
    `docker-credential-ecr-login` implements `get` and deliberately not `store`, while the
    CDK CLI runs `docker login` regardless, so the publish step dies about eight seconds
    after synth with `error saving credentials ... not implemented` -- an error that names
    neither Docker configuration nor ECR, and that costs the ~1.3 GB ARM64 build to reach.
    Hence the four probes below: the `get-` verb that takes the token, the login itself, and
    the two `~/.docker/config.json` keys the `fix:` has to name.

    The synth check is check 10 and the only thing in this repository that ever calls
    `infra/app.py::main()`: `AgentStack` and `ApiStack` bundle Docker at construction time,
    so no pytest can build them, and `test_the_cdk_entry_point_imports_without_building_an_app`
    deliberately forbids an `aws_cdk.App()` at import. Hence the three assertions after the
    table: the `-c` flags it passes are a superset of the deploy's, so it cannot drift into
    synthesizing a different app than the deploy builds; it runs after every cheap check,
    because on a cold machine it pays the ~1.3 GB ARM64 build and a missing region must not
    cost that to discover; and it names no stack, so the cross-stack references -- which are
    the part that broke -- are still reported.
    """
    recipe = _recipe("preflight")

    for probe, what in (
        ("command -v docker", "docker on PATH"),
        ("docker info", "the docker DAEMON answering, not merely docker installed"),
        ("command -v uv", "uv on PATH, which is how Claude Code starts the MCP server"),
        ("node --version", "a Node new enough for the CDK CLI"),
        ("sts get-caller-identity", "credentials that answer, and the account they answer for"),
        ("AWS_REGION", "a region resolved from the environment or the profile, never guessed"),
        ("$(BOOTSTRAP_STACK)", "cdk bootstrap already done in that account and region"),
        (
            "bedrock-agentcore-control list-agent-runtimes",
            "Bedrock AgentCore actually existing "
            "in the region preflight resolved -- a region without it cannot even resolve the "
            "endpoint",
        ),
        (
            "Could not connect to the endpoint URL",
            "the endpoint-resolution failure told apart "
            "from an AccessDenied, which means the service IS there",
        ),
        (
            "CreateAgentRuntime",
            "the AgentCore check saying what it does NOT prove: "
            "`list-agent-runtimes` answering is the endpoint resolving, and eu-west-1 answered "
            "that same call and then refused the create 26 minutes into a deploy",
        ),
        (
            "list-inference-profiles",
            "every inference profile this deploy forwards into a runtime existing in that region",
        ),
        ("ACTIVE", "the profile STATUS, not merely a name that appears in the listing"),
        (
            "RCA_MODEL_IDS",
            "the model ids read out of infra/app.py rather than re-typed into the Makefile",
        ),
        (
            "CDK_CONTEXT='-c ",
            "the geography `fix:` offering the one way out that edits no "
            "tracked file -- `make deploy CDK_CONTEXT='-c haikuModelId=...'`, which the "
            "RCA_MODEL_IDS block already reads back so the check validates the ids the deploy "
            "will really use",
        ),
        (
            "ecr get-login-password",
            "the ECR authorization token the CDK publish step needs, "
            "taken with a `get-` verb so this target stays read-only",
        ),
        (
            "docker login",
            "the login CDK itself performs, actually performed -- a daemon that "
            "answers is not a daemon that can store an ECR credential",
        ),
        (
            "credHelpers",
            "the `fix:` naming the ~/.docker/config.json key that causes `error "
            "saving credentials ... not implemented`, an error that names neither Docker "
            "configuration nor ECR",
        ),
        (
            "credsStore",
            "the other half of that `fix:` -- the setting to point at a helper "
            "that implements `store`",
        ),
        (
            "$(CDK) synth",
            "the whole five-stack app synthesized through the pinned CDK CLI, "
            "which is the only thing in this repository that ever calls infra/app.py::main()",
        ),
    ):
        assert probe in recipe, f"the preflight recipe no longer checks {what} ({probe!r} is gone)"

    # The synth check earns its minutes only if it synthesizes the app the deploy builds, so
    # every `-c` key the deploy passes has to be passed here too. Subset and not equality:
    # preflight may check more than the deploy sets.
    preflight_context = set(re.findall(r"-c ([A-Za-z]+)=", recipe))
    deploy_context = set(re.findall(r"-c ([A-Za-z]+)=", _recipe("deploy")))
    assert deploy_context <= preflight_context, (
        f"the preflight synth passes {sorted(preflight_context)} but the deploy passes "
        f"{sorted(deploy_context)}, so it is validating a different app. A synth missing "
        "`account` or `region` does not fail for a reason about the app at all -- it fails "
        "with CrossRegionReferencesRequireExplicitRegion, because the CDK CLI injects neither "
        "context key (it exports CDK_DEFAULT_ACCOUNT/CDK_DEFAULT_REGION instead) and "
        "AccountStack and WebStack both set cross_region_references=True. One missing "
        "`stackPrefix` or `budgetAlertEmail` is quieter and worse: the synth passes on a "
        "composition `make deploy` is never going to build."
    )

    assert (
        recipe.index("$(CDK) synth") > recipe.index("docker info")
        and recipe.index("$(CDK) synth") > recipe.index("ecr get-login-password")
        and recipe.index("$(CDK) synth") > recipe.index("list-inference-profiles")
    ), (
        "the `cdk synth` check runs before a cheaper one. It is the only check ordered by COST "
        "rather than by likelihood: on a machine with no image layers cached it builds the "
        "~1.3 GB ARM64 image, so a dead daemon, a broken credential helper or a region without "
        "the inference profiles must be discovered before it and not after paying for it."
    )

    synth = next(line for line in recipe.splitlines() if "$(CDK) synth" in line)
    for stack in ("$(API_STACK)", "$(CONSOLE_STACK)", "$(ACCOUNT_STACK)"):
        assert stack not in synth, (
            f"the preflight synth names {stack}, which makes it a stack subset. All of "
            "infra/app.py::main() runs either way, so the stacks are still CONSTRUCTED -- what "
            "a selector drops is the reporting of the cross-stack references between them, "
            "which is the part that broke. No stack selector means every stack."
        )

    # The geography `fix:` has to name all three ways out AND prefer the one that edits no
    # tracked file. `CDK_CONTEXT` is not a convenience here: `RCA_MODEL_IDS` already
    # `shlex.split`s it when resolving which ids the deploy will use, and both this target's
    # synth check and the `deploy` recipe already forward `$(CDK_CONTEXT)` to `cdk` -- so a
    # `fix:` that offers only `export AWS_REGION` or editing `infra/cdk.json` sends the
    # operator to change a tracked file for something the deploy already accepts from the
    # environment, and disagrees with the implementation it is describing.
    geography = recipe[recipe.index("are not ACTIVE in") :]
    geography = geography[: geography.index("exit 1")]
    assert "CDK_CONTEXT='-c " in geography, (
        "the geography `fix:` no longer prints `make deploy CDK_CONTEXT='-c "
        "haikuModelId=...'`, which is the only one of the three ways out that edits no "
        "tracked file -- and the one preflight itself reads back"
    )
    assert geography.index("CDK_CONTEXT") < geography.index("infra/cdk.json"), (
        "the geography `fix:` offers editing infra/cdk.json before it offers CDK_CONTEXT. "
        "Editing a tracked file is the worst of the three options and must be listed last"
    )
    for kept in (
        "MODEL_RE",
        "agent/src/coding_agent/config.py",
        "AWS_REGION",
        "list-inference-profiles",
    ):
        assert kept in geography, (
            f"the geography failure no longer mentions {kept!r}. The `fix:` gained an option; "
            "it must not have lost the geography explanation, the note that MODEL_RE "
            "validates an id's pattern and not its region, or the command that lists what "
            "the region does have"
        )

    assert "fix:" in recipe, "every preflight failure names an action; that action is the `fix:`"
    for line in recipe.splitlines():
        if "fix:" not in line:
            continue
        assert line.lstrip().startswith('"') or "printf" in line or "echo" in line, (
            f"preflight line {line.strip()!r} carries a `fix:` outside a printf or echo "
            "argument, so it is shell and not a message the operator sees"
        )

    assert "bootstrap aws://" in recipe, (
        "preflight no longer prints the exact `cdk bootstrap aws://<account>/<region>` "
        "command, which leaves an operator told to bootstrap and guessing the arguments"
    )
    for line in recipe.splitlines():
        if "bootstrap aws://" not in line:
            continue
        assert "printf" in line or "echo" in line, (
            f"preflight line {line.strip()!r} looks like it RUNS cdk bootstrap. It is printed "
            "and never run, deliberately: different privilege level, needed once per account "
            "and region."
        )

    # The drift guard. The whole Makefile, not just the recipe: the ids live in the
    # `RCA_MODEL_IDS` block above it, and that block matches `infra/app.py` by regex.
    makefile = _MAKEFILE.read_text(encoding="utf-8")
    app = _INFRA_APP.read_text(encoding="utf-8")
    for const in ("_DEFAULT_PLAN_MODEL", "_DEFAULT_CODE_MODEL", "_DEFAULT_HAIKU_MODEL"):
        assert const in makefile, (
            f"the Makefile's RCA_MODEL_IDS block no longer reads {const}, so preflight is "
            "checking fewer profiles than the deploy forwards into every runtime"
        )
        assert re.search(rf'^{const} = "[^"]+"', app, re.MULTILINE), (
            f"infra/app.py no longer spells {const} as a plain assignment, which is the "
            "form the Makefile's RCA_MODEL_IDS block matches. Renaming or reformatting it "
            "does not fail the deploy -- it makes the preflight profile check silently "
            "check nothing, and a wrong-region profile then fails at INVOCATION time "
            "because config.MODEL_RE validates the id's pattern and not its geography."
        )

    # The read-only guard. Message lines are skipped exactly as the `fix:` loop above does
    # it, or the 'aws sso login' inside a `fix:` string is a false positive.
    for line in recipe.splitlines():
        if line.lstrip().lstrip("@").startswith('"'):
            continue
        for service, verb in re.findall(r"\baws\s+([a-z0-9-]+)\s+([a-z0-9-]+)", line):
            assert verb.startswith(("get-", "describe-", "list-")) or verb == "get", (
                f"preflight calls `aws {service} {verb}`, which is not a read-only verb. "
                "This target is documented as safe to run at any time, and `deploy` runs it "
                "before it has asked the operator for anything at all, so a call that "
                "creates, deletes or updates cannot live here."
            )


def test_the_deploy_target_does_what_the_skill_promises() -> None:
    """The `deploy` recipe still does the six steps, in the order the console needs them.

    `README.md`'s deploy step promises this and cannot check it, so a step quietly dropped from
    the recipe becomes a walkthrough that lies. Each one fails silently in its own way: no
    `web/config.js` and `s3 sync` and the console URL serves an empty bucket
    (`infra/stacks/web_stack.py` carries no BucketDeployment precisely because this recipe
    does it); no `s3api head-object` and an upload that produced nothing is a green deploy;
    no `admin-create-user` and self-signup is off, so nobody can sign in at all; no
    `UsernameExistsException` arm and the SECOND deploy fails on the operator who already
    exists; no `endpoint.json` and a running MCP server needs a restart it was promised
    it would not.
    """
    recipe = _recipe("deploy")

    for probe, step in (
        ("$(EMAIL)", "the EMAIL guard, and the first console operator it becomes"),
        ("preflight", "step 1, preflight"),
        ("$(CDK) deploy --all", "step 2, the four stacks through the pinned CDK CLI"),
        (
            "--outputs-file",
            "step 2's outputs file, which every later step reads instead of "
            "calling describe-stacks per value",
        ),
        (
            "-c account=",
            "step 2's account context, without which the four in-region stacks are "
            "environment-agnostic and AccountStack's cross_region_references=True refuses them",
        ),
        (
            "-c region=",
            "step 2's region context -- the CDK CLI exports CDK_DEFAULT_REGION as an "
            "environment variable and injects no `region` context key, so app.py's "
            "try_get_context('region') is None without this and synth fails outright",
        ),
        ("web/config.js", "step 3, the console configuration written from the stack's outputs"),
        ("s3 sync", "step 3, the console upload"),
        ("create-invalidation", "step 3's CloudFront invalidation after the upload"),
        ("s3api head-object", "step 3's assertion that index.html actually landed"),
        ("admin-create-user", "step 4, the first console operator"),
        (
            "UsernameExistsException",
            "step 4's idempotence -- an existing operator is not a failure",
        ),
        ("endpoint.json", "step 5, the endpoint document a running MCP server already reads"),
        ("remote.origin.url", "step 6, the repository this was run from"),
    ):
        assert probe in recipe, f"the deploy recipe no longer does {step} ({probe!r} is gone)"

    assert "15-25" in recipe, (
        "the `deploy` recipe dropped the 15-25 minute warning. A human running the target "
        "directly reads this one, and it has to be printed BEFORE cdk starts: a twenty-minute "
        "silence while the ARM64 image builds is indistinguishable from a hang."
    )

    assert recipe.index("s3 sync") < recipe.index("admin-create-user"), (
        "the deploy recipe uploads the console AFTER `cognito-idp admin-create-user`, which "
        "`exit 1`s on anything but `UsernameExistsException` -- a credential without "
        "cognito-idp:AdminCreateUser, a rejected address, or a Ctrl-C during the 15-25 minute "
        "first deploy. The distribution is live from the moment `cdk deploy` returns, so an "
        "upload behind a step that can fail for its own reasons leaves a live CloudFront "
        "distribution over an empty S3 origin, and the console URL then answers with S3's own "
        "<Error><Code>AccessDenied</Code></Error> XML instead of anything about the real cause."
    )

    for key in _CACHE_KEYS:
        assert key in recipe, (
            f"the endpoint document written by `make deploy` omits {key!r}. The five keys are a "
            "contract with rca_mcp.client._write_cache, and a cache that disagrees is not an "
            "error -- it is silently discarded on every server start."
        )

    # Derived from the stack rather than spelled out, so this cannot drift from the outputs
    # `tests/test_web_stack.py` pins. Importing api_stack is safe; only CONSTRUCTING a stack
    # needs Docker.
    outputs = re.findall(
        r'CfnOutput\(\s*self,\s*"([A-Za-z]+)"', _WEB_STACK.read_text(encoding="utf-8")
    )
    assert len(outputs) >= 7, f"WebStack publishes the values this recipe reads; found {outputs}"
    for name in (*outputs, API_URL_OUTPUT_KEY):
        assert name in recipe, (
            f"WebStack publishes {name!r} for `make deploy` to read and the deploy recipe never "
            "mentions it; an output nothing reads is an output nobody notices is wrong"
        )

    for label in ("console:", "api:", "region:"):
        assert label in recipe, (
            f"the deploy prints exactly three labelled facts and {label!r} is not one of them"
        )

    # The closing paragraph must name the override fields (`api_url`, `aws_region`,
    # `stack_name`) because `mcp/src/rca_mcp/client.py::resolve_endpoint` returns an
    # `RCA_API_URL` override without reading the cache, so a plugin configured with one
    # keeps registering against the older deployment while the freshly deployed fleet lists
    # zero configurations. Without the warning an operator following the deploy output would
    # not know to clear the stale value.
    closing = recipe[recipe.index("endpoint.json carries that API URL now") :]
    assert "userConfig" in closing or all(
        name in closing for name in ("api_url", "aws_region", "stack_name")
    ), (
        "the deploy's closing output promises `endpoint.json` is picked up with no restart "
        "and never mentions that a plugin userConfig override (api_url, aws_region, "
        "stack_name) beats it. resolve_endpoint returns RCA_API_URL without reading the "
        "cache at all, so that promise is false for exactly the operator most likely to "
        "have two deployments"
    )
    assert "precedence" in closing or "takes precedence" in closing, (
        "the closing note names the override fields but not that they WIN over the "
        "endpoint document just written, which is the whole point of the warning"
    )


def test_the_pinned_reference_is_not_older_than_the_fix_it_ships() -> None:
    """No commit reachable from `HEAD` that touches `mcp/` may be newer than the pin.

    Invariant 34 pins a sha, and `test_nothing_is_installed_from_a_moving_reference` checks
    the *shape* of that pin -- but a sha that is merely well-formed can still be older than
    the code it is supposed to ship, which is silent: the plugin installs, every tool runs,
    and the operator gets guidance from an older version. Enumerating the symbols the pinned
    tree must contain is not sufficient: a pin can satisfy a symbol list and still be behind
    every later change to `mcp/`. The primary property is *currency* -- expressed as
    `git rev-list -1 <pin>..HEAD -- mcp/` printing nothing -- and it goes red for any future
    `mcp/` change that lands without a re-pin. The `_PROVENANCE_SYMBOLS` loop stays as a
    secondary check for *content* rather than ancestry: a pin on an unrelated branch could
    conceivably pass the range check while missing expected symbols.

    A skip is appropriate here because this asserts a property of *history*, and history is
    an optional part of a checkout: a shallow clone or an image without git has the pin but
    not the object it names, so there is no evidence either way. The skip is narrow: once the
    commit is known to be present, a stale pin is a hard failure.
    """
    sha = _pinned_reference()
    if not shutil.which("git") or not (REPO_ROOT / ".git").exists():
        pytest.skip("no git or no .git directory: the pinned commit's tree cannot be read")

    present = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=REPO_ROOT,
        env=_git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if present.returncode != 0:
        pytest.skip(
            f"the pinned commit {sha} is not in this checkout (a shallow clone has no "
            "history), so its tree cannot be inspected"
        )

    # The currency check. Anything this lists is a commit reachable from HEAD, not from the
    # pin, that touched mcp/ -- i.e. code the plugin does not ship.
    listed = subprocess.run(
        ["git", "rev-list", "-1", f"{sha}..HEAD", "--", "mcp/"],
        cwd=REPO_ROOT,
        env=_git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    # Asserted before reading stdout, so a git failure is not mistaken for "no output,
    # therefore current" -- that silence is the failure mode this test exists to break.
    assert listed.returncode == 0, (
        f"`git rev-list -1 {sha}..HEAD -- mcp/` exited {listed.returncode} "
        f"({listed.stderr.strip()}), so the pin's currency is unknown rather than good"
    )
    newest = listed.stdout.strip()
    subject = newest
    if newest:
        described = subprocess.run(
            ["git", "log", "-1", "--format=%h %s", newest],
            cwd=REPO_ROOT,
            env=_git_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        # A diagnostic must not itself raise, so a failed `git log` degrades to the bare sha.
        subject = described.stdout.strip() if described.returncode == 0 else newest
    assert not newest, (
        f"plugins/{_PLUGIN_NAME}/.mcp.json pins {sha}, but {newest} ({subject}) changed mcp/ "
        "after it. The pin must be moved forward to a sha that is at or after the newest "
        "commit touching mcp/ AND already pushed -- uvx fetches the tree from the GitHub "
        "remote, so a local-only commit installs nothing; a sha reachable from origin/main is "
        "what is required. A stale pin is silent: the plugin installs, every tool runs, and "
        "the operator gets wrong guidance from a working server -- at 766004d6 the four "
        "endpoint-discovery messages in mcp/src/rca_mcp/client.py name /sample-rca:setup's "
        "predecessor /rca:setup, which does not exist in this plugin and, on a workstation "
        "where an unqualified plugin of that name is installed, resolves into someone "
        "else's deploy flow."
    )

    for path, symbol in _PROVENANCE_SYMBOLS:
        shown = subprocess.run(
            ["git", "show", f"{sha}:{path}"],
            cwd=REPO_ROOT,
            env=_git_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert shown.returncode == 0, (
            f"plugins/{_PLUGIN_NAME}/.mcp.json pins {sha}, whose tree has no {path} at all "
            f"(git show exited {shown.returncode}: {shown.stderr.strip()}). That pin cannot "
            f"define {symbol}, so the `endpoint used:` line is missing from the server every "
            "operator installs."
        )
        assert symbol in shown.stdout, (
            f"plugins/{_PLUGIN_NAME}/.mcp.json pins {sha}, and {path} at that commit does "
            f"not define `{symbol}`. The pin is older than the fix it is supposed to ship: an "
            "operator installing the plugin gets an MCP server with no `endpoint used:` line, so a "
            "`userConfig` override (api_url, aws_region, stack_name) silently aims every "
            "tool at a different deployment with no way to tell. Repin to a sha reachable "
            "from origin/main whose mcp/ tree has it."
        )
