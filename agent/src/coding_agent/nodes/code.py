"""The coding role: one single pass over the whole spec, then the no-op guard.

There is no per-subtask commit loop in this rewrite. The role gets the spec the planner
wrote, 200 turns, and effort `high` (invariant 8 -- do not lower it), and then the guard
below decides whether anything actually happened.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .. import agentcore_memory, config, git_ops, memory, subagents
from ..state import TaskState

log = logging.getLogger(__name__)


class CodeError(RuntimeError):
    """Raised when the coding session produced no change outside `.agent/`."""


CODE_PROMPT = """You are the implementation role for an automated coding task on the
repository `{repository}`. Implement the specification below, in this working tree.

The task, as the human wrote it:

{description}

The specification to implement (also on disk at `{spec_path}`):

---
{spec_text}
---

What is expected of you:

- Implement what the specification describes, and nothing more. No unrequested
  refactors, no abstractions for hypothetical future needs.
- Write code that reads like the code already here: match its naming, its idioms, its
  error handling and its comment density. Read `CLAUDE.md` and `CONTRIBUTING.md` if they
  exist before you write anything.
- {verify_sentence}
- Do not commit, do not push, and do not touch a git remote. A later step commits your
  work; a commit of your own empties the diff that step reads.
- Nothing you write under `.agent/` counts as implementing the task. That directory is
  for notes and specifications, and a change confined to it is treated as a run that did
  nothing.
- If the specification is wrong about something you discover while implementing, follow
  its intent and say so in your closing message.
- Append anything durable you learn -- a test that needs a fixture, a build step with a
  hidden prerequisite, an approach you ruled out and why -- to
  `.agent/memory/code.md`. Append; never rewrite. An earlier entry is evidence.

{fanout_guidance}

{tool_paragraph}

What this repository has learned across every task it has ever run -- its own memory of
itself, not this task's notes:

{recalled_memory}

Notes carried over from earlier iterations of this task:

{memory_digest}
"""


# Fan-out is OFFERED, never required, and the wording matters: a prompt that *demanded*
# parallel subagents on a one-unit specification would buy an extra session and a
# reconciliation problem for nothing. `subagents.implementer_definition` is the other half
# of this -- the definition exists only for the coding role, and it is bounded by
# `max_budget_usd`, spawn depth 1 and five concurrent subagents.
#
# The one-file-per-subagent rule is not stylistic. `memory.append` and the `Write` tool are
# both read-modify-write of a whole file, so two subagents appending to `code.md` lose an
# entry with no error at all. `memory.read_digest`'s `*.md` glob reads every
# `code-<unit>.md` back with no change needed.
FANOUT_GUIDANCE = """About the `implementer` subagent, which is offered to you and is
never mandatory:

- Where the specification's `## Changes` section marks two or more units of work as
  **independent of each other**, you may dispatch them in parallel to the `implementer`
  subagent -- one call per unit, in a single message, so they run at the same time. Tell
  each one the part of the specification it owns and the exact files it may touch.
- Never dispatch two subagents that touch the same file, and do not edit a file yourself
  while a subagent is working in it. These tools write whole files, so the loser of that
  race loses everything, silently.
- A specification with one unit of work, or one that marks nothing as independent, you
  implement directly. One subagent for one unit buys nothing.
- Each subagent appends its findings to its OWN `.agent/memory/code-<unit>.md`, never to
  `.agent/memory/code.md`: appending is a read-modify-write of a whole file, so two
  writers on one file lose an entry with no error. The digest reads every one of them back.
