"""The two mechanisms in `git_ops` that a comment cannot protect.

**The ownership exemption.** The work root is a session-storage mount owned by another
uid, and it imposes that ownership on everything the container creates under it, so git
refuses a repository this process made a millisecond earlier with `fatal: detected dubious
ownership`. The exemption travels in `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_<n>` /
`GIT_CONFIG_VALUE_<n>` in `os.environ`, because that is the only channel reaching both
`commands._base_env()` -- which builds a command's environment from nothing -- and every
coding subagent's inherited `Bash`. git 2.39 expands no glob in `safe.directory`, so every
path is named in full. This file reproduces the failure with git's own
`GIT_TEST_ASSUME_DIFFERENT_OWNER=1`, which is confirmed to work in this image's git 2.39.5.

**The token.** The GitHub PAT must never enter this process's environment, the `origin`
URL, or a command line: every role holds `Bash` and inherits what this process has, and a
credential in a URL is also in `.git/config` and in the reflog. It reaches git only through
a throwaway `GIT_ASKPASS` script. That is asserted here by putting a fake `git` first on
`PATH` and reading back exactly what the real code handed it.

Nothing here touches a network. Every repository is a real `git init` under `tmp_path`,
because half of what is being checked is git's own behaviour rather than ours.
"""

from __future__ import annotations

import importlib
import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from agent_helpers import make_git_repo, trust, write
from coding_agent import git_ops
from conftest import FAKE_PAT

# git's own test hook: it makes git treat every repository as owned by another user, which
# is precisely the mount's behaviour. Verified present in git 2.39.5, the version this
# image ships. If a future git drops it, `_assume_foreign` below fails loudly rather than
# letting the strongest test in this file pass vacuously.
_FOREIGN = "GIT_TEST_ASSUME_DIFFERENT_OWNER"


def _without_the_group(**extra: str) -> dict[str, str]:
    """This process's environment with the whole git config group stripped out.

    `GIT_CONFIG_COUNT` alone would be enough for git, but the keys and values are removed
    too so the environment handed to the control run is honestly free of the exemption.
    """
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_CONFIG_")}
    return env | extra


