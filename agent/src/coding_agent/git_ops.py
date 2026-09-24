"""Every git invocation in the container, and the one configuration group they share.

Two facts about this environment shape the whole module:

**The session-storage mount is writable but not owned by this process's user**
(invariant 25), so git refuses every repository on it with `fatal: detected dubious
ownership` -- including one this process created a millisecond earlier. The exemption
is carried in `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>`
rather than in a config file, because that is the only channel reaching **both**
`commands._base_env()` (which builds a command's environment from nothing and copies
the group back explicitly) **and** every coding subagent's inherited `Bash`.

**The GitHub PAT must never enter this process's environment, the `origin` URL or a
command line** (invariant 4), because every role holds `Bash` and inherits what this
process has. Network git therefore goes through `_run_authenticated`, which builds a
fresh minimal environment and points `GIT_ASKPASS` at a script in a 0700 directory
that exists only for the length of the call.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

log = logging.getLogger(__name__)

# A clone is the long pole here; every other command in this module is sub-second. The
# bound exists so a hung git surfaces as a `GitError` naming the timeout instead of
# consuming the session until AgentCore reaps it with nothing in the log.
_GIT_TIMEOUT = 600

# The commit identity, and it has to be configurable rather than hardcoded, because no
# single value is right everywhere. `agent@localhost` is the default and matches no GitHub
# account, so GitHub resolves *no author* for the commit -- `.author` is `null` in the
# commits API -- and any check that gates on commit attribution then refuses the pull
# request. A Vercel preview reports `Deployment was blocked` and never builds; a repository
# with a DCO or signed-commit policy fails the same way. That failure is expensive to read:
# the pull request goes red with a deployment error and nothing in the diff explains it,
# because the cause is the author of the commit rather than anything in it.
#
# Fleet policy (`provisioner._fleet_environment`), not per-configuration: it is one
# deployment-wide identity, so a runtime provisioned before this existed converges onto it
# on the next update instead of keeping what it was born with (invariant 24).
#
# Deliberately NOT git's own `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`: those are read directly
# by every git process and would take precedence over this config group, so the two could
# disagree with the environment silently winning. `AGENT_` prefixed, they cannot.
_DEFAULT_AUTHOR_NAME = "agentcore-coding-agent"
_DEFAULT_AUTHOR_EMAIL = "agent@localhost"

# The process-wide config group, in `GIT_CONFIG_KEY_<n>` order. Seeded with an identity
# because `submit` commits and a container has no `~/.gitconfig`, and with
# `advice.detachedHead=false` because that advice is six lines of noise in every log.
# `trust_directory` appends `safe.directory` entries to it; nothing is ever removed.
#
# The two reads are spelled out here rather than pulled into a helper so the variable names
# are *literals* at the read site: `tests/test_env_contract.py`'s scan recovers names
# statically and can only prove a variable is forwarded when it can see one, so a helper
# taking the name as an argument would have to be excused in
# `CONTAINER_DYNAMIC_SITES` instead of checked. `or` on a stripped value because a
# Lambda-forwarded variable arrives as `""` rather than absent when nothing set it, so
# empty has to mean "use the default" and not "commit with an empty identity", which git
# rejects outright.
_CONFIG_ENTRIES: list[tuple[str, str]] = [
    ("user.name", os.environ.get("AGENT_GIT_AUTHOR_NAME", "").strip() or _DEFAULT_AUTHOR_NAME),
    ("user.email", os.environ.get("AGENT_GIT_AUTHOR_EMAIL", "").strip() or _DEFAULT_AUTHOR_EMAIL),
    ("advice.detachedHead", "false"),
]

# git's own minimal environment. Built here and not imported from `commands._base_env()`
# -- that would be a circular import, since `commands` copies `git_config_env()` into
# every repository command's environment. No `AWS_*`, no token, nothing by accident.
_GIT_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG")

# Exactly the script git runs to answer a credential prompt. `$1` is the prompt text,
# so the username branch is matched on it; the token is read from a sibling file rather
# than interpolated here, because this script's contents are world-readable the moment
# anything widens the directory's mode.
_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  Username*) printf '%s' 'x-access-token' ;;
  *) cat "$(dirname "$0")/token" ;;
esac
"""

# `.agent/` is the roles' own workspace and `.agent.repo/` is the clone's quarantined
# one; neither is the work being reviewed.
_SOURCE_PATHSPEC = (".", ":(exclude).agent", ":(exclude).agent.repo")


