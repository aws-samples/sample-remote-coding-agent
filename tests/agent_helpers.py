"""Shared helpers for the agent tests. Plain functions, deliberately not fixtures.

A fixture would have to be imported into every module that wants it and then left
unused-looking, which is ruff `F401`; and a module named `test_*` would be collected.
So this is `agent_helpers.py` holding functions the tests call directly.

The one thing worth knowing before writing a new agent test: **this container's
environment is already polluted**. `TASKS_TABLE`, `WORK_ROOT`, `VENV_ROOT`,
`AGENT_SKILL_PATHS` and `ANTHROPIC_MODEL` are all set in the image the tests run in,
so `store` is *not* a no-op by default and `config.load_config()` does *not* return the
defaults. `no_aws(monkeypatch)` is what makes a test independent of that.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coding_agent import git_ops

# Everything the agent package reads out of the environment. Listed in one place so a
# new variable cannot be added to `config.py` and silently leak the host's value into
# every test that calls `load_config()`.
AGENT_ENV_VARS = (
    "TASKS_TABLE",
    "CONFIG_TABLE",
    "WORK_ROOT",
    "VENV_ROOT",
    "AGENT_SKILL_PATHS",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_FALLBACK_MODEL",
    # The three alias variables the provisioner forwards (`config.MODEL_ENV_VARS`). They
    # are cleared here because this container really does set them and `config.ROLES`
    # routes by alias -- so a test that did not clear them would assert the host's image
    # rather than the fleet's routing.
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CODE_GOAL_ENABLED",
    "GITHUB_TOKEN",
    # `CONFIG_ID` is what `store.github_token` keys the GitHub credential on, and
    # `CONFIG_TABLE` -- cleared above with the other table names -- is the switch that stops it
    # building a client at all.
    "CONFIG_ID",
)


def no_aws(monkeypatch: Any) -> None:
    """Unset every variable that would make the agent talk to AWS or read host config.

    Not the same thing as the `aws` fixture: that one *creates* moto tables and sets
    `TASKS_TABLE`, which is what a test of `store` wants. This is what every other test
    wants -- `store` returning early, and `load_config()` returning its own defaults.
    """
    for name in AGENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for role in ("PLAN", "REPLAN", "CODE", "REVIEW"):
        for suffix in ("MODEL", "EFFORT", "TURNS"):
            monkeypatch.delenv(f"ROLE_{role}_{suffix}", raising=False)


def make_git_repo(
    path: Path,
    *,
    files: dict[str, str] | None = None,
    makefile_targets: tuple[str, ...] = (),
) -> Path:
    """Create a real git repository at `path` with one commit, and return it.

    A real `git init` and not a fake, because half of what these tests check is git's own
    behaviour -- the ownership exemption, `--porcelain` output shapes, a content digest
    over `git diff`. `trust` is called for the same reason it is called in production:
    the temporary directory a test runs in may be on a foreign-owned mount.

    `makefile_targets` writes a Makefile whose recipes are all `@true`, which is what
    `checkout`'s command detection reads.
    """
    path.mkdir(parents=True, exist_ok=True)
    trust(path)
    content = dict(files or {})
    if makefile_targets:
        content["Makefile"] = "\n".join(f"{target}:\n\t@true\n" for target in makefile_targets)
    if not content:
        content["README.md"] = "seed\n"
    for name, text in content.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    _git(["init", "--quiet", "--initial-branch=main"], path)
    _git(["add", "-A"], path)
    _git(["commit", "--quiet", "-m", "seed"], path)
    return path


def _git(args: list[str], cwd: Path) -> None:
    """git, with the process-wide config group `git_ops` exported at import time.

    `check=True` on purpose: a helper that silently produced a non-repository would
    surface as an incomprehensible failure three assertions later.
    """
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=os.environ | git_ops.git_config_env(),
        check=True,
        capture_output=True,
        text=True,
    )


def trust(path: Path) -> None:
    """Exempt `path` from git's ownership check, the way `checkout` does."""
    git_ops.trust_directory(path)


def write(path: Path, text: str) -> Path:
    """Write `text` to `path`, creating parents. Saves three lines in a dozen tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@dataclass(frozen=True)
class FakeRoleResult:
    """A stand-in for `subagents.RoleResult`, with the two text fields that matter.

    Invariant 7 is that nodes read `final_text` and never `text`, so the default here is
    for `text` to hold something a node must *not* act on: a test that passes only
    `final_text` still gets a transcript, and any node that reads the wrong field fails.

    `final_text` defaults to something non-empty because `subagents.run_role` raises
    `RoleError` on an empty one: a result with no text at all is a shape no real session
    can hand a node, so a test leaning on the default would be asserting against an
    impossible session.
    """

    final_text: str = "the role finished."
    text: str = "I'll start by exploring the repository. VERDICT: FAIL"
    role: str = "stub"
    num_turns: int = 1
    total_cost_usd: float | None = None
    session_id: str | None = None
    is_error: bool = False
    # The `ResultMessage` accounting `subagents.cost_row` reads. Every node that runs a
    # role builds a cost row from its result, so a stand-in missing these fields fails
    # with an `AttributeError` inside the node rather than in the assertion.
    usage: dict[str, Any] | None = None
    model_usage: dict[str, Any] | None = None
    subtype: str | None = None
    terminal_reason: str | None = None


@dataclass
class RoleRecorder:
    """A `run_role` replacement that records its calls and returns canned results.

    Patch it in with `monkeypatch.setattr(module.subagents, "run_role", recorder)`, which
    works because every node calls through the module object rather than importing the
    function -- that convention exists for this.

    `side_effect` is called with `(role, prompt, cwd)` before the result is returned, so
    a test can make the "role" actually write the artefact the node reads back.
    """

    results: dict[str, FakeRoleResult] = field(default_factory=dict)
    default: FakeRoleResult = FakeRoleResult(final_text="done")
    side_effect: Any = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, role: str, prompt: str, **kwargs: Any) -> FakeRoleResult:
        cwd = kwargs.get("cwd")
        self.calls.append({"role": role, "prompt": prompt, "cwd": cwd, **kwargs})
        if self.side_effect is not None:
            self.side_effect(role, prompt, cwd)
        return self.results.get(role, self.default)

    @property
    def roles(self) -> list[str]:
        return [call["role"] for call in self.calls]