def _status(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _safe_directories() -> list[str]:
    return [value for key, value in git_ops._CONFIG_ENTRIES if key == "safe.directory"]


def _untrusted_git_repo(path: Path) -> Path:
    """A real repository at `path` that is deliberately **not** exempt from the check.

    Not `agent_helpers.make_git_repo`: that helper `trust`s the path it builds, and
    `git_ops._CONFIG_ENTRIES` is a process-global that only ever grows, so one `trust()`
    of this path -- or of anything above it -- would leave
    `test_only_the_exact_path_is_exempt_and_never_its_parent` passing for the wrong
    reason for the rest of the worker. Raw `subprocess.run` with the config group and no
    exemption instead: `git init` itself succeeds under the ownership hook, and only the
    commands that read the repository afterwards refuse, so this builds cleanly while
    `_FOREIGN` is unset and refuses the moment it is set.
    """
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    env = os.environ | git_ops.git_config_env()
    for args in (
        ["init", "--quiet", "--initial-branch=main"],
        ["add", "-A"],
        ["commit", "--quiet", "-m", "seed"],
    ):
        subprocess.run(
            ["git", *args],
            cwd=str(path),
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    return path


# --------------------------------------------------------------------------------------
# the ownership exemption
# --------------------------------------------------------------------------------------


def test_a_foreign_owned_repository_is_usable_after_trust_directory(tmp_path: Path) -> None:
    """Invariant 25: the config group is what makes the session-storage mount usable."""
    repo = make_git_repo(tmp_path / "repo")

    # The control. Without the group, git refuses -- and this half is what proves the
    # other half is not passing for some unrelated reason.
    refused = _status(repo, _without_the_group(**{_FOREIGN: "1"}))
    assert refused.returncode != 0, (
        f"{_FOREIGN} no longer makes git refuse a repository, so the exemption below is "
        "untested; find the current hook before trusting this file again"
    )
    assert "dubious ownership" in refused.stderr

    # The real thing: the same command, the same hook, the group restored.
    allowed = _status(repo, _without_the_group(**{_FOREIGN: "1"}) | git_ops.git_config_env())
    assert allowed.returncode == 0, allowed.stderr


def test_the_probe_survives_a_foreign_owned_work_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`probe_repository` is a real `git init`, because `mkdir` succeeding proves nothing.

    On a real deploy three tasks died thirty seconds AFTER `mkdir` had reported success,
    inside the clone. The probe exists to move that failure to the top of the run, so it
    has to work under exactly the condition that killed them.
    """
    monkeypatch.setenv(_FOREIGN, "1")
    root = tmp_path / "work"
    root.mkdir()
    trust(root)

    git_ops.probe_repository(root)  # must not raise

    assert not list(root.glob(".probe-*")), "the probe cleans up after itself"
    assert not any(value.startswith(str(root / ".probe-")) for value in _safe_directories()), (
        "the probe's own exemption goes into a throwaway copy of the group: an entry for "
        "a path that no longer exists is permanent noise in every later git command"
    )


def test_an_unusable_work_root_raises_rather_than_failing_later(tmp_path: Path) -> None:
    """A root git cannot work in is a `GitError` at the top of the run, not a mystery."""
    blocked = tmp_path / "blocked"
    blocked.write_text("this is a file, not a directory\n", encoding="utf-8")
    with pytest.raises(git_ops.GitError):
        git_ops.probe_repository(blocked)


def test_the_config_group_is_never_partial(tmp_path: Path) -> None:
    """A count that disagrees with its pairs breaks *every* later git command.

    `GIT_CONFIG_COUNT=4` with three pairs is `fatal: missing config key GIT_CONFIG_KEY_3`
    on everything git does afterwards, which is why no caller assembles a subset by hand.
    """
    trust(make_git_repo(tmp_path / "a"))
    trust(make_git_repo(tmp_path / "b"))

    group = git_ops.git_config_env()
    count = int(group["GIT_CONFIG_COUNT"])
    assert count == len(git_ops._CONFIG_ENTRIES)
    for index in range(count):
        assert f"GIT_CONFIG_KEY_{index}" in group
        assert f"GIT_CONFIG_VALUE_{index}" in group
    assert len(group) == 1 + 2 * count, "no key without its value, and nothing else"

    # `_export` publishes the same complete group into `os.environ`, which is the channel
    # `commands._base_env()` copies back and every subagent's `Bash` inherits.
    for name, value in group.items():
        assert os.environ[name] == value
    assert f"GIT_CONFIG_KEY_{count}" not in os.environ, "no stale key above the count"
    assert f"GIT_CONFIG_VALUE_{count}" not in os.environ

    # And git itself accepts it, which is the only assertion that cannot be fooled by a
    # self-consistent but wrong shape.
    assert _status(tmp_path / "a", _without_the_group() | group).returncode == 0


def test_no_glob_is_ever_written_into_safe_directory(tmp_path: Path) -> None:
    """git 2.39 expands no glob here, so `*` would be a literal path that matches nothing."""
    repo = make_git_repo(tmp_path / "repo")
    trust(repo)

    values = _safe_directories()
    assert values, "trust_directory has run"
    for value in values:
        assert "*" not in value
        assert Path(value).is_absolute()

    resolved = repo.resolve()
    # Both the worktree and its gitdir: git names one or the other depending on what it is
    # doing -- a plain `status` reports the worktree, a *clone source* reports `<path>/.git`
    # -- and adding one only fails in exactly one of the two paths.
    assert str(resolved) in values
    assert str(resolved / ".git") in values

    before = len(git_ops._CONFIG_ENTRIES)
    trust(repo)
    assert len(git_ops._CONFIG_ENTRIES) == before, "trust_directory is idempotent"


def test_only_the_exact_path_is_exempt_and_never_its_parent(tmp_path: Path) -> None:
    """A trusted parent covers nothing below it, because git 2.39 expands no glob here.

    This is the reason `probe_repository` names its own throwaway directory even though it
    sits inside an already-trusted root, and the reason `nodes/checkout._prepare_root`
    trusts the work root, the work dir and the repo dir individually instead of trusting
    the root and assuming the rest is covered.
    """
    parent = tmp_path / "parent"
    repo = _untrusted_git_repo(parent / "child")

    def foreign_status() -> subprocess.CompletedProcess[str]:
        # The group is rebuilt on every call, so each assertion below sees exactly the
        # exemptions trusted so far and nothing else.
        return _status(repo, _without_the_group(**{_FOREIGN: "1"}) | git_ops.git_config_env())

    # The control: unnamed, so refused. Without this the two assertions below could both
    # be true of a git that stopped honouring the hook.
    refused = foreign_status()
    assert refused.returncode != 0, (
        f"{_FOREIGN} no longer makes git refuse a repository, so nothing below is tested; "
        "find the current hook before trusting this file again"
    )
    assert "dubious ownership" in refused.stderr

    trust(parent)
    still_refused = foreign_status()
    assert still_refused.returncode != 0, (
        "a trusted parent must not cover a repository below it: if git ever starts "
        "expanding safe.directory, every caller that names three paths can name one"
    )
    assert "dubious ownership" in still_refused.stderr

    trust(repo)
    allowed = foreign_status()
    assert allowed.returncode == 0, allowed.stderr


def test_the_exemption_reaches_a_subagent_that_inherited_this_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exemption has to travel in `os.environ`, not only in a built environment.

    A fix living inside `commands._base_env()` alone passes every other test in this file
    and fails this one, and the symptom in production is a coding role whose every `git`
    command dies -- the role holds `Bash` and gets whatever this process exported, not
    whatever `commands` assembled for the verify command.
    """
    monkeypatch.setenv(_FOREIGN, "1")
    repo = make_git_repo(tmp_path / "repo")

    def inherited_status() -> subprocess.CompletedProcess[str]:
        # `env=None`, i.e. the child inherits `os.environ`. That is a subagent's `Bash`.
        return subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo),
            env=None,
            capture_output=True,
            text=True,
            check=False,
        )

    allowed = inherited_status()
    assert allowed.returncode == 0, allowed.stderr
    # What `_export()` at import time buys: the whole group is live in this process, so
    # anything descended from it is exempt without being handed an environment.
    assert git_ops.git_config_env().items() <= os.environ.items()

    # The control: take the group back out of *this* process and the same inherited-
    # environment command refuses, which is what the production failure looked like.
    for name in [name for name in os.environ if name.startswith("GIT_CONFIG_")]:
        monkeypatch.delenv(name)
    refused = inherited_status()
    assert refused.returncode != 0, (
        f"{_FOREIGN} no longer makes git refuse a repository, so the assertion above is "
        "untested; find the current hook before trusting this file again"
    )
    assert "dubious ownership" in refused.stderr


