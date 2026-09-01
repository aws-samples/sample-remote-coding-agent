"""The hooks every role session registers, and the prompt rule the first one retires.

A separate module from `subagents.py` for two reasons. `subagents.py` is already the
largest file in the package, and these are pure callbacks: `denies_commit` is a
function of a string, and the three observability callbacks do one bounded filesystem
copy and one activity row between them. Nothing here opens a session.

Hooks are configured **programmatically**, through `ClaudeAgentOptions(hooks=...)`, so
`setting_sources=[]` is untouched: there is no `.claude/settings.json` anywhere in this
design and invariant 10 stays exactly as it was. And hooks fire **inside subagents too**,
so the one registration in `build_options` also covers the fan-out implementer -- which
is the whole reason the commit denial can be described as mechanical rather than
advisory (invariant 10c).

`include_hook_events=True` is deliberately NOT set. The activity rows come from the
callbacks below; enabling the SDK's own hook-event messages would put the same event in
the feed twice, once from here and once from `activity.rows_from_message`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookContext, HookJSONOutput, HookMatcher

# `HookMatcher` is re-exported at the top level; neither `HookEvent` -- the key type of
# `ClaudeAgentOptions.hooks` -- nor `HookInput` -- the first parameter of the SDK's own
# `HookCallback` -- is, so both come from the types module by name.
from claude_agent_sdk.types import HookEvent, HookInput

from . import activity
from .state import TaskState

log = logging.getLogger(__name__)

# A shell command is split on these before matching, so `cd sub && git commit` is seen
# as two segments rather than as one string that happens not to start with `git`.
_SEGMENT_SPLIT = re.compile(r";|&&|\|\||\||\n")

# A GUARD, not a shell parser, and the difference is the point. It catches what a role
# actually types -- `git commit -m x`, `cd sub && git commit`, `git -C . commit`,
# `GIT_AUTHOR_NAME=x git commit` -- by allowing leading environment assignments and
# git's own global options before the subcommand. It does not catch
# `eval "$(printf 'git com'; printf mit)"`, and it is not trying to: a role that goes
# to that length has left the territory a hook can police, and the belt-and-braces
# there is that `submit` is the only publisher and `git_ops.diff` is read back.
_COMMIT_SEGMENT = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"git(?:\s+-(?:C\s+\S+|c\s+\S+|-git-dir[= ]\S+|-work-tree[= ]\S+))*"
    r"\s+commit\b"
)

# The reason a role reads when the hook fires. It states the CONSEQUENCE rather than
# the rule, because the rule on its own reads as bureaucracy and gets worked around:
# a role-side commit is a silent failure two nodes later, not a policy violation.
_DENY_REASON = (
    "Committing is denied to every role. A role-side commit empties git_ops.diff and "
    'makes changed_paths look clean, which the no-op guard then reads as "nothing was '
    'implemented" -- so a commit here turns finished work into a confusing failure. '
    "Leave your changes uncommitted in the working tree; the submit node commits and "
    "publishes them. The denied command was: "
)

# Past this bound the archive copies the HEAD of the transcript, not the tail. The
# whole point of archiving before a compaction is to keep the *early* reasoning, which
# is exactly what the SDK's summary is about to drop -- the tail is what survives
# compaction anyway.
_MAX_TRANSCRIPT_BYTES = 8_000_000


def denies_commit(command: str) -> str | None:
    """The offending segment when `command` runs `git commit`, else `None`. Pure.

    Split-then-match rather than one regex over the whole string, so a commit hidden
    behind a `&&` or a newline is found at the segment it lives in and the reason can
    quote it back.
    """
    for segment in _SEGMENT_SPLIT.split(command or ""):
        if _COMMIT_SEGMENT.match(segment):
            return segment.strip()
    return None


def build_hooks(state: TaskState | None, *, node: str) -> dict[HookEvent, list[HookMatcher]]:
    """The complete hook registration for one role session.

    A factory returning closures rather than four module-level callbacks: three of the
    four events need somewhere to put an activity row, and closing over `state` and
    `node` keeps `build_options` pure (building a closure is not I/O) while leaving
    every callback directly callable from a test with a hand-built input dict and no
    `claude` process anywhere.

    All four events are registered for all four roles. A role that spawns no subagent
    simply never fires two of them, and one registration is one place to drift.
    """

    def _record(rows: list[dict[str, Any]]) -> None:
        """Rows only when there is a task item to put them on, as `run_role` does."""
        if state is not None:
            activity.record(state, rows)

    # The SDK invokes every hook positionally, so the first parameter's name is ours to
    # choose: `hook_input` rather than the SDK's own `input`, which shadows the builtin.
    #
    # `HookInput` -- a union of the ten per-event TypedDicts -- is what the SDK's
    # `HookCallback` declares, and the parameter is contravariant, so a callback has to
    # accept ANY member of it: annotating one member such as `PreToolUseHookInput` would not
    # do. `dict[str, Any]` is not a supertype of a TypedDict either (`dict` is invariant in
    # its value type), which is why the old annotation made every `HookMatcher(hooks=[...])`
    # below a type error.
    async def _no_commit(
        hook_input: HookInput,
        _tool_use_id: str | None,
        _context: HookContext,
    ) -> HookJSONOutput:
        """Deny `git commit` on the `Bash` tool. Every role, and every subagent.

        This cannot break `submit`: `git_ops.commit` shells out through `subprocess`,
        not through the `Bash` **tool**, so the one legitimate committer never reaches
        a `PreToolUse` hook. `git push` is deliberately not denied here for the same
        reason -- the brief scopes the mechanism to `git commit`.

        Every read is a `.get`, never a `[...]`: the matcher is `"Bash"`, but a hook
        that raises is worse than a hook that misses, because a raising hook takes the
        role session with it.
        """
        # Read through a `Mapping` view, which every member of `HookInput` already is.
        # `tool_input` lives on the four tool-lifecycle members only, so `.get` against the
        # whole union types it `object` and the chained `.get` below would not type-check.
        # This is an annotation and not a copy: same object, same reads, and the defensive
        # `.get` style is preserved exactly.
        fields: Mapping[str, Any] = hook_input
        if fields.get("tool_name") != "Bash":
            return {}
        command = fields.get("tool_input", {}).get("command", "") or ""
        segment = denies_commit(command)
        if segment is None:
            return {}
        log.warning("denied a commit from the %s session: %s", node, segment)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _DENY_REASON + segment,
            }
        }

    async def _archive_transcript(
        hook_input: HookInput,
        _tool_use_id: str | None,
        _context: HookContext,
    ) -> HookJSONOutput:
        """Copy the transcript aside before the SDK summarises it away.

        The archive lands under `state["workDir"]` and **never** inside the clone:
        `submit` commits the tree, so a multi-megabyte transcript written under
        `.agent/` would land in the pull request.

        Returns `{}` always -- there is no decision to make on `PreCompact` -- and any
        failure is a `log.warning`. Losing an archive costs a reader some history;
        raising here would cost the run.
        """
        work_dir = (state or {}).get("workDir")
        if not work_dir:
            log.warning("no workDir on the state, so the %s transcript was not archived", node)
            return {}

        try:
            source = Path(hook_input.get("transcript_path") or "")
            target_dir = Path(work_dir) / "transcripts"
            target_dir.mkdir(parents=True, exist_ok=True)
            # `n` counts up from the files already there: one session can compact more
            # than once, and the second archive must not clobber the first.
            n = len(list(target_dir.glob(f"precompact-{node}-*.jsonl")))
            target = target_dir / f"precompact-{node}-{n}.jsonl"
            with source.open("rb") as handle:
                head = handle.read(_MAX_TRANSCRIPT_BYTES)
            target.write_bytes(head)
        except Exception:
            log.warning(
                "could not archive the %s transcript before compaction", node, exc_info=True
            )
            return {}

        _record(
            activity.compaction(
                node,
                trigger=str(hook_input.get("trigger") or "auto"),
                archived=str(target),
            )
        )
        return {}

    # `SubagentStart`/`SubagentStop` are the SUPPORTED progress signal for fan-out.
    # There are no `TaskStarted`/`TaskProgress` message classes to rely on, and reading
    # tool-use blocks instead would mean tracking the v2.1.63 `Task` -> `Agent` rename
    # in a second place (`activity.SUBAGENT_TOOL_NAMES` owns that).
    async def _subagent_started(
        hook_input: HookInput,
        _tool_use_id: str | None,
        _context: HookContext,
    ) -> HookJSONOutput:
        """One row when a subagent starts. `SubagentStartHookInput` carries the pair."""
        _record(
            activity.subagent_started(
                node,
                agent_id=str(hook_input.get("agent_id") or ""),
                agent_type=str(hook_input.get("agent_type") or ""),
            )
        )
        return {}

    async def _subagent_stopped(
        hook_input: HookInput,
        _tool_use_id: str | None,
        _context: HookContext,
    ) -> HookJSONOutput:
        """One row when a subagent stops.

        `SubagentStopHookInput` adds `stop_hook_active` and `agent_transcript_path` to
        the start event's pair; neither is on the row, because the feed is a sample of
        what happened and a per-subagent transcript path is not something the console
        can open.
        """
        _record(
            activity.subagent_stopped(
                node,
                agent_id=str(hook_input.get("agent_id") or ""),
                agent_type=str(hook_input.get("agent_type") or ""),
            )
        )
        return {}

    return {
        # The matcher is a tool name: `"Bash"` is the only tool that can reach a shell.
        "PreToolUse": [HookMatcher(matcher="Bash", hooks=[_no_commit])],
        "PreCompact": [HookMatcher(hooks=[_archive_transcript])],
        "SubagentStart": [HookMatcher(hooks=[_subagent_started])],
        "SubagentStop": [HookMatcher(hooks=[_subagent_stopped])],
    }
