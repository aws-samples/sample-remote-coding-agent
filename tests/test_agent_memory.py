"""`agentcore_memory` and `memory_tools`: the keying rule, the degradation, and the schemas.

Three properties are protected here, and all three fail *quietly* when they break.

**The keying rule.** An *episodic* record belongs to the registered repository
(`/rca/<configId>/episodic`, ONE namespace shared by every task that repository ever runs)
and a *session* record belongs to the task (`/rca/<configId>/session/<taskId>`). It falls
out of a single pairing on the write -- `actorId` is the configuration id, `sessionId` is
the task id, deliverable D6 -- and getting it backwards does not fail: every namespace
still resolves, just to somewhere no reader looks. The two templates are also written down
in the provisioner under another card, so they are pinned here as exact literals.

**Both halves of the namespace contract, in one file.** A retyped literal only pins the
half that retyped it, and retyping is the failure being prevented -- so this file now also
imports the *other* half, `handlers.provisioner`, and asserts the two published constants
are the same object's worth of string. The container image and the control-plane bundle are
separate deployment units that cannot import each other, which is exactly the position
`tests/invoke_payload.py` was written for: there the dispatcher sent `prompt` while the
container demanded `description` for a whole phase with both suites green, because each
half asserted its own spelling. Here the symptom is quieter still -- there is no exception
anywhere, no 500, no warning; the service extracts records into the namespace the
provisioner spelled and `retrieve_memory_records` searches the one the container spelled,
so recall is simply empty, forever. `handlers.provisioner` is imported inside a test body
rather than at module scope because `handlers._common` builds
`boto3.resource("dynamodb").Table(...)` at import and so needs a region --
`tests/test_env_contract.py` says exactly this and reads the file with `ast` to avoid it --
and `conftest`'s autouse `_aws_env` fixture has set one by the time a test *body* runs,
where collection time guarantees nothing.

**The degradation (D11).** Every AWS call is a `log.warning` and an empty result, never an
exception. Memory is an optional feature reached from inside a model's tool call, and a run
whose code, verification and pull request all succeeded must not die because a memory write
failed. A repository with no memory resource -- and `python -m coding_agent.local_run`,
which has no tables, no memory and no AWS -- gets a clean no-op with no client built at all.

**The invariant 10b argument, made mechanical.** The `memory` server ships an MCP tool that
calls an AWS API, and what makes that safe is that the *model cannot name the memory it
touches*: `memoryId`, `actorId`, `sessionId` and `namespace` all come from the process, off
the state. `test_no_tool_lets_a_model_name_the_memory_it_reads` is that enforcement.

**Why this file is offline.** moto does not model the `bedrock-agentcore` data plane at all,
so there is no mock to reach for. The client is faked the way `tests/test_agent_store.py`
fakes an absent one -- by patching the module's own `boto3` reference, never global `boto3`
-- and the two tools are reached through `memory_tools._tools`, which hands back the
`SdkMcpTool` objects so a handler can be awaited with no MCP transport standing up. The two
tests that do need a table (`resolve_memory_id`, a real `GetItem` on `CONFIG_TABLE`) take
the `aws` fixture; `no_aws(monkeypatch)` is taken wherever a test must be sure the
environment is clean, because this container's environment really does have `TASKS_TABLE`
set.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

import boto3
import pytest
from agent_helpers import no_aws
from botocore.exceptions import ClientError
from coding_agent import agentcore_memory, local_run, memory_tools, store
from coding_agent.agentcore_memory import (
    DEFAULT_TOP_K,
    EPISODIC_NAMESPACE,
    MAX_QUERY_CHARS,
    SESSION_NAMESPACE,
    TaskMemory,
    episodic_namespace,
    format_records,
    recall,
    resolve_memory_id,
)
from coding_agent.state import TaskState
from conftest import AwsFixture

# The logger every degradation warning arrives on, named once so a `caplog.at_level` cannot
# drift from the module under test.
_LOGGER = "coding_agent.agentcore_memory"

# What a model must never be able to name in a tool call. `memoryId` and `namespace` are the
# ones that would reach another repository's memory; `actorId`, `sessionId`, `configId` and
# `taskId` are the ones that would rewrite the keying rule from inside a prompt.
_FORBIDDEN_PARAMETERS = ("memoryid", "actorid", "sessionid", "namespace", "configid", "taskid")


def _memory_state() -> TaskState:
    """One task that has a memory resource. A *function*, so no test can hand the next one a
    state a previous assertion mutated."""
    return {
        "taskId": "task-1",
        "configId": "cfg-1",
        "repository": "acme/widget",
        "memoryId": "mem-abc",
    }


def _canned_records() -> list[dict[str, Any]]:
    """What `retrieve_memory_records` hands back, in AWS's shape (`content.text`)."""
    return [
        {"memoryRecordId": "rec-1", "content": {"text": "the suite needs `make setup` first"}},
        {"memoryRecordId": "rec-2", "content": {"text": "the checkout is shallow"}},
    ]


