"""The two pure seams of `subagents`: the option surface, and the fold to a `RoleResult`.

Everything here is offline by construction. `build_options` opens no session and reaches
no network, and `_collect` takes any async iterable, so the security-relevant fields and
the transcript-versus-verdict rule are both asserted over hand-built dataclasses -- no
`claude` process, no Bedrock call, no AWS. A test in this file that needed a live session
would take that whole property down with it, so `run_role` is only ever reached with
`subagents.query` monkeypatched.

`build_options` touches nothing at all -- no filesystem, no environment read -- so every
option surface below is decided by the arguments the test passes in.

The config these tests pass in is built by `_cfg`, which reads no environment at all.
That is deliberate: this container has `AGENT_SKILL_PATHS` and `ANTHROPIC_MODEL` preset,
and a `load_config()` here would silently assert the host's values instead of the
fleet's.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, Message, ResultMessage, TextBlock, ToolUseBlock
from coding_agent import config, subagents
from coding_agent.nodes import code as code_module
from coding_agent.state import TaskState


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


def _memory_state() -> TaskState:
    """One task whose repository has a memory resource, so `build_server` returns a server.

    A *function* and not a module constant, the same way `_cfg` is: a state is a mutable
    dict, and one test handing the next one a state a previous assertion edited is a failure
    that reads as a bug in the wiring.
    """
    return {
        "taskId": "task-1",
        "configId": "cfg-1",
        "repository": "acme/widget",
        "memoryId": "mem-abc",
    }


async def _stream(*messages: Message) -> AsyncIterator[Message]:
    """The offline stand-in for an SDK message stream: any async iterable will do."""
    for message in messages:
        yield message


def _says(text: str) -> AssistantMessage:
    """One assistant message carrying `text`. `model` is a required field on 0.2.x."""
    return AssistantMessage([TextBlock(text)], "opus")


def _result(
    *,
    is_error: bool = False,
    result: str | None = None,
    num_turns: int = 3,
    total_cost_usd: float | None = 0.42,
    subtype: str = "success",
    terminal_reason: str | None = None,
) -> ResultMessage:
    """A terminator with the six required positionals filled in.

    The accounting fields are keywords because a CLI command that aborts before any model
    call reports its own combination of them -- `subtype: "success"`, `num_turns: 0`,
    `total_cost_usd: 0` -- and that shape is what `run_role`'s emptiness gate is about.
    The defaults are the ordinary session, so no existing caller changes.
    """
    return ResultMessage(
        subtype=subtype,
        duration_ms=1200,
        duration_api_ms=900,
        is_error=is_error,
        num_turns=num_turns,
        session_id="session-1",
        total_cost_usd=total_cost_usd,
        result=result,
        terminal_reason=terminal_reason,
    )


def test_the_option_surface_is_locked_down(tmp_path: Path) -> None:
    """Invariants 9 and 10: every role gets acceptEdits, no setting source, strict MCP."""
    clone = tmp_path / "clone"
    cfg = _cfg()

    for role in config.ROLES:
        options = subagents.build_options(role, cwd=clone, cfg=cfg)

        # Invariant 9. `"plan"` writes to a host-side plan file and returns only
        # narration, so the real spec never reaches the coding role: the session looks
        # busy and the tree is untouched.
        assert options.permission_mode == "acceptEdits", role
        # Invariant 10, both halves. The clone is untrusted input, so a checked-in
        # `.claude/` must not be able to widen this session's permissions, and no MCP
        # server may arrive from the clone, from user settings or from a plugin root.
        assert options.setting_sources == [], role
        assert options.strict_mcp_config is True, role
        # Invariant 11: the caller resolves the clone and `build_options` derives no
        # path of its own, so what goes in is exactly what comes out.
        assert options.cwd == str(clone), role
        assert Path(options.cwd) == clone, role


def test_no_role_can_reach_the_network(tmp_path: Path) -> None:
    """Invariant 33, over every surface a role's toolset reaches.

    `WebFetch` is the one remaining VENDOR-BOUND call in this design: before fetching a
    URL the CLI sends the HOSTNAME to the vendor for a safety check, and that check is
    explicitly NOT suppressed by `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`. Removing the
    tool is simpler than a settings key -- there is no settings file, and
    `setting_sources=[]` means there would be nowhere to put one -- and it closes an
    exfiltration path at the same time. `WebSearch` needs no action because it does not
    exist on Bedrock; it is asserted anyway so the two fail the same way.

    The change this test exists to stop is "let the agent read the docs": one line in
    `ALLOWED_TOOLS` undoes the whole no-network claim, and every surface below is a place
    it could reappear -- the shared tuple, the per-role list, the session options, the
    generated tool paragraph, and the fan-out implementer, which is a session too.
    """
    cfg = _cfg()

    for tool in ("WebFetch", "WebSearch"):
        assert tool not in subagents.ALLOWED_TOOLS, tool
        assert tool in subagents.DENIED_TOOLS, tool

    for role in config.ROLES:
        options = subagents.build_options(role, cwd=tmp_path, cfg=cfg)
        paragraph = subagents.tool_paragraph(role)
        for tool in ("WebFetch", "WebSearch"):
            # Two mechanisms because they fail differently: an absent tool is a tool the
            # model is never told about, a denied tool is one the CLI refuses.
            assert tool not in subagents.role_tools(role), (role, tool)
            assert tool not in options.allowed_tools, (role, tool)
            assert tool in options.disallowed_tools, (role, tool)
            # A role told it may `WebFetch` spends turns discovering that it may not.
            assert tool not in paragraph, (role, tool)

        # The only server any role is ever given is the read-only documentation lookup,
        # so no role holds a network-backed tool other than that one. Its pinning and its
        # absent `env` are `test_the_documentation_server_is_pinned_and_read_only`'s.
        assert options.mcp_servers in ({"aws-docs": subagents.AWS_DOCS_SERVER}, {}), role

    # And again with a task that has a memory resource, because that is the one input that
    # adds a server and a tool to every role at once. The memory server is IN-PROCESS and
    # reaches exactly two `bedrock-agentcore` operations -- `CreateEvent` and
    # `RetrieveMemoryRecords` -- on a memory, an actor, a session and a namespace the model
    # cannot name, so it is not a network reach a role can point anywhere. What must stay
    # true is that it did not smuggle a general-purpose fetch in alongside itself, and that
    # the set of servers any role can hold is still closed at two.
    for role in config.ROLES:
        options = subagents.build_options(role, cwd=tmp_path, cfg=cfg, state=_memory_state())
        paragraph = subagents.tool_paragraph(role, memory=True)
        for tool in ("WebFetch", "WebSearch"):
            assert tool not in subagents.role_tools(role, memory=True), (role, tool)
            assert tool not in options.allowed_tools, (role, tool)
            assert tool in options.disallowed_tools, (role, tool)
            assert tool not in paragraph, (role, tool)

        assert set(options.mcp_servers) <= {"aws-docs", subagents.MEMORY_SERVER_NAME}, (
            "the servers a role may hold are closed at two -- the read-only documentation "
            f"lookup and the in-process memory server; {role} holds "
            f"{sorted(options.mcp_servers)}"
        )

    # The fan-out implementer inherits nothing it is not given, so it states both halves
    # for itself: a subagent that could fetch a URL would reach the vendor on behalf of a
    # role that cannot.
    definition = subagents.implementer_definition(config.ROLES["code"], cfg)
    for tool in ("WebFetch", "WebSearch"):
        assert tool not in (definition.tools or []), tool
        assert tool in (definition.disallowedTools or []), tool


@pytest.mark.parametrize("role", sorted(config.ROLES))
def test_the_tool_paragraph_cannot_promise_a_tool_the_role_lacks(role: str, tmp_path: Path) -> None:
    """The prompt's tool sentence and the role's real toolset name the same tools.

    The roles no longer share one list -- `plan`, `replan` and `code` reach the AWS
    documentation server and `review` does not -- so this is parametrised over every role:
    a per-role difference is exactly the case a hand-written sentence would have got wrong.
    """
    paragraph = subagents.tool_paragraph(role)

    # The sentence is generated from the tuple for exactly this reason, so parse it back
    # out: "Your tools are A, B and C." -> {"A", "B", "C"}.
    listing = paragraph.split(".")[0].removeprefix("Your tools are")
    named = {
        name.strip()
        for chunk in listing.split(",")
        for name in chunk.split(" and ")
        if name.strip()
    }

    assert named == set(subagents.ALLOWED_TOOLS), role
    # A role told it may `WebFetch` spends turns discovering that it may not.
    for tool in subagents.DENIED_TOOLS:
        assert tool not in paragraph, (role, tool)

    # The MCP wildcard is not a plain tool name, so it gets a sentence of its own -- and
    # only for a role that really has the server.
    assert ("mcp__aws-docs__" in paragraph) is (role in subagents.MCP_ROLES), role

    # `role_tools` is the single owner: the session's real toolset and the sentence the
    # prompt makes about it read the same function.
    options = subagents.build_options(role, cwd=tmp_path, cfg=_cfg())
    assert set(subagents.role_tools(role)) == set(options.allowed_tools), role


def test_only_the_planning_and_coding_roles_reach_the_documentation_server(tmp_path: Path) -> None:
    """`review` judges a diff it can read, so it holds no network-backed tool at all."""
    cfg = _cfg()

    review = subagents.build_options("review", cwd=tmp_path, cfg=cfg)
    assert review.mcp_servers == {}, (
        "the SDK's own default, and the statement that the reviewing role has no server: "
        "`verify` makes no model call and `review` judges a diff that is already on disk"
    )
    assert subagents.AWS_DOCS_TOOLS not in review.allowed_tools

    # `replan` is in the set deliberately: it is the same planning job on a harder input,
    # and a re-planner that could not look something up would be the one role guessing.
    for role in ("plan", "replan", "code"):
        options = subagents.build_options(role, cwd=tmp_path, cfg=cfg)
        assert options.mcp_servers == {"aws-docs": subagents.AWS_DOCS_SERVER}, role
        assert subagents.AWS_DOCS_TOOLS in options.allowed_tools, role
        # Invariant 10: the server arrives programmatically and nothing else may add one.
        assert options.strict_mcp_config is True, role


def test_the_documentation_server_is_pinned_and_read_only() -> None:
    """Invariants 34 and 10b in one test: an exact version, and no way in for a credential."""
    args = subagents.AWS_DOCS_SERVER["args"]
    assert subagents.AWS_DOCS_SERVER["command"] == "uvx"
    assert len(args) == 1, f"one requirement, so there is one thing to read; got {args}"

    requirement = args[0]
    assert re.match(r"^awslabs\.aws-documentation-mcp-server==\d+\.\d+\.\d+$", requirement), (
        "invariant 34: an exact `==` installed at build time. A runtime `@latest` is a "
        f"moving reference executing in the container; got {requirement!r}"
    )
    for moving in ("@latest", "@main", ">=", "~=", "*"):
        assert moving not in requirement, moving

    # Invariant 10b: documentation tools only. No `env`, so no credential can be handed
    # in -- anything that could call an AWS API would run under the container's execution
    # role and hand a prompt-injected agent an AWS client.
    assert set(subagents.AWS_DOCS_SERVER) == {"command", "args"}, (
        f"an `env` here is a route for a credential; got {sorted(subagents.AWS_DOCS_SERVER)}"
    )


def test_every_role_holds_the_memory_server_when_the_task_has_one(tmp_path: Path) -> None:
    """The memory server is gated on the *state* and not on the role: all four get it.

    `review` is in the set deliberately, and it is the assertion worth reading twice: a
    review that cannot record what it found is the role with the most to say and no way to
    say it. `verify` is absent from `config.ROLES` because it makes no model call at all.
    """
    for role in config.ROLES:
        options = subagents.build_options(role, cwd=tmp_path, cfg=_cfg(), state=_memory_state())

        server = options.mcp_servers[subagents.MEMORY_SERVER_NAME]
        # A `McpSdkServerConfig` TypedDict and not a dataclass, so these are subscripts:
        # `server.type` would raise.
        assert server["type"] == "sdk", (
            f"in-process, never a stdio subprocess: a subprocess could hold neither the "
            f"task's state nor this container's credentials; got {server['type']!r}"
        )
        assert server["name"] == subagents.MEMORY_SERVER_NAME, role
        assert subagents.MEMORY_TOOLS in options.allowed_tools, role

        # The single-owner property, restated for the memory case: `role_tools` decides the
        # session's toolset and the prompt's sentence about it, so a memory wildcard bolted
        # onto `allowed_tools` alone would be a second place for the boundary to drift.
        assert set(subagents.role_tools(role, memory=True)) == set(options.allowed_tools), role


def test_a_task_with_no_memory_gets_no_memory_server_and_no_memory_tools(tmp_path: Path) -> None:
    """A memoryless run gets no server rather than two tools that fail on every call.

    Both shapes a real run produces: `state=None`, which `build_options` is called with
    everywhere the node has nothing to pass, and a state for a repository that has no memory
    resource -- which is also `python -m coding_agent.local_run`, whose state carries no
    `memoryId` by construction because it never goes through `graph.run_task`.
    """
    without_memory: TaskState = {"taskId": "task-1", "configId": "cfg-1", "repository": "a/b"}

    for role in config.ROLES:
        for state in (None, without_memory):
            options = subagents.build_options(role, cwd=tmp_path, cfg=_cfg(), state=state)

            assert subagents.MEMORY_SERVER_NAME not in options.mcp_servers, (role, state)
            assert subagents.MEMORY_TOOLS not in options.allowed_tools, (
                f"{role} would be told it may call `{subagents.MEMORY_TOOLS}` and would "
                "spend its turns discovering that every call fails; absence is the signal"
            )


def test_the_memory_server_does_not_displace_the_documentation_server(tmp_path: Path) -> None:
    """Both servers, for the roles that have both: `mcp_servers` is merged, never assigned.

    This is what a naive second `mcp_servers=` -- or an assignment of
    `{MEMORY_SERVER_NAME: server}` over the dict -- would break, and it would break it
    silently: `plan`, `replan` and `code` would simply stop being able to look an AWS API up.
    """
    cfg = _cfg()

    for role in ("plan", "replan", "code"):
        options = subagents.build_options(role, cwd=tmp_path, cfg=cfg, state=_memory_state())
        assert set(options.mcp_servers) == {"aws-docs", subagents.MEMORY_SERVER_NAME}, role
        assert options.mcp_servers["aws-docs"] == subagents.AWS_DOCS_SERVER, role
        assert subagents.AWS_DOCS_TOOLS in options.allowed_tools, role
        assert subagents.MEMORY_TOOLS in options.allowed_tools, role

    # `review` is the other half of the same statement: it gains the memory server and gains
    # no network-backed tool with it.
    review = subagents.build_options("review", cwd=tmp_path, cfg=cfg, state=_memory_state())
    assert set(review.mcp_servers) == {subagents.MEMORY_SERVER_NAME}
    assert subagents.AWS_DOCS_TOOLS not in review.allowed_tools


def test_build_options_stays_pure_with_a_memory_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`build_options` builds a memory server and still reaches no AWS.

    The load-bearing one. `build_options` is documented as pure -- no session, no network, no
    filesystem -- and every other test in this file depends on that: a server whose
    construction reached AWS would take the whole offline test surface down with it, and in
    production it would put an STS call on the critical path of every role session.

    It holds because `TaskMemory` builds its boto3 client inside `record`/`search`, per call,
    so replacing `_client` with something that raises is a tripwire for a client built
    anywhere on this path.
    """

    def exploding_client(_self: Any) -> Any:
        raise AssertionError(
            "`build_options` built a `bedrock-agentcore` client; it is documented as pure"
        )

    monkeypatch.setattr(subagents.memory_tools.TaskMemory, "_client", exploding_client)

    for role in config.ROLES:
        options = subagents.build_options(role, cwd=tmp_path, cfg=_cfg(), state=_memory_state())
        # The server really was built -- otherwise this test would pass on a `build_options`
        # that had stopped calling `build_server` at all.
        assert subagents.MEMORY_SERVER_NAME in options.mcp_servers, role


