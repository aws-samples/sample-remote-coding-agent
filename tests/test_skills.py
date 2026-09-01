"""The two Agent Skills: one plugin root, two skills, static content, no runtime generation.

Everything here is a contract on files that a *model* reads, which is exactly why it is
tested. A skill whose frontmatter does not parse does not load, and the CLI reports that
as nothing at all -- no error, no skill, and a role that silently never invokes it. The
same goes for the verdict token: `nodes/review.py` parses `VERDICT: PASS` out of the
review role's closing message, and a skill that showed the role any other spelling would
turn every review into the missing-verdict veto.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from agent_helpers import no_aws
from coding_agent import config, subagents
from conftest import REPO_ROOT

# The one plugin root. Named once, because "exactly one" is half of what this file
# asserts and a second literal would be a second place for it to become two.
_PLUGIN_ROOT = REPO_ROOT / "agent/skills"
_SKILLS_DIR = _PLUGIN_ROOT / "skills"
_SKILL_NAMES = ("repo-conventions", "review-rigor")


def _frontmatter(path: Path) -> dict[str, str]:
    """Parse a `SKILL.md`'s YAML frontmatter into a flat dict of stripped strings.

    By hand and not with pyyaml: this repository pins its dependencies and adding one
    for four lines of splitting is a worse trade than the four lines. The frontmatter a
    skill may use is one `key: value` per line, which is all this parses -- a block
    scalar or a nested key here would fail loudly rather than be half-read.
    """
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} does not open with a `---` frontmatter fence"
    block, fence, _ = text[4:].partition("\n---")
    assert fence, f"{path} never closes its frontmatter fence"

    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # `partition` keeps everything after the FIRST colon, so a value containing a
        # colon arrives intact -- which is what makes the no-colon assertion below real
        # rather than an artefact of the parser having truncated it.
        key, colon, value = line.partition(":")
        assert colon, f"{path}: frontmatter line {line!r} is not `key: value`"
        fields[key.strip()] = value.strip()
    return fields


def test_there_is_exactly_one_plugin_root_with_exactly_two_skills() -> None:
    """One `plugin.json` under `agent/skills`, and exactly two skill directories under it."""
    manifest = _PLUGIN_ROOT / ".claude-plugin/plugin.json"
    assert manifest.is_file(), f"{manifest} is the plugin root's manifest and must exist"

    found = sorted(path.relative_to(_PLUGIN_ROOT) for path in _PLUGIN_ROOT.rglob("plugin.json"))
    assert found == [Path(".claude-plugin/plugin.json")], (
        "a second plugin.json makes a second plugin root, and `config.skill_paths()` "
        f"hands the session only the outer one, so the inner skills silently vanish; got {found}"
    )

    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["name"], "a plugin with no name cannot be referenced as `plugin:skill`"
    assert data["description"].strip()

    directories = sorted(path.name for path in _SKILLS_DIR.iterdir() if path.is_dir())
    assert directories == sorted(_SKILL_NAMES), (
        "there are exactly two skills and they are static, embedded content -- no "
        f"per-repository generation and nothing written at runtime; got {directories}"
    )
    for name in directories:
        assert (_SKILLS_DIR / name / "SKILL.md").is_file()


def test_the_embedded_skill_names_match_the_directories_on_disk() -> None:
    """`config.EMBEDDED_SKILLS` is what the fan-out implementer is handed, by name.

    `skills="all"` is valid at the top level of `ClaudeAgentOptions` but not inside an
    `AgentDefinition`, so the subagent's skills are a list of names -- and a name that
    matches no directory is a skill that silently does not load.
    """
    directories = sorted(path.name for path in _SKILLS_DIR.iterdir() if path.is_dir())

    assert sorted(config.EMBEDDED_SKILLS) == directories, (
        "a constant rather than a directory listing keeps `subagents.build_options` pure, "
        f"so this test is what keeps the two in step; got {config.EMBEDDED_SKILLS}"
    )


@pytest.mark.parametrize("name", _SKILL_NAMES)
def test_every_skill_has_frontmatter_a_test_can_read(name: str) -> None:
    """Each skill's `name` matches its directory and its `description` carries no colon."""
    fields = _frontmatter(_SKILLS_DIR / name / "SKILL.md")

    assert fields.get("name") == name, (
        "the frontmatter `name` is what `skills=` and `plugin:skill` match on, so a "
        f"name that disagrees with its directory is a skill nothing can enable; got {fields.get('name')!r}"
    )
    description = fields.get("description", "")
    assert description, (
        "the description is the whole of the trigger: the model reads it to decide "
        "whether to invoke the skill, and an empty one is a skill that never fires"
    )
    assert ":" not in description, (
        "a colon in an unquoted YAML scalar is a parse error, and the CLI reports a "
        f"skill whose frontmatter failed to parse as a skill that simply does not load; got {description!r}"
    )


