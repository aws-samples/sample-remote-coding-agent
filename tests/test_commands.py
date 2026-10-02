"""The repository's own commands run in an environment built from nothing.

Invariant 32 is the subject of most of this file: a setup or verification command comes
out of a clone nobody has reviewed, so it gets an allow-list -- `PATH`, `HOME`, `LANG`,
`CI` -- plus the whole `git_ops.git_config_env()` group, and nothing that identifies this
container. The group is the one thing copied back, because without it every `git` the
repository's suite runs dies with `fatal: detected dubious ownership` (invariant 25).

The rest pins the runner's measurement-not-verdict promises: the output bound keeps the
tail, a timeout is a `CommandResult`, and setup and verification go through one `_run`.

Every command here is a shell one-liner running *this* interpreter, so nothing depends on
coreutils being installed, nothing reaches the network, and no timeout is ever waited out
in real time. `run_command`/`run_setup` are synchronous -- the nodes are what wrap them in
`asyncio.to_thread` -- so nothing here is async.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from agent_helpers import make_git_repo
from coding_agent import commands, git_ops
from coding_agent.state import CommandResult

# Everything that identifies this container, or pays for it. Note that `AWS_REGION` and
# `AWS_ACCESS_KEY_ID` are genuinely set in the process these tests run in (the image sets
# the region, `conftest._aws_env` sets the credentials), so "absent from the child" is a
# real measurement rather than a tautology about a variable nobody set.
_LEAKY_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "GITHUB_TOKEN",
    # The configuration id, which is what `store.github_token` exchanges for a GitHub token in
    # the vault. It is not itself a credential, but a repository command has no business
    # knowing it: with it and the container's execution-role credentials, the two-call exchange
    # is reachable from inside an untrusted `make test`. `SECRET_ARN` was here and went with
    # the secret.
    "CONFIG_ID",
)

# `shell=True` means there is a `/bin/sh` between the runner and the command, and a shell
# adds these to what it execs regardless of what it was handed. They are dropped so every
# assertion below is about what `commands._base_env()` contributed and nothing else --
# `_` in particular is the pathname of the command being run, so it differs per call.
_SHELL_ADDED = frozenset({"PWD", "OLDPWD", "SHLVL", "_"})

# The child reports its own `os.environ` as JSON rather than running `env`: `os.environ` is
# exactly what the invariant is about, and coreutils is not a declared dependency of this
# repository. The path to write is `argv[1]`, so nothing is parsed out of the output.
_DUMP_ENV = r"""
import json
import os
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps(dict(os.environ)), encoding="utf-8")
"""

# `argv[1]` characters of padding between the two markers, so the caller sizes the flood
# from `commands._MAX_OUTPUT` instead of hard-coding a number that can drift away from it.
_FLOOD = r"""
import sys

sys.stdout.write("HEAD-MARKER\n")
sys.stdout.write("x" * int(sys.argv[1]))
sys.stdout.write("\nTAIL-MARKER\n")
"""

_SLEEP = r"""
import sys
import time

sys.stdout.write("started\n")
sys.stdout.flush()
time.sleep(float(sys.argv[1]))
"""

_BOTH_STREAMS = r"""
import sys

sys.stdout.write("on-stdout\n")
sys.stderr.write("on-stderr\n")
"""

_PRINT_CWD = r"""
import sys
from pathlib import Path

sys.stdout.write(str(Path.cwd()))
"""

# `git`'s own hook for pretending a repository belongs to somebody else, which is the only
# way to reproduce the managed filesystem's ownership on a normal `tmp_path`. It goes in
# the *command* rather than in this process's environment on purpose: `_base_env()` builds
# from nothing, so a `monkeypatch.setenv` of it would never reach the child at all -- which
# is invariant 32 doing its job, not a way to configure the child.
_ASSUME_FOREIGN_STATUS = "GIT_TEST_ASSUME_DIFFERENT_OWNER=1 git status --porcelain"


def _shell(*args: object) -> str:
    """A shell one-liner running this interpreter, quoted the way a shell needs."""
    return shlex.join([sys.executable, *(str(arg) for arg in args)])


def _child_env(
    tmp_path: Path,
    runner: Callable[..., CommandResult] = commands.run_command,
    dump: str = "env.json",
) -> dict[str, str]:
    """The environment a command actually saw, as `commands` handed it over."""
    target = tmp_path / dump
    result = runner(_shell("-c", _DUMP_ENV, target), cwd=tmp_path)
    assert result["exitCode"] == 0, result["output"]
    seen: dict[str, str] = json.loads(target.read_text(encoding="utf-8"))
    return {name: value for name, value in seen.items() if name not in _SHELL_ADDED}


@pytest.mark.parametrize(
    "runner", [commands.run_command, commands.run_setup], ids=["verify", "setup"]
)
@pytest.mark.parametrize("name", _LEAKY_ENV)
def test_the_environment_is_built_from_nothing(
    name: str,
    runner: Callable[..., CommandResult],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant 32: both runners get an allow-list, not a filtered copy of ours.

    Parametrised over the *runner* as well as the variable, because a leak that reached
    only `run_setup` would be invisible to a test that measured verification alone --
    and setup runs first, on the same untrusted clone.
    """
    sentinel = f"sentinel-{name.lower().replace('_', '-')}"
    monkeypatch.setenv(name, sentinel)

    child = _child_env(tmp_path, runner)

    assert name not in child, (
        f"invariant 32: {name} reached the repository's own command. The environment is "
        "built from an allow-list precisely so that adding a variable to this container "
        f"cannot leak it into untrusted code. The child saw {sorted(child)}"
    )
    assert sentinel not in "\n".join(child.values()), (
        f"invariant 32: the value of {name} reached the command under another name"
    )
    # What *is* allowed has to be there, or every assertion above would also hold for a
    # runner that passed no environment at all -- including `PATH`, without which a
    # `make test` cannot find its own interpreter.
    assert child["PATH"] == os.environ["PATH"]
    assert child["CI"] == "1", "a suite that asks is asking whether it may prompt"