class GitError(RuntimeError):
    """A git command failed, timed out, or ran somewhere unusable.

    The message carries git's own stderr rather than a paraphrase: "detected dubious
    ownership" and "could not read Username" are diagnosable, and anything this module
    invented instead would not be.
    """


def _git(
    args: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> tuple[int, str, str]:
    """Run one git command, returning `(exit_code, stdout, stderr)`.

    `env=None` inherits this process's environment, which is where `_export` put the
    config group. `check=False` is for the commands whose non-zero exit is an answer
    (`rev-parse --verify` on a branch that may not exist) rather than a failure.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {_GIT_TIMEOUT}s") from exc
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise GitError(f"git {' '.join(args)} failed with exit {proc.returncode}: {detail}")
    return proc.returncode, proc.stdout, proc.stderr


def _config_env(entries: Sequence[tuple[str, str]]) -> dict[str, str]:
    """Render config entries as git's environment-variable config form.

    The count and its pairs move as a unit: a `GIT_CONFIG_COUNT` of 4 with three pairs
    is `fatal: missing config key GIT_CONFIG_KEY_3` on *every* subsequent git command,
    which is why no caller ever builds a partial group by hand.
    """
    env = {"GIT_CONFIG_COUNT": str(len(entries))}
    for n, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{n}"] = key
        env[f"GIT_CONFIG_VALUE_{n}"] = value
    return env


def git_config_env() -> dict[str, str]:
    """The whole config group as an environment mapping -- never a subset."""
    return _config_env(_CONFIG_ENTRIES)


def _export() -> None:
    """Publish the group into `os.environ`, where subagent `Bash` inherits it.

    Stale higher-numbered keys are deleted rather than left behind: the group only
    grows within one process, but a parent could have exported a longer one, and a
    `KEY_<n>` above the count is at best ignored and at worst confusing to debug.
    """
    os.environ.update(git_config_env())
    n = len(_CONFIG_ENTRIES)
    while f"GIT_CONFIG_KEY_{n}" in os.environ or f"GIT_CONFIG_VALUE_{n}" in os.environ:
        os.environ.pop(f"GIT_CONFIG_KEY_{n}", None)
        os.environ.pop(f"GIT_CONFIG_VALUE_{n}", None)
        n += 1


def trust_directory(path: Path) -> None:
    """Exempt `path` from git's ownership check, by exact name.

    git 2.39 (what this image ships) expands **no glob** in `safe.directory`, so
    `*` is not an option and every path is named in full.

    Both the worktree and its gitdir go in, because git names different ones depending
    on what it is doing: a plain `git status` on a foreign-owned repo reports the
    worktree, while a *clone source* is reported as `<path>/.git`. Adding one and not
    the other produces a failure in exactly one of the two paths, which reads as
    something far more mysterious than it is.
    """
    resolved = path.resolve()
    for candidate in (resolved, resolved / ".git"):
        entry = ("safe.directory", str(candidate))
        if entry not in _CONFIG_ENTRIES:
            _CONFIG_ENTRIES.append(entry)
    _export()


# Once at import, so the identity and the exemptions are live for anything that runs
# git -- this module, `commands`, or a subagent's `Bash` -- without a caller having to
# remember to prime them.
_export()


def probe_repository(root: Path) -> None:
    """Prove git can actually operate under `root`, with a throwaway repository.

    `mkdir` reporting success proves nothing about the mount: it is exactly what let
    three real tasks die thirty seconds later, inside the clone, with an error nobody
    connected to the directory that had been created happily. So this does a real
    `git init` plus a real `git status` and raises `GitError` carrying git's stderr.
    """
    probe = root / f".probe-{os.getpid()}"
    try:
        probe.mkdir(parents=True, exist_ok=True)
        # The probe is a *child* of an already-trusted root, and with no glob in
        # `safe.directory` a trusted parent covers nothing below it. Its two paths go
        # into a throwaway copy of the group rather than the process-wide one, since
        # the directory is deleted a few lines below and an entry for a path that no
        # longer exists is permanent noise in every later git command.
        resolved = probe.resolve()
        env = dict(os.environ) | _config_env(
            [
                *_CONFIG_ENTRIES,
                ("safe.directory", str(resolved)),
                ("safe.directory", str(resolved / ".git")),
            ]
        )
        _git(["init", "--quiet"], cwd=probe, env=env)
        _git(["status", "--porcelain"], cwd=probe, env=env)
    except OSError as exc:
        raise GitError(f"{root} is not usable as a work root: {exc}") from exc
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def _is_local(source: str) -> bool:
    """True for a filesystem path or a `file://` URL: there is nothing to authenticate."""
    if source.startswith("file://"):
        return True
    # A scheme, or an scp-style `git@host:owner/repo`, means a network remote.
    return "://" not in source and not source.startswith("git@")


def clone(source: str, dest: Path, *, token: str | None = None) -> None:
    """Clone `source` into `dest`, authenticating only when the source is remote."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", "--quiet", source, str(dest)]
    if _is_local(source):
        # Nothing to authenticate, but the source repository is on the same
        # foreign-owned mount and git reports a clone source as its *gitdir*, so it
        # needs the exemption too -- no caller above here knows the source path.
        trust_directory(Path(source.removeprefix("file://")))
        _git(args, cwd=dest.parent)
        return
    _run_authenticated(args, cwd=dest.parent, token=token)


