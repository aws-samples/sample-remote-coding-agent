"""What the graph does when verification fails, when the reviewer edits the tree, and
when it runs out of budget.

Every test here builds the **real** graph -- the real edges, the real `_after_review` --
out of stub coroutines that append their own name to a visit list. That is what
`build_graph(nodes=...)` exists for, and it is why these assertions are on the sequence
of nodes a run actually visits rather than on anything a node computed. Nothing here
needs Bedrock, git, or a repository on disk.

`instrument=False` in the routing tests: the first act of `_instrument` is a
`store.set_status`, and this container's environment really does have `TASKS_TABLE` set, so
an instrumented routing test would talk to DynamoDB. `no_aws(monkeypatch)` is the second
belt.

The `run_task` tests at the bottom are the exception, and deliberately: they leave
`instrument` at its default because the production entry point is what they are about, and
under `no_aws` every `store.set_status` returns early anyway. `run_task` has no other
coverage here -- `tests/test_checkpoint.py` owns the resume half -- so the memory id's one
resolution point is pinned in this file. The memory tests are the other exception, for the
inverse reason: `_instrument` is the one writer of a node's memory event, so a run with the
wrapper switched off would assert nothing at all.
"""

from __future__ import annotations

import inspect
import io
import itertools
import logging
import tokenize
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_args

import invoke_payload
import pytest
from agent_helpers import no_aws
from botocore.exceptions import ClientError
from coding_agent import checkpoint, graph, local_run
from coding_agent.nodes import checkout as checkout_module
from coding_agent.nodes import code as code_module
from coding_agent.nodes import plan as plan_module
from coding_agent.nodes import review as review_module
from coding_agent.nodes import submit as submit_module
from coding_agent.nodes import verify as verify_module
from coding_agent.state import MAX_ITERATIONS, TaskState, TaskStatus

# The five nodes whose behaviour no routing decision reads. `review` is always supplied
# by the test, because it is the only node whose output the router looks at.
_PASSIVE_NODES = ("checkout", "plan", "code", "verify", "submit")


def _initial_state() -> TaskState:
    """The cold-start state, holding only what a dispatcher knows."""
    return {
        "taskId": "t-graph",
        "configId": "c-graph",
        "repository": "acme/widgets",
        "description": "add a --version flag",
        "iteration": 0,
        "reviewRerun": False,
    }


def _recorder(name: str, visits: list[str]) -> graph.NodeFn:
    """A node that records its visit and updates nothing."""

    async def node(state: TaskState) -> dict[str, Any]:
        visits.append(name)
        return {}

    return node


def _review(visits: list[str], *, passed: bool, edits_the_tree: bool = False) -> graph.NodeFn:
    """A `review` stub that copies the real node in the two ways routing depends on.

    It advances `iteration`, which the real `review` is the only node to do, and when
    `edits_the_tree` it spends the one verify/review hop exactly as the real node does:
    `{"reviewRerun": True}` and *nothing else* on the first pass of an iteration, then
    the verdict on the second. A stub that returned a `verifyResult` alongside the flag
    would be testing a node that cannot exist.
    """

    async def review(state: TaskState) -> dict[str, Any]:
        visits.append("review")
        if edits_the_tree and not state.get("reviewRerun"):
            return {"reviewRerun": True}
        return {
            "verifyResult": {"passed": passed, "verified": passed},
            "reviewRerun": False,
            "iteration": state.get("iteration", 0) + 1,
        }

    return review


def _expected(attempt: list[str]) -> list[str]:
    """`checkout`, then one `attempt` per iteration, then `submit`: an exhausted run."""
    return ["checkout", *(attempt * MAX_ITERATIONS), "submit"]


def _stub_nodes(visits: list[str], review: graph.NodeFn) -> dict[str, graph.NodeFn]:
    """The whole node table as stubs, so `DEFAULT_NODES` contributes nothing but names."""
    nodes = {name: _recorder(name, visits) for name in _PASSIVE_NODES}
    nodes["review"] = review
    return nodes


