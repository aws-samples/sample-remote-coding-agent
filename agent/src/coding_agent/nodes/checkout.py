"""Prepare the workspace: probe the root, clone, branch, detect the commands, link the venv.

Everything here happens before a model is invoked, and most of it exists because of the
managed filesystem the work root lives on. See `_prepare_root` and `_link_venv`.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .. import config, git_ops, memory, store
from ..state import TaskState

log = logging.getLogger(__name__)

# A Makefile target: a name at the start of a line followed by a colon. Anchored so a
# `.PHONY: setup test` line cannot register `setup` and `test` as targets -- it starts
# with a dot, and the pattern requires a letter.
_MAKE_TARGET = re.compile(r"^([A-Za-z][\w-]*)\s*:", re.MULTILINE)


class CheckoutError(RuntimeError):
    """Raised when the workspace could not be prepared."""


def _prepare_root(work_root: Path, work_dir: Path, repo_dir: Path) -> None:
    """Create the workspace directories, exempt them from git's ownership check, and prove
    git can actually work in them.

    The session-storage mount arrives owned by another uid and *imposes* that ownership on
    what the container creates under it, so a directory this very process just made reads
    as someone else's and git dies with `fatal: detected dubious ownership`. On a real
    deploy that killed three tasks -- thirty seconds AFTER `mkdir` had reported success,
    which is exactly why the probe below is a real `git init` and not another `mkdir`.
    """
    for path in (work_root, work_dir, repo_dir):
        path.mkdir(parents=True, exist_ok=True)
        git_ops.trust_directory(path)
    git_ops.probe_repository(work_dir)


def _quarantine_repo_agent_dir(repo_dir: Path, *, resumed: bool) -> None:
    """Move a clone-provided `.agent/` aside to `.agent.repo/`.

    `.agent/` is where this task's own specification, review and memory live, so a
    repository that ships a directory of that name would have its content read as this
    task's state -- and would have this task's state written over it.

    Two guards, and they are not the same guard twice. The `resumed` gate is the rule: on
    a resume the `.agent/` present is the one *we* wrote on the first attempt, and moving
    it would destroy the spec and the memory the resume exists to reuse. The
    `.agent.repo` existence check is the belt: if it is already there, the move already
    happened, whatever the flag says.
    """
    source = repo_dir / ".agent"
    destination = repo_dir / ".agent.repo"
    if resumed:
        log.info("resumed run: leaving .agent/ alone, it is ours")
        return
    if not source.is_dir() or destination.exists():
        return
    source.rename(destination)
    log.info(
        "the clone shipped its own .agent/; moved it to .agent.repo/ (it is repository content)"
    )


def _link_venv(cfg: config.AgentConfig, repo_dir: Path, task_id: str) -> None:
    """Point `repo/.venv` at a per-task virtualenv directory under `VENV_ROOT`.

    `VENV_ROOT` defaults to `/tmp/venv`, which is **off** the session-storage mount, and
    that is deliberate: the mount's cap is 1 GB and a virtualenv is around 692 MB of
    perfectly reconstructible bytes. Moving it onto the mount "for tidiness" fills the
    session and kills the task at a point that looks nothing like a disk-space problem.

    The link is created empty. `setupCommand` is what populates it, and this repository's
    own `make setup` resolves the symlink before handing it to `python -m venv` precisely
    because `venv` refuses to build into one.

    The link is then excluded through `.git/info/exclude`, which is not a tidiness measure:
    a repository whose own `.gitignore` does not mention `.venv` reports the symlink WE just
    created as an untracked change, and that single path satisfies the no-op guard in
    `nodes/code.py` and in `nodes/submit.py` (invariant 5) and gets committed into the pull
    request by `git add -A`. `.git/info/exclude` rather than an edit to `.gitignore` because
    it is per-clone and untracked, so the fix cannot itself appear in the diff.
    """
    venv_dir = cfg.venv_root / task_id
    venv_dir.mkdir(parents=True, exist_ok=True)
    link = repo_dir / ".venv"
    if not link.exists() and not link.is_symlink():
        link.symlink_to(venv_dir)
        log.info("linked %s -> %s", link, venv_dir)
    _exclude_locally(repo_dir, ".venv")


def _exclude_locally(repo_dir: Path, pattern: str) -> None:
    """Append `pattern` to this clone's `.git/info/exclude`, idempotently."""
    exclude = repo_dir / ".git" / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if pattern in existing.split():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n# added by the coding agent: this is ours, not the repository's\n{pattern}\n"
        )