def create_branch(repo: Path, branch: str) -> None:
    """Check `branch` out, creating it only when it does not exist yet.

    Idempotent on purpose: a resumed task re-runs `checkout` against a clone that
    already carries the branch, and `git checkout -b` fails there. Failing on the
    resume path would discard the hours of work the checkpoint exists to keep.
    """
    code, _, _ = _git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo, check=False
    )
    if code == 0:
        log.info("branch %s already exists in %s; checking it out", branch, repo)
        _git(["checkout", branch], cwd=repo)
        return
    _git(["checkout", "-b", branch], cwd=repo)


def current_branch(repo: Path) -> str:
    """The checked-out branch name (`HEAD` when detached)."""
    _, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    return out.strip()


def _origin_head_ref(repo: Path) -> str:
    """`refs/remotes/origin/HEAD`'s target, or `""` when the ref does not resolve.

    The one place that runs this command, because two callers now ask two different
    questions of the same answer -- which branch, and whether the ref exists at all.
    """
    code, out, _ = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo, check=False)
    return out.strip() if code == 0 else ""


def has_origin_head(repo: Path) -> bool:
    """Whether `refs/remotes/origin/HEAD` resolves in this clone.

    Read by `nodes/review.py` before it dispatches `/security-review`. That command is a
    prompt template whose body opens with a shell pre-command,
    `!git log --no-decorate origin/HEAD...`, and where this ref does not resolve it
    aborts **before any model call** and the CLI still returns `subtype: "success"`,
    `is_error: false`, `num_turns: 0`, `total_cost_usd: 0` and an empty `result` in about
    100 ms -- a silent no-op that reads as a clean pass (probe-verified against CLI
    2.1.236). The ref is absent from a local clone and from a `git init` scratch repo,
    which is exactly `local_run.py`'s situation, so the caller has to ask first.
    """
    return bool(_origin_head_ref(repo))


def default_branch(repo: Path) -> str:
    """The clone's default branch, falling back to `"main"`.

    `refs/remotes/origin/HEAD` is absent from a local clone and from a `git init`
    scratch repo, and the answer is the base of a pull request, so the fallback is the
    modern default rather than an error -- a task that cannot name a base branch would
    otherwise die before it planned anything.
    """
    ref = _origin_head_ref(repo)
    if ref:
        return ref.rsplit("/", 1)[-1]
    log.info("%s has no refs/remotes/origin/HEAD; assuming the base branch is main", repo)
    return "main"


def changed_paths(repo: Path) -> list[str]:
    """Repo-relative paths with uncommitted changes, untracked files included.

    One path per entry, so the callers' guards can filter on a prefix.
    `--untracked-files=all` because the default collapses a new directory to `dir/`
    and the no-op guard in `code` needs the file that was actually written.
    """
    _, out, _ = _git(["status", "--porcelain", "--untracked-files=all"], cwd=repo)
    paths: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:  # `XY path` -- anything shorter is not an entry
            continue
        entry = line[3:].strip()
        # `R  old -> new`: the new path is the one in the tree now, and every caller is
        # asking what this run produced rather than what it consumed.
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1].strip()
        paths.append(entry.strip('"'))
    return paths


def has_changes(repo: Path) -> bool:
    """Whether anything at all is uncommitted.

    Note for callers: this is true the moment the plan node writes a spec, so it is
    never the guard for "did the coder do any work" -- that is a filter over
    `changed_paths`.
    """
    return bool(changed_paths(repo))


def diff(repo: Path, *, base: str | None = None) -> str:
    """The uncommitted change, or the change against `base` when one is named.

    `git diff <base>` and not `<base>...HEAD`: until `submit` commits, the work is in
    the worktree and a commit-to-commit range shows none of it.
    """
    args = ["diff", base] if base else ["diff", "HEAD"]
    _, out, _ = _git(args, cwd=repo)
    return out