# --------------------------------------------------------------------------------------
# the content digest behind the one hop back to verify
# --------------------------------------------------------------------------------------


def test_the_digest_moves_for_an_in_place_edit(tmp_path: Path) -> None:
    """The one hop back to `verify` rests on this: content, never paths.

    The likeliest shape of a helpful reviewer's edit is an in-place change to a file the
    coder already touched, which adds and removes no path at all. A digest over paths
    would see nothing and the verdict would be composed against a tree nobody ran.
    """
    repo = make_git_repo(tmp_path / "repo", files={"src/app.py": "x = 1\n"})
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")

    first = git_ops.source_diff_digest(repo)
    assert git_ops.source_diff_digest(repo) == first, "stable when nothing changed"

    # The reviewer's own notes are not the work being reviewed.
    write(repo / ".agent" / "review" / "iteration-0.md", "the reviewer's analysis\n")
    assert git_ops.source_diff_digest(repo) == first

    # The edit that matters: same path, different bytes.
    (repo / "src" / "app.py").write_text("x = 3\n", encoding="utf-8")
    assert git_ops.source_diff_digest(repo) != first

    # And a repository with no commit at all still gets a digest rather than a `GitError`
    # raised in the middle of `review`.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    trust(fresh)
    subprocess.run(
        ["git", "init", "--quiet", "."],
        cwd=str(fresh),
        env=os.environ | git_ops.git_config_env(),
        check=True,
        capture_output=True,
    )
    write(fresh / "a.txt", "hello\n")
    assert isinstance(git_ops.source_diff_digest(fresh), str)