@pytest.mark.parametrize(
    "runner", [commands.run_command, commands.run_setup], ids=["verify", "setup"]
)
def test_no_aws_variable_of_any_name_reaches_the_command(
    runner: Callable[..., CommandResult], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 32: no `AWS_*` at all, including the ones nobody has thought of yet.

    An allow-list rather than a deny-list is the whole point: this test passes for a
    variable AWS invents next year, which is a property no filtered copy of `os.environ`
    can have. `AWS_CONTAINER_CREDENTIALS_FULL_URI` is the one that matters most -- it is
    how a process on this task role picks the role up without a key in sight, so a
    deny-list written around `AWS_ACCESS_KEY_ID` would hand an untrusted clone the whole
    execution role while looking clean.
    """
    sentinels = {
        "AWS_ACCESS_KEY_ID": "sentinel-key-id",
        "AWS_SECRET_ACCESS_KEY": "sentinel-secret",
        "AWS_SESSION_TOKEN": "sentinel-session",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.2/sentinel-credentials",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN": "sentinel-container-token",
        "AWS_LAMBDA_FUNCTION_NAME": "sentinel-function",
        "AWS_A_VARIABLE_THAT_DOES_NOT_EXIST_YET": "sentinel-invented",
    }
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)

    child = _child_env(tmp_path, runner)

    assert [name for name in child if name.startswith("AWS_")] == [], (
        f"invariant 32: the command was handed AWS identity: {sorted(child)}"
    )
    leaked = sorted(value for value in sentinels.values() if value in child.values())
    assert leaked == [], (
        f"invariant 32: an AWS value reached the command under another name: {leaked}"
    )

    # What *is* allowed has to be there, or every assertion above would also hold for a
    # runner that handed the command no environment at all -- and `GIT_CONFIG_COUNT`
    # without its group is a fatal git error on the managed filesystem (invariant 25).
    assert child["PATH"] == os.environ["PATH"]
    assert child["CI"] == "1"
    group = {name: value for name, value in child.items() if name.startswith("GIT_CONFIG_")}
    assert group == git_ops.git_config_env()


def test_the_git_config_group_survives_into_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 25: the ownership exemption reaches the command whole, or its git dies."""
    repo = make_git_repo(tmp_path / "repo")
    # After `make_git_repo`, because `trust` appends `repo` to the process-wide group.
    expected = git_ops.git_config_env()

    child = _child_env(tmp_path)
    group = {name: value for name, value in child.items() if name.startswith("GIT_CONFIG_")}

    assert group == expected, (
        "the group moves as a unit or not at all: a `GIT_CONFIG_COUNT` of 4 with three "
        "pairs is `fatal: missing config key GIT_CONFIG_KEY_3` on every git the "
        "repository's command runs, not a warning"
    )
    assert child["GIT_CONFIG_COUNT"] == str(len(git_ops._CONFIG_ENTRIES))
    assert str(repo.resolve()) in child.values(), "the repo's own exemption is in the group"

    status = commands.run_command(_ASSUME_FOREIGN_STATUS, cwd=repo)
    assert status["exitCode"] == 0, (
        f"invariant 25: git refused a foreign-owned repository through `run_command`: "
        f"{status['output']}"
    )

    # A control, with the group withheld, proving the exemption is what earned that 0
    # rather than the check being inactive here. `GIT_TEST_ASSUME_DIFFERENT_OWNER` is
    # git's own test hook: if a build ever stops honouring it this control simply passes,
    # and the two assertions below are skipped rather than asserting something false.
    bare = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if bare.returncode != 0:
        assert "dubious ownership" in bare.stderr
        monkeypatch.setattr(git_ops, "git_config_env", lambda: {})
        withheld = commands.run_command(_ASSUME_FOREIGN_STATUS, cwd=repo)
        assert withheld["exitCode"] != 0
        assert "dubious ownership" in withheld["output"]


def test_setup_and_verify_share_one_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant 27/32: one `_run`, so the environment and the bound cannot drift."""
    command = _shell("-c", "pass")
    verify = commands.run_command(command, cwd=tmp_path)
    setup = commands.run_setup(command, cwd=tmp_path)

    assert set(verify) == set(setup) == set(CommandResult.__annotations__)
    assert set(verify) == {"command", "exitCode", "output", "warning"}
    assert verify == setup, "the same command through either entry point is the same result"
    assert _child_env(tmp_path, dump="verify.json") == _child_env(
        tmp_path, commands.run_setup, dump="setup.json"
    ), "setup and verification must not be able to run under different environments"

    # Two copies of the runner would drift, and the copy that drifts is the one nobody
    # looks at -- so this pins that both entry points go through the *same* `_run`, and
    # that the timeout is the only thing they choose.
    calls: list[dict[str, object]] = []

    def spy(command: str, *, cwd: Path, timeout: int, label: str) -> CommandResult:
        calls.append({"command": command, "cwd": cwd, "timeout": timeout, "label": label})
        return CommandResult(command=command, exitCode=0, output="", warning=None)

    monkeypatch.setattr(commands, "_run", spy)
    commands.run_command(command, cwd=tmp_path)
    commands.run_setup(command, cwd=tmp_path)

    assert [call["command"] for call in calls] == [command, command]
    assert [call["cwd"] for call in calls] == [tmp_path, tmp_path]
    assert [call["label"] for call in calls] == ["verification", "setup"]
    assert [call["timeout"] for call in calls] == [commands.VERIFY_TIMEOUT, commands.SETUP_TIMEOUT]
    assert commands.SETUP_TIMEOUT != commands.VERIFY_TIMEOUT
    # Invariant 27: setup finishes first and `verify` writes an activity row there, so the
    # two silences never add up past the watchdog's quiet threshold.
    assert commands.SETUP_TIMEOUT <= commands.VERIFY_TIMEOUT


def test_output_keeps_the_tail(tmp_path: Path) -> None:
    """The output bound keeps the tail, because a suite's failure summary is at the end."""
    result = commands.run_command(_shell("-c", _FLOOD, commands._MAX_OUTPUT * 2), cwd=tmp_path)

    assert result["exitCode"] == 0
    assert len(result["output"]) <= commands._MAX_OUTPUT, (
        "the marker counts against the bound, so a truncated field never exceeds it"
    )
    assert "TAIL-MARKER" in result["output"], "the end of a failing build is the part read"
    assert "HEAD-MARKER" not in result["output"], "the head is what gets dropped, not the tail"
    assert "truncated" in result["output"], "a truncated read must not look like a short one"


def test_a_timeout_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout is a measurement: `review` gets exit 124 and a warning, never an exception."""
    # `_run` reads `VERIFY_TIMEOUT` at call time, so one second is the whole cost of this
    # test rather than the real 1800.
    monkeypatch.setattr(commands, "VERIFY_TIMEOUT", 1)

    result = commands.run_command(_shell("-c", _SLEEP, 5), cwd=tmp_path)

    assert result["exitCode"] == 124, (
        f"a hung suite has to reach `review` like any other red run: {result}"
    )
    assert result["warning"] is not None
    assert "timed out" in result["warning"]
    assert "1s" in result["warning"], "the warning names the bound that was hit"
    # `TimeoutExpired` carries the *undecoded* partial buffers on POSIX; anything that
    # forgot that would have raised a `TypeError` before reaching this line.
    assert isinstance(result["output"], str)


def test_a_non_zero_exit_is_reported_not_raised(tmp_path: Path) -> None:
    """Nothing here forms a verdict: a red command is an `exitCode`, not an exception."""
    result = commands.run_command(_shell("-c", "raise SystemExit(3)"), cwd=tmp_path)

    assert result["exitCode"] == 3
    assert result["warning"] is None, "a warning is for a run nothing measured, not a red one"


def test_output_merges_stdout_and_stderr(tmp_path: Path) -> None:
    """One stream, which is how a human reads a failing build."""
    result = commands.run_command(_shell("-c", _BOTH_STREAMS), cwd=tmp_path)

    assert "on-stdout" in result["output"]
    assert "on-stderr" in result["output"]


def test_the_command_runs_in_the_repository(tmp_path: Path) -> None:
    """`cwd` is the clone: a repository's `make test` never runs in the agent's own tree."""
    repo = tmp_path / "repo"
    repo.mkdir()

    result = commands.run_command(_shell("-c", _PRINT_CWD), cwd=repo)

    assert result["exitCode"] == 0
    assert result["output"].strip() == str(repo.resolve())
    assert result["output"].strip() != str(Path.cwd())


def test_the_runner_writes_nothing_into_this_process_environment(tmp_path: Path) -> None:
    """Invariant 31's spirit: `commands` reads this environment and never adds to it."""
    before = dict(os.environ)

    commands.run_command(_shell("-c", "pass"), cwd=tmp_path)
    commands.run_setup(_shell("-c", "pass"), cwd=tmp_path)

    assert dict(os.environ) == before, (
        "`CI=1` and the git config group belong in the child's environment; setting "
        "either here would put them in every subagent's `Bash` too"
    )
