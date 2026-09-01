"""The checkpointer exists so a task killed at hour two resumes instead of restarting.

Every round-trip here is driven through a **compiled graph** rather than through
`FileCheckpointer`'s raw `put`/`get_tuple`, because the graph's call pattern -- the
interleaving of `put`, `put_writes` and the blob versions behind them -- is the one that
has to survive a new container. A test that only exercised the saver's own API would keep
passing while every real resume came back empty.

The nodes are six trivial coroutines: `build_graph(nodes=...)` merges over
`DEFAULT_NODES`, so nothing here needs Bedrock, git or a network.
"""

from __future__ import annotations

import pickle  # nosec B403 -- reads back a file this test wrote a line earlier
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import invoke_payload
import pytest
from agent_helpers import no_aws, write
from coding_agent import checkpoint, graph
from coding_agent.state import TaskState

_TASK_ID = "task-checkpoint-1"

# Recognisable values: each is written by a different node, so a partial round-trip
# (checkpoint without pending writes, say) shows up as one missing key rather than as an
# empty state.
_BRANCH = "agent/task-checkpoint-1"
_SPEC = "the spec a resume must not have to re-plan"
_PR_URL = "https://example.invalid/acme/widgets/pull/7"


def _initial_state() -> TaskState:
    """A fresh cold-start state. A function, not a constant, so no test can feed the
    next one a state the graph already merged into."""
    return {
        "taskId": _TASK_ID,
        "configId": "cfg-1",
        "repository": "acme/widgets",
        "description": "make the tests pass",
        "iteration": 0,
        "reviewRerun": False,
        "resumed": False,
    }


def _stub_nodes(work_dir: Path) -> dict[str, graph.NodeFn]:
    """The six nodes as trivial coroutines that write recognisable state.

    `review` reports `passed`, so `_after_review` routes straight to `submit` and the run
    is six supersteps rather than a loop.
    """

    async def checkout(state: TaskState) -> dict[str, Any]:
        return {
            "workDir": str(work_dir),
            "repoDir": str(work_dir / "repo"),
            "branch": _BRANCH,
        }

    async def plan(state: TaskState) -> dict[str, Any]:
        return {"specText": _SPEC, "iteration": state.get("iteration", 0) + 1}

    async def code(state: TaskState) -> dict[str, Any]:
        return {"codeChangedPaths": ["agent/src/coding_agent/graph.py"]}

    async def verify(state: TaskState) -> dict[str, Any]:
        return {
            "commandResult": {
                "command": "make test",
                "exitCode": 0,
                "output": "ok",
                "warning": None,
            }
        }

    async def review(state: TaskState) -> dict[str, Any]:
        return {"verifyResult": {"passed": True, "verified": True}}

    async def submit(state: TaskState) -> dict[str, Any]:
        return {"prUrl": _PR_URL, "status": "SUCCEEDED"}

    return {
        "checkout": checkout,
        "plan": plan,
        "code": code,
        "verify": verify,
        "review": review,
        "submit": submit,
    }


def _recorded_nodes(work_dir: Path, visits: list[str]) -> dict[str, graph.NodeFn]:
    """`_stub_nodes`, with every node also appending its own name to `visits`.

    A resume is only worth anything if it re-enters at the node that died, so the
    assertions the two tests below need are on the *sequence of visits* rather than on
    anything a node computed -- a state key can be restored from the checkpoint while the
    run still re-clones and re-plans on the way back to it.
    """

    def _recorded(name: str, fn: graph.NodeFn) -> graph.NodeFn:
        async def node(state: TaskState) -> dict[str, Any]:
            visits.append(name)
            return await fn(state)

        return node

    return {name: _recorded(name, fn) for name, fn in _stub_nodes(work_dir).items()}


async def _run_stub_graph(saver: checkpoint.FileCheckpointer, work_dir: Path) -> dict[str, Any]:
    """Run the stub graph to completion against `saver`, and return the final state."""
    compiled = graph.build_graph(nodes=_stub_nodes(work_dir), instrument=False, checkpointer=saver)
    return await compiled.ainvoke(_initial_state(), graph.graph_config(_TASK_ID))


async def test_a_checkpoint_survives_a_new_process_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """State written through one `FileCheckpointer` is readable by a second one on the same path."""
    no_aws(monkeypatch)
    work_dir = tmp_path / "work" / _TASK_ID
    path = checkpoint.checkpoint_path({"workDir": str(work_dir)})
    run_config = graph.graph_config(_TASK_ID)

    saver = checkpoint.FileCheckpointer(path)
    final = await _run_stub_graph(saver, work_dir)
    assert final["branch"] == _BRANCH
    assert final["prUrl"] == _PR_URL

    # A second object on the same path stands in for the next container: same file, and
    # not one dict shared with the first.
    resumed = checkpoint.FileCheckpointer(path)
    assert resumed is not saver
    assert resumed.storage is not saver.storage
    assert resumed.get_tuple(run_config) is not None

    rebuilt = graph.build_graph(nodes=_stub_nodes(work_dir), instrument=False, checkpointer=resumed)
    snapshot = await rebuilt.aget_state(run_config)
    assert snapshot.values["branch"] == _BRANCH
    assert snapshot.values["specText"] == _SPEC
    assert snapshot.values["prUrl"] == _PR_URL
    # Nothing left to run: the resume knows the task finished, which is what stops a
    # reaped-then-restarted task from re-cloning and re-planning.
    assert snapshot.next == ()