class _RecordingClient:
    """A `bedrock-agentcore` data-plane client that captures its kwargs and answers canned.

    The kwargs are the whole point: `actorId`, `sessionId` and `namespace` are supplied by
    the process, so capturing them is the only way to assert the keying rule at all.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.searches: list[dict[str, Any]] = []

    def create_event(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return {"event": {"eventId": "ev-1"}}

    def retrieve_memory_records(self, **kwargs: Any) -> dict[str, Any]:
        self.searches.append(kwargs)
        return {"memoryRecordSummaries": _canned_records()}


class _ExplodingClient:
    """A client whose every operation throws the error a throttled account really throws."""

    @staticmethod
    def _throttle(operation: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, operation
        )

    def create_event(self, **_kwargs: Any) -> dict[str, Any]:
        raise self._throttle("CreateEvent")

    def retrieve_memory_records(self, **_kwargs: Any) -> dict[str, Any]:
        raise self._throttle("RetrieveMemoryRecords")


def _install(monkeypatch: pytest.MonkeyPatch, client: Any) -> Any:
    """Patch the module's own `boto3.client`, the way `tests/test_agent_store.py` does.

    Never global `boto3`: the reference the module holds is the one a call goes through, and
    patching it is what proves the client is built *inside* the call rather than at import.
    """

    def _fake_client(name: str, **kwargs: Any) -> Any:
        assert name == "bedrock-agentcore", (
            f"the data plane is the only client this module may build; got {name!r} -- the "
            "control plane (`bedrock-agentcore-control`) creates memory resources and is "
            "the provisioner's, not the container's"
        )
        return client

    monkeypatch.setattr(agentcore_memory.boto3, "client", _fake_client)
    return client


def _explode(*args: Any, **kwargs: Any) -> Any:
    """A client factory that fails the test if it is ever reached."""
    raise AssertionError("an AWS client was built for a task that has no memory")


def _provisioner() -> Any:
    """The control plane's half of the namespace contract, which the container cannot import.

    The import is *here* rather than at module scope because `handlers._common` builds
    `boto3.resource("dynamodb").Table(...)` at import, so `handlers.provisioner` cannot be
    imported without a region at all -- `tests/test_env_contract.py` records the same
    obstacle. `conftest`'s autouse `_aws_env` fixture has set `AWS_DEFAULT_REGION` by the
    time a test body runs; collection time promises nothing, and a module-scope import would
    turn one missing environment variable into a collection error for this whole file.
    """
    from handlers import provisioner

    return provisioner


class _RecordingControl:
    """A `bedrock-agentcore-control` client that captures its `create_memory` kwargs.

    The kwargs are the point: `memoryStrategies` carries the namespaces the *service*
    extracts records into, and with no control plane to call it is the only place the
    provisioner's half of the contract is observable at all. Answers the chained-`.get`
    shape `_ensure_memory` reads its id out of.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_memory(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"memory": {"id": "mem-abc"}}


def _tool_by_name(memory: TaskMemory, name: str) -> Any:
    """One of the two tools, by name, so a handler test does not index into a list."""
    for candidate in memory_tools._tools(memory):
        if candidate.name == name:
            return candidate
    raise AssertionError(f"no tool named {name!r}")


def _tool_text(result: dict[str, Any]) -> str:
    """The text out of an MCP content result."""
    return str(result["content"][0]["text"])


def test_the_episodic_namespace_is_the_repositorys_and_not_the_tasks() -> None:
    """The keying rule: one episodic namespace per registered repository (D6)."""
    assert episodic_namespace("cfg-1") == "/rca/cfg-1/episodic"

    # The load-bearing half: the task id is not in it anywhere, so two tasks on the same
    # repository read and write the same namespace.
    first = agentcore_memory.TaskMemory(memory_id="mem-abc", config_id="cfg-1", task_id="task-1")
    second = agentcore_memory.TaskMemory(memory_id="mem-abc", config_id="cfg-1", task_id="task-2")
    assert first._namespace("episodic") == second._namespace("episodic") == "/rca/cfg-1/episodic", (
        "an episodic namespace that varied by task would make the episodic memory a "
        "task's memory, so nothing a task learned would ever be readable by the next one "
        "-- and nothing would fail: retrieval would simply return an empty list forever"
    )
    assert episodic_namespace("cfg-2") != episodic_namespace("cfg-1"), (
        "two registered repositories must not share a namespace: one repository's lessons "
        "recalled inside another repository's container is a cross-tenant leak"
    )


def test_the_session_namespace_is_one_per_task() -> None:
    """A session record belongs to the task, under the same repository prefix (D6)."""
    assert agentcore_memory.session_namespace("cfg-1", "task-1") == "/rca/cfg-1/session/task-1"
    assert agentcore_memory.session_namespace(
        "cfg-1", "task-1"
    ) != agentcore_memory.session_namespace("cfg-1", "task-2"), (
        "a session namespace shared by two tasks is not a session namespace"
    )

    # Both scopes sit under the repository's own prefix, which is what a `LeadingKeys`-style
    # fence on the memory resource would key off.
    assert agentcore_memory.session_namespace("cfg-1", "task-1").startswith("/rca/cfg-1/")
    assert episodic_namespace("cfg-1").startswith("/rca/cfg-1/")