async def _run(visits: list[str], review: graph.NodeFn) -> TaskState:
    """Run the real graph with stub nodes and the production `recursion_limit`."""
    compiled = graph.build_graph(nodes=_stub_nodes(visits, review), instrument=False)
    return await compiled.ainvoke(_initial_state(), graph.graph_config("t-graph"))


def _observed_nodes(visits: list[str], seen: list[dict[str, Any]]) -> dict[str, graph.NodeFn]:
    """The whole stub table, with every node also copying the state it was handed.

    A `dict(state)` copy rather than the state object, because the graph merges each
    update into the state it goes on to hand the next node -- a list of references would
    end up holding six views of the last one.

    `review` reports `passed`, so `_after_review` routes straight to `submit` and a
    `run_task` over these nodes is six visits rather than a loop.
    """

    def _observe(fn: graph.NodeFn) -> graph.NodeFn:
        async def node(state: TaskState) -> dict[str, Any]:
            seen.append(dict(state))
            return await fn(state)

        return node

    nodes = _stub_nodes(visits, _review(visits, passed=True))
    return {name: _observe(fn) for name, fn in nodes.items()}


def _recording_resolver(calls: list[str], *returns: str) -> Callable[[str], str]:
    """A stand-in for `agentcore_memory.resolve_memory_id` that records its argument.

    `returns` is consumed one value per call and the last value repeats, so a two-phase
    resume can hand back `""` on the first attempt and a memory id on the second.
    """

    def resolve(config_id: str) -> str:
        calls.append(config_id)
        return returns[min(len(calls) - 1, len(returns) - 1)]

    return resolve


# The logger every `agentcore_memory` degradation warning arrives on, named once here the
# way `tests/test_agent_memory.py` names it, so a `caplog.at_level` cannot drift from the
# module that does the writing.
_MEMORY_LOGGER = "coding_agent.agentcore_memory"


class _RecordingMemoryClient:
    """A `bedrock-agentcore` data-plane client that captures the kwargs of every write.

    The kwargs are the point: `actorId` and `sessionId` are supplied by the process, so
    capturing them is the only way to assert the keying rule (D6) from out here.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def create_event(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return {"event": {"eventId": f"ev-{len(self.events)}"}}


class _ThrottledMemoryClient:
    """A client whose write throws what a throttled account really throws."""

    def create_event(self, **_kwargs: Any) -> dict[str, Any]:
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "CreateEvent"
        )


def _install_memory(monkeypatch: pytest.MonkeyPatch, client: Any) -> Any:
    """Patch the reference `agentcore_memory` itself holds, in that module's own test idiom.

    Never global `boto3`: the module's reference is the one a call goes through, and moto
    does not model the `bedrock-agentcore` data plane at all, so there is no mock to reach
    for and a fake is the whole offline seam.
    """

    def _fake_client(name: str, **kwargs: Any) -> Any:
        assert name == "bedrock-agentcore", (
            f"the data plane is the only client a node visit may build; got {name!r} -- the "
            "control plane (`bedrock-agentcore-control`) creates memory resources and is "
            "the provisioner's, never the container's"
        )
        return client

    monkeypatch.setattr(graph.agentcore_memory.boto3, "client", _fake_client)
    return client


def _no_client(*args: Any, **kwargs: Any) -> Any:
    """A client factory that fails the test if a memoryless run ever reaches it."""
    raise AssertionError("an AWS client was built for a task that has no memory")


def _memory_state() -> TaskState:
    """The cold-start state of a task whose repository *has* a memory resource.

    A function and not a constant, for the same reason `_initial_state` is: no test may hand
    the next one a state a previous assertion mutated.
    """
    return {**_initial_state(), "memoryId": "mem-abc"}


def _texts(client: Any) -> list[str]:
    """The text of every event the fake client was handed, in the order it was written."""
    return [event["payload"][0]["conversational"]["content"]["text"] for event in client.events]


async def _run_instrumented(nodes: dict[str, graph.NodeFn], state: TaskState) -> TaskState:
    """Run the real graph with stub nodes and `_instrument` **on** -- it is the subject here.

    Under `no_aws` both writes `_instrument` makes to DynamoDB return early, so the only
    thing left reaching outside the process is the memory event each test fakes.
    """
    compiled = graph.build_graph(nodes=nodes, instrument=True)
    return await compiled.ainvoke(state, graph.graph_config("t-graph"))


async def test_failure_returns_to_plan_not_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed attempt is re-planned, MAX_ITERATIONS times, and then submitted anyway."""
    no_aws(monkeypatch)
    visits: list[str] = []
    result = await _run(visits, _review(visits, passed=False))

    assert visits == _expected(["plan", "code", "verify", "review"])
    # The point of the edge, stated separately from the sequence above: re-running the
    # coder against the spec that just failed is how a loop spends its whole budget
    # producing the same diff three times.
    transitions = list(itertools.pairwise(visits))
    assert ("review", "code") not in transitions
    assert result["iteration"] == MAX_ITERATIONS
    # Out of iterations still means `submit`, because a draft pull request is worth more
    # than a run that vanishes.
    assert result["verifyResult"]["passed"] is False