async def test_the_write_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pickle is renamed into place and leaves no temp file, so a reader never sees half a checkpoint."""
    no_aws(monkeypatch)
    work_dir = tmp_path / "work" / _TASK_ID
    path = checkpoint.checkpoint_path({"workDir": str(work_dir)})

    saver = checkpoint.FileCheckpointer(path)
    await _run_stub_graph(saver, work_dir)

    assert path.exists()
    payload = pickle.loads(path.read_bytes())  # nosec B301 -- the checkpoint this test just wrote
    assert set(payload) == {"storage", "writes", "blobs"}
    assert list(payload["storage"]) == [_TASK_ID]
    # The durable file is the only file: the sibling temp file was renamed, not left.
    assert [entry.name for entry in sorted(path.parent.iterdir())] == [path.name]

    durable = path.read_bytes()

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise OSError("no space left on device")

    # A failed write must leave the last good checkpoint exactly as it was, and must not
    # litter the capped mount with a half-written temp file.
    monkeypatch.setattr(
        checkpoint,
        "pickle",
        SimpleNamespace(dump=_explode, HIGHEST_PROTOCOL=pickle.HIGHEST_PROTOCOL),
    )
    saver.delete_thread("some-other-task")

    assert path.read_bytes() == durable
    assert [entry.name for entry in sorted(path.parent.iterdir())] == [path.name]


async def test_a_missing_or_corrupt_file_is_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing or unreadable checkpoint is a cold start, never an exception: losing a resume must not kill the task."""
    no_aws(monkeypatch)
    run_config = graph.graph_config(_TASK_ID)

    missing = tmp_path / "cold" / "checkpoint" / "graph.pkl"
    cold = checkpoint.FileCheckpointer(missing)
    # The directory is created eagerly (the first `_dump` must not fail on it); the file
    # is not.
    assert missing.parent.is_dir()
    assert not missing.exists()
    assert not dict(cold.storage)
    assert cold.get_tuple(run_config) is None

    garbage = write(
        tmp_path / "corrupt" / "checkpoint" / "graph.pkl", "not a pickle, a bag of bytes\n"
    )
    salvaged = checkpoint.FileCheckpointer(garbage)
    assert not dict(salvaged.storage)
    assert not dict(salvaged.writes)
    assert not dict(salvaged.blobs)
    assert salvaged.get_tuple(run_config) is None

    # And it is still a working checkpointer. `_load` clears the live `defaultdict`s
    # rather than replacing them, so the run that could not resume can still checkpoint
    # -- otherwise one corrupt file would cost every resume after it too.
    final = await _run_stub_graph(salvaged, tmp_path / "corrupt")
    assert final["prUrl"] == _PR_URL
    assert list(pickle.loads(garbage.read_bytes())["storage"]) == [_TASK_ID]  # nosec B301 -- the checkpoint this test just wrote


def test_the_path_is_derived_from_the_work_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checkpoint lives under the task's `workDir`, so it travels with the workspace on the session-storage mount."""
    no_aws(monkeypatch)
    work_dir = Path("/mnt/session") / "work" / _TASK_ID
    path = checkpoint.checkpoint_path({"workDir": str(work_dir)})

    assert path.is_relative_to(work_dir)
    assert path.suffix == ".pkl"
    # Beside the clone and not inside it: nothing a coding role does to the repository,
    # `git clean -xfd` included, may destroy the record of how far the task got.
    assert not path.is_relative_to(work_dir / "repo")
    # One work directory, one checkpoint: two tasks sharing a file would resume as each
    # other.
    assert checkpoint.checkpoint_path({"workDir": "/mnt/session/work/other"}) != path