@pytest.mark.parametrize("role", sorted(config.ROLES))
def test_the_tool_paragraph_cannot_promise_a_memory_tool_the_role_lacks(role: str) -> None:
    """The memory sentence appears exactly when the task has a memory, for every role.

    Same reasoning as the `mcp__aws-docs__` assertion above: the wildcard is not a plain
    tool name, so it gets a sentence of its own -- and a role told it may search a memory
    that does not exist spends turns discovering that every call fails.
    """
    assert "mcp__memory__" in subagents.tool_paragraph(role, memory=True), role
    assert "mcp__memory__" not in subagents.tool_paragraph(role, memory=False), role


def test_the_fanout_implementer_inherits_the_memory_tools() -> None:
    """The implementer runs inside the coding session's process, so the server is reachable.

    `role_tools` being the single owner is what makes that fall out rather than need wiring:
    the definition's `tools` is that tuple minus `"Skill"`. The two-argument call is the one
    `nodes/code.py` makes today and it must keep behaving exactly as it did.
    """
    cfg = _cfg()

    with_memory = subagents.implementer_definition(config.ROLES["code"], cfg, memory=True)
    assert subagents.MEMORY_TOOLS in (with_memory.tools or [])

    without_memory = subagents.implementer_definition(config.ROLES["code"], cfg)
    assert subagents.MEMORY_TOOLS not in (without_memory.tools or []), (
        "the default is `False`, so a task with no memory dispatches an implementer that is "
        "not told about a tool it cannot use"
    )