# --------------------------------------------------------------------------------------
# the token
# --------------------------------------------------------------------------------------

_TOKEN = FAKE_PAT

# A fake `git` that appends its own argv and its own environment to a fixed path. The path
# is baked in rather than passed through a variable, because `_run_authenticated` builds
# the child's environment from nothing and a variable of ours would not survive it -- which
# is the property under test.
_FAKE_GIT = """#!/bin/sh
{{
  printf 'ARGV'
  for arg in "$@"; do printf ' %s' "$arg"; done
  printf '\\n'
  env
  printf 'END\\n'
}} >> '{record}'
exit 0
"""


def _install_fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a recording `git` first on `PATH` and return the file it records into."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "git-calls.txt"
    fake = bindir / "git"
    fake.write_text(_FAKE_GIT.format(record=record), encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return record


def _invocations(record: Path) -> list[tuple[str, dict[str, str]]]:
    """Parse the recording into `(argv_line, environment)` pairs, oldest first."""
    parsed: list[tuple[str, dict[str, str]]] = []
    for block in record.read_text(encoding="utf-8").split("END\n"):
        lines = block.strip().splitlines()
        if not lines or not lines[0].startswith("ARGV"):
            continue
        env = dict(
            line.split("=", 1)  # type: ignore[misc]
            for line in lines[1:]
            if "=" in line
        )
        parsed.append((lines[0], env))
    return parsed


def test_the_token_never_reaches_the_environment_or_a_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 4: the PAT is in neither the argv nor the environment of the push."""
    repo = make_git_repo(tmp_path / "repo")
    # Sentinels a leak would carry along with the token. Every role has `Bash` and inherits
    # this process's environment, so what the push does not see, a role cannot print.
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-sentinel")
    monkeypatch.setenv("GITHUB_TOKEN", "env-token-sentinel")
    record = _install_fake_git(tmp_path, monkeypatch)

    git_ops.push(repo, "agent/t-1", token=_TOKEN, repository="owner/repo")

    calls = _invocations(record)
    pushes = [call for call in calls if " push " in f"{call[0]} "]
    assert len(pushes) == 1, f"expected exactly one push, got {[c[0] for c in calls]}"
    argv, env = pushes[0]

    assert _TOKEN not in argv, "the PAT is not on the command line, which /proc exposes"
    assert _TOKEN not in "\n".join(f"{k}={v}" for k, v in env.items())
    assert "x-access-token" not in argv

    # The push itself names only the remote, and the URL behind it was written by a
    # separate `remote` call -- which is where a credential-in-the-URL would have gone.
    # `.git/config` outlives the push and `git remote -v` is one `Bash` call away from
    # every role, so this is the assertion that matters most after the argv one.
    assert " origin " in f"{argv} "
    remotes = [call for call in calls if " remote " in f"{call[0]} "]
    assert remotes, f"expected a remote call, got {[c[0] for c in calls]}"
    for remote_argv, _ in remotes:
        assert "https://github.com/owner/repo.git" in remote_argv or "get-url" in remote_argv
        assert _TOKEN not in remote_argv
        assert "@github.com" not in remote_argv

    # The environment is built from nothing, not copied.
    assert env.get("GIT_ASKPASS"), "authentication goes through the askpass script"
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    assert "GITHUB_TOKEN" not in env
    assert not [name for name in env if name.startswith("AWS_")], sorted(env)
    # ...but the config group survives, or the push would die on the mount.
    assert env["GIT_CONFIG_COUNT"] == git_ops.git_config_env()["GIT_CONFIG_COUNT"]

    # Nothing durable is left holding the PAT.
    assert not list(Path(env["GIT_ASKPASS"]).parent.parent.glob("askpass-*/token"))

    # And the token never entered this process's own environment either.
    assert _TOKEN not in "\n".join(f"{k}={v}" for k, v in os.environ.items())


# The three invocations in this module that reach a network, as callables over the scratch
# directory the test hands them. `_run_authenticated` is the only path any of them may take.
_REMOTE = "https://github.com/owner/repo.git"

# The verbs that talk to a remote. Everything else git is asked to do here is local and
# runs with this process's own environment on purpose.
_NETWORK_VERBS = ("clone", "push", "fetch", "ls-remote")


def _a_clone_with_a_token(dest: Path) -> None:
    git_ops.clone(_REMOTE, dest / "clone", token=_TOKEN)


def _a_clone_of_a_public_repository(dest: Path) -> None:
    git_ops.clone(_REMOTE, dest / "clone", token=None)


def _a_push(dest: Path) -> None:
    git_ops.push(dest / "repo", "agent/t-1", token=_TOKEN, repository="owner/repo")


@pytest.mark.parametrize(
    ("operation", "expect_askpass"),
    [
        (_a_clone_with_a_token, True),
        (_a_clone_of_a_public_repository, False),
        (_a_push, True),
    ],
    ids=["clone-with-a-token", "clone-of-a-public-repository", "push"],
)
def test_every_network_operation_authenticates_and_leaks_nothing(
    operation: Callable[[Path], None],
    expect_askpass: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant 4, swept over every remote invocation rather than just the push.

    A new network call added to this module without going through `_run_authenticated`
    would leak the PAT into an argv or inherit this process's environment, and nothing
    would notice until a role printed it. The fake git creates no clone, which is fine:
    nothing here reads one back -- the assertions are about what git was handed. The
    *local* clone path short-circuits before authentication and is covered by
    `test_a_local_clone_source_is_trusted_too`.
    """
    # The push arm's repository, built before the recorder goes on `PATH`, because
    # `make_git_repo` shells out to git too and the fake would answer instead of the real
    # thing. The clone arms ignore it.
    make_git_repo(tmp_path / "repo")
    # Sentinels a leak would carry along with the token, in this process's environment
    # exactly as a deployed container has them.
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-sentinel")
    monkeypatch.setenv("GITHUB_TOKEN", "env-token-sentinel")
    record = _install_fake_git(tmp_path, monkeypatch)

    operation(tmp_path)

    calls = _invocations(record)
    assert calls, "the recording git ran at least once"
    for argv, env in calls:
        assert _TOKEN not in argv, "the PAT is not on a command line, which /proc exposes"
        assert _TOKEN not in "\n".join(f"{name}={value}" for name, value in env.items())
        assert "x-access-token" not in argv
        # A credential in a URL is also in `.git/config` and in the reflog, both of which
        # outlive the command and are one `Bash` call away from every role.
        assert "@github.com" not in argv, argv

    network = [(argv, env) for argv, env in calls if argv.split()[1] in _NETWORK_VERBS]
    assert len(network) == 1, f"expected one network invocation, got {[a for a, _ in calls]}"
    argv, env = network[0]
    # `GIT_TERMINAL_PROMPT` is the fingerprint only `_run_authenticated` leaves, so this is
    # how "every network operation goes through the authenticated path" is asserted without
    # reading the source.
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # ...and the config group survives that fresh environment, or the operation would die
    # on the foreign-owned mount.
    assert env["GIT_CONFIG_COUNT"] == git_ops.git_config_env()["GIT_CONFIG_COUNT"]
    assert bool(env.get("GIT_ASKPASS")) is expect_askpass, (
        "the askpass script exists exactly when a token was passed; a public clone needs "
        "no credentials and must not be handed one"
    )
    # Built from nothing rather than copied. Asserted on the network invocation and not on
    # the whole recording: the `remote get-url`/`set-url` calls the push makes on the way
    # are local and inherit this process's environment by design.
    assert not [name for name in env if name.startswith("AWS_")], sorted(env)
    assert "GITHUB_TOKEN" not in env


def test_the_askpass_script_answers_both_prompts() -> None:
    """The script is how the PAT reaches git without a command line or an environment."""
    with git_ops._askpass(_TOKEN) as script:
        directory = script.parent
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "token").stat().st_mode) == 0o600
        # `$1` is the prompt text, which is the only thing distinguishing the two calls.
        # A list argv: the askpass script this test just created, and a fixed prompt.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        username = subprocess.run(
            [str(script), "Username for 'https://github.com': "],
            capture_output=True,
            text=True,
            check=True,
        )
        assert username.stdout == "x-access-token"
        # A list argv: the askpass script this test just created, and a fixed prompt.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        password = subprocess.run(
            [str(script), "Password for 'https://x-access-token@github.com': "],
            capture_output=True,
            text=True,
            check=True,
        )
        assert password.stdout == _TOKEN
        # The token is read from a sibling file, never interpolated into the script, whose
        # contents are world-readable the moment anything widens the directory's mode.
        assert _TOKEN not in script.read_text(encoding="utf-8")

    assert not directory.exists(), "removed in `finally`, so nothing durable holds the PAT"


