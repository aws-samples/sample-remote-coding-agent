"""The sole publisher: commit, push, open the pull request.

Nothing else in the tree writes to a git remote or to the GitHub API. Two properties are
worth stating because both were once violated:

- `draft=` keys on `verified` alone, never on `passed`. A run with no verification command
  has `passed: True` and must still produce a draft.
- The guard here is this node's **own**, independent of the one in `nodes/code.py`. Two
  guards for one rule looks redundant until a graph change routes around the first.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .. import git_ops, github, store
from .. import state as state_module
from ..state import TaskState, VerifyResult

log = logging.getLogger(__name__)

_MAX_TITLE = 72
_MAX_BODY_WARNING = 2000
# How much of an advisory pass's file the body quotes. Lower than `review._MAX_ADVISORY`
# on purpose: the file is the record, the body is the summary a human skims.
_MAX_BODY_ADVISORY = 4000


class SubmitError(RuntimeError):
    """Raised when there is nothing publishable, or the push/PR failed."""


def has_source_changes(repo: Path) -> bool:
    """True when something outside `.agent/` changed.

    Deliberately duplicated rather than imported from `nodes/code.py`: this node is the
    last thing between an empty run and a pull request, and the point of a second guard
    is that it does not share a code path with the first (invariant 5).
    """
    return any(not p.startswith(".agent/") for p in git_ops.changed_paths(repo))


def _title(state: TaskState) -> str:
    """The first line of the human's description, bounded. Never derived from the
    verdict -- a title that said "fix" or "attempt" would be editorialising."""
    first = (state.get("description") or "automated change").strip().splitlines()[0].strip()
    if len(first) > _MAX_TITLE:
        first = first[: _MAX_TITLE - 1].rstrip() + "…"
    return first


def _commit_message(state: TaskState) -> str:
    body = (state.get("description") or "").strip()
    return f"{_title(state)}\n\n{body}\n\nTask: {state.get('taskId', '')}\n"


def _pr_body(state: TaskState) -> str:
    """What ran, what it said, and -- when the run is unverified -- why this is a draft."""
    vr = state.get("verifyResult") or {}
    verified = bool(vr.get("verified"))
    lines = [
        (state.get("description") or "").strip(),
        "",
        "## Verification",
        "",
    ]
    command = vr.get("command") or ""
    if command:
        lines.append(f"- Command: `{command}`")
        lines.append(f"- Exit code: `{vr.get('exitCode')}`")
    else:
        lines.append("- No verification command was detected in this repository.")
    if vr.get("warning"):
        # Verbatim. The warning is the only field that can say *why* nothing was judged,
        # and paraphrasing it here would lose the one detail a human needs.
        lines.append(f"- Warning: {str(vr['warning'])[:_MAX_BODY_WARNING]}")
    lines.append(f"- Attempts: {state.get('iteration', 0)} of {state_module.MAX_ITERATIONS}")

    if not verified:
        lines += [
            "",
            "**This pull request is a draft because the change was not verified.** "
            "`verified` is true only when a real verification command ran and a reviewer "
            "judged it green; anything else -- no command configured, a failed setup, a "
            "reviewer veto, a missing verdict -- lands here instead of quietly opening a "
            "pull request that looks checked.",
        ]

    spec_path = state.get("specPath")
    review_path = state.get("reviewPath")
    if spec_path or review_path:
        lines += ["", "## Artefacts", ""]
        if spec_path:
            lines.append(f"- Specification: `{spec_path}`")
        if review_path:
            lines.append(f"- Review: `{review_path}`")

    lines += _advisory_section(
        state,
        heading="## Security review (advisory)",
        path_key="securityReviewPath",
        count_key="securityFindings",
    )
    lines += _advisory_section(
        state,
        heading="## Code review (advisory)",
        path_key="codeReviewPath",
        count_key="codeReviewFindings",
    )

    return "\n".join(lines) + "\n"


def _advisory_section(
    state: TaskState, *, heading: str, path_key: str, count_key: str
) -> list[str]:
    """One advisory pass's section, or nothing at all when the run did not do that pass.

    The sentence saying so is not decoration: a security section in a pull request body
    reads like a gate unless it says otherwise, and this one is not a gate. `verified` has
    exactly one writer and a finding here has never been one of its inputs (invariant 6b).
    """
    path = state.get(path_key)
    if not path:
        return []

    count = state.get(count_key, 0)
    lines = [
        "",
        heading,
        "",
        f"- Findings (heuristic count): {count}",
        "- **Advisory only: this pass did not affect `verified` and did not gate this "
        "pull request.** The output below is not documented as machine-readable, so the "
        "count is a hint and the text is the record.",
        "",
    ]
    try:
        lines.append(Path(str(path)).read_text(encoding="utf-8")[:_MAX_BODY_ADVISORY])
    except OSError:
        # The file is written by `review` under `.agent/`, so this is the resumed-run and
        # hand-edited-tree case. A missing advisory file is not worth a failed submission.
        lines.append(f"(the output at `{path}` is not available)")
    return lines


async def submit(state: TaskState) -> dict[str, Any]:
    """Commit, push and open the pull request, and return the task's terminal status."""
    repo = Path(state["repoDir"])
    # Annotated, because this one is passed on to `store.record_result`, which declares
    # `VerifyResult`: a `TypedDict` is not assignable to `dict[str, Any]`, so the type has
    # to survive the binding. `{}` is a perfectly good `VerifyResult` -- every field is
    # optional, and an absent `verified` degrades to a draft, which is the point.
    vr: VerifyResult = state.get("verifyResult") or {}

    if not has_source_changes(repo):
        raise SubmitError(
            "there is nothing to submit outside .agent/: a pull request containing only a "
            "specification must never exist (invariant 5)"
        )

    if state.get("noSubmit"):
        log.info("--no-submit: skipping the commit, the push and the pull request")
        return {"status": state_module.terminal_status(vr)}

    sha = git_ops.commit(repo, _commit_message(state))
    log.info("committed %s", sha[:12])

    # Fetched at this moment, and passed as an argument. It is never written into
    # os.environ, never into the origin URL and never onto a command line (invariant 4);
    # every coding subagent has `Bash` and inherits this process's environment.
    #
    # The `GITHUB_TOKEN` arm is the developer-machine path that `local_run.py` uses. In the
    # container there is no such variable and there must not be: the credential is fetched
    # from AgentCore Identity's token vault here, at the last moment, keyed on this task's
    # own `configId`.
    #
    # The environment is checked first and that is safe rather than sloppy: `GITHUB_TOKEN` is
    # never set in a provisioned container -- nothing in `provisioner._fleet_environment` or
    # `_container_environment` writes it, and `tests/test_env_contract.py` holds that -- so in
    # the container this falls through to the vault every time. The order buys the local run:
    # `store.github_token` builds no client when `CONFIG_TABLE` is unset, so a developer's
    # machine reaches AWS for nothing.
    token = os.environ.get("GITHUB_TOKEN", "") or store.github_token(state.get("configId", ""))
    # Raised only in a provisioned container -- `CONFIG_TABLE` is the same switch
    # `store.github_token` itself gates on. `runs.queue_run` already refuses to dispatch a run
    # against a repository with no authorisation, so reaching here empty means the
    # authorisation went away mid-run; saying so beats a `git push` that fails on a blank
    # password. A local run has no vault and legitimately pushes with whatever credentials the
    # developer's own git is configured with, so it must not be refused here.
    if not token and os.environ.get("CONFIG_TABLE"):
        raise SubmitError(
            "no GitHub credential for this repository. Authorise it in the console "
            "(Repository settings -> Authorise GitHub), or install the deployment's GitHub "
            "App on it if the authorisation is already in place."
        )
    branch = state["branch"]
    git_ops.push(repo, branch, token=token, repository=state["repository"])

    pr_url = github.create_pull_request(
        state["repository"],
        head=branch,
        base=state.get("baseBranch") or "main",
        title=_title(state),
        body=_pr_body(state),
        # One line, with `bool(...)` spelled out, so an absent `verified` field degrades
        # to a draft rather than to a confident pull request.
        draft=not bool(vr.get("verified")),
        token=token,
    )
    log.info("opened %s", pr_url)

    status = state_module.terminal_status(vr)
    # `verify_result=vr` and not `dict(vr)`: `record_result` already does `dict(...)` into
    # the `extra` map it builds, so the second copy bought nothing and cost the declared
    # type on the way through.
    store.record_result(state, status=status, pr_url=pr_url, verify_result=vr)
    return {"prUrl": pr_url, "status": status}