def _make_targets(repo_dir: Path) -> set[str]:
    makefile = repo_dir / "Makefile"
    if not makefile.is_file():
        return set()
    return set(_MAKE_TARGET.findall(makefile.read_text(encoding="utf-8", errors="replace")))


def _package_scripts(repo_dir: Path) -> dict[str, str]:
    manifest = repo_dir / "package.json"
    if not manifest.is_file():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        log.warning("package.json does not parse; ignoring it for command detection")
        return {}
    scripts = data.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def _detect_verify_command(repo_dir: Path) -> str:
    """The repository's own verification entry point, or `""`.

    Only ever a *declared* entry point (invariant 35). A wrong verify command is a red exit
    the loop can see and react to; guessing `python -m pytest` against an interpreter with
    no dependencies installed reports a missing environment as a red suite, which is a
    failure mode that reads like a bad change.
    """
    if "test" in _make_targets(repo_dir):
        return "make test"
    if "test" in _package_scripts(repo_dir):
        return "npm test"
    return ""


def _detect_setup_command(repo_dir: Path) -> str:
    """The repository's own preparation entry point, or `""`.

    Detection is **stricter** here than for verification, and invariant 35 is why: a wrong
    setup command switches verification off *silently*, one draft pull request at a time.
    So only `setup:`/`bootstrap:` or `scripts.setup` count -- never `install:`, which in
    the wild means anything from "install dependencies" to "install this program onto the
    system", and which would happily run as root inside a container.
    """
    targets = _make_targets(repo_dir)
    for target in ("setup", "bootstrap"):
        if target in targets:
            return f"make {target}"
    if "setup" in _package_scripts(repo_dir):
        return "npm run setup"
    return ""


async def checkout(state: TaskState) -> dict[str, Any]:
    """Prepare the work root, clone the repository, branch it, and detect its commands."""
    cfg = config.load_config()
    task_id = state["taskId"]
    work_dir = cfg.work_root / task_id
    repo_dir = work_dir / "repo"
    _prepare_root(cfg.work_root, work_dir, repo_dir)

    if not (repo_dir / ".git").exists():
        # A resume finds the clone already there and must not re-clone over it.
        source = state.get("cloneSource") or f"https://github.com/{state['repository']}.git"
        # Keyed on `configId`, which is also the 3LO `userId` the operator's authorisation
        # is stored under -- so the clone needs no credential on the state and there is no
        # secret ARN to carry. `""` means "this repository is not authorised yet", which
        # `git_ops.clone` turns into an unauthenticated clone: that is correct for a public
        # repository and fails with GitHub's own 404 for a private one, which is a better
        # message than anything this node could invent.
        token = store.github_token(state.get("configId", "")) or None
        log.info("cloning %s", state.get("repository") or source)
        git_ops.clone(source, repo_dir, token=token)
    else:
        log.info("the clone is already present at %s; not re-cloning", repo_dir)

    base_branch = state.get("baseBranch") or git_ops.default_branch(repo_dir)
    branch = state.get("branch") or f"agent/{task_id}"
    git_ops.create_branch(repo_dir, branch)
    git_ops.exclude_build_artifacts(repo_dir)

    _quarantine_repo_agent_dir(repo_dir, resumed=bool(state.get("resumed", False)))
    memory.ensure_dirs(repo_dir)
    _link_venv(cfg, repo_dir, task_id)

    # An explicit value always wins over detection: that is what `local_run`'s
    # `--verify-command` is, and what an operator's configuration will be in a later phase.
    verify_command = state.get("verifyCommand")
    if verify_command is None:
        verify_command = _detect_verify_command(repo_dir)
    setup_command = state.get("setupCommand")
    if setup_command is None:
        setup_command = _detect_setup_command(repo_dir)
    log.info("detected verifyCommand=%r setupCommand=%r", verify_command, setup_command)

    update: dict[str, Any] = {
        "workDir": str(work_dir),
        "repoDir": str(repo_dir),
        "branch": branch,
        "baseBranch": base_branch,
        "verifyCommand": verify_command,
        "setupCommand": setup_command,
    }
    # Only when absent: a resume arrives with an iteration count and a spent re-run flag,
    # and resetting either would hand the loop budget back.
    if "iteration" not in state:
        update["iteration"] = 0
    if "reviewRerun" not in state:
        update["reviewRerun"] = False
    return update