# --------------------------------------------------------------------------------------
# the answers the nodes' guards are built on
# --------------------------------------------------------------------------------------


def test_changed_paths_names_files_and_not_directories(tmp_path: Path) -> None:
    """`code`'s no-op guard filters on a path prefix, so a collapsed `dir/` would defeat it."""
    repo = make_git_repo(tmp_path / "repo", files={"README.md": "seed\n"})
    write(repo / "pkg" / "a.py", "a = 1\n")
    write(repo / "pkg" / "b.py", "b = 2\n")
    write(repo / ".agent" / "spec" / "iteration-0.md", "the spec\n")

    paths = git_ops.changed_paths(repo)
    assert "pkg/a.py" in paths
    assert "pkg/b.py" in paths
    assert "pkg/" not in paths, "the default --untracked-files collapses this and hides it"

    # The guard both nodes apply, spelled out here because it is the reason for the above.
    assert [p for p in paths if not p.startswith(".agent/")] == ["pkg/a.py", "pkg/b.py"]
    assert git_ops.has_changes(repo)


def test_a_rename_is_reported_as_the_path_that_exists_now(tmp_path: Path) -> None:
    """Every caller is asking what this run produced, not what it consumed."""
    repo = make_git_repo(tmp_path / "repo", files={"old.py": "x = 1\n"})
    subprocess.run(
        ["git", "mv", "old.py", "new.py"],
        cwd=str(repo),
        env=os.environ | git_ops.git_config_env(),
        check=True,
        capture_output=True,
    )
    assert git_ops.changed_paths(repo) == ["new.py"]