async def test_a_reviewer_that_edits_the_tree_reruns_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A review that invalidated the last measurement routes back to `verify`, not onward."""
    no_aws(monkeypatch)
    visits: list[str] = []
    result = await _run(visits, _review(visits, passed=True, edits_the_tree=True))

    assert visits == ["checkout", "plan", "code", "verify", "review", "verify", "review", "submit"]
    # Two measurements, one iteration: the hop is not an attempt at the task, so it does
    # not spend the re-plan budget (invariant 6c).
    assert visits.count("verify") == 2
    assert result["iteration"] == 1


async def test_the_rerun_is_spent_once_per_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reviewer that edits the tree on every pass cannot make the hop loop."""
    no_aws(monkeypatch)
    visits: list[str] = []
    # The pathological role: it edits the tree every single time it is asked to judge it.
    result = await _run(visits, _review(visits, passed=False, edits_the_tree=True))

    assert visits == _expected(["plan", "code", "verify", "review", "verify", "review"])
    assert result["iteration"] == MAX_ITERATIONS
    assert result["reviewRerun"] is False
    assert len(visits) < graph.RECURSION_LIMIT


async def test_the_retry_flag_survives_a_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reviewRerun` is on `TaskState`, so a container reaped mid-hop resumes still owing it."""
    no_aws(monkeypatch)
    visits: list[str] = []
    nodes = _stub_nodes(visits, _review(visits, passed=True, edits_the_tree=True))

    async def reaped_verify(state: TaskState) -> dict[str, Any]:
        """Die on the re-run the hop just asked for -- the worst moment to lose the flag."""
        visits.append("verify")
        if visits.count("verify") == 2:
            raise RuntimeError("the container was reaped")
        return {}

    nodes["verify"] = reaped_verify
    path = checkpoint.checkpoint_path({"workDir": str(tmp_path)})
    cfg = graph.graph_config("t-graph")

    compiled = graph.build_graph(
        nodes=nodes, checkpointer=checkpoint.FileCheckpointer(path), instrument=False
    )
    with pytest.raises(RuntimeError, match="reaped"):
        await compiled.ainvoke(_initial_state(), cfg)
    assert visits == ["checkout", "plan", "code", "verify", "review", "verify"]

    # A second checkpointer over the same file, not the same object: the flag has to come
    # back off disk, which is the only place a replacement container can read it from.
    resumed = graph.build_graph(
        nodes=nodes, checkpointer=checkpoint.FileCheckpointer(path), instrument=False
    )
    values = (await resumed.aget_state(cfg)).values
    assert values["reviewRerun"] is True
    # And the hop still has not cost an iteration, so the resume has its full budget.
    assert values["iteration"] == 0


async def test_recursion_limit_is_not_what_stops_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant 12: the re-plan budget ends a run, so a lost draft PR is never a recursion error."""
    no_aws(monkeypatch)
    # Recomputed from the graph's shape rather than imported: checkout, then
    # MAX_ITERATIONS supersteps of plan/code/verify/review plus the one permitted
    # verify/review re-run, then submit, plus two spare. Reading `_VISITS_PER_ITERATION`
    # here would only assert that a constant equals itself.
    assert graph.RECURSION_LIMIT == 1 + MAX_ITERATIONS * 6 + 1 + 2

    visits: list[str] = []
    result = await _run(visits, _review(visits, passed=False, edits_the_tree=True))
    # The worst run there is: every iteration fails and every review costs an extra
    # measurement. It ends at `submit`, and with room to spare.
    assert visits[-1] == "submit"
    assert len(visits) < graph.RECURSION_LIMIT
    assert result["iteration"] == MAX_ITERATIONS


async def test_the_node_cost_never_reaches_the_graph_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_instrument` pops `roleCosts`: it is written out and it is not state.

    Instrumented, unlike every other test here, because `_instrument` is the subject. The
    two DynamoDB writes it makes are already no-ops under `no_aws`; `record_cost` is
    patched so the rows can be looked at rather than because it would reach an endpoint.
    """
    no_aws(monkeypatch)
    written: list[list[dict[str, Any]]] = []
    rows: list[dict[str, Any]] = []

    def capture(_state: TaskState, node_rows: list[dict[str, Any]]) -> None:
        written.append(node_rows)

    def capture_activity(_state: TaskState, activity_rows: list[dict[str, Any]]) -> None:
        rows.extend(activity_rows)

    monkeypatch.setattr(graph.store, "record_cost", capture)
    monkeypatch.setattr(graph.activity, "record", capture_activity)

    cost_row = {"node": "plan", "role": "plan", "totalCostUsd": 0.5, "numTurns": 4}
    visits: list[str] = []
    nodes = _stub_nodes(visits, _review(visits, passed=True))

    async def costly_plan(state: TaskState) -> dict[str, Any]:
        visits.append("plan")
        return {"specPath": "/tmp/spec.md", "roleCosts": [cost_row]}

    nodes["plan"] = costly_plan
    compiled = graph.build_graph(nodes=nodes, instrument=True)
    result = await compiled.ainvoke(_initial_state(), graph.graph_config("t-graph"))

    assert written == [[cost_row]], "the rows reach `store.record_cost` exactly once"
    assert "roleCosts" not in result, (
        "cost is observability, not state: leaving it on the update would put a growing "
        "list into every checkpoint a resume has to read back"
    )
    # The node's own output still comes through, so the pop took the one key and no more.
    assert result["specPath"] == "/tmp/spec.md"
    assert result["currentNode"] == "submit"

    # One feed row per node, carrying the node's total rather than a row per session.
    cost_rows = [row for row in rows if row.get("kind") == "cost"]
    assert len(cost_rows) == 1
    assert cost_rows[0]["node"] == "plan"
    assert cost_rows[0]["totalCostUsd"] == 0.5
    assert cost_rows[0]["sessions"] == 1


async def test_every_node_visit_records_one_memory_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """One bounded memory record per node visit, keyed the way `agentcore_memory` keys it.

    Instrumented, like the cost test above and unlike the routing tests: `_instrument` is the
    only writer of these events, which is the whole reason they can be counted at all.
    """
    no_aws(monkeypatch)
    client = _install_memory(monkeypatch, _RecordingMemoryClient())
    visits: list[str] = []

    await _run_instrumented(_stub_nodes(visits, _review(visits, passed=True)), _memory_state())

    assert visits == ["checkout", "plan", "code", "verify", "review", "submit"]
    texts = _texts(client)
    assert len(texts) == len(visits), (
        "one event per visit, no more and no fewer: `_instrument` is the single writer, so a "
        "node cannot forget to record its outcome and two nodes cannot record it twice"
    )
    for node, text in zip(visits, texts, strict=True):
        assert text.startswith(node), (
            f"{text!r} does not name {node!r} first -- the node name leads every line so that "
            "the 500-character bound can only ever cost the tail"
        )
        assert len(text) <= graph._MEMORY_TEXT_LIMIT

    # The keying rule, D6, off the captured kwargs: this is the pairing that makes
    # `/rca/<configId>/episodic` a repository's memory rather than a task's.
    for event in client.events:
        assert event["memoryId"] == "mem-abc"
        assert event["actorId"] == "c-graph", "the actor is the CONFIGURATION id, not the task's"
        assert event["sessionId"] == "t-graph", "and the session is the TASK id"
        assert event["payload"][0]["conversational"]["role"] == "ASSISTANT"
        assert "namespace" not in event, (
            "`CreateEvent` takes no namespace and the wrapper supplies none: the memory "
            "resource's two strategies are what fan this one event out to the "
            "repository-shared and the per-session namespace, so a namespace here would mean "
            "the wrapper had chosen a record's lifetime"
        )

    # And the content, spot-checked where the composer actually reads something.
    review_text = texts[visits.index("review")]
    assert "passed=True" in review_text
    assert "verified=True" in review_text, (
        "both halves of the verdict: `passed` is what `_after_review` reads and `verified` is "
        "what decides a draft pull request, and a run with no command configured has one "
        "without the other (invariant 6)"
    )
    assert "verified=True" in texts[visits.index("submit")], (
        "`submit` writes no verdict, so its line reads `verified` off the state `review` left "
        "behind on the previous superstep"
    )


async def test_the_memory_write_never_reaches_the_graph_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule `_instrument` already follows for `status` and `roleCosts`, restated.

    Nothing an observability write produces may reach the checkpoint a resume reads back, so
    a memory-enabled run and a memoryless one must end on the same state.
    """
    no_aws(monkeypatch)
    client = _install_memory(monkeypatch, _RecordingMemoryClient())
    remembered_visits: list[str] = []
    with_memory = await _run_instrumented(
        _stub_nodes(remembered_visits, _review(remembered_visits, passed=True)), _memory_state()
    )

    monkeypatch.setattr(graph.agentcore_memory.boto3, "client", _no_client)
    plain_visits: list[str] = []
    without_memory = await _run_instrumented(
        _stub_nodes(plain_visits, _review(plain_visits, passed=True)), _initial_state()
    )

    assert client.events, "the first run really did write, or this compares two silent runs"
    assert {key: value for key, value in with_memory.items() if key != "memoryId"} == dict(
        without_memory
    ), (
        "the memory write is observability: it reaches AgentCore Memory and it does not reach "
        "the returned update, so a resume reads back the same state either way"
    )


