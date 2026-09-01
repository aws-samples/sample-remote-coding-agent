"""The planning role, and the re-planning role that runs after a failed verification.

The planner writes `.agent/spec/iteration-<n>.md` and this node reads it back. A
per-iteration path, never one mutable file: a node reading a path it does not own cannot
tell "never written" from "last iteration's", and the whole re-plan loop depends on being
able to tell those apart.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .. import agentcore_memory, memory, subagents
from ..state import MAX_ITERATIONS, TaskState

log = logging.getLogger(__name__)

# A spec shorter than this is not a spec. The planning role has 200 turns; one that
# exhausts them, or that answers in its closing message instead of writing the file,
# leaves nothing for the coder to implement.
_MIN_SPEC_CHARS = 200


class PlanError(RuntimeError):
    """Raised when the planning role produced no usable specification."""


_SECTIONS = """## Goal
## Approach
## Changes
## Verification
## Risks"""


PLAN_PROMPT = """You are the planning role for an automated coding task on the repository
`{repository}`. You do not implement anything: you write the specification another role
will implement in a single pass, with no chance to ask you a question.

The task, as the human wrote it:

{description}

Ground truth about this working tree:

- The base branch is `{base_branch}`.
- {verify_sentence}

Read enough of the repository to be specific. Then write the specification to
`{spec_path}` with exactly these sections, in this order:

{sections}

`## Changes` is the section the implementer works from, so enumerate the units of work
and say which of them are independent of each other and which must land together, naming
the files each unit owns. `## Verification` names the commands that prove the change, and
you must have run them yourself against the unchanged tree first, so the specification
records whether the baseline was already red. `## Risks` names what could go wrong and
what you deliberately chose not to do.

Then append anything durable you learned about this repository -- a build step with a
hidden prerequisite, a test that needs a fixture, an approach you ruled out and why -- to
`.agent/memory/plan.md`. Append; never rewrite. An earlier entry is evidence, and losing
it costs the next iteration the same discovery twice.

{tool_paragraph}

What this repository has learned across every task it has ever run -- its own memory of
itself, not this task's notes:

{recalled_memory}

Notes carried over from earlier iterations of this task:

{memory_digest}
"""


REPLAN_PROMPT = """You are the re-planning role for an automated coding task on the
repository `{repository}`. A previous attempt was implemented and then failed
verification. Your job is to work out **why** and to write the specification for the next
attempt. That is harder than planning from scratch: the obvious change has already been
tried.

The task, as the human wrote it:

{description}

This is attempt {attempt} of {max_attempts}.

What happened last time:

- The verification command was `{verify_command}` and it reported: {verify_summary}
- The previous specification is on disk at `{previous_spec_path}`.
- The reviewer's analysis of the previous attempt is on disk at `{previous_review_path}`.
  Read it. It is the closest thing you have to a second opinion.

The working tree still carries the previous attempt's uncommitted changes. Decide
deliberately whether to build on them or to reverse them, and say which in the
specification -- an implementer who is told to undo something will undo it properly
rather than layering a fix on top.

Write the new specification to `{spec_path}` with exactly these sections, in this order:

{sections}

Then append what you learned about the failure to `.agent/memory/plan.md`. Append; never
rewrite.

{tool_paragraph}

What this repository has learned across every task it has ever run -- its own memory of
itself, not this task's notes:

{recalled_memory}

Notes carried over from earlier iterations of this task:

{memory_digest}
"""

# A backticked token that could be a repository-relative path: at least one `/` or a
# recognisable file suffix, and no shell metacharacters or spaces.
_BACKTICKED = re.compile(r"`([A-Za-z0-9_./-]+)`")
_LOOKS_LIKE_A_FILE = re.compile(r"(/|\.[A-Za-z0-9]{1,6}$)")


def spec_path(repo: Path, iteration: int) -> Path:
    """`.agent/spec/iteration-<n>.md`, the one path this node both writes and reads."""
    return repo / ".agent" / "spec" / f"iteration-{iteration}.md"


def _named_paths(repo: Path, spec_text: str) -> list[str]:
    """The repository-relative paths the specification names, from its backticked tokens.

    Used only for the *warning* side of `code`'s no-op guard, so a false positive costs a
    log line and a false negative costs nothing. That is why the test is deliberately
    loose: a token that already exists in the tree, or that looks like a file and lands
    under the repository, counts.
    """
    found: list[str] = []
    for token in _BACKTICKED.findall(spec_text):
        if token.startswith("/") or ".." in token:
            continue  # not repository-relative; and never follow a token out of the tree
        if not _LOOKS_LIKE_A_FILE.search(token):
            continue
        candidate = repo / token
        if candidate.exists() or candidate.parent.is_dir():
            found.append(token)
    return sorted(set(found))