@pytest.mark.parametrize("absent", ["TodoWrite", "Agent"])
def test_no_tool_list_names_a_tool_the_session_does_not_expose(absent: str) -> None:
    """Probe (c): neither name is in the session's init tool list even when allowlisted.

    Run with `--tools "...,Agent,TodoWrite"`, CLI 2.1.236 reported exactly
    `['Bash','Edit','Glob','Grep','Read','Skill','Task','Write']`. Unknown names in
    `--tools` are silently tolerated, so a name here fails as an absent tool and never
    loudly -- and `tool_paragraph` is generated from these tuples, so it would be a prompt
    promising a tool the role does not have.
    """
    assert absent not in subagents.ALLOWED_TOOLS
    for role in config.ROLES:
        assert absent not in subagents.role_tools(role), role


def test_only_the_coding_role_can_fan_out(tmp_path: Path) -> None:
    """`agents=`, the budget and the two blast-radius variables are the coder's alone."""
    cfg = _cfg()

    coding = subagents.build_options("code", cwd=tmp_path, cfg=cfg)
    assert coding.agents is not None
    assert set(coding.agents) == {"implementer"}
    # The real runaway cap, with a defined failure: `subtype == "error_max_budget_usd"`.
    assert coding.max_budget_usd == 5.0
    # A pacing hint that rides an Anthropic-API beta header, so it may be silently inert
    # on Bedrock. Asserted as a value that was set, never as a guarantee that it bounds
    # anything.
    assert coding.task_budget == {"total": 2_000_000}
    for name, value in subagents.FANOUT_ENV.items():
        assert coding.env[name] == value, name

    for role in ("plan", "replan", "review"):
        options = subagents.build_options(role, cwd=tmp_path, cfg=cfg)
        assert options.agents is None, role
        assert options.max_budget_usd is None, role
        assert options.task_budget is None, role
        # `CLAUDE_CODE_ENABLE_TELEMETRY: "0"` is a literal now rather than a probe's answer:
        # the collector it used to depend on is gone, so nothing about the image can change it.
        assert options.env == {
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
        }, role