def source_diff_digest(repo: Path) -> str:
    """sha256 of the uncommitted change outside `.agent/` -- *content*, not paths.

    This is the whole mechanism of the one permitted hop back to `verify`: `review`
    digests the tree either side of its own session, and a moved digest means the
    exit code on the state describes a tree nobody ran. It has to be a content digest
    because the likeliest shape of a helpful reviewer's edit is an in-place change to
    a file the coder already touched, which changes no path at all.

    Not side-effect free, stated rather than hidden: `git add -A -N` records
    intent-to-add so untracked files appear in the diff, which mutates the index.
    `submit` runs `git add -A` regardless; keeping this pure would mean shelling out
    to a temporary index, which costs more than it buys.
    """
    _git(["add", "-A", "-N"], cwd=repo)
    code, out, _ = _git(["diff", "HEAD", "--", *_SOURCE_PATHSPEC], cwd=repo, check=False)
    if code != 0:
        # `checkout` always clones a repository with history, so HEAD exists in every
        # real run; this is the belt. A repository with no commit yet still gets a
        # defined digest instead of a `GitError` raised in the middle of `review`.
        _, out, _ = _git(["diff", "--cached", "--", *_SOURCE_PATHSPEC], cwd=repo, check=False)
    return hashlib.sha256(out.encode("utf-8")).hexdigest()


def commit(repo: Path, message: str) -> str:
    """Stage everything and commit, returning the new sha.

    No `-c user.email` here: the identity is in the exported config group, so a commit
    made by a subagent's `Bash` carries the same one.
    """
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-m", message], cwd=repo)
    _, out, _ = _git(["rev-parse", "HEAD"], cwd=repo)
    return out.strip()


def push(repo: Path, branch: str, *, token: str, repository: str) -> None:
    """Push the working commit to `refs/heads/<branch>` on `origin`.

    `repository` (`owner/repo`) is what the remote URL is built from when the clone has
    none usable -- a local clone source, on a developer machine. The PAT is **never**
    part of that URL: `.git/config` outlives the push, `git remote -v` is one `Bash`
    call away from every role, and a credential in a URL is also in the reflog
    (invariant 4). Authentication goes through `GIT_ASKPASS` instead.
    """
    code, out, _ = _git(["remote", "get-url", "origin"], cwd=repo, check=False)
    url = f"https://github.com/{repository}.git"
    if code != 0:
        _git(["remote", "add", "origin", url], cwd=repo)
    elif _is_local(out.strip()):
        _git(["remote", "set-url", "origin", url], cwd=repo)
    _run_authenticated(
        ["push", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=repo,
        token=token,
    )


@contextlib.contextmanager
def _askpass(token: str) -> Iterator[Path]:
    """Yield the path of a throwaway `GIT_ASKPASS` script that prints `token`.

    A 0700 directory holding a 0600 `token` file and the script that cats it, removed
    in `finally`. This is the only place the PAT touches anything durable, and it
    reaches git without appearing on a command line (readable through `/proc`) or in
    this process's environment (inherited by every subagent's `Bash`).
    """
    tmp = Path(tempfile.mkdtemp(prefix="askpass-"))
    try:
        tmp.chmod(0o700)
        token_file = tmp / "token"
        token_file.write_text(token, encoding="utf-8")
        token_file.chmod(0o600)
        script = tmp / "askpass.sh"
        script.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
        script.chmod(0o700)
        yield script
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _base_env_for_git() -> dict[str, str]:
    """`PATH`, `HOME`, `LANG` when set -- and deliberately nothing else."""
    return {name: os.environ[name] for name in _GIT_ENV_PASSTHROUGH if name in os.environ}


def _run_authenticated(args: Sequence[str], *, cwd: Path, token: str | None) -> None:
    """Run a network git command with credentials supplied out of band.

    The environment is built from nothing rather than copied, so the token is the only
    secret anywhere near it and `AWS_*` is absent by construction.
    `GIT_TERMINAL_PROMPT=0` because there is no terminal to answer a prompt and a
    blocked git looks exactly like a slow clone until the session is reaped.
    """
    env = _base_env_for_git() | git_config_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token is None:
        # A public clone needs no credentials, so `clone` has no special case: it
        # passes on whatever token it was given, including none.
        _git(list(args), cwd=cwd, env=env)
        return
    with _askpass(token) as script:
        env["GIT_ASKPASS"] = str(script)
        _git(list(args), cwd=cwd, env=env)