def test_the_namespace_templates_are_the_provisioners_spelling() -> None:
    """The two templates are half of a contract with the card that creates the resource."""
    assert EPISODIC_NAMESPACE == "/rca/{actorId}/episodic", (
        "the provisioner configures the memory resource with these same two templates under "
        "another card, so a change here is a change to a contract whose other half is not in "
        "this file -- and a mismatch does not fail, it makes retrieval silently return "
        "nothing, forever, with no error and no warning"
    )
    assert SESSION_NAMESPACE == "/rca/{actorId}/session/{sessionId}"

    # And the resolvers really read the templates rather than re-spelling the path, so the
    # two halves cannot drift while both look right.
    assert episodic_namespace("cfg-1") == EPISODIC_NAMESPACE.format(actorId="cfg-1")
    assert agentcore_memory.session_namespace("cfg-1", "task-1") == SESSION_NAMESPACE.format(
        actorId="cfg-1", sessionId="task-1"
    )


def test_the_two_deployment_units_spell_the_namespace_templates_identically() -> None:
    """The other half of the contract, compared as a value rather than retyped a third time."""
    provisioner = _provisioner()

    assert agentcore_memory.EPISODIC_NAMESPACE == provisioner.EPISODIC_NAMESPACE, (
        "the container searches the namespace it spells and the service extracts into the "
        "namespace the provisioner spells, so a one-character difference between them is not "
        "an error anywhere: `retrieve_memory_records` returns an empty list, forever, with no "
        "error and no warning, and the agent simply never remembers"
    )
    assert agentcore_memory.SESSION_NAMESPACE == provisioner.SESSION_NAMESPACE, (
        "same silence on the session half: a resumed task would read back nothing of its own "
        "run, and no call would fail to say so"
    )

    # Both placeholders are the service's, not ours: `CreateMemory` substitutes them when it
    # extracts a record, so a template that lost one would collapse two lifetimes into one.
    assert "{actorId}" in provisioner.EPISODIC_NAMESPACE
    assert "{sessionId}" in provisioner.SESSION_NAMESPACE


def test_the_containers_resolvers_agree_with_the_provisioners_templates() -> None:
    """The resolvers read a template rather than re-spelling a path, checked across the units."""
    provisioner = _provisioner()
    config_id = "cfg-1"
    task_id = "task-1"

    assert agentcore_memory.episodic_namespace(config_id) == provisioner.EPISODIC_NAMESPACE.format(
        actorId=config_id
    ), (
        "this is what survives a resolver that stops reading its own module constant and "
        "spells the path inline: the constants would still match while the path the container "
        "actually searches drifts, and an empty recall is the only symptom. "
        "`api/src/handlers/configurations.py:305,314` `.format()`s these same constants for "
        "the read route, so the console's memory view rides on this too"
    )
    assert agentcore_memory.session_namespace(
        config_id, task_id
    ) == provisioner.SESSION_NAMESPACE.format(actorId=config_id, sessionId=task_id), (
        "and the session resolver takes its two ids in the order the template names them -- "
        "a transposition resolves to a real-looking namespace nothing ever writes to"
    )


def test_the_event_keys_resolve_the_provisioners_namespaces_and_are_not_interchangeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D6's one load-bearing sentence, read off the write and checked against the other unit."""
    provisioner = _provisioner()
    client = _install(monkeypatch, _RecordingClient())
    memory = TaskMemory.for_state(_memory_state())

    assert memory.record("a durable finding") is True
    (event,) = client.events

    assert event["actorId"] == "cfg-1", (
        "the actor the write carried is the CONFIGURATION id, because that is the id the "
        "episodic strategy's `{actorId}` is substituted with -- keyed by anything else the "
        "record lands where no reader looks and recall is empty, forever, with no error"
    )
    assert event["sessionId"] == "task-1", (
        "and the session is the TASK id, so `{sessionId}` resolves to this run and a resumed "
        "task reads back its own summary rather than nothing at all"
    )
    assert event["actorId"] != event["sessionId"], (
        "the guard that keeps the rest of this test from being vacuous: with one id used for "
        "both, every assertion below would hold under a swap"
    )

    assert provisioner.EPISODIC_NAMESPACE.format(actorId=event["actorId"]) == memory._namespace(
        "episodic"
    ), (
        "the namespace the episodic strategy is configured with resolves off the same "
        "`actorId` the write carried -- otherwise the service files a record in one place and "
        "the search reads another, with nothing raising in between"
    )
    assert provisioner.SESSION_NAMESPACE.format(
        actorId=event["actorId"], sessionId=event["sessionId"]
    ) == memory._namespace("session")

    assert provisioner.EPISODIC_NAMESPACE.format(actorId=event["sessionId"]) != memory._namespace(
        "episodic"
    ), (
        "the two ids are not interchangeable: with them swapped every episodic record is filed "
        "under a task id, `/rca/<configId>/episodic` is empty for every repository, no lesson "
        "ever crosses a task boundary, and nothing raises"
    )