- You do the integration yourself -- reconciling the units, and the closing statement --
  because no subagent saw the whole change."""


# The standing objective for the coding role and no other role, with the condition scoped
# hard. The scoping IS the design, so it is written out here rather than left to be
# inferred.
#
# The condition is drawn from the SPECIFICATION and must NEVER be the verification verdict.
# "every unit in `## Changes` is implemented" composes with our loop: it is a statement
# about this working tree that the role itself can act on. "the tests pass" does not -- a
# verdict-shaped condition would put a second writer on the pass/fail signal (invariant 6b)
# and invite a nine-minute suite to be re-run an unbounded number of times inside one
# session, with nothing for the watchdog to see because the session is still alive and
# still making calls. `verify` runs the command, `review` composes the verdict, and neither
# of them is a goal.
#
# Coding role only: `plan` and `review` each produce one artefact and have nothing to
# iterate toward, so a goal there would be a condition met the moment the file is written.
#
# It must NOT be sent as a `/goal <condition>` first line of the session input. The CLI
# treats an input that *begins* with a slash command as a COMMAND INVOCATION, so everything
# after it becomes that command's argument: the entire coding prompt -- specification text,
# verify sentence, tool paragraph, recalled memory and memory digest -- is consumed, the
# session makes no model turn, and it still reports `subtype: "success"` with no cost row.
# The visible symptom is `CodeError: the coding role changed nothing outside .agent/`, which
# points a reader at the model rather than at the prompt's first line.
#
# So the condition now rides in the session's system prompt, which cannot consume the
# input. Yielding `/goal` as its own user message from an async-iterable `prompt=` was the
# other candidate and is rejected: `claude_agent_sdk._internal.query`'s
# `wait_for_result_and_end_input` hold is one-shot and releases at the FIRST turn boundary,
# and `build_options` registers hooks for every role, so a two-message iterable would close
# stdin at the `/goal` turn's result frame and every later hook control request -- including
# the `PreToolUse`/`Bash` commit denial, invariant 10c -- would find it closed. Trading a
# destroyed prompt for a disabled commit guard is not a fix. `cfg.goal_enabled` remains the
# switch over the whole mechanism and still defaults on.
GOAL_CONDITION = (
    "every unit of work in the specification's ## Changes section is implemented in this "
    "working tree"
)
GOAL_SYSTEM_PROMPT = (
    "You have one standing objective for this session, and it is not met until "
    f"{GOAL_CONDITION}. Re-read it before you decide you are finished: a session that "
    "stops with part of the specification's ## Changes section unimplemented has not "
    "done the job it was given."
)


async def code(state: TaskState, cfg: config.AgentConfig | None = None) -> dict[str, Any]:
    """Run the coding role once, then check that it changed something that matters.

    `cfg` is optional and resolved here when it is not given, matching
    `subagents.run_role`'s own pattern: the graph calls this node with the state alone,
    and a test hands one in rather than editing the environment.
    """
    cfg = cfg or config.load_config()
    repo = Path(state["repoDir"])
    verify_command = state.get("verifyCommand") or ""
    verify_sentence = (
        f"Run `{verify_command}` before you finish and leave it passing."
        if verify_command
        else "This repository declares no verification command, so there is nothing to run; "
        "be correspondingly careful."
    )

    # The specification is what this role is about to implement, so it is the better half of
    # the query -- and it is far longer than a semantic search wants. `recall` caps it, so the
    # cap has one owner rather than a slice at every call site.
    recall_query = f"{state.get('description', '')}\n{state.get('specText', '')}"
    prompt = CODE_PROMPT.format(
        repository=state.get("repository", ""),
        description=state.get("description", ""),
        spec_path=state.get("specPath", ""),
        spec_text=state.get("specText", ""),
        verify_sentence=verify_sentence,
        fanout_guidance=FANOUT_GUIDANCE,
        # Per task, not per fleet: the sentence may only promise `mcp__memory__*` on a run
        # that really has a memory resource, or the role spends turns discovering a tool it
        # does not have.
        tool_paragraph=subagents.tool_paragraph(
            "code", memory=agentcore_memory.TaskMemory.for_state(state).enabled
        ),
        recalled_memory=agentcore_memory.recall(state, recall_query) or "(nothing recorded yet)",
        memory_digest=memory.read_digest(repo) or "(none yet)",
    )
    result = await subagents.run_role(
        "code",
        prompt,
        cwd=repo,
        cfg=cfg,
        state=state,
        node="code",
        # `None` when the switch is off, which is the SDK's own default for the field.
        system_prompt=GOAL_SYSTEM_PROMPT if cfg.goal_enabled else None,
    )
    log.info("coding role finished in %d turns", result.num_turns)

    # The two-way no-op guard (invariant 5). `git_ops.has_changes()` is deliberately NOT
    # what is checked: the plan node wrote `.agent/spec/iteration-<n>.md` before this
    # role ran, so `has_changes()` is unconditionally true by now. Using it as the "did
    # the coder do anything?" test lets a session that reported success having changed
    # nothing pass an untouched baseline suite and open a NON-DRAFT pull request
    # containing only a specification -- green, confident and empty.
    changed = git_ops.changed_paths(repo)
    source = [p for p in changed if not p.startswith(".agent/")]
    if not source:
        raise CodeError(
            "the coding role changed nothing outside .agent/: "
            f"{len(changed)} path(s) changed, all of them notes or specification. "
            "A run that implemented nothing must fail loudly rather than open a pull "
            "request containing only a specification (invariant 5)"
        )

    # The other direction is a warning and never a failure. A spec cannot enumerate
    # every file a correct change touches -- a new import, a fixture, a generated
    # manifest -- so extra paths are information, not a violation.
    planned = state.get("planChangedPaths") or ()
    extra = sorted(set(source) - set(planned))
    if planned and extra:
        log.warning(
            "the coding role changed %d path(s) the specification did not name: %s",
            len(extra),
            ", ".join(extra[:20]),
        )

    # `graph._instrument` pops `roleCosts`, so it is observability and never state. It is
    # the coding node's figure that matters most of the three: `model_usage` includes the
    # fan-out subagents' spend and `usage` does not, and this is the only role that
    # dispatches any.
    return {
        "codeChangedPaths": source,
        "roleCosts": [subagents.cost_row(result, node="code")],
    }
