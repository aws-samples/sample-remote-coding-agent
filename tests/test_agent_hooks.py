"""The hooks, driven directly: no `claude` process, no Bedrock call, no AWS.

Every test here calls a callback returned by `hooks.build_hooks` with a hand-built input
dict, which is why the factory returns closures in the first place. A test in this file
that needed a live session would take the whole offline property down with it -- and the
property matters most for this module, because the commit denial is the mechanism that
retired a prompt-only rule (invariant 10c) and "it is registered" is not the same claim
as "it fires".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from coding_agent import activity, config, hooks, subagents


def _cfg(**overrides: Any) -> config.AgentConfig:
    """The fleet configuration with every field pinned, so no test reads the host's."""
    fields: dict[str, Any] = {
        "model": "sonnet",
        "roles": config.ROLES,
        "work_root": Path("/mnt/workspace/work"),
        "venv_root": Path("/tmp/venv"),
        "skill_paths": (Path("/app/skills"),),
        "fallback_model": "sonnet",
        "goal_enabled": True,
    }
    return config.AgentConfig(**{**fields, **overrides})


def _callback(event: str, *, state: Any = None, node: str = "code") -> Any:
    """The single callback registered for `event`, ready to await."""
    registered = hooks.build_hooks(state, node=node)
    matchers = registered[event]
    assert len(matchers) == 1, event
    assert len(matchers[0].hooks) == 1, event
    return matchers[0].hooks[0]


def _bash(command: str) -> dict[str, Any]:
    """A `PreToolUse` input for a `Bash` call, shaped as the SDK delivers it."""
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "session-1",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/mnt/workspace/work/task/repo",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "tu-1",
    }


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git commit --amend",
        "cd sub && git commit",
        "git -C . commit",
        " git   commit ",
        "GIT_AUTHOR_NAME=role git commit -m x",
        "make test; git commit -am wip",
    ],
)
async def test_a_git_commit_is_denied_with_a_reason_naming_the_consequence(command: str) -> None:
    """Invariant 10c: the rule is mechanical now, and it says what goes wrong."""
    no_commit = _callback("PreToolUse")

    output = await no_commit(_bash(command), "tu-1", {"signal": None})

    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    # The reason must explain the CONSEQUENCE, not just refuse: a role-side commit
    # empties `git_ops.diff` and makes `changed_paths` look clean, which the no-op guard
    # reads as "nothing was implemented".
    reason = specific["permissionDecisionReason"]
    assert "no-op guard" in reason
    assert "changed_paths" in reason
    assert "uncommitted" in reason
    # And it quotes the segment back, so a compound command says which half was refused.
    assert "commit" in reason


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff",
        "git diff --stat origin/main",
        "make test",
        "python -m pytest -q",
        'echo "git commit"',
        "git commitizen",
        "grep -rn 'git commit' .",
        "",
    ],
)
async def test_an_innocent_bash_command_is_not_denied(command: str) -> None:
    """A guard that denied `git status` would be worked around within one session."""
    no_commit = _callback("PreToolUse")

    assert await no_commit(_bash(command), "tu-1", {"signal": None}) == {}


async def test_a_non_bash_tool_use_is_read_defensively() -> None:
    """The matcher is `"Bash"`, but a hook that raises is worse than one that misses."""
    no_commit = _callback("PreToolUse")

    # No `tool_input` at all, and a tool that is not `Bash`: both are `{}`, neither raises.
    assert await no_commit({"tool_name": "Read"}, None, {"signal": None}) == {}
    assert await no_commit({"tool_name": "Bash"}, None, {"signal": None}) == {}
    assert await no_commit({}, None, {"signal": None}) == {}


def test_the_hook_is_registered_for_every_role_and_matches_bash(tmp_path: Path) -> None:
    """One registration, all four roles, all four events -- including the subagent pair."""
    cfg = _cfg()

    for role in config.ROLES:
        options = subagents.build_options(role, cwd=tmp_path, cfg=cfg)

        assert options.hooks is not None, role
        assert set(options.hooks) == {
            "PreToolUse",
            "PreCompact",
            "SubagentStart",
            "SubagentStop",
        }, role
        # A tool-name matcher: `Bash` is the only tool that can reach a shell.
        assert [matcher.matcher for matcher in options.hooks["PreToolUse"]] == ["Bash"], role
        # The other three are not tool events, so they carry no matcher.
        for event in ("PreCompact", "SubagentStart", "SubagentStop"):
            assert options.hooks[event][0].matcher is None, (role, event)
        # The SDK's own hook-event messages stay off: the rows come from the callbacks,
        # and enabling them would put the same event in the feed twice.
        assert options.include_hook_events is not True, role


