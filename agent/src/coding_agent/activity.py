"""The activity feed: what the console shows while a task is still running.

Best-effort, bounded, and it drops rows rather than raising. The feed is an
observability aid -- it exists so a human watching a 40-minute task can see which
tool the coding role is on -- and nothing about it may ever be able to fail a task.
That shapes the whole module: `rows_from_message` is pure and cannot touch the
network, every string is trimmed before it leaves here, and the single impure
function delegates to `store.append_activity`, which already swallows its own boto3
errors.

Rows are plain dicts because they are written straight into a DynamoDB list; keeping
them JSON-shaped here means the store layer needs no serialisation step of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from . import store
from .state import TaskState

# A `detail` is one cell in a console table, not a log line: 400 characters is about
# what fits before the reader stops reading, and an untrimmed `Write` input would
# otherwise put a whole file into the task item.
_MAX_DETAIL = 400

# One assistant message can carry a dozen parallel tool calls. The feed is a sample of
# what happened, not a transcript, so a burst is capped rather than paged.
_MAX_ROWS_PER_MESSAGE = 8

# The input field to show, in order of how well it identifies the call: a `Bash` row is
# useless without its `command` and a `Read` row without its `file_path`, and those are
# the two most common rows by a wide margin. `prompt`/`description`/`subagent_type`
# cover `Task`, and the fall-through to any remaining string means a tool nobody
# anticipated still renders as something rather than as a blank cell.
_DETAIL_FIELDS = (
    "command",
    "file_path",
    "pattern",
    "path",
    "prompt",
    "description",
    "subagent_type",
)

# BOTH names, and the reason is a rename mid-2.1.x: `Task` became `Agent` in Claude Code
# v2.1.63. Current releases emit `"Agent"` in tool-use blocks while the session's init
# tool list still says `"Task"` -- probe-verified against CLI 2.1.236, the build bundled
# with `claude-agent-sdk` 0.2.141. So any code reading the message stream checks both,
# and a stream that switches back mid-flight still stamps its rows.
#
# It lives here rather than in `subagents.py` because `subagents` imports `activity` and
# the arrow points one way; putting it there would make the cycle.
SUBAGENT_TOOL_NAMES: tuple[str, ...] = ("Agent", "Task")


def utcnow_iso() -> str:
    """The one owner of the feed's timestamp format.

    Every row's `at` comes from here, so the console never has to parse two shapes --
    seconds resolution and an explicit UTC offset.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def _trim(value: str) -> str:
    """Collapse to one line and bound the length.

    Whitespace is collapsed because a row is rendered in a single-line cell: a heredoc
    passed to `Bash` would otherwise blow the table apart for no extra information.
    """
    text = " ".join(value.split())
    if len(text) <= _MAX_DETAIL:
        return text
    return text[: _MAX_DETAIL - 1] + "…"


def _detail(tool_input: dict[str, Any]) -> str:
    """The first useful input field of a tool call, trimmed."""
    for field in _DETAIL_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return _trim(value)
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            return _trim(value)
    return ""


def rows_from_message(message: object, *, node: str) -> list[dict[str, Any]]:
    """Fold one SDK message into feed rows. Pure -- no I/O, no state, no exceptions.

    Being pure is what lets `subagents._collect` call this on every message of a live
    stream and lets a test assert the shape with no CLI process anywhere.

    Note what it keys on: *any* named tool block becomes a row, because the name comes
    off `block.name` rather than off a whitelist. So `Skill`, an MCP tool (`mcp__*`)
    and `TodoWrite` all render correctly the day a later phase turns them on, with no
    edit here -- a whitelist would have silently dropped exactly the new surface
    somebody wanted to watch.
    """
    rows: list[dict[str, Any]] = []
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if len(rows) >= _MAX_ROWS_PER_MESSAGE:
                break
            if isinstance(block, ToolUseBlock):
                row: dict[str, Any] = {
                    "at": utcnow_iso(),
                    "node": node,
                    "kind": "tool",
                    "tool": block.name,
                    "detail": _detail(block.input or {}),
                }
                if block.name in SUBAGENT_TOOL_NAMES:
                    # The one branch that keeps `SUBAGENT_TOOL_NAMES` load-bearing
                    # rather than decorative: a fan-out dispatch is the row a reader
                    # watching the coding node most wants to pick out of the feed.
                    row["fanout"] = True
                rows.append(row)
            elif isinstance(block, TextBlock):
                rows.append(
                    {
                        "at": utcnow_iso(),
                        "node": node,
                        "kind": "text",
                        "detail": _trim(block.text),
                    }
                )
    elif isinstance(message, ResultMessage):
        rows.append(
            {
                "at": utcnow_iso(),
                "node": node,
                "kind": "result",
                "num_turns": message.num_turns,
                "is_error": message.is_error,
            }
        )
    return rows