def test_the_implementer_repeats_the_denied_tools_as_its_own() -> None:
    """A subagent that could push would bypass `submit`, which is why that list exists."""
    definition = subagents.implementer_definition(config.ROLES["code"], _cfg())

    for tool in subagents.DENIED_TOOLS:
        assert tool in (definition.disallowedTools or []), tool
        assert tool not in (definition.tools or []), tool

    # One unit of work, not the whole specification: 80 turns against the coding role's 200.
    assert definition.maxTurns == 80
    # Inherited from the coding role, so fan-out is not an unannounced second routing
    # table (invariant 8).
    assert definition.model == config.ROLES["code"].model
    assert definition.effort == config.ROLES["code"].effort
    assert definition.permissionMode == "acceptEdits"
    # `skills="all"` is valid at the top level and not inside an `AgentDefinition`, so the
    # two embedded skills are named -- and `"Skill"` is out of `tools`, which the SDK
    # deprecates in favour of `skills=`.
    assert definition.skills == list(config.EMBEDDED_SKILLS)
    assert "Skill" not in (definition.tools or [])
    # The implementer reaches the documentation server for the same reason `code` does.
    assert subagents.AWS_DOCS_TOOLS in (definition.tools or [])
    assert definition.prompt == subagents.IMPLEMENTER_PROMPT
    # Its own memory file, never the shared one: appending is a read-modify-write of a
    # whole file, so two subagents on `code.md` lose an entry with no error.
    assert ".agent/memory/code-<unit>.md" in definition.prompt
    assert "do not push" in definition.prompt