async def test_a_task_with_no_memory_records_nothing_and_builds_no_client(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Every local run and every repository registered before the resource existed: silent."""
    no_aws(monkeypatch)
    monkeypatch.setattr(graph.agentcore_memory.boto3, "client", _no_client)
    visits: list[str] = []

    with caplog.at_level(logging.WARNING, logger=_MEMORY_LOGGER):
        result = await _run_instrumented(
            _stub_nodes(visits, _review(visits, passed=True)), _initial_state()
        )

    assert result["currentNode"] == "submit"
    assert visits == ["checkout", "plan", "code", "verify", "review", "submit"]
    assert [record for record in caplog.records if record.name == _MEMORY_LOGGER] == [], (
        "the `.enabled` guard in `_record_memory` and not `record`'s own early return is what "
        "makes a memoryless run quiet: `record` logs a warning and returns `False`, so calling "
        "it unconditionally would put one line per node visit into every local run -- up to 22 "
        "of them, hiding the one warning that means something"
    )


async def test_a_node_that_raises_still_records_its_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure path records too, and the wrapper still re-raises afterwards."""
    no_aws(monkeypatch)
    client = _install_memory(monkeypatch, _RecordingMemoryClient())
    visits: list[str] = []
    nodes = _stub_nodes(visits, _review(visits, passed=True))

    async def failing_code(state: TaskState) -> dict[str, Any]:
        visits.append("code")
        raise RuntimeError("the coder produced no diff")

    nodes["code"] = failing_code

    with pytest.raises(RuntimeError, match="produced no diff"):
        await _run_instrumented(nodes, _memory_state())

    assert visits == ["checkout", "plan", "code"], (
        "the record is written and then the exception is re-raised, so `run_task`'s caller "
        "still sees the failure and the graph still stops where it stopped"
    )
    texts = _texts(client)
    assert len(texts) == len(visits)
    assert texts[-1].startswith("code failed")
    assert "the coder produced no diff" in texts[-1], (
        "the exception text is half the signal on this path -- the same two things the "
        "`FAILED` status write beside it carries -- and a future run reading 'code failed' "
        "with no reason has learned nothing"
    )


async def test_a_memory_write_that_fails_does_not_fail_the_node(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """D11 through the wrapper: a throttled `CreateEvent` is a warning, not a dead run."""
    no_aws(monkeypatch)
    _install_memory(monkeypatch, _ThrottledMemoryClient())
    visits: list[str] = []

    with caplog.at_level(logging.WARNING, logger=_MEMORY_LOGGER):
        result = await _run_instrumented(
            _stub_nodes(visits, _review(visits, passed=True)), _memory_state()
        )

    assert result["currentNode"] == "submit", (
        "a run whose code, verification and pull request all succeeded must not die because a "
        "memory write failed -- which is why `_instrument` adds no second swallow of its own: "
        "`agentcore_memory` already degrades every AWS failure to a warning and a `False`"
    )
    assert visits == ["checkout", "plan", "code", "verify", "review", "submit"]
    assert "could not record a memory event" in caplog.text, (
        "and it is not silent either: the drop is logged by the layer that made the call, so a "
        "reader can tell a failed write from a task that had no memory to write to"
    )


def test_every_node_has_a_status() -> None:
    """Invariant 13: every node maps to a status, and every status is in the vocabulary."""
    assert set(graph._NODE_STATUS) == set(graph.DEFAULT_NODES)
    assert set(graph._NODE_STATUS.values()) <= set(get_args(TaskStatus))
    # Distinct, because the console renders the status as the step the task is on: two
    # nodes sharing one status is a progress bar that stops moving.
    assert len(set(graph._NODE_STATUS.values())) == len(graph._NODE_STATUS)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        # `reviewRerun` first: on the hop, `verifyResult` still describes the tree as it
        # was before the review role edited it, green or not.
        ({"reviewRerun": True, "verifyResult": {"passed": True}, "iteration": 0}, "verify"),
        (
            {"reviewRerun": True, "verifyResult": {"passed": False}, "iteration": MAX_ITERATIONS},
            "verify",
        ),
        # Then the verdict, which stops the loop whatever the budget says.
        (
            {"reviewRerun": False, "verifyResult": {"passed": True}, "iteration": MAX_ITERATIONS},
            "submit",
        ),
        # Then the budget.
        ({"verifyResult": {"passed": False}, "iteration": 0}, "plan"),
        ({"verifyResult": {"passed": False}, "iteration": MAX_ITERATIONS - 1}, "plan"),
        ({"verifyResult": {"passed": False}, "iteration": MAX_ITERATIONS}, "submit"),
        # A state with nothing measured is not a pass: it re-plans while it still can.
        ({}, "plan"),
    ],
)
def test_after_review_checks_the_rerun_before_the_verdict(state: TaskState, expected: str) -> None:
    """The order of `_after_review`'s three checks, pinned one row at a time."""
    assert graph._after_review(state) == expected