def node_started(state: TaskState, node: str) -> list[dict[str, Any]]:
    """One row marking a node's entry.

    The iteration rides along because the feed is read top to bottom: without it, the
    second `plan` row is indistinguishable from the first and the reader cannot tell a
    re-plan from a restart.
    """
    return [
        {
            "at": utcnow_iso(),
            "node": node,
            "kind": "node",
            "phase": "start",
            "iteration": int(state.get("iteration", 0) or 0),
        }
    ]


def node_finished(state: TaskState, node: str, seconds: float) -> list[dict[str, Any]]:
    """One row marking a node's exit, carrying how long it took."""
    return [
        {
            "at": utcnow_iso(),
            "node": node,
            "kind": "node",
            "phase": "finish",
            "iteration": int(state.get("iteration", 0) or 0),
            "seconds": round(seconds, 1),
        }
    ]


def subagent_started(node: str, *, agent_id: str, agent_type: str) -> list[dict[str, Any]]:
    """One row marking a subagent's start, from the `SubagentStart` hook.

    The hook is the supported progress signal for fan-out: there are no
    `TaskStarted`/`TaskProgress` message classes, and the tool-use block only says that
    a dispatch happened, not that the subagent got as far as running.
    """
    return [
        {
            "at": utcnow_iso(),
            "node": node,
            "kind": "subagent",
            "phase": "start",
            "agentId": agent_id,
            "agentType": agent_type,
        }
    ]


def subagent_stopped(node: str, *, agent_id: str, agent_type: str) -> list[dict[str, Any]]:
    """One row marking a subagent's exit, from the `SubagentStop` hook.

    Deliberately the same shape as `subagent_started` bar `phase`, so the console pairs
    them by `agentId` without a second renderer.
    """
    return [
        {
            "at": utcnow_iso(),
            "node": node,
            "kind": "subagent",
            "phase": "stop",
            "agentId": agent_id,
            "agentType": agent_type,
        }
    ]


def compaction(node: str, *, trigger: str, archived: str) -> list[dict[str, Any]]:
    """One row marking a compaction, carrying where the transcript was archived.

    `archived` is a path under `workDir` and never inside the clone, which is the
    `hooks._archive_transcript` rule surfaced where a reader of the feed can check it.
    """
    return [
        {
            "at": utcnow_iso(),
            "node": node,
            "kind": "compact",
            "trigger": trigger,
            "archived": _trim(archived),
        }
    ]


def node_cost(node: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row for what a node's role sessions cost, from `subagents.cost_row` rows.

    One row per *node*, not one per session: the feed is a sample a human reads top to
    bottom and `review` alone runs three sessions, so a row each would push the narration
    it exists to show off the bottom. The full per-session rows go to the task item's own
    `costs` list through `store.record_cost`.

    The figure is the CLI's client-side estimate and not an invoice -- `subagents.cost_row`
    carries that caveat where the number is computed.
    """
    return [
        {
            "at": utcnow_iso(),
            "node": node,
            "kind": "cost",
            "sessions": len(rows),
            "totalCostUsd": sum(float(row.get("totalCostUsd") or 0.0) for row in rows),
            "numTurns": sum(int(row.get("numTurns") or 0) for row in rows),
        }
    ]


def record(state: TaskState, rows: list[dict[str, Any]]) -> None:
    """Write rows to the task item. The only impure function here.

    Called through the module object (`store.append_activity`, never a directly
    imported name) so one monkeypatched attribute substitutes the whole write -- the
    convention `nodes/__init__.py` explains. No try/except: `store.append_activity` is
    already best-effort, and a second one here would only hide which layer dropped it.
    """
    if not rows:
        return
    store.append_activity(state, rows)
