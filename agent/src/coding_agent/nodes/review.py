"""The review role, and the only place in the tree where `verifyResult` is composed.

This node holds the review role's verdict and the exit code `nodes/verify.py` measured,
which is why it is the one that may combine them: `passed = exitCode == 0 and not vetoed`.
It deliberately does **not** import `commands`, so it cannot re-run the command and
cannot turn a red exit into a green one no matter what the role says (invariant 6).

The verdict is read out of `result.final_text` and never out of `result.text` (invariant 7)
and never out of a file. A file in the tree is something every role can write, and the one
bit that decides whether the pull request is a draft must not be forgeable by the role
being judged.

This node also runs two **advisory** passes, `/security-review` and `/code-review`, after
the verdict is composed. They are advisory in the strong sense: they never touch
`verified`, and that is invariant 6b. `verified` means "a real command ran and was judged
green" and it has exactly one writer; letting a security finding flip it would make the one
signal `submit`'s `draft=`, the pull request body and `state.terminal_status` all key on
mean two different things at once. So the passes return paths and counts on this node's
update, `_compose` cannot see them, and any failure inside one is a `log.warning` and an
empty section -- an advisory pass that could fail the node would give a finding a route to
the pass/fail signal by the back door.
"""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path
from typing import Any

from .. import agentcore_memory, git_ops, subagents
from ..state import CommandResult, TaskState, VerifyResult

log = logging.getLogger(__name__)

# The last match wins: a role that reasons out loud about what a FAIL would look like
# before concluding PASS must not be vetoed by its own reasoning.
_VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(PASS|FAIL)\b", re.MULTILINE | re.IGNORECASE)

# How much of the closing message is kept as the human-readable summary.
_MAX_SUMMARY = 4000
# How much of the command output the prompt carries. The tail, because the failure is at
# the end.
_MAX_OUTPUT_TAIL = 6000

# The two advisory passes. Both are built-in slash commands and both are reachable
# headless: a slash command is an ordinary SDK input and the CLI expands it before the
# model sees it.
SECURITY_COMMAND = "/security-review"
CODE_REVIEW_COMMAND = "/code-review"
# How much of an advisory pass's output is kept on disk. It reaches a DynamoDB item only
# as a count, but `submit` reads the file back into a pull request body.
_MAX_ADVISORY = 8000

# What goes on disk when `/security-review` is skipped. Written rather than omitted,
# because a missing file and a pass that found nothing look identical to a human, and this
# is the case the whole guard exists for.
_NO_ORIGIN_HEAD = (
    "# Security review (advisory) -- not run\n\n"
    "This clone has no `refs/remotes/origin/HEAD`, so `/security-review` would abort "
    'before any model call and return an empty success: `subtype: "success"`, '
    "`num_turns: 0`, `total_cost_usd: 0` and an empty result, in about 100 ms. That is "
    "indistinguishable from a pass that found nothing, so the pass was not dispatched at "
    "all and this file records why. Nothing was scanned; this is not a finding of zero.\n"
)

# What goes on disk when a pass returned nothing. `num_turns` is NOT evidence either way:
# `/code-review` reports `num_turns: 0` even when it produces real findings, so a non-empty
# `result` is the only did-it-run signal there is.
_PRODUCED_NOTHING = (
    "# {command} (advisory) -- no output\n\n"
    'The pass returned an empty result. An empty result with `subtype: "success"` is the '
    "CLI's shape for \"the command's own shell pre-command failed\", so this records that "
    "the pass DID NOT RUN. It is not a finding of zero.\n"
)

# `/security-review` emits:
#     ## Vuln 1: Command injection: `app.py:42`
#     * **Severity: HIGH**
#     * **Category:** `command-injection`
#     * **Confidence:** 9/10
# and `/code-review` emits free-form prose with a `Findings:` list. Neither shape is
# documented, so `count_findings` matches all of them loosely and none of them as a
# contract.
_VULN_HEADING_RE = re.compile(r"^\s*##+\s*Vuln\b", re.MULTILINE | re.IGNORECASE)
_SEVERITY_RE = re.compile(r"\*\*Severity:", re.IGNORECASE)
_FINDINGS_HEADING_RE = re.compile(r"^\s*Findings?\b", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*]\s")
_NO_FINDINGS_RE = re.compile(r"no (?:other )?(?:findings|issues|vulnerabilities)", re.IGNORECASE)