def test_episodic_is_the_shared_namespace_and_session_is_the_per_run_one() -> None:
    """The property that makes one deployment's memory reusable, stated against both units."""
    provisioner = _provisioner()
    first = TaskMemory.for_state({**_memory_state(), "taskId": "task-1"})
    second = TaskMemory.for_state({**_memory_state(), "taskId": "task-2"})

    assert (
        first._namespace("episodic")
        == second._namespace("episodic")
        == provisioner.EPISODIC_NAMESPACE.format(actorId="cfg-1")
    ), (
        "this is what makes one AgentCore deployment's memory reusable across sessions. An "
        "episodic namespace that varied by task would make it a task's memory: nothing a task "
        "learned would ever be readable by the next one, and there would be no error to say "
        "so -- only an empty recall on every run"
    )

    assert first._namespace("session") != second._namespace("session"), (
        "and the session half is per run: two tasks sharing a session namespace would have "
        "each one reading back the other's rolling summary as its own"
    )
    assert first._namespace("session") == provisioner.SESSION_NAMESPACE.format(
        actorId="cfg-1", sessionId="task-1"
    )
    assert second._namespace("session") == provisioner.SESSION_NAMESPACE.format(
        actorId="cfg-1", sessionId="task-2"
    )


def test_the_provisioned_strategies_are_exactly_the_namespaces_the_container_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two strategies configured on the resource are the two scopes a model can reach."""
    provisioner = _provisioner()
    control = _RecordingControl()
    # `_ensure_memory` touches no table -- it reads the mapping it is handed and calls the
    # control plane -- so a recording fake is the whole harness, with no moto and no `aws`.
    monkeypatch.setattr(provisioner._common, "agentcore_control", lambda: control)

    assert provisioner._ensure_memory("cfg-1", {"repository": "acme/widget"}) == "mem-abc"
    (call,) = control.calls

    configured: dict[str, tuple[str, str]] = {}
    for strategy in call["memoryStrategies"]:
        ((kind, body),) = strategy.items()  # one strategy kind per entry
        (namespace,) = body["namespaces"]  # one namespace per strategy
        configured[namespace] = (kind, body["name"])

    # Keyed by the *container's* constants, so a rename on the provisioner side alone fails;
    # exact equality, so a third strategy or a second namespace on one of them fails too.
    assert configured == {
        agentcore_memory.EPISODIC_NAMESPACE: (
            "semanticMemoryStrategy",
            provisioner.EPISODIC_STRATEGY_NAME,
        ),
        agentcore_memory.SESSION_NAMESPACE: (
            "summaryMemoryStrategy",
            provisioner.SESSION_STRATEGY_NAME,
        ),
    }, (
        "the strategy kind is not decoration: a semantic strategy is what turns a repository's "
        "events into recallable lessons and a summary strategy is what rolls one session up, "
        "so a kind or a namespace changed on this side alone extracts records into a path the "
        "container never searches -- and the only symptom is an empty recall"
    )

    memory = TaskMemory.for_state(_memory_state())
    scopes = memory_tools._SEARCH_SCHEMA["properties"]["scope"]["enum"]
    reachable = {memory._namespace(scope) for scope in scopes}
    # `.format()` tolerates the unused `sessionId` on the episodic template.
    provisioned = {ns.format(actorId="cfg-1", sessionId="task-1") for ns in configured}
    assert reachable == provisioned, (
        "closing the loop in both directions: a strategy the container has no scope for "
        "extracts records nothing ever reads, and a scope with no strategy behind it searches "
        "a namespace the service never writes to -- both are an empty result and neither is "
        "an error"
    )


def test_an_event_is_keyed_by_configuration_as_actor_and_task_as_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliverable D6: `actorId` is the configuration id and `sessionId` is the task id."""
    client = _install(monkeypatch, _RecordingClient())
    memory = TaskMemory.for_state(_memory_state())

    assert memory.record("the venv symlink is dangling on arrival") is True
    assert len(client.events) == 1
    event = client.events[0]

    assert event["memoryId"] == "mem-abc"
    assert event["actorId"] == "cfg-1", (
        "the actor is the CONFIGURATION and not the task: swapping the two would key every "
        "episodic record to a task id, so `/rca/<configId>/episodic` would be empty for "
        "every repository and no lesson would ever cross a task boundary"
    )
    assert event["sessionId"] == "task-1"
    assert event["payload"] == [
        {
            "conversational": {
                "role": "ASSISTANT",
                "content": {"text": "the venv symlink is dangling on arrival"},
            }
        }
    ]
    assert event["eventTimestamp"].tzinfo is not None, (
        "an *aware* datetime, because botocore serialises a naive one as if it were UTC and "
        "the suite already carries a `utcnow()` deprecation warning it must not add to"
    )

    # The event and the namespaces cannot drift apart: the ids that went on the write are
    # exactly the ids the two resolvers are handed on the read.
    assert episodic_namespace(event["actorId"]) == memory._namespace("episodic")
    assert agentcore_memory.session_namespace(
        event["actorId"], event["sessionId"]
    ) == memory._namespace("session")


def test_a_non_default_role_reaches_the_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """`role=` is forwarded, so a caller can record something other than an assistant turn."""
    client = _install(monkeypatch, _RecordingClient())

    assert TaskMemory.for_state(_memory_state()).record("the task as stated", role="USER") is True
    assert client.events[0]["payload"][0]["conversational"]["role"] == "USER", (
        "`CreateEvent`'s role enum is ASSISTANT|USER|TOOL|OTHER, and a hard-coded ASSISTANT "
        "would make every recorded turn look like the model's own"
    )


