"""The graph's state, the two result shapes, and the task status vocabulary.

Nothing here imports langgraph or the Claude SDK: `state.py` is the one module every
other module in the package may import, so it stays free of heavy dependencies.
"""

from __future__ import annotations

from typing import Literal, TypedDict

# The re-plan budget. Three attempts is the whole loop: plan -> code -> verify ->
# review, up to three times, then submit whatever the last attempt produced.
MAX_ITERATIONS = 3

# Adding a status is FOUR edits across THREE deployment units (invariant 13), and
# three of them are outside this package:
#   1. this literal,
#   2. `graph._NODE_STATUS`, if the status belongs to a node,
#   3. # FOLLOW-UP (phases 5/6): `watchdog.RESUMABLE_STATUSES` and
#      `cards._STEP_COLUMN` in `api/` -- another card owns that directory this wave,
#   4. # FOLLOW-UP (phases 5/6): `web/js/status.js`, which a later phase parses line
#      by line, so its formatting is load-bearing.
# `review`/`REVIEWING` is the worked example: one node, four edits. There is no
# `RESEARCHING` and no `AWAITING_ANSWERS` -- research mode and the question loop are
# cut from this rewrite.
TaskStatus = Literal[
    "QUEUED",
    "DISPATCHED",
    "CHECKING_OUT",
    "PLANNING",
    "CODING",
    "VERIFYING",
    "REVIEWING",
    "SUBMITTING",
    "SUCCEEDED",
    "SUCCEEDED_UNVERIFIED",
    "FAILED_VERIFICATION",
    "FAILED",
    "CANCELLED",
]

# What the watchdog must never resume and the console renders as finished.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"SUCCEEDED", "SUCCEEDED_UNVERIFIED", "FAILED_VERIFICATION", "FAILED", "CANCELLED"}
)


class CommandResult(TypedDict):
    """What `nodes/verify.py` produces, and the whole of what it produces.

    `exitCode is None` means *nothing ran* -- no command was configured, or setup
    failed before the verification command was reached. That is deliberately
    distinguishable from `exitCode == 0`: only `review` may turn either into a
    verdict, and it needs to tell "green" from "unmeasured".
    """

    command: str
    exitCode: int | None
    output: str
    warning: str | None


class VerifyResult(TypedDict, total=False):
    """The three-way verification signal, composed in exactly one place.

    `nodes/review.py` is the only writer. The three fields are read by three
    different consumers and must not be conflated:

    | field      | meaning                                    | who may read it |
    |------------|--------------------------------------------|-----------------|
    | `passed`   | the loop has nothing more to do            | **only** `graph._after_review` |
    | `verified` | a real command ran and was judged green    | `submit` (`draft=`), the PR body, `terminal_status` |
    | `warning`  | nothing ran, or nothing judged it          | humans |

    The rule for anything added later: a consumer asking "is this green?" reads
    `bool(vr.get("verified"))`, so an absent field degrades to *unverified* rather
    than to a confident non-draft PR.
    """

    passed: bool
    verified: bool
    warning: str | None
    command: str
    exitCode: int | None
    vetoed: bool
    summary: str


class TaskState(TypedDict, total=False):
    """The graph state, checkpointed after every node.

    camelCase because these keys are the DynamoDB item's attribute names, and one
    spelling across the wire, the table and the graph is worth more than PEP 8 here
    (ruff's `N815` does not fire inside a `TypedDict` body).

    `cloneSource` is the one field that must never reach a subagent (invariant 11):
    on a local run it is a filesystem path, and a role handed it followed it out of
    its `cwd`. Roles get `repository` -- `owner/repo` -- and nothing else.
    """

    taskId: str
    configId: str
    repository: str
    cloneSource: str
    secretArn: str
    # The repository's AgentCore memory resource, resolved exactly once in
    # `graph.run_task` off the configuration item and read by nobody else. Absent on a
    # local run and on a repository with no memory resource, which is the signal
    # `agentcore_memory.TaskMemory.for_state` reads as "disabled".
    memoryId: str
    description: str
    baseBranch: str
    branch: str
    workDir: str
    repoDir: str
    verifyCommand: str
    setupCommand: str
    iteration: int
    reviewRerun: bool
    resumed: bool
    specPath: str
    specText: str
    planChangedPaths: list[str]
    codeChangedPaths: list[str]
    reviewPath: str
    # The two advisory passes' output, and their heuristic finding counts. Four fields and
    # NO new `TaskStatus`, so invariant 13's four-edit tax across three deployment units is
    # not triggered and there is no `api/` or `web/` half of this to go looking for: a
    # finding is something a human reads in the pull request body, never a state the graph
    # branches on. They cannot reach `verified` (invariant 6b).
    securityReviewPath: str
    codeReviewPath: str
    securityFindings: int
    codeReviewFindings: int
    commandResult: CommandResult
    verifyResult: VerifyResult
    prUrl: str
    status: TaskStatus
    currentNode: str
    noSubmit: bool


def terminal_status(vr: VerifyResult | None) -> TaskStatus:
    """The task's final status, derived from the verification result.

    One function so the PR body, the store write and `local_run`'s printout cannot
    disagree about what a run ended as. Note which field decides: `verified`, never
    `passed` -- a run with no command configured has `passed: True` and is still
    only `SUCCEEDED_UNVERIFIED`.
    """
    vr = vr or {}
    if vr.get("verified"):
        return "SUCCEEDED"
    if not vr.get("passed", False):
        return "FAILED_VERIFICATION"
    return "SUCCEEDED_UNVERIFIED"