def test_the_review_skill_names_the_base_branch_comparison() -> None:
    """`review-rigor` demands a base-branch diff and shows the exact verdict token."""
    body = (_SKILLS_DIR / "review-rigor/SKILL.md").read_text(encoding="utf-8")

    assert "base branch" in body or "base-branch" in body, (
        "reviewing `git diff HEAD~1` reviews the last commit, not the change: a branch "
        "is usually several commits and an amend moves HEAD~1 under the reviewer"
    )
    # `nodes/review.py` matches `^\s*VERDICT:\s*(PASS|FAIL)` with the LAST match winning,
    # so the skill must show that spelling literally. Any other -- "Verdict: pass",
    # "VERDICT = FAIL" -- parses as no verdict at all, which the node treats as a veto.
    for token in ("VERDICT: PASS", "VERDICT: FAIL"):
        assert token in body, f"{token!r} is the literal token the review node parses"


def test_the_skills_are_reachable_from_the_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`skill_paths()` finds the in-tree root and `build_options` makes one local plugin of it."""
    # This container really does have AGENT_SKILL_PATHS=/app/skills set, so the
    # fallback path is only reachable from a cleared environment.
    no_aws(monkeypatch)

    roots = config.skill_paths()
    assert roots == (_PLUGIN_ROOT,), (
        "with AGENT_SKILL_PATHS unset the fallback is `agent/skills` resolved from "
        f"`config.__file__`, so a local run gets the same two skills the image bakes in; got {roots}"
    )

    cfg = config.load_config()
    options = subagents.build_options("code", cwd=tmp_path, cfg=cfg)
    assert options.plugins == [{"type": "local", "path": str(_PLUGIN_ROOT)}]
    assert options.skills == "all", (
        "every role gets every skill in every root: the skill's own frontmatter decides "
        "when it triggers, so there is no per-role wiring to get wrong"
    )
    assert "Skill" in options.allowed_tools, "a role with no Skill tool can invoke none of them"

    # One entry per root, not one entry for the first root: the variable is
    # colon-separated and the image is free to mount a second plugin root later.
    two_roots = dataclasses.replace(cfg, skill_paths=(_PLUGIN_ROOT, tmp_path / "extra"))
    assert subagents.build_options("review", cwd=tmp_path, cfg=two_roots).plugins == [
        {"type": "local", "path": str(_PLUGIN_ROOT)},
        {"type": "local", "path": str(tmp_path / "extra")},
    ]


def test_the_skills_are_static_content_not_written_at_runtime() -> None:
    """No package module writes a `SKILL.md` or a `plugin.json`: the skills are embedded."""
    # The predicate is deliberately loose so it cannot rot into a false alarm: a module
    # that never names either filename cannot write one, so only a module that mentions
    # `SKILL.md` or `plugin.json` is examined, and then for any write call at all. A
    # module that legitimately *reads* a SKILL.md one day stays green; one that
    # generates skills per repository does not.
    write_calls = ("write_text", "write_bytes", "open(", "mkdtemp", "copytree", "copyfile", "dump(")
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "agent/src/coding_agent").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "SKILL.md" not in source and "plugin.json" not in source:
            continue
        if any(call in source for call in write_calls):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "the skills are static, embedded content handed to every role as one plugin "
        f"root; these modules look like they generate or rewrite them: {offenders}"
    )

    # The other half of "static": the plugin root is documentation, not a generator.
    assert not list(_PLUGIN_ROOT.rglob("*.py")), "nothing under the plugin root executes"