@pytest.mark.parametrize("scope", ["episodic", "session"])
def test_a_search_resolves_the_scope_to_a_namespace(
    scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each scope resolves to its own resolver's namespace, and nothing else moves (D6)."""
    client = _install(monkeypatch, _RecordingClient())
    memory = TaskMemory.for_state(_memory_state())

    records = memory.search("how do I run the suite", scope=scope, top_k=3)

    assert records == _canned_records()
    assert len(client.searches) == 1
    call = client.searches[0]
    expected = (
        episodic_namespace("cfg-1")
        if scope == "episodic"
        else agentcore_memory.session_namespace("cfg-1", "task-1")
    )
    assert call["namespace"] == expected, (
        f"the {scope} scope must reach {expected}; a scope that resolved to the other "
        "namespace would return records from the wrong lifetime and never say so"
    )
    assert call["memoryId"] == "mem-abc"
    assert call["searchCriteria"] == {"searchQuery": "how do I run the suite", "topK": 3}


def test_an_unknown_scope_falls_back_to_the_episodic_namespace(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A *model* supplies the scope, so an unrecognised one must be cheap rather than fatal."""
    client = _install(monkeypatch, _RecordingClient())
    memory = TaskMemory.for_state(_memory_state())

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert memory.search("anything", scope="epsiodic") == _canned_records()

    assert client.searches[0]["namespace"] == episodic_namespace("cfg-1"), (
        "the repository's episodic namespace is the safe fallback: a typo from a model must "
        "not raise out of a tool call, because the exception would land in the middle of a "
        "role's turn rather than anywhere a human is reading"
    )
    assert "unknown memory scope" in caplog.text
    assert "epsiodic" in caplog.text, "the warning names the scope, or it cannot be diagnosed"


def test_a_client_error_is_logged_and_degrades_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """D11: every AWS failure is a warning and an empty result, never an exception."""
    _install(monkeypatch, _ExplodingClient())
    memory = TaskMemory.for_state(_memory_state())

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert memory.record("a durable finding") is False
        assert memory.search("a question") == []

    assert "could not record a memory event" in caplog.text, (
        "the calls are best-effort by design: a run whose code, verification and pull "
        "request all succeeded must not die because a memory write failed"
    )
    assert "could not search memory" in caplog.text


def test_a_task_with_no_memory_id_builds_no_client_at_all(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A repository with no memory resource is a quiet no-op, not a stream of failures."""
    monkeypatch.setattr(agentcore_memory.boto3, "client", _explode)
    memory = TaskMemory.for_state({"configId": "local", "taskId": "t"})

    assert memory.enabled is False
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert memory.record("nobody is listening") is False
        assert memory.search("nobody is answering") == []
    # Reaching the client at all would mean an `EndpointConnectionError` per call on a
    # developer machine, which is a fifteen-second stall inside a role's turn, twice.


def test_for_state_tolerates_a_state_that_names_no_configuration() -> None:
    """A state with no `configId` is a shape the suite already builds and `build_options` takes."""
    partial = TaskMemory.for_state({"taskId": "task-1"})
    assert partial.enabled is False, (
        "`tests/test_subagents.py` builds `{'taskId': ..., 'repository': ...}` with no "
        "`configId` and hands it to `build_options`, so subscription here would fail a "
        "currently-green test and, in production, kill a role session over an absent "
        "optional feature"
    )

    # `build_options` really does pass `state=None` -- `local_run` hands it no state at all.
    assert TaskMemory.for_state(None).enabled is False
    assert TaskMemory.for_state(None).config_id == ""


def test_a_local_run_has_no_memory(tmp_path: Path) -> None:
    """`python -m coding_agent.local_run` works unchanged: no tables, no memory, no AWS."""
    state = local_run._initial_state(
        argparse.Namespace(
            repo=str(tmp_path),
            task="do a thing",
            no_submit=True,
            work_root=None,
            verify_command=None,
            setup_command=None,
            task_id="local-abcd1234",
        )
    )

    assert state["configId"] == "local"
    assert "memoryId" not in state, (
        "`local_run` walks the nodes itself and never calls `run_task`, so it has no "
        "`memoryId` by construction rather than by a guard -- and absence is the signal "
        "`for_state` reads"
    )
    assert TaskMemory.for_state(state).enabled is False


def test_no_client_is_built_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """`store.py`'s rule 1: the client is built inside each call, never at import."""
    built: list[str] = []

    def _recording_factory(name: str, **kwargs: Any) -> Any:
        built.append(name)
        return _RecordingClient()

    monkeypatch.setattr(agentcore_memory.boto3, "client", _recording_factory)

    memory = TaskMemory.for_state(_memory_state())
    assert built == [], (
        "constructing a `TaskMemory` must reach nothing: `build_options` builds one per "
        "role session and is documented as pure, so a client in the constructor would take "
        "the whole offline test surface down with it"
    )

    memory.record("a durable finding")
    assert built == ["bedrock-agentcore"], (
        "the *data plane*, built per call -- which is what lets the `aws` fixture's "
        "`monkeypatch.setenv` take effect with no `importlib.reload`"
    )


def test_the_formatter_turns_records_into_text_a_model_can_read() -> None:
    """One formatter, so the search tool and any prompt cannot spell a record two ways."""
    rendered = format_records(_canned_records())
    assert "the suite needs `make setup` first" in rendered
    assert "the checkout is shallow" in rendered
    assert rendered.count("\n") == 1, "one bullet per record, so a model can count them"

    # The response shape is AWS's, not ours, and an empty record renders as a bullet saying
    # nothing -- which reads to a model as a memory that recorded silence.
    assert format_records([{"memoryRecordId": "rec-3"}]) == agentcore_memory._NO_RECORDS
    assert format_records([{"content": {"text": "  "}}]) == agentcore_memory._NO_RECORDS

    empty = format_records([])
    assert empty, (
        "never `\"\"`: a model handed an empty string cannot tell 'nothing matched' from "
        "'the tool broke', and the second reading invites a retry loop over a working tool"
    )
    assert empty == agentcore_memory._NO_RECORDS


def test_recall_renders_through_the_shared_formatter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node-facing entry point is `format_records` over `.search`, and nothing else."""
    client = _install(monkeypatch, _RecordingClient())

    assert recall(_memory_state(), "how do I run the suite") == format_records(_canned_records()), (
        "a prompt and the `memory_search` tool must spell a record the same way, which is "
        "only true while `format_records` is the one renderer -- `recall` composes it rather "
        "than laying out records itself"
    )

    call = client.searches[0]
    assert call["namespace"] == episodic_namespace("cfg-1"), (
        "episodic is the default because it is the *repository's* lifetime, and that is the "
        "whole point of the prompt slot: a session-scoped default would hand a role back "
        "what it wrote itself ten minutes ago and nothing a previous task learned"
    )
    assert call["searchCriteria"] == {
        "searchQuery": "how do I run the suite",
        "topK": DEFAULT_TOP_K,
    }

    # Both keywords really reach `.search` rather than being defaults a caller cannot move.
    recall(_memory_state(), "how do I run the suite", scope="session", top_k=3)
    assert client.searches[1]["namespace"] == agentcore_memory.session_namespace("cfg-1", "task-1")
    assert client.searches[1]["searchCriteria"]["topK"] == 3


def test_recall_is_empty_for_a_task_with_no_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A memoryless task builds no client at all -- the `.enabled` return comes first."""
    monkeypatch.setattr(agentcore_memory.boto3, "client", _explode)

    assert recall({"configId": "local", "taskId": "t"}, "anything") == ""
    # `build_options` really does pass `state=None`, so `recall` has to take one too.
    assert recall(None, "anything") == ""
    # `_explode` is the assertion: every role prompt now calls this once, so a client built
    # for a repository with no memory resource would be an `EndpointConnectionError` and a
    # fifteen-second stall in front of `plan`, `code` and `review` alike.


def test_recall_ignores_a_blank_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank query costs nothing, and that is true of this function on its own."""
    monkeypatch.setattr(agentcore_memory.boto3, "client", _explode)

    assert recall(_memory_state(), "  ") == ""
    assert recall(_memory_state(), "") == "", (
        "a task registered with no description reaches `recall` with an empty string, and "
        "`RetrieveMemoryRecords` on a blank query is a round trip that can only return "
        "nothing -- the guard is here rather than in three nodes"
    )


def test_recall_degrades_to_an_empty_string_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """D11 on the prompt path: a throttle is a warning and an empty slot, never an exception."""
    _install(monkeypatch, _ExplodingClient())

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        recalled = recall(_memory_state(), "a question")

    assert recalled == "", (
        "this call is synchronous on the node's thread before a role session starts, so a "
        "throttled account must not become a new way for a task to die over an optional "
        "feature -- the swallow is `.search`'s and `recall` adds no second one"
    )
    assert "could not search memory" in caplog.text
    assert recalled != agentcore_memory._NO_RECORDS, (
        "a throttle must not paste the tool's own nothing-matched sentence into a prompt as "
        "if a real search had run and found nothing: the role would read 'this repository "
        "has learned nothing' off an error"
    )


def test_recall_returns_nothing_rather_than_the_formatters_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`""` and not `_NO_RECORDS`, which is the whole reason `recall` exists as a function."""

    class _EmptyClient(_RecordingClient):
        def retrieve_memory_records(self, **kwargs: Any) -> dict[str, Any]:
            self.searches.append(kwargs)
            return {"memoryRecordSummaries": []}

    _install(monkeypatch, _EmptyClient())

    assert recall(_memory_state(), "how do I run the suite") == ""
    assert format_records([]) == agentcore_memory._NO_RECORDS, (
        "the two placeholders are different on purpose: a tool result is read back by the "
        "model that just called it, while a prompt slot is read cold, so the *caller* "
        'chooses what a prompt shows (`recall(...) or "(nothing recorded yet)"`) and '
        "`format_records` stays the one renderer of a record"
    )


def test_recall_caps_the_query_it_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """One owner of the number, because the coding role's query is a whole specification."""
    client = _install(monkeypatch, _RecordingClient())

    recall(_memory_state(), "x" * 5000)

    assert len(client.searches[0]["searchCriteria"]["searchQuery"]) == MAX_QUERY_CHARS, (
        "`RetrieveMemoryRecords` is a semantic search and not a grep: the `code` node's "
        "query is the description plus the specification text, which is thousands of "
        "characters of headings and file lists that embed to noise. The cap lives here so "
        "three nodes cannot each pick a different few-hundred"
    )


def test_the_memory_id_comes_off_the_configuration_item(aws: AwsFixture) -> None:
    """Invariant 31's only permitted read: a `GetItem` on this task's own configuration."""
    table = boto3.resource("dynamodb", region_name=aws.region).Table(aws.config_table)
    table.put_item(Item={"configId": "cfg-1", "repository": "acme/widget", "memoryId": "mem-abc"})
    table.put_item(Item={"configId": "cfg-2", "repository": "acme/gadget"})

    assert resolve_memory_id("cfg-1") == "mem-abc"
    assert resolve_memory_id("cfg-2") == "", (
        "a repository registered before the memory resource existed has an item with no "
        "`memoryId`, and that is 'no memory', not an error"
    )
    assert resolve_memory_id("cfg-absent") == ""


def test_resolve_memory_id_is_empty_with_no_configuration_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local-run path: no `CONFIG_TABLE`, so no client and no memory."""
    no_aws(monkeypatch)

    def _no_resource(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("resolve_memory_id built an AWS client with no table configured")

    monkeypatch.setattr(store.boto3, "resource", _no_resource)

    assert resolve_memory_id("local") == "", (
        "the table name is read inside `get_configuration` and its absence is a return, "
        "which is what makes `python -m coding_agent.local_run` work on a machine with no "
        "tables at all"
    )


def test_resolve_memory_id_degrades_when_the_configuration_read_fails(
    aws: AwsFixture, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The swallow lives in the new caller, not in `store.py` (D11)."""
    monkeypatch.setenv("CONFIG_TABLE", "Configurations-that-was-never-created")

    # The control, so the swallow is shown to be this module's: `get_configuration`
    # deliberately propagates, because its other caller wants the repository's commands and
    # a `None` on a throttle would look exactly like an unconfigured repository.
    with pytest.raises(ClientError):
        store.get_configuration("cfg-1")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert resolve_memory_id("cfg-1") == ""

    assert "could not read the configuration item" in caplog.text, (
        "this read is on the critical path in `run_task`, before the first node runs, so a "
        "throttle on it must not become a new way for a task to die over an optional feature"
    )


def test_a_task_with_no_memory_gets_no_server() -> None:
    """`build_server` returns `None`, so a memoryless run holds no `memory` server at all."""
    for state in ({"configId": "cfg-1", "taskId": "task-1"}, {**_memory_state(), "memoryId": ""}):
        assert memory_tools.build_server(state) is None, (
            "no server rather than two tools that fail on every call: a role that discovers "
            "a memory tool and spends its turns learning it is broken has been given a "
            "worse deal than one that never saw it"
        )
    assert memory_tools.build_server(None) is None


def test_the_memory_server_exposes_exactly_two_tools() -> None:
    """The whole surface is `memory_search` and `memory_record`, in that order."""
    server = memory_tools.build_server(_memory_state())
    assert server is not None
    # `create_sdk_mcp_server` returns a TypedDict, so this is subscription and not attribute
    # access -- `server.type` raises.
    assert server["type"] == "sdk", "in-process, not a stdio subprocess that cannot hold the state"
    assert server["name"] == memory_tools.MEMORY_SERVER_NAME

    memory = TaskMemory.for_state(_memory_state())
    assert [candidate.name for candidate in memory_tools._tools(memory)] == [
        "memory_search",
        "memory_record",
    ], (
        "two tools is the whole surface, and it is the invariant 10b argument: exactly "
        "`CreateEvent` and `RetrieveMemoryRecords` are reachable from a prompt, so nothing "
        "else in `bedrock-agentcore` is"
    )

    assert memory_tools.MEMORY_TOOLS == "mcp__memory__*"
    assert f"mcp__{memory_tools.MEMORY_SERVER_NAME}__*" == memory_tools.MEMORY_TOOLS, (
        "the wildcard is derived from the server name by Claude Code, so a rename that "
        "touched only one of the two would leave every role allow-listing a server that "
        "does not exist and holding no memory tools at all"
    )


def test_no_tool_lets_a_model_name_the_memory_it_reads() -> None:
    """Invariant 10b, made mechanical: the model cannot name the memory, actor or namespace."""
    memory = TaskMemory.for_state(_memory_state())
    expected = {"query", "scope", "top_k", "text"}

    for candidate in memory_tools._tools(memory):
        properties = set(candidate.input_schema["properties"])
        assert properties <= expected, (
            f"{candidate.name} accepts {sorted(properties - expected)}, which is outside the "
            "four values a model may supply -- a query, a scope, a width and a text"
        )
        lowered = {name.lower() for name in properties}
        for forbidden in _FORBIDDEN_PARAMETERS:
            assert forbidden not in lowered, (
                f"{candidate.name} would let a model name the {forbidden} it touches. This "
                "is what makes an MCP tool that calls an AWS API safe at all: the memory "
                "id, the actor, the session and the namespace are supplied by the process "
                "from the state, so a prompt-injected role cannot read another "
                "repository's memory, cannot write into one, and cannot widen its reach"
            )


def test_the_search_tool_only_requires_a_query() -> None:
    """The explicit JSON Schema form, because the SDK's shorthand marks every key required."""
    schema = memory_tools._SEARCH_SCHEMA
    assert schema["required"] == ["query"], (
        "`create_sdk_mcp_server`'s `_build_input_schema` marks *every* key of a "
        "dict-of-types shorthand schema required, so `{'query': str, 'scope': str, 'top_k': "
        "int}` would force the model to pass all three on every call -- and a model made to "
        "choose a scope and a width every time will get one of them wrong"
    )
    assert {"scope", "top_k"} <= set(schema["properties"])
    assert "scope" not in schema["required"]
    assert "top_k" not in schema["required"]
    assert schema["properties"]["scope"]["enum"] == ["episodic", "session"], (
        "a two-value enum is the whole scope surface: anything else is a namespace the "
        "model chose, and `_namespace` falls back rather than raising precisely because it "
        "cannot rely on this"
    )
    assert schema["properties"]["top_k"]["default"] == DEFAULT_TOP_K, (
        "the advertised default and the client's are one number, shared from "
        "`agentcore_memory.DEFAULT_TOP_K`"
    )
    assert memory_tools._RECORD_SCHEMA["required"] == ["text"]


def test_the_search_tools_description_says_what_the_two_scopes_are() -> None:
    """Prompt surface: the description is all a model has to choose a scope from."""
    description = memory_tools._SEARCH_DESCRIPTION.lower()
    assert "episodic" in description
    assert "session" in description
    assert "every task" in description, (
        "the episodic scope spans every task this repository has ever run, and a model told "
        "only the word 'episodic' has no way to know that -- it would search the wrong "
        "lifetime and read the empty result as a repository that has learned nothing"
    )
    assert "this task" in description, "and the session scope is this run, and nothing else"


def test_the_record_tools_description_says_it_is_for_a_durable_finding_not_narration() -> None:
    """Prompt surface: both halves, because the second one is what bounds the cost."""
    description = memory_tools._RECORD_DESCRIPTION.lower()
    assert "durable finding" in description
    assert "future task" in description
    assert "narration" in description, (
        "every write is a synchronous `create_event` on the event loop's thread inside a "
        "tool call, so a role that narrates into memory pays a round trip per sentence and "
        "leaves a future run a transcript instead of a finding"
    )


async def test_the_search_tool_returns_the_shared_formatter_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool renders through `format_records`, so the tool and a prompt agree on a record."""
    client = _install(monkeypatch, _RecordingClient())
    memory = TaskMemory.for_state(_memory_state())

    result = await _tool_by_name(memory, "memory_search").handler(
        {"query": "how do I run the suite"}
    )

    assert _tool_text(result) == format_records(_canned_records()), (
        "one formatter, so the search tool and the next wave's prompt work cannot spell a "
        "record two ways -- two spellings of the same record is two things to keep in step"
    )

    # The two optional parameters really are optional, and the handler's defaults are the
    # ones the schema advertises rather than a second set.
    call = client.searches[0]
    assert call["namespace"] == episodic_namespace("cfg-1"), (
        "episodic is the default scope: it is the one worth reaching for before planning, "
        "and a session default would make the tool useless on the first search of a run"
    )
    assert call["searchCriteria"] == {
        "searchQuery": "how do I run the suite",
        "topK": DEFAULT_TOP_K,
    }


async def test_a_tool_whose_aws_call_fails_returns_text_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D11 through the tool layer: degradation is text, and never an `is_error` result."""
    _install(monkeypatch, _ExplodingClient())
    memory = TaskMemory.for_state(_memory_state())

    searched = await _tool_by_name(memory, "memory_search").handler({"query": "a question"})
    recorded = await _tool_by_name(memory, "memory_record").handler({"text": "a finding"})

    for result in (searched, recorded):
        assert result["content"][0]["type"] == "text"
        assert not result.get("is_error"), (
            "a failed memory call is degradation, and flagging it as a tool error invites "
            "the model to retry-loop over something that is not its problem and cannot be "
            "fixed from inside its turn"
        )
    assert "did not land" in _tool_text(recorded), (
        "the model is told the finding was not saved, so it does not carry on believing a "
        "future run will have it"
    )
    assert _tool_text(searched) == agentcore_memory._NO_RECORDS


async def test_the_record_tool_writes_one_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """One tool call is one `CreateEvent`, carrying the model's own text."""
    client = _install(monkeypatch, _RecordingClient())
    memory = TaskMemory.for_state(_memory_state())

    result = await _tool_by_name(memory, "memory_record").handler(
        {"text": "`make setup` is a hard prerequisite in a fresh container"}
    )

    assert len(client.events) == 1, "one call, one event -- no retry and no batching"
    payload = client.events[0]["payload"][0]["conversational"]
    assert payload["content"]["text"] == "`make setup` is a hard prerequisite in a fresh container"
    assert payload["role"] == "ASSISTANT"
    assert "Recorded" in _tool_text(result)