def test_the_auto_memory_switch_survives_the_fanout_env_merge(tmp_path: Path) -> None:
    """Invariant 10's third mechanism is a literal in the dict, so a merge cannot drop it."""
    coding = subagents.build_options("code", cwd=tmp_path, cfg=_cfg())

    assert coding.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1", (
        "auto memory is the one input that loads regardless of `setting_sources`, and the "
        "fan-out variables are merged in *after* it for exactly this reason"
    )
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in subagents.FANOUT_ENV


def test_no_role_gets_an_otlp_exporter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The session environment turns the CLI's OTLP exporter off and configures none.

    This replaces a test that asserted the opposite direction. There used to be an
    in-container ADOT collector that SigV4-signed the CLI's metrics to CloudWatch for Coding
    Agent Insights, and `build_options` splatted a `telemetry.exporter_env` group in here
    carrying `OTEL_EXPORTER_OTLP_ENDPOINT` and four siblings. That feature is gone --
    `CLAUDE.md` records why -- and what remains is the one variable that turns OTLP on,
    pinned off.

    Asserted over every role, and as an absence rather than a value:
    `CLAUDE_CODE_ENABLE_TELEMETRY=1` with no receiver makes every session retry against a
    closed port, and an `OTEL_EXPORTER_OTLP_ENDPOINT` that named a real host would be an
    egress destination invariant 33 has not sanctioned -- neither of which fails loudly.
    """
    cfg = _cfg()
    state: TaskState = {"taskId": "task-1", "repository": "owner/repo"}
    for role in ("plan", "replan", "code", "review"):
        options = subagents.build_options(role, cwd=tmp_path, cfg=cfg, state=state)
        assert options.env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "0", (
            f"{role} must carry the exporter switch explicitly off: it is the only variable "
            f"that turns the CLI's OTLP export on, and this deployment runs no receiver"
        )
        exporter = sorted(name for name in options.env if name.startswith("OTEL_"))
        assert exporter == [], (
            f"{role} carries {exporter}, which configures an OTLP exporter. Reintroducing one "
            f"means choosing an endpoint, a receiver and an egress destination first -- see "
            f"`CLAUDE.md` on why Coding Agent Insights was removed rather than left wired up"
        )
    # `code` is the fan-out role, and the implementer inherits its parent's environment rather
    # than getting one of its own, so a fan-out session cannot export what its parent may not.
    assert not any(name.startswith("OTEL_") for name in subagents.FANOUT_ENV), (
        "FANOUT_ENV is merged in after the literals above, so an OTEL_ name there would reach "
        "the coding role and its implementer while every other role stayed clean"
    )


def test_the_fanout_prompt_offers_rather_than_requires() -> None:
    """Fan-out is offered, never mandatory, and each subagent owns its own memory file."""
    guidance = code_module.FANOUT_GUIDANCE

    assert "never mandatory" in guidance
    assert "implement directly" in guidance, (
        "a one-unit specification is implemented directly: one subagent for one unit buys "
        "nothing, and a prompt that demanded parallelism would pay for it anyway"
    )
    assert "independent of each other" in guidance

    assert ".agent/memory/code-<unit>.md" in guidance
    # The bare shared file must not be offered to a subagent: two writers on it lose an
    # entry with no error.
    assert "`.agent/memory/code.md`" in guidance and "never to" in guidance
    assert "lose an entry" in guidance


def test_the_skills_reach_the_session_as_a_plugin_root(tmp_path: Path) -> None:
    """A skill arrives as a local plugin root and never as a setting source."""
    roots = (Path("/app/skills"), tmp_path / "extra-skills")
    options = subagents.build_options("code", cwd=tmp_path, cfg=_cfg(skill_paths=roots))

    assert options.plugins == [{"type": "local", "path": str(path)} for path in roots]
    # Every role gets every skill in every root: the frontmatter decides when one fires,
    # so there is no per-role wiring here to get wrong.
    assert options.skills == "all"
    # Invariant 10 has switched the setting sources off, and a skill root is not a
    # permission -- so `plugins` has to be the only channel.
    assert options.setting_sources == []


def test_the_model_and_turns_come_from_the_routing_table(tmp_path: Path) -> None:
    """The per-role model, turn budget and effort are the routing table's, verbatim."""
    cfg = _cfg()

    planning = subagents.build_options("plan", cwd=tmp_path, cfg=cfg)
    assert planning.model == "opus"
    assert planning.max_turns == 200
    assert planning.effort == "high"

    # `review` is the one component whose failure is silent -- a missed veto is a
    # confident green PR containing untested code -- so it keeps Opus at 40 turns.
    review = subagents.build_options("review", cwd=tmp_path, cfg=cfg)
    assert review.model == "opus"
    assert review.max_turns == 40
    assert review.effort == "low"

    # Invariant 8: at `medium` a coding role once reported the task complete with ZERO
    # source edits, against 214 insertions at `high` on the identical task.
    coding = subagents.build_options("code", cwd=tmp_path, cfg=cfg)
    assert coding.model == "sonnet"
    assert coding.effort == "high"

    inherited = _cfg(
        model="global.anthropic.claude-opus-4-5-20251101-v1:0",
        roles={**config.ROLES, "code": config.RoleConfig(model=None, effort="high", turns=200)},
    )
    options = subagents.build_options("code", cwd=tmp_path, cfg=inherited)
    assert options.model == "global.anthropic.claude-opus-4-5-20251101-v1:0"


