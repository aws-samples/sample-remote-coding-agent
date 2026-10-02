"""The runner for the repository's *own* commands -- setup and verification.

One private `_run` for both, so the environment, the output bound and the
timeout-is-a-result rule cannot drift between them. Nothing here forms a verdict:
a `CommandResult` is a measurement, and `nodes/review.py` is the only module
allowed to read one as pass or fail.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from . import git_ops
from .state import CommandResult

log = logging.getLogger(__name__)

# Invariant 27: `verify` makes no model call, so a *healthy* verification is silent
# for up to 30 minutes -- that is the figure the watchdog's quiet threshold has to
# clear. `SETUP_TIMEOUT <= VERIFY_TIMEOUT` and `nodes/verify.py` writes an activity
# row when setup finishes, so the two silences never add up to 45 minutes.
VERIFY_TIMEOUT = 1800
SETUP_TIMEOUT = 900

# The output is carried on the graph state, into the checkpoint and into the review
# prompt, so it is bounded here rather than at each of those three readers.
_MAX_OUTPUT = 20_000

# Everything a `make test` legitimately needs and nothing that identifies us.
_ALLOWED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")


def _base_env() -> dict[str, str]:
    """The environment the repository's own command runs under, built from nothing.

    Invariant 32: no `AWS_*`, no GitHub token, nothing else -- the command comes out
    of an untrusted clone and gets an allow-list, not a filtered copy of ours.

    `CI=1` because a suite that asks is asking whether it may prompt or colourise.

    The `GIT_CONFIG_*` group is copied back explicitly *because* the environment is
    built from nothing, and it moves as a unit or not at all: a `GIT_CONFIG_COUNT`
    without its matching `GIT_CONFIG_KEY_<n>`/`VALUE_<n>` is a fatal git error, not
    a warning. It is what lets a verification command run `git` at all on the
    managed filesystem (invariant 25).
    """
    env = {name: os.environ[name] for name in _ALLOWED_ENV if name in os.environ}
    env["CI"] = "1"
    env.update(git_ops.git_config_env())
    return env


def _merge(*parts: str | bytes | None) -> str:
    """stdout and stderr as one stream, which is how a human reads a failing build.

    `bytes` are tolerated because `subprocess.TimeoutExpired` carries the *undecoded*
    partial buffers on POSIX even when the call asked for `text=True` -- decoding
    them here is what keeps the timeout path from raising a `TypeError` of its own.
    """
    return "".join(
        part.decode("utf-8", "replace") if isinstance(part, bytes) else part
        for part in parts
        if part
    )


def _bounded(output: str) -> str:
    """Trim to `_MAX_OUTPUT` keeping the **tail** -- the failure is at the end.

    The marker is at the front and counts against the bound, so the field never
    exceeds `_MAX_OUTPUT` and a truncated read is never mistaken for a short one.
    """
    if len(output) <= _MAX_OUTPUT:
        return output
    marker = f"...({len(output) - _MAX_OUTPUT} characters of earlier output truncated)...\n"
    keep = _MAX_OUTPUT - len(marker)
    return marker + output[-keep:]


def _run(command: str, *, cwd: Path, timeout: int, label: str) -> CommandResult:
    """Run one declared command and report what happened; never raise."""
    log.info("running the %s command in %s: %s", label, cwd, command)
    try:
        # `shell=True` is deliberate: `command` is a declared entry point such as `make
        # test`, detected from the repository or configured by an operator, and it needs a
        # shell to be one (`&&`, `$PATH` lookup, quoting). It is not an injection site to
        # be closed either -- running an untrusted repository's own declared command is
        # the whole product, which the dedicated account, the repository-scoped PAT and
        # `_base_env` are what bound. See `docs/security.md`, "Only register repositories
        # you trust".
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        proc = subprocess.run(  # nosec B602 -- running the repository's own command IS the product
            command,
            # The same deliberate choice as above, flagged again by its own rule. Both id
            # spellings, because semgrep reports the rule's own id with its last segment
            # doubled while a report may quote the single form, and a marker naming one
            # does not match the other.
            # nosemgrep: python.lang.security.audit.subprocess-shell-true, python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
            shell=True,
            capture_output=True,
            text=True,
            env=_base_env(),
            cwd=cwd,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # A timeout is a result, not an exception: it has to reach `review` like any
        # other red run, or a hung suite becomes an opaque `FAILED` with no output.
        # 124 is what `timeout(1)` reports, so it reads correctly in the PR body.
        log.warning("the %s command timed out after %ds: %s", label, timeout, command)
        return CommandResult(
            command=command,
            exitCode=124,
            output=_bounded(_merge(exc.stdout, exc.stderr)),
            warning=f"the {label} command timed out after {timeout}s",
        )
    return CommandResult(
        command=command,
        exitCode=proc.returncode,
        output=_bounded(_merge(proc.stdout, proc.stderr)),
        warning=None,
    )


def run_command(command: str, *, cwd: Path) -> CommandResult:
    """Run the verification command. Sync -- nodes call it via `asyncio.to_thread`."""
    return _run(command, cwd=cwd, timeout=VERIFY_TIMEOUT, label="verification")


def run_setup(command: str, *, cwd: Path) -> CommandResult:
    """Run the setup command. Same runner, same environment, shorter timeout."""
    return _run(command, cwd=cwd, timeout=SETUP_TIMEOUT, label="setup")