def test_the_node_walk_in_local_run_exercises_no_conditional_edge() -> None:
    """`local_run` compiles no graph and hard-codes the one hop, so it is not evidence
    about routing -- which is why every routing assertion in this repository is here."""
    source = inspect.getsource(local_run)
    # `_walk`'s comment at the hop deliberately *names* `graph._after_review` to point a
    # reader at the router that does the real thing, so searching the raw text would fail
    # on prose. Drop the comments and search the code.
    code = " ".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type != tokenize.COMMENT
    )
    assert "build_graph" not in code
    assert "_after_review" not in code
    # No `graph` in the module's namespace at all: the router is not merely unused here,
    # it is unreachable, so no future edit to `_walk` can accidentally start agreeing
    # with production while the CLI's printed hop stays the thing a developer trusts.
    assert not hasattr(local_run, "graph")
    # The one hop, hard-coded in the walk itself. `--no-submit` can therefore print a hop
    # the real router would not take (or miss one it would), which is the whole cost the
    # module's own docstring admits to.
    assert 'state.get("reviewRerun")' in inspect.getsource(local_run._walk)

    # And what `local_run` *is*: the six real node functions, imported directly, with no
    # wrapper between the CLI and the node. That is the half of the split this module does
    # cover, and it is why `tests/test_agent_nodes.py` drives the nodes and not the CLI.
    assert local_run.checkout is checkout_module.checkout
    assert local_run.plan is plan_module.plan
    assert local_run.code is code_module.code
    assert local_run.verify is verify_module.verify
    assert local_run.review is review_module.review
    assert local_run.submit is submit_module.submit