@pytest.mark.parametrize("role", sorted(config.ROLES))
def test_every_role_carries_the_fleet_fallback_model(role: str, tmp_path: Path) -> None:
    """With per-role models, an unavailable model must degrade the run rather than kill it.

    One fleet value and not a per-role one: a per-role fallback is a second routing table.
    Both values are Bedrock ones, so the worst a misconfiguration here can do is run a
    role on the wrong model, never send a request to a vendor endpoint.
    """
    cfg = _cfg(fallback_model="global.anthropic.claude-sonnet-4-5-20250929-v1:0")
    options = subagents.build_options(role, cwd=tmp_path, cfg=cfg)

    assert options.fallback_model == "global.anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_an_unknown_role_raises(tmp_path: Path) -> None:
    """A role this fleet does not run fails loudly, naming the four that it does."""
    with pytest.raises(KeyError) as excinfo:
        subagents.build_options("reviewer", cwd=tmp_path, cfg=_cfg())

    message = str(excinfo.value)
    assert "reviewer" in message
    for role in ("plan", "replan", "code", "review"):
        assert role in message, role


async def test_the_verdict_is_the_last_text_message_not_the_transcript() -> None:
    """Invariant 7: `final_text` is the closing statement, `text` is the whole run."""
    narration = "I'll start by exploring the repository"
    verdict = "VERDICT: PASS"

    result = await subagents._collect(
        _stream(_says(narration), _says("running the tests"), _says(verdict), _result()),
        role="review",
    )

    # This exact bug persisted "I'll start by exploring the repository" as a
    # specification once, because the first message was read instead of the last.
    assert result.final_text == verdict
    assert narration not in result.final_text
    # The transcript still has everything: it is what the log and the activity feed read.
    assert narration in result.text
    assert verdict in result.text
    assert result.role == "review"
    assert result.num_turns == 3
    assert result.session_id == "session-1"
    assert result.is_error is False