REVIEW_PROMPT = """You are the review role for an automated coding task on the repository
`{repository}`. You judge the uncommitted change in this working tree. You do not fix it.

The task, as the human wrote it:

{description}

The specification the change was implemented from is on disk at `{spec_path}`.

What the repository's own verification command did:

- Command: `{verify_command}`
- Exit code: {exit_code}
- Output (tail):

---
{output_tail}
---

A green exit code means one of two very different things: the change works, or the suite
never touched the change. Telling those apart by reading the diff is the entire reason
this role exists, and it is the one judgement no amount of exit-code plumbing can make.

Diff the working tree against `{base_branch}` and decide. If a `review-rigor` skill is
available to you, follow its method.

Write your analysis to `{review_path}` -- the reasoning, the hunks you checked and the
tests you found for each.

Then end your reply with exactly one line of the form

    VERDICT: PASS

or

    VERDICT: FAIL

followed by one short paragraph giving the reason. That line is what is read; a verdict
written only into a file is not read at all, and a reply with no verdict line is treated
as a FAIL.

Do not commit, do not push, and prefer not to edit the tree at all: an edit after the
last measurement leaves an exit code that describes a tree nobody ran, and it costs this
task a whole extra verification run.

What this repository has learned across every task it has ever run -- its own memory of
itself, not this task's notes:

{recalled_memory}

{tool_paragraph}
"""


class ReviewError(RuntimeError):
    """Raised when the review role could not be run at all."""


def review_path(repo: Path, iteration: int) -> Path:
    """`.agent/review/iteration-<n>.md`. Written by the role, read by nobody -- the
    re-plan prompt names the path and leaves reading it to the re-planning role."""
    return repo / ".agent" / "review" / f"iteration-{iteration}.md"


def security_review_path(repo: Path, iteration: int) -> Path:
    """`.agent/review/security-<n>.md`, the advisory security pass's output."""
    return repo / ".agent" / "review" / f"security-{iteration}.md"


def code_review_path(repo: Path, iteration: int) -> Path:
    """`.agent/review/code-<n>.md`, the advisory code-review pass's output."""
    return repo / ".agent" / "review" / f"code-{iteration}.md"


def count_findings(text: str) -> int:
    """How many findings an advisory pass appears to have reported. HEURISTIC.

    Neither command's output is documented as machine-readable, so this must never be
    parsed as a contract: the number is a hint in a pull-request body and on the task
    item, and nothing in the graph branches on it. A format change upstream makes this
    count wrong, which is a wrong number beside a body a human can read -- the failure
    mode is deliberately that boring.

    The shapes, against CLI 2.1.236: `/security-review` emits
    ``## Vuln <n>: <title>: `<file>:<line>` `` followed by `* **Severity: HIGH**`,
    `* **Category:** `...`` and `* **Confidence:** 9/10`; `/code-review` emits free-form
    prose with a `Findings:` list.
    """
    body = text or ""
    # The two structured signals, whichever counts more: a pass can emit a heading per
    # vulnerability, a severity line per vulnerability, or both.
    best = max(len(_VULN_HEADING_RE.findall(body)), len(_SEVERITY_RE.findall(body)))
    if best:
        return best

    # Checked before the bullets, not after: a summary that says "no other findings" and
    # then lists what it *did* check would otherwise count its own checklist.
    if _NO_FINDINGS_RE.search(body):
        return 0

    # The free-form fallback: bullets under a line that announces findings.
    bullets = 0
    in_findings = False
    for line in body.splitlines():
        if _FINDINGS_HEADING_RE.match(line):
            in_findings = True
            continue
        if not in_findings:
            continue
        if _BULLET_RE.match(line):
            bullets += 1
        elif line.strip() and not line.startswith((" ", "\t")):
            # An unindented prose line ends the list. Anything indented is a bullet's
            # continuation.
            in_findings = False
    return bullets