async def test_run_task_resolves_the_memory_id_once_and_puts_it_on_the_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The memory id is resolved once per task, in `run_task`, and every node sees it."""
    no_aws(monkeypatch)
    # After `no_aws`, which deletes it: `run_task` derives the checkpoint path from
    # `WORK_ROOT` before the first node runs, so it has to point at this test's tmp_path.
    monkeypatch.setenv("WORK_ROOT", str(tmp_path / "work"))
    calls: list[str] = []
    visits: list[str] = []
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        graph.agentcore_memory, "resolve_memory_id", _recording_resolver(calls, "mem-abc")
    )
    # `run_task` builds its own graph and takes no `nodes` argument, but `build_graph`
    # reads this module global at call time -- which is why patching it is enough.
    monkeypatch.setattr(graph, "DEFAULT_NODES", _observed_nodes(visits, seen))

    payload = invoke_payload.required_only(taskId="t-memory-cold", configId="cfg-1")
    await graph.run_task(payload)

    assert visits == ["checkout", "plan", "code", "verify", "review", "submit"]
    assert calls == ["cfg-1"], (
        "one `GetItem` per task and not one per node visit: the memory id cannot change "
        "mid-task, so resolving it in a node would buy six reads of the same value -- and "
        "resolving it in `build_options` would cost `build_options` its purity"
    )
    assert [state.get("memoryId") for state in seen] == ["mem-abc"] * len(visits), (
        "every node runs with the memory id on its state, because `subagents.build_options` "
        "reads it off the state to decide whether the role gets a `memory` server at all"
    )


async def test_run_task_leaves_memory_id_off_the_state_when_the_repository_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository with no memory resource carries no `memoryId` key at all."""
    no_aws(monkeypatch)
    monkeypatch.setenv("WORK_ROOT", str(tmp_path / "work"))
    calls: list[str] = []
    visits: list[str] = []
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(graph.agentcore_memory, "resolve_memory_id", _recording_resolver(calls, ""))
    monkeypatch.setattr(graph, "DEFAULT_NODES", _observed_nodes(visits, seen))

    payload = invoke_payload.required_only(taskId="t-memory-none", configId="cfg-1")
    await graph.run_task(payload)

    assert calls == ["cfg-1"]
    assert visits == ["checkout", "plan", "code", "verify", "review", "submit"]
    assert all("memoryId" not in state for state in seen), (
        "absence is the signal `agentcore_memory.TaskMemory.for_state` reads, so a falsy "
        "`memoryId` written into the checkpointed state would be a second spelling of the "
        "same thing -- and every reader would then have to know both"
    )