def test_create_branch_is_idempotent(tmp_path: Path) -> None:
    """The resume path re-runs `checkout` against a clone that already has the branch.

    `git checkout -b` fails there, and failing on the resume would discard the hours of
    work the checkpoint exists to keep.
    """
    repo = make_git_repo(tmp_path / "repo")
    git_ops.create_branch(repo, "agent/t-1")
    assert git_ops.current_branch(repo) == "agent/t-1"

    subprocess.run(
        ["git", "checkout", "--quiet", "main"],
        cwd=str(repo),
        env=os.environ | git_ops.git_config_env(),
        check=True,
        capture_output=True,
    )
    git_ops.create_branch(repo, "agent/t-1")  # must not raise
    assert git_ops.current_branch(repo) == "agent/t-1"


def test_default_branch_falls_back_to_main(tmp_path: Path) -> None:
    """The base of a pull request, so a missing `origin/HEAD` must not be fatal.

    A task that cannot name a base branch would otherwise die before it planned anything,
    and `refs/remotes/origin/HEAD` is absent from a local clone and from a scratch repo.
    """
    assert git_ops.default_branch(make_git_repo(tmp_path / "repo")) == "main"


def test_a_local_clone_source_is_trusted_too(tmp_path: Path) -> None:
    """git reports a clone *source* as its gitdir, and no caller above knows that path."""
    source = make_git_repo(tmp_path / "source", files={"README.md": "seed\n"})
    dest = tmp_path / "work" / "repo"

    git_ops.clone(str(source), dest)

    assert (dest / ".git").exists()
    assert (dest / "README.md").read_text(encoding="utf-8") == "seed\n"
    values = _safe_directories()
    assert str(source.resolve()) in values
    assert str(source.resolve() / ".git") in values