def _scrub(text: str, repo: Path) -> str:
    """Replace the clone's absolute path with `.`, and bound the result.

    `/code-review` prints absolute paths -- `/tmp/pb2/work/app.py` was observed -- and the
    container's `repoDir` must not reach a pull-request body: it is an implementation
    detail of the runtime that means nothing to a reader and tells them where this agent
    keeps its filesystem.
    """
    body = text or ""
    for absolute in (str(repo), str(repo.resolve())):
        if absolute and absolute != ".":
            body = body.replace(absolute, ".")
    return body[:_MAX_ADVISORY]


def _write_advisory(target: Path, text: str) -> None:
    """Write one advisory pass's output. Under `.agent/`, so it is not the work reviewed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


async def _advisory_pass(
    state: TaskState,
    *,
    command: str,
    target: Path,
    repo: Path,
    costs: list[dict[str, Any]],
    node: str = "review",
) -> tuple[str, int]:
    """Run one advisory slash command and record what it said. Never raises.

    `costs` is appended to rather than returned, so the caller's list ends up holding one
    `subagents.cost_row` per session **in the order the sessions ran** with no bookkeeping
    at either call site. A pass that raised before its session finished contributes no row,
    which is why this node returns *up to* three.

    Everything is inside the `try`, including the write: an advisory pass that could raise
    would hand a security finding a back-door route to the pass/fail signal, which is the
    thing invariant 6b exists to prevent. A failure here is a log line and an empty
    section.
    """
    try:
        result = await subagents.run_role("review", command, cwd=repo, state=state, node=node)
        costs.append(subagents.cost_row(result, node=node))
        body = _scrub(result.final_text or "", repo)
        if not body.strip():
            # NOT "no findings". An empty result is the CLI's shape for a command whose own
            # shell pre-command failed, and `num_turns` is worthless here -- `/code-review`
            # returned 0 turns while producing real findings. A non-empty result is the only
            # evidence the pass ran at all. `run_role` now catches the common case earlier
            # and raises, so this branch is belt-and-braces for a body `_scrub` emptied.
            log.warning("%s produced no output, so it is recorded as not having run", command)
            _write_advisory(target, _PRODUCED_NOTHING.format(command=command))
            return "", 0
        _write_advisory(target, body)
        return body, count_findings(body)
    except subagents.RoleError:
        # `run_role` now raises on a session that produced no text, which for an advisory
        # command is the fact this function already recorded: the pass did not run. It is
        # written to disk rather than swallowed, because an omitted file reads as
        # "nothing to say".
        log.warning("%s produced no output, so it is recorded as not having run", command)
        try:
            _write_advisory(target, _PRODUCED_NOTHING.format(command=command))
        except Exception:
            log.warning("could not record that %s did not run", command, exc_info=True)
        return "", 0
    except Exception:
        log.warning("the advisory %s pass failed; it cannot fail this node", command, exc_info=True)
        return "", 0


def _verdict(final_text: str) -> tuple[bool, str]:
    """`(vetoed, reason)` from the role's closing message.

    A missing `VERDICT:` line is a veto. That direction is deliberate and it can cost an
    iteration; the alternative is a non-draft pull request whose reviewer never actually
    spoke, which is the exact failure this design exists to prevent.
    """
    matches = _VERDICT_RE.findall(final_text or "")
    if not matches:
        log.error(
            "the review role's closing message carried no VERDICT: line; treating it as a "
            "veto, because an unjudged change must not become a non-draft pull request"
        )
        return True, "the review role returned no VERDICT: line, which is treated as a veto"
    verdict = matches[-1].upper()
    if verdict == "FAIL":
        return True, "the review role returned VERDICT: FAIL"
    return False, "the review role returned VERDICT: PASS"


async def review(state: TaskState) -> dict[str, Any]:
    """Run the review role, then compose the one `verifyResult` in the tree."""
    repo = Path(state["repoDir"])
    iteration = state.get("iteration", 0)
    target = review_path(repo, iteration)
    target.parent.mkdir(parents=True, exist_ok=True)

    cr = state.get("commandResult") or {
        "command": "",
        "exitCode": None,
        "output": "",
        "warning": None,
    }
    before = git_ops.source_diff_digest(repo)

    # The description is the query, and the command output tail deliberately is not: the
    # reviewer is judging *this* change, while the tail is a stack trace, which embeds to
    # noise in a semantic search.
    recall_query = state.get("description", "")
    prompt = REVIEW_PROMPT.format(
        repository=state.get("repository", ""),
        description=state.get("description", ""),
        spec_path=state.get("specPath", ""),
        verify_command=cr.get("command") or "(none was detected in this repository)",
        exit_code=cr.get("exitCode") if cr.get("exitCode") is not None else "(nothing ran)",
        output_tail=(cr.get("output") or "")[-_MAX_OUTPUT_TAIL:] or "(no output)",
        base_branch=state.get("baseBranch", ""),
        review_path=str(target.relative_to(repo)),
        # Per task, not per fleet: the sentence may only promise `mcp__memory__*` on a run
        # that really has a memory resource, or the role spends turns discovering a tool it
        # does not have.
        tool_paragraph=subagents.tool_paragraph(
            "review", memory=agentcore_memory.TaskMemory.for_state(state).enabled
        ),
        recalled_memory=agentcore_memory.recall(state, recall_query) or "(nothing recorded yet)",
    )
    result = await subagents.run_role("review", prompt, cwd=repo, state=state, node="review")
    log.info("review role finished in %d turns", result.num_turns)

    vetoed, reason = _verdict(result.final_text)

    after = git_ops.source_diff_digest(repo)
    if after != before:
        if not state.get("reviewRerun"):
            log.warning(
                "the review role edited the tree after it was last measured; re-running "
                "the verification command so the verdict is composed against the tree "
                "that will actually be pushed"
            )
            # Nothing else. `_after_review` checks this flag before it looks at
            # `verifyResult`, precisely because `verifyResult` here would still describe
            # the measurement the role has just invalidated.
            return {"reviewRerun": True}
        log.error(
            "the review role edited the tree twice; the verdict stands against the second "
            "run, which is the closest measurement there is"
        )

    vr = _compose(cr, vetoed=vetoed, reason=reason, final_text=result.final_text)

    # The two advisory passes, and they run HERE -- after `verifyResult` is composed, and
    # after the `reviewRerun` early return above. Nothing below this line can reach `vr`.
    #
    # A high-severity finding forcing a draft PR is deliberately DEFERRED: it needs
    # severity calibration nobody here has done, and a gate that fires on false positives
    # is switched off within a week. What is here instead is a count and a file, which a
    # human reads and acts on.
    security_target = security_review_path(repo, iteration)
    code_target = code_review_path(repo, iteration)

    # One row per session that actually ran, in the order they ran: the verdict first,
    # because it is the session `verifyResult` was composed from.
    costs = [subagents.cost_row(result, node="review")]

    if git_ops.has_origin_head(repo):
        _, security_findings = await _advisory_pass(
            state, command=SECURITY_COMMAND, target=security_target, repo=repo, costs=costs
        )
    else:
        # Skipped rather than dispatched into a silent empty success. `git_ops`' own
        # docstring carries the probe: without this ref the command aborts before any
        # model call and comes back looking like a clean pass.
        log.info(
            "%s has no refs/remotes/origin/HEAD, so %s was not dispatched",
            repo,
            SECURITY_COMMAND,
        )
        _write_advisory(security_target, _NO_ORIGIN_HEAD)
        security_findings = 0

    # Unconditional: `/code-review` needs no upstream ref and was observed finding a real
    # problem that lived only in the working tree.
    _, code_findings = await _advisory_pass(
        state, command=CODE_REVIEW_COMMAND, target=code_target, repo=repo, costs=costs
    )

    if git_ops.source_diff_digest(repo) != after:
        # Loud, and that is all. `reviewRerun` is deliberately NOT set: the hop budget is
        # one boolean spent once per iteration (invariant 6c), and an advisory pass -- which
        # cannot move `verified` -- must not be able to spend it.
        log.error(
            "an advisory pass (%s / %s) edited the working tree after the verdict was "
            "composed; the exit code on the state describes the tree as it was before, and "
            "the one re-measurement hop belongs to the review role",
            SECURITY_COMMAND,
            CODE_REVIEW_COMMAND,
        )
        warning = (
            "\n\n> WARNING: the working tree changed while the advisory passes ran, so the "
            "verification measurement predates the tree that will be pushed.\n"
        )
        for path in (security_target, code_target):
            with contextlib.suppress(OSError), path.open("a", encoding="utf-8") as handle:
                handle.write(warning)

    return {
        "verifyResult": vr,
        # Up to three rows -- the verdict session plus whichever advisory passes ran --
        # and `graph._instrument` pops the key, so none of this reaches `verifyResult` or
        # the checkpoint. This node is the reason `roleCosts` is a list rather than a row.
        "roleCosts": costs,
        "securityReviewPath": str(security_target),
        "codeReviewPath": str(code_target),
        "securityFindings": security_findings,
        "codeReviewFindings": code_findings,
        # Cleared on the same update that composes the verdict, so the hop is spent once
        # per iteration and a resume cannot hand it back.
        "reviewRerun": False,
        # `iteration` advances here and nowhere else in the graph.
        "iteration": iteration + 1,
        "reviewPath": str(target),
    }


def _compose(cr: CommandResult, *, vetoed: bool, reason: str, final_text: str) -> VerifyResult:
    """Turn a `CommandResult` plus a verdict into the task's `VerifyResult`.

    `cr` is a `CommandResult` and not a `dict[str, Any]`, which is not a widening but the
    only annotation that type-checks: `dict` is invariant in its value type, so a
    `TypedDict` is not assignable to `dict[str, Any]` at all. `nodes/verify.py` produces
    one and `review` reads it off `state["commandResult"]`, so that is what has always
    flowed through here. The reads stay `.get` regardless -- this composes the one verdict
    in the tree, and a `KeyError` here would lose it.
    """
    summary = (final_text or "").strip()[-_MAX_SUMMARY:]

    if not cr.get("command"):
        # Nothing was configured, so nothing ran. `passed: True` lets the loop stop --
        # there is nothing to re-plan against -- and it is safe *only* because `submit`,
        # the pull request body and `state.terminal_status` all key on `verified`, which
        # is False. The result is a DRAFT pull request and SUCCEEDED_UNVERIFIED.
        return {
            "passed": True,
            "verified": False,
            "warning": cr.get("warning") or "no verification command was detected",
            "command": "",
            "exitCode": None,
            "vetoed": vetoed,
            "summary": summary,
        }

    if cr.get("warning"):
        # A command was configured but something upstream of it failed -- setup, most
        # likely. Unverified, never failed-verification: re-planning a change that
        # nothing was wrong with wastes an iteration on a broken environment.
        return {
            "passed": True,
            "verified": False,
            "warning": cr["warning"],
            "command": cr.get("command", ""),
            "exitCode": cr.get("exitCode"),
            "vetoed": vetoed,
            "summary": summary,
        }

    exit_code = cr.get("exitCode")
    passed = exit_code == 0 and not vetoed
    # Both halves, in the order they were established, because either can refuse on its
    # own and the pull request body quotes this verbatim. Reporting only the verdict here
    # produced "Warning: the review role returned VERDICT: PASS" on a run the *exit code*
    # had failed, which reads as if the reviewer's approval were the problem.
    reasons = []
    if exit_code != 0:
        reasons.append(f"the verification command exited {exit_code}")
    if vetoed:
        reasons.append(reason)
    return {
        "passed": passed,
        # `verified` is `passed` here and only here: a real command ran, and it was
        # judged. There is no branch in which a verdict of PASS raises a non-zero exit
        # code to green -- the `and` above is the whole one-way veto.
        "verified": passed,
        "warning": None if passed else "; ".join(reasons),
        "command": cr.get("command", ""),
        "exitCode": cr.get("exitCode"),
        "vetoed": vetoed,
        "summary": summary,
    }