async def test_a_precompact_archive_failure_cannot_fail_the_session(tmp_path: Path) -> None:
    """No `workDir`, and an unreadable transcript: `{}` both times, and nothing raises."""
    archive = _callback("PreCompact", state={})
    output = await archive(
        {
            "hook_event_name": "PreCompact",
            "transcript_path": str(tmp_path / "t.jsonl"),
            "trigger": "auto",
        },
        None,
        {"signal": None},
    )
    assert output == {}

    # A `workDir` this time, but the transcript does not exist.
    archive = _callback("PreCompact", state={"workDir": str(tmp_path / "work")})
    output = await archive(
        {"hook_event_name": "PreCompact", "transcript_path": str(tmp_path / "missing.jsonl")},
        None,
        {"signal": None},
    )
    assert output == {}

    # And a state of `None` at all -- `local_run` passes one.
    archive = _callback("PreCompact", state=None)
    assert await archive({"hook_event_name": "PreCompact"}, None, {"signal": None}) == {}


async def test_the_precompact_archive_lands_outside_the_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`submit` commits the tree, so a transcript under `.agent/` would land in the PR."""
    work_dir = tmp_path / "work"
    clone = work_dir / "repo"
    (clone / ".agent").mkdir(parents=True)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"early":"reasoning"}\n', encoding="utf-8")

    rows: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(activity, "record", lambda _state, batch: rows.append(batch))

    state = {"taskId": "t-1", "configId": "c-1", "workDir": str(work_dir), "repoDir": str(clone)}
    archive = _callback("PreCompact", state=state, node="code")

    output = await archive(
        {
            "hook_event_name": "PreCompact",
            "transcript_path": str(transcript),
            "trigger": "auto",
            "cwd": str(clone),
        },
        None,
        {"signal": None},
    )

    assert output == {}
    archived = work_dir / "transcripts" / "precompact-code-0.jsonl"
    assert archived.read_text(encoding="utf-8") == '{"early":"reasoning"}\n'
    # Nothing anywhere under the clone: that is the whole point of the `workDir` rule.
    assert list(clone.rglob("*.jsonl")) == []

    # A second compaction in the same session must not clobber the first.
    await archive(
        {"hook_event_name": "PreCompact", "transcript_path": str(transcript), "trigger": "manual"},
        None,
        {"signal": None},
    )
    assert (work_dir / "transcripts" / "precompact-code-1.jsonl").exists()

    assert [row["kind"] for batch in rows for row in batch] == ["compact", "compact"]
    assert rows[0][0]["trigger"] == "auto"
    assert rows[0][0]["archived"].endswith("precompact-code-0.jsonl")
    assert rows[1][0]["trigger"] == "manual"


async def test_the_precompact_archive_keeps_the_head_not_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the bound the HEAD is kept: the early reasoning is what compaction drops."""
    monkeypatch.setattr(hooks, "_MAX_TRANSCRIPT_BYTES", 8)
    monkeypatch.setattr(activity, "record", lambda _state, _rows: None)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("HEADHEADTAILTAIL", encoding="utf-8")

    archive = _callback("PreCompact", state={"workDir": str(tmp_path / "work")}, node="plan")
    await archive(
        {"hook_event_name": "PreCompact", "transcript_path": str(transcript)},
        None,
        {"signal": None},
    )

    archived = tmp_path / "work" / "transcripts" / "precompact-plan-0.jsonl"
    assert archived.read_text(encoding="utf-8") == "HEADHEAD"


async def test_the_subagent_hooks_produce_one_row_each(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SubagentStart`/`SubagentStop` are the supported fan-out progress signal."""
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(activity, "record", lambda _state, batch: rows.extend(batch))
    state = {"taskId": "t-1", "configId": "c-1"}

    started = _callback("SubagentStart", state=state, node="code")
    stopped = _callback("SubagentStop", state=state, node="code")

    assert (
        await started(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "agent-1",
                "agent_type": "implementer",
            },
            None,
            {"signal": None},
        )
        == {}
    )
    assert (
        await stopped(
            {
                "hook_event_name": "SubagentStop",
                "agent_id": "agent-1",
                "agent_type": "implementer",
                "stop_hook_active": False,
                "agent_transcript_path": "/tmp/agent-1.jsonl",
            },
            None,
            {"signal": None},
        )
        == {}
    )

    assert len(rows) == 2
    assert [row["phase"] for row in rows] == ["start", "stop"]
    for row in rows:
        assert row["kind"] == "subagent"
        assert row["node"] == "code"
        assert row["agentId"] == "agent-1"
        assert row["agentType"] == "implementer"
        assert row["at"]


async def test_a_subagent_row_with_no_state_is_dropped_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`local_run` passes no state, and observability may never fail a role."""
    calls: list[Any] = []
    monkeypatch.setattr(activity, "record", lambda _state, batch: calls.append(batch))

    started = _callback("SubagentStart", state=None)
    assert (
        await started({"agent_id": "a", "agent_type": "implementer"}, None, {"signal": None}) == {}
    )
    assert calls == []


def test_the_commit_guard_is_a_guard_and_says_so() -> None:
    """`denies_commit` is pure, returns the segment, and does not pretend to be a shell."""
    assert hooks.denies_commit("cd sub && git commit -m x") == "git commit -m x"
    assert hooks.denies_commit("git status") is None
    # Documented non-goal, asserted so nobody reads the guard as airtight.
    assert hooks.denies_commit("eval \"$(printf 'git com'; printf mit)\"") is None