async def test_a_resume_re_asserts_the_memory_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task whose first attempt predates the memory resource picks it up on the resume."""
    no_aws(monkeypatch)
    monkeypatch.setenv("WORK_ROOT", str(tmp_path / "work"))
    calls: list[str] = []
    visits: list[str] = []
    seen: list[dict[str, Any]] = []
    # `""` on the first call and a memory id on the second: the repository was registered
    # with a memory resource in between the two attempts.
    monkeypatch.setattr(
        graph.agentcore_memory, "resolve_memory_id", _recording_resolver(calls, "", "mem-abc")
    )
    nodes = _observed_nodes(visits, seen)
    observed_verify = nodes["verify"]
    reaped = {"pending": True}

    async def reaped_verify(state: TaskState) -> dict[str, Any]:
        """Die once, so the second `run_task` finds a checkpoint and takes the resume branch."""
        if reaped["pending"]:
            reaped["pending"] = False
            visits.append("verify")
            raise RuntimeError("the container was reaped")
        return await observed_verify(state)

    nodes["verify"] = reaped_verify
    monkeypatch.setattr(graph, "DEFAULT_NODES", nodes)

    payload = invoke_payload.required_only(taskId="t-memory-resume", configId="cfg-1")
    with pytest.raises(RuntimeError, match="reaped"):
        await graph.run_task(payload)
    assert visits == ["checkout", "plan", "code", "verify"]
    assert all("memoryId" not in state for state in seen), (
        "the first attempt ran against a repository that had no memory resource yet"
    )

    seen.clear()
    final = await graph.run_task(payload)

    assert calls == ["cfg-1", "cfg-1"], "resolved once per attempt, off the payload's configId"
    assert visits == ["checkout", "plan", "code", "verify", "verify", "review", "submit"]
    assert seen, "the resume re-entered the graph, so there are states to assert on"
    assert [state.get("memoryId") for state in seen] == ["mem-abc"] * len(seen), (
        "the resume branch re-asserts the memory id alongside `resumed`, so a task that "
        "was checkpointed before its repository had a memory resource gains one on the "
        "way back in rather than running the rest of its nodes memoryless"
    )
    assert final["resumed"] is True
    assert final["memoryId"] == "mem-abc"


def test_the_memory_id_is_resolved_outside_the_nodes() -> None:
    """The one resolution point, asserted at the source level because it is an absence.

    In the idiom of `tests/test_agent_store.py`'s invariant 31 test: no test can catch a
    `get_configuration` that nobody happened to call, so the property that no node reads
    the configuration item is checked by reading the nodes.
    """
    package = Path(inspect.getsourcefile(graph) or "")
    node_sources = sorted((package.parent / "nodes").glob("*.py"))
    assert node_sources, "the node package was not found, so this assertion checked nothing"
    for path in node_sources:
        source = path.read_text(encoding="utf-8")
        for name in ("get_configuration", "resolve_memory_id", "memoryId"):
            assert name not in source, (
                f"{path.name} names `{name}`: the memory id is resolved once in "
                "`graph.run_task` and read off the state by nobody but "
                "`subagents.build_options`. A per-node read would be one `GetItem` per "
                "node visit for a value that cannot change mid-task."
            )

    assert package.read_text(encoding="utf-8").count("resolve_memory_id(") == 1, (
        "exactly one call site in `graph.py`, in `run_task`: a second one would be a "
        "second read of the configuration item on the critical path before the first node"
    )