async def test_deleting_a_thread_clears_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`delete_thread` forgets the thread on disk too, or the next `_load` would resurrect it."""
    no_aws(monkeypatch)
    work_dir = tmp_path / "work" / _TASK_ID
    path = checkpoint.checkpoint_path({"workDir": str(work_dir)})
    run_config = graph.graph_config(_TASK_ID)

    saver = checkpoint.FileCheckpointer(path)
    await _run_stub_graph(saver, work_dir)
    assert checkpoint.FileCheckpointer(path).get_tuple(run_config) is not None

    saver.delete_thread(_TASK_ID)

    payload = pickle.loads(path.read_bytes())  # nosec B301 -- the checkpoint this test just wrote
    assert _TASK_ID not in payload["storage"]
    assert not [key for key in payload["writes"] if key[0] == _TASK_ID]
    assert not [key for key in payload["blobs"] if key[0] == _TASK_ID]
    assert checkpoint.FileCheckpointer(path).get_tuple(run_config) is None


async def test_a_run_that_stopped_mid_graph_re_enters_at_the_node_it_stopped_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container reaped inside `verify` comes back at `verify`, not at `checkout`."""
    no_aws(monkeypatch)
    work_dir = tmp_path / "work" / _TASK_ID
    path = checkpoint.checkpoint_path({"workDir": str(work_dir)})
    cfg = graph.graph_config(_TASK_ID)

    visits: list[str] = []
    nodes = _recorded_nodes(work_dir, visits)
    # The bare stub, not `nodes["verify"]`: the replacement below does its own recording,
    # so delegating to the wrapped one would count the resumed visit twice.
    measured = _stub_nodes(work_dir)["verify"]
    # A dict flag rather than a counter on the closure: the second graph object is built
    # from the same node table, so "the first call" spans two `ainvoke`s.
    reaped = {"pending": True}

    async def reaped_verify(state: TaskState) -> dict[str, Any]:
        visits.append("verify")
        if reaped["pending"]:
            raise RuntimeError("the container was reaped")
        return await measured(state)

    nodes["verify"] = reaped_verify

    compiled = graph.build_graph(
        nodes=nodes, instrument=False, checkpointer=checkpoint.FileCheckpointer(path)
    )
    with pytest.raises(RuntimeError, match="reaped"):
        await compiled.ainvoke(_initial_state(), cfg)
    assert visits == ["checkout", "plan", "code", "verify"]

    # A second `FileCheckpointer` over the same file stands in for the replacement
    # container: the state has to come back off disk, not out of the first object.
    rebuilt = graph.build_graph(
        nodes=nodes, instrument=False, checkpointer=checkpoint.FileCheckpointer(path)
    )
    assert (await rebuilt.aget_state(cfg)).next == ("verify",)

    visits.clear()
    reaped["pending"] = False
    final = await rebuilt.ainvoke(None, cfg)

    # Three visits and not seven: re-entering at `checkout` would re-clone the repository
    # and re-plan the task, which is the entire cost the checkpoint exists to avoid.
    assert visits == ["verify", "review", "submit"]
    # And the work the dead container already did is still on the state, so the resumed
    # `submit` has the branch to push and the spec nobody had to write twice.
    assert final["branch"] == _BRANCH
    assert final["specText"] == _SPEC
    # Deliberately no assertion on `currentNode`: `instrument=False` means `_instrument`
    # never runs and that field is never written.


async def test_run_task_resumes_the_thread_it_finds_rather_than_starting_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production entry point re-enters the thread whose checkpoint it finds.

    And it re-asserts `resumed` on the state it resumes, so every node on the second pass
    can tell a redelivery from a cold start -- which is what keeps a re-run `checkout`
    from quarantining the `.agent/` directory the first attempt wrote.
    """
    no_aws(monkeypatch)
    # After `no_aws`, which deletes it: `run_task` derives the checkpoint path from
    # `WORK_ROOT` before the first node runs, so it has to point at this test's tmp_path.
    monkeypatch.setenv("WORK_ROOT", str(tmp_path / "work"))
    work_dir = tmp_path / "work" / _TASK_ID

    seen: list[tuple[str, Any]] = []
    reaped = {"pending": True}

    def _observed(name: str, fn: graph.NodeFn) -> graph.NodeFn:
        async def node(state: TaskState) -> dict[str, Any]:
            seen.append((name, state.get("resumed")))
            if name == "verify" and reaped["pending"]:
                reaped["pending"] = False
                raise RuntimeError("the container was reaped")
            return await fn(state)

        return node

    stubs = {name: _observed(name, fn) for name, fn in _stub_nodes(work_dir).items()}
    # `run_task` builds its own graph and takes no `nodes` argument, but `build_graph`
    # reads this module global at call time -- which is why patching it is enough.
    monkeypatch.setattr(graph, "DEFAULT_NODES", stubs)

    # A *wire* payload, so the description arrives as `prompt`: `graph._initial_state` is
    # where it becomes `description`, and `tests/invoke_payload.py` is the one definition of
    # the shape (invariant 13b).
    payload = invoke_payload.required_only(taskId=_TASK_ID, configId="cfg-1")

    with pytest.raises(RuntimeError, match="reaped"):
        await graph.run_task(payload)
    # The dispatcher never says `resumed`: it is `run_task`'s own reading of the
    # checkpoint that decides, so a redelivered payload cannot lie about it either way.
    assert "resumed" not in payload
    assert [name for name, _ in seen] == ["checkout", "plan", "code", "verify"]
    assert {flag for _, flag in seen} == {False}

    final = await graph.run_task(payload)

    # `checkout` once across both calls. `run_task` re-asserts `resumed` with
    # `aupdate_state` before resuming precisely so that a `checkout` which *does* re-run
    # will not quarantine the `.agent/` directory we wrote on the first attempt --
    # the spec and the memory the resume exists to reuse.
    assert [name for name, _ in seen].count("checkout") == 1
    assert seen[4:] == [("verify", True), ("review", True), ("submit", True)]
    assert final["resumed"] is True
    assert final["prUrl"] == _PR_URL
    # `instrument=True` is left at its default because that is the production path: with
    # no `TASKS_TABLE` every `store.set_status` returns early, and `conftest._aws_env`'s
    # bogus credentials mean a stray call would fail loudly rather than bill anyone.