async def test_a_session_that_only_ran_tools_falls_back_to_the_result_message() -> None:
    """With no text-carrying assistant message, `ResultMessage.result` is the verdict.

    Untouched by `run_role`'s emptiness gate on purpose: this drives `_collect` directly,
    so the raise is not on its path, and its `result=` is non-empty anyway. It is the test
    that proves the fallback the gate depends on -- without it, every tools-only session
    would look like a session that never ran.
    """
    summary = "wrote the specification to .agent/SPEC.md"
    tool_only = AssistantMessage([ToolUseBlock("tu-1", "Write", {"file_path": "SPEC.md"})], "opus")

    result = await subagents._collect(
        _stream(tool_only, _result(result=summary)),
        role="plan",
    )

    assert result.final_text == summary
    assert summary in result.text


async def test_an_activity_row_failure_cannot_fail_the_role() -> None:
    """Observability is best-effort by construction: a broken `on_row` drops rows only."""
    calls: list[int] = []

    def exploding_on_row(rows: list[dict[str, Any]]) -> None:
        calls.append(len(rows))
        raise RuntimeError("DynamoDB is having a day")

    result = await subagents._collect(
        _stream(_says("editing config.py"), _says("VERDICT: PASS"), _result()),
        role="code",
        node="verify",
        on_row=exploding_on_row,
    )

    # The hook really ran -- otherwise this test would pass on a `_collect` that never
    # built a row at all.
    assert calls
    assert result.role == "code"
    assert result.final_text == "VERDICT: PASS"
    assert result.num_turns == 3
    assert result.total_cost_usd == 0.42
    assert result.session_id == "session-1"
    assert result.is_error is False