def test_a_commit_carries_the_exported_identity(tmp_path: Path) -> None:
    """A container has no `~/.gitconfig`, and the identity is in the group for that reason.

    In the group rather than on the command line, so a commit made by a subagent's `Bash`
    carries the same one -- which is what makes the PR's authorship consistent.
    """
    repo = make_git_repo(tmp_path / "repo")
    write(repo / "src.py", "x = 1\n")
    sha = git_ops.commit(repo, "the change\n\nbody\n")

    assert len(sha) == 40
    shown = subprocess.run(
        ["git", "show", "--no-patch", "--format=%an <%ae>%n%s", sha],
        cwd=str(repo),
        env=os.environ | git_ops.git_config_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    assert shown.stdout.splitlines()[0] == "agentcore-coding-agent <agent@localhost>"
    assert git_ops.changed_paths(repo) == [], "commit stages everything"


@pytest.mark.parametrize(
    ("name", "email", "expected"),
    [
        # The case this exists for. `agent@localhost` resolves to no GitHub account, so
        # GitHub reports no author for the commit and a check gating on commit attribution
        # blocks the pull request -- observed as a Vercel preview answering `Deployment was
        # blocked`, which reads as a build failure and is not one.
        (
            "release-bot",
            "1234+release-bot@users.noreply.github.com",
            "release-bot <1234+release-bot@users.noreply.github.com>",
        ),
        # Empty is what a Lambda forwards when the context key was never set, and it has to
        # mean "use the default" rather than "commit with an empty identity" -- git rejects
        # the latter with `empty ident name not allowed`, which would fail every submit.
        ("", "", "agentcore-coding-agent <agent@localhost>"),
        # One set and one not: they fall back independently, so a deployment can override
        # just the address and keep the name that says a machine wrote this.
        (
            "",
            "1234+bot@users.noreply.github.com",
            "agentcore-coding-agent <1234+bot@users.noreply.github.com>",
        ),
        # Surrounding whitespace is not an identity. Stripped, so a stray space in a CDK
        # context value does not become part of the committer's name.
        ("  spaced  ", "  bot@example.com  ", "spaced <bot@example.com>"),
    ],
)
def test_the_commit_identity_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, email: str, expected: str
) -> None:
    """`AGENT_GIT_AUTHOR_NAME`/`_EMAIL` are fleet policy, read at import into the config
    group, so a reload is how a test sees a different one. The group -- not `-c user.email`
    on the commit -- is still what carries it, so a subagent's own `git` agrees.
    """
    monkeypatch.setenv("AGENT_GIT_AUTHOR_NAME", name)
    monkeypatch.setenv("AGENT_GIT_AUTHOR_EMAIL", email)
    reloaded = importlib.reload(git_ops)
    try:
        repo = make_git_repo(tmp_path / "repo")
        write(repo / "src.py", "x = 1\n")
        sha = reloaded.commit(repo, "the change")
        shown = subprocess.run(
            ["git", "show", "--no-patch", "--format=%an <%ae>", sha],
            cwd=str(repo),
            env=os.environ | reloaded.git_config_env(),
            capture_output=True,
            text=True,
            check=True,
        )
        assert shown.stdout.strip() == expected
    finally:
        # Every other test in this file uses the module-level default, and the reload above
        # is process-wide. `--dist loadfile` keeps this file on one xdist worker, so leaving
        # a rebound module behind would corrupt its neighbours rather than only this test.
        monkeypatch.delenv("AGENT_GIT_AUTHOR_NAME", raising=False)
        monkeypatch.delenv("AGENT_GIT_AUTHOR_EMAIL", raising=False)
        importlib.reload(git_ops)