async def plan(state: TaskState) -> dict[str, Any]:
    """Run the planning (or re-planning) role and read its specification back."""
    repo = Path(state["repoDir"])
    iteration = state.get("iteration", 0)
    role = "replan" if iteration > 0 else "plan"
    target = spec_path(repo, iteration)
    target.parent.mkdir(parents=True, exist_ok=True)

    verify_command = state.get("verifyCommand") or ""
    verify_sentence = (
        f"Verification for this repository is `{verify_command}`. The specification's "
        "`## Verification` section must use it."
        if verify_command
        else "No verification command was detected in this repository, so nothing will "
        "mechanically check the change. Say so in `## Risks`."
    )
    # Hoisted above `common` because the re-plan summary feeds two things now -- the prompt
    # and the recall query -- and one owner is what keeps them saying the same thing. Inert
    # for `plan`, which never reads either.
    vr = state.get("verifyResult") or {}
    verify_summary = vr.get("warning") or vr.get("summary") or "no summary was recorded"
    # The two roles ask memory different questions. `plan` asks about the task; `replan` asks
    # about the *failure*, because a re-plan that recalled "what do we know about this task"
    # would get back exactly the records the attempt that just failed already had.
    recall_query = (
        state.get("description", "")
        if role == "plan"
        else f"{state.get('description', '')}\n{verify_summary}"
    )
    has_memory = agentcore_memory.TaskMemory.for_state(state).enabled
    common = {
        "repository": state.get("repository", ""),
        "description": state.get("description", ""),
        "base_branch": state.get("baseBranch", ""),
        "spec_path": str(target.relative_to(repo)),
        "sections": _SECTIONS,
        # Per role, not per fleet: `plan` and `replan` reach the AWS documentation server
        # and `review` does not, so the sentence has to be generated for the role that
        # actually runs. Per *task* as well, now: the sentence may only promise
        # `mcp__memory__*` on a run that really has a memory resource, or a role spends
        # turns discovering a tool it does not have.
        "tool_paragraph": subagents.tool_paragraph(role, memory=has_memory),
        "recalled_memory": agentcore_memory.recall(state, recall_query) or "(nothing recorded yet)",
        "memory_digest": memory.read_digest(repo) or "(none yet)",
        "verify_sentence": verify_sentence,
    }

    if role == "plan":
        prompt = PLAN_PROMPT.format(**common)
    else:
        prompt = REPLAN_PROMPT.format(
            **common,
            attempt=iteration + 1,
            max_attempts=MAX_ITERATIONS,
            verify_command=vr.get("command") or verify_command or "(none)",
            verify_summary=verify_summary,
            previous_spec_path=str(spec_path(repo, iteration - 1).relative_to(repo)),
            # Named, never read. The reviewer's analysis is the reviewer's file; a node
            # that read it could not tell "never written" from "last iteration's".
            previous_review_path=f".agent/review/iteration-{iteration - 1}.md",
        )

    result = await subagents.run_role(role, prompt, cwd=repo, state=state, node="plan")
    log.info("%s role finished in %d turns", role, result.num_turns)

    # This has to fail loudly. The re-plan loop only handles *verification* failures, so
    # there is no recovery path from a planner that came back with nothing -- and a role
    # that exhausted its 200 turns comes back with narration, not a specification.
    if not target.exists():
        raise PlanError(
            f"the {role} role wrote no specification at {target.relative_to(repo)}; "
            "there is nothing for the implementation role to implement"
        )
    spec_text = target.read_text(encoding="utf-8", errors="replace").strip()
    if len(spec_text) < _MIN_SPEC_CHARS:
        raise PlanError(
            f"the {role} role wrote only {len(spec_text)} characters to "
            f"{target.relative_to(repo)}; that is not a specification"
        )

    return {
        "specPath": str(target),
        "specText": spec_text,
        "planChangedPaths": _named_paths(repo, spec_text),
        # Tagged with the NODE and not the role, because the console shows cost per node
        # and this one node runs `plan` or `replan` depending on the iteration.
        # `graph._instrument` pops this key, so it never reaches the state.
        "roleCosts": [subagents.cost_row(result, node="plan")],
    }