async def test_the_cost_fields_come_off_the_result_message_and_are_tagged_with_the_node() -> None:
    """`_collect` keeps all four accounting fields, and `cost_row` tags them with the node."""
    terminator = ResultMessage(
        subtype="success",
        duration_ms=1200,
        duration_api_ms=900,
        is_error=False,
        num_turns=7,
        session_id="session-1",
        total_cost_usd=0.2059445,
        usage={"input_tokens": 12, "output_tokens": 34},
        model_usage={"anthropic.claude-opus-4-5": {"costUSD": 0.125, "provider": "bedrock"}},
        terminal_reason="end_turn",
    )

    result = await subagents._collect(_stream(_says("VERDICT: PASS"), terminator), role="review")
    assert result.usage == {"input_tokens": 12, "output_tokens": 34}
    assert result.subtype == "success"
    assert result.terminal_reason == "end_turn"

    # The node, not the role: `plan` and `replan` are two roles on one node, and the
    # console shows cost per node.
    row = subagents.cost_row(result, node="review")
    assert row["node"] == "review"
    assert row["role"] == "review"
    assert row["totalCostUsd"] == 0.2059445
    assert row["numTurns"] == 7
    assert row["sessionId"] == "session-1"
    assert row["subtype"] == "success"
    assert row["terminalReason"] == "end_turn"
    assert row["at"]
    # `model_usage` and not `usage`, because `usage` excludes subagents and `model_usage`
    # does not -- a difference that stops being invisible the moment fan-out is on.
    assert row["modelUsage"]["anthropic.claude-opus-4-5"]["provider"] == "bedrock"
    assert "usage" not in row

    # A stream with no terminator still produces a row rather than raising: the session
    # ran, and a missing figure is `None` rather than a node that fails after the work.
    truncated = await subagents._collect(_stream(_says("VERDICT: PASS")), role="review")
    empty = subagents.cost_row(truncated, node="review")
    assert empty["totalCostUsd"] is None
    assert empty["modelUsage"] == {}


async def test_an_errored_session_with_output_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An errored run that still produced its artefact returns; an empty one raises."""

    def fake_query_with_output(**kwargs: Any) -> AsyncIterator[Message]:
        return _stream(_says("SPEC written to .agent/SPEC.md"), _result(is_error=True))

    # Patched by name on the module, which works because `run_role` calls
    # `query(...)` through the module's own global rather than a bound import.
    monkeypatch.setattr(subagents, "query", fake_query_with_output)

    result = await subagents.run_role("plan", "plan it", cwd=tmp_path, cfg=_cfg())
    # A role that hit its turn limit *after* writing the spec has done the job; the node
    # that owns the artefact reads it back and decides.
    assert result.is_error is True
    assert result.final_text == "SPEC written to .agent/SPEC.md"

    def fake_query_with_nothing(**kwargs: Any) -> AsyncIterator[Message]:
        return _stream(_result(is_error=True, num_turns=200))

    monkeypatch.setattr(subagents, "query", fake_query_with_nothing)

    with pytest.raises(subagents.RoleError) as excinfo:
        await subagents.run_role("plan", "plan it", cwd=tmp_path, cfg=_cfg())
    assert "plan" in str(excinfo.value)
    assert "nothing for the node to read back" in str(excinfo.value)


async def test_a_session_that_produced_nothing_is_a_failed_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Emptiness alone is the gate, for every role, and `is_error` is `False` on purpose.

    This is the measured shape of a CLI command that aborts before any model call:
    `subtype: "success"`, `is_error: false`, `num_turns: 0`, `total_cost_usd: 0` and
    `result: ""`. The old `is_error and not final_text` condition let it through, so the
    session returned normally and a downstream guard invented a different explanation --
    `CodeError: the coding role changed nothing outside .agent/`, which points a reader at
    the model. `num_turns` cannot be the signal either (`/code-review` returned 0 turns
    while producing 1234 characters of real findings and costing $0.157); a non-empty
    `result` is the only reliable one, so the check lives in `run_role` and every role
    inherits it.
    """

    def fake_query_that_aborted(**kwargs: Any) -> AsyncIterator[Message]:
        return _stream(_result(is_error=False, num_turns=0, total_cost_usd=0.0, result=""))

    monkeypatch.setattr(subagents, "query", fake_query_that_aborted)

    for role in config.ROLES:
        with pytest.raises(subagents.RoleError) as excinfo:
            await subagents.run_role(role, "do it", cwd=tmp_path, cfg=_cfg())
        message = str(excinfo.value)
        assert role in message, message
        assert "did not run" in message
        # The accounting is in the message because it is what says the session never ran.
        assert "num_turns=0" in message
        assert "total_cost_usd=0.0" in message
        assert "success" in message
