"""The loop end to end: the **compiled** graph, the **real** nodes, a **real** git repository.

Every other agent test sits on one side of a seam this file is here to cross. `test_graph.py`
compiles the graph but out of `_stub_nodes`, so it exercises the real edges against fake work;
`test_agent_nodes.py` runs the real nodes one at a time with `subagents.run_role` patched, so it
exercises real work against no edges. Both are the right shape for what they check, and between
them the real nodes had never been driven through the real routing -- which is where the
node-to-node contracts live: what `plan` leaves on disk for `code` to be measured against, what
`code` leaves for `verify` to run over, what `review` reads back, and which of the three branches
out of `review` a given state actually takes.

**Why this needs no AWS, and no network.** Four off-switches that already exist for their own
reasons, none of them added for tests:

- **DynamoDB** -- `store._table` returns `None` when its variable is unset and every writer
  returns early, so with `TASKS_TABLE` and `CONFIG_TABLE` absent no client is built. That is the
  same path `local_run` takes, and `agent_helpers.no_aws` is the switch.
- **The GitHub credential** -- `store.github_token` returns `""` and builds no client when
  `CONFIG_TABLE` is unset, which is the same switch the DynamoDB writers use. `submit` reads
  `GITHUB_TOKEN` from the environment first, which is the local path and the only one that can
  produce a token here.
- **Bedrock** -- `subagents.run_role` is the one call that reaches a model, and `RoleRecorder`
  replaces it. Its `side_effect` is what makes this a test of the nodes rather than of a mock:
  the stand-in *writes the files a real role would write*, so every node still reads its input
  back off the filesystem through the real code path.
- **AgentCore Memory** -- `TaskMemory.enabled` needs a `memoryId`, and D11 builds the client
  inside the call rather than at import. With no `memoryId` nothing is constructed.

**github.com** is reached by exactly one node, and it cannot be redirected at a local remote.
`git_ops.push` rewrites an origin it recognises as local to `https://github.com/<repository>.git`
on purpose -- that is what makes `local_run` clone from a directory and still publish to GitHub --
so a bare repository in `tmp_path` is not a substitute for the real remote and never will be. The
seam that *is* offline is one layer down: `git_ops._run_authenticated` is the single function
every network git command goes through, so stubbing it records the argv while leaving `push`'s own
logic -- the remote rewrite, the `HEAD:refs/heads/<branch>` refspec -- running for real. The two
tests below therefore split as: one stops at `noSubmit`, the flag `local_run --no-submit` already
uses; the other runs `submit` with the wire stubbed and asserts what `push` *would* have sent.

What this deliberately does **not** cover: the prompts, the Claude Code CLI, the hooks, the MCP
servers, `ALLOWED_TOOLS`. All of those live *below* `run_role`, and asserting them needs a real
session against Bedrock. `CLAUDE.md`'s "What was probed against the pinned SDK" section is the
record of those having been measured by hand; nothing here re-measures them.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from agent_helpers import (
    FakeRoleResult,
    RoleRecorder,
    make_git_repo,
    no_aws,
    trust,
)
from coding_agent import graph as graph_module
from coding_agent.nodes import code as code_module
from coding_agent.nodes import plan as plan_module
from coding_agent.nodes import review as review_module
from coding_agent.nodes import submit as submit_module

# Long enough to be a real session id (invariant 18 wants >= 33 characters) and fixed rather than
# generated, because the checkpoint thread id is the task id and a test that wants to read a
# checkpoint back has to know what it was called.
TASK_ID = "t-end-to-end-0000000000000000000000000"

# `plan` refuses a specification under `_MIN_SPEC_CHARS` (200), so the stand-in planner has to
# write something of a plausible length. The content is irrelevant to every assertion here; the
# length is not, and a shorter string fails inside the node with a message about the spec rather
# than about whatever the test was actually checking.
SPEC_TEXT = (
    "# Specification\n\n"
    "Make the failing check pass by correcting the value `answer.py` returns.\n\n"
    "## Changed paths\n\n- answer.py\n\n"
    "## Approach\n\n"
    + (
        "The repository ships a deliberately wrong constant and a check that asserts the "
        "right one. Correct the constant. Nothing else in the tree needs to move, and the "
        "check is the whole acceptance criterion for this task. "
    )
    * 2
)

# A repository whose own `make test` genuinely fails until `answer.py` is corrected, and whose
# `make setup` genuinely runs. Both are real recipes: `verify` shells out to them through
# `commands._run`, so a `@true` stand-in would make the exit code this test asserts meaningless.
WRONG = "ANSWER = 41\n"
RIGHT = "ANSWER = 42\n"
CHECK = (
    "import answer\nassert answer.ANSWER == 42, f'expected 42, got {answer.ANSWER}'\nprint('ok')\n"
)
MAKEFILE = "setup:\n\t@true\n\ntest:\n\t@python3 check.py\n"
# `make test` imports `answer.py`, so CPython writes `__pycache__/` into the clone. Without this
# the bytecode lands in `git status` and `code`'s second guard warns about a path the
# specification did not name -- a real warning about an artefact of the test, which is the kind of
# noise that gets an assertion loosened later for the wrong reason.
GITIGNORE = "__pycache__/\n*.pyc\n"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No AWS, no host configuration, and a work root inside `tmp_path`.

    `WORK_ROOT` is set *after* `no_aws` clears it: the order matters, because `no_aws` deletes
    every variable `config.py` reads and would otherwise take this one with it.
    """
    no_aws(monkeypatch)
    work_root = tmp_path / "work"
    work_root.mkdir()
    trust(work_root)
    monkeypatch.setenv("WORK_ROOT", str(work_root))
    yield


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A real bare repository to clone from, so `checkout` does a real clone over the filesystem.

    Bare rather than a plain worktree because `submit` pushes to it in the second test, and a
    push to a non-bare repository's checked-out branch is refused by git itself.
    """
    source = make_git_repo(
        tmp_path / "source",
        files={
            "answer.py": WRONG,
            "check.py": CHECK,
            "Makefile": MAKEFILE,
            ".gitignore": GITIGNORE,
        },
    )
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", "--quiet", str(source), str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    trust(bare)
    return bare


def _state(origin_path: Path, **overrides: Any) -> dict[str, Any]:
    """The cold-start state, with the field that keeps this offline left off.

    No `memoryId`, so no memory client: `TaskMemory.enabled` branches on it, and spelling it as
    absent rather than empty is the point -- absence is the documented disabled signal rather
    than a second falsy spelling of it. The GitHub credential needs nothing on the state at all
    now; it is keyed on `configId` and gated on `CONFIG_TABLE`, which this suite never sets.
    """
    return {
        "taskId": TASK_ID,
        "configId": "cfg_end_to_end",
        "repository": "example/end-to-end",
        "cloneSource": str(origin_path),
        "description": "correct the constant so the check passes",
        "iteration": 0,
        **overrides,
    }


def _planner(repo_root: Path) -> Any:
    """A `side_effect` that writes what each role really writes, and nothing more.

    This is the whole reason the test is worth more than a mock: `plan` reads its own
    specification back off disk, `code`'s no-op guard reads `git_ops.changed_paths`, and `review`
    reads its artefact back. A stand-in that only returned text would leave all three reading an
    empty tree and every one of them would raise.
    """

    def side_effect(role: str, _prompt: str, cwd: Path | None) -> None:
        repo = Path(cwd) if cwd is not None else repo_root
        if role == "plan":
            # `.agent/spec/iteration-<n>.md` is the one path `plan` both writes and reads.
            for iteration in (0, 1):
                target = plan_module.spec_path(repo, iteration)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(SPEC_TEXT, encoding="utf-8")
        elif role == "code":
            # The source edit. This is what makes the real `make test` in `verify` flip from a
            # non-zero exit code to zero, so the exit code this test asserts is measured and not
            # injected.
            (repo / "answer.py").write_text(RIGHT, encoding="utf-8")
        elif role == "review":
            for iteration in (0, 1):
                for path in (
                    review_module.review_path(repo, iteration),
                    review_module.security_review_path(repo, iteration),
                    review_module.code_review_path(repo, iteration),
                ):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("# Review\n\nVERDICT: PASS\n", encoding="utf-8")

    return side_effect


def _recorder(repo_root: Path) -> RoleRecorder:
    """`run_role` for all four roles. `review` returns the `VERDICT:` line the node parses.

    A missing `VERDICT:` line is a veto (`review._verdict`), so the review result has to carry
    one explicitly -- the default `FakeRoleResult` deliberately puts `VERDICT: FAIL` in `text`
    and not in `final_text`, which is invariant 7's trap and would veto this run.
    """
    return RoleRecorder(
        results={
            "plan": FakeRoleResult(final_text="wrote the specification"),
            "code": FakeRoleResult(final_text="corrected the constant"),
            "review": FakeRoleResult(final_text="Looks correct.\n\nVERDICT: PASS\n"),
        },
        default=FakeRoleResult(final_text="done"),
        side_effect=_planner(repo_root),
    )


def _patch_roles(monkeypatch: pytest.MonkeyPatch, recorder: RoleRecorder) -> None:
    """Patch `run_role` on every node module that calls it.

    Each node reaches it through its own module object (`<node>.subagents.run_role`), which is
    the convention that makes this seam patchable at all -- so every module has to be patched,
    and a node added later without being listed here runs a real session.
    """
    for module in (plan_module, code_module, review_module):
        monkeypatch.setattr(module.subagents, "run_role", recorder)


async def test_the_whole_loop_runs_on_the_real_nodes_and_the_real_edges(
    monkeypatch: pytest.MonkeyPatch, origin: Path, tmp_path: Path
) -> None:
    """checkout -> plan -> code -> verify -> review -> submit, compiled, with `noSubmit` set.

    The assertions are on observable facts rather than on calls: a branch that exists in a real
    clone, a diff that names one file, and an exit code produced by a `make test` that really
    ran. `noSubmit` stops before the push and the pull request, which is `submit`'s own flag and
    the only part of the loop that would reach github.com.
    """
    repo_root = tmp_path / "work" / TASK_ID / "repo"
    recorder = _recorder(repo_root)
    _patch_roles(monkeypatch, recorder)

    compiled = graph_module.build_graph()
    final = await compiled.ainvoke(
        _state(origin, noSubmit=True), graph_module.graph_config(TASK_ID)
    )

    # The routing: every node visited once, in order, with no re-plan and no verify re-run.
    assert recorder.roles == ["plan", "code", "review", "review", "review"], (
        "expected one planning session, one coding session and three review sessions -- the "
        f"review node runs `/security-review` and `/code-review` beside its own. Got "
        f"{recorder.roles}"
    )

    # The command detection in `checkout` read the repository's own Makefile rather than guessing.
    assert final["verifyCommand"] == "make test"
    assert final["setupCommand"] == "make setup"

    # The verification is a real measurement: `make test` ran `python3 check.py`, which imports
    # the file the coding role edited.
    assert final["commandResult"]["exitCode"] == 0, (
        f"`make test` should pass once the constant is corrected; got {final['commandResult']}"
    )
    assert final["verifyResult"]["passed"] is True
    assert final["verifyResult"]["verified"] is True, (
        "a real command ran and was judged green, so `verified` is true -- this is the one "
        "field with a single writer (invariant 6)"
    )
    assert final["verifyResult"]["vetoed"] is False

    # The clone is real, on the branch `checkout` created, and the diff names only the source file
    # the coding role touched. `.agent/` is excluded by `has_source_changes`, which is invariant 5.
    assert final["branch"] == f"agent/{TASK_ID}"
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == f"agent/{TASK_ID}"
    assert (repo_root / "answer.py").read_text(encoding="utf-8") == RIGHT
    assert final["codeChangedPaths"] == ["answer.py"], (
        "the coding role's changed paths should name the source edit and not the `.agent/` "
        f"artefacts the planner wrote before it. Got {final['codeChangedPaths']}"
    )

    # `noSubmit` means the loop still reaches a terminal status -- it just does not publish.
    assert not final.get("prUrl")


async def test_a_failing_check_routes_back_to_plan_and_the_second_attempt_submits(
    monkeypatch: pytest.MonkeyPatch, origin: Path, tmp_path: Path
) -> None:
    """The `review -> plan` edge, driven by a command that really fails the first time.

    This is the branch `_after_review` takes when `verifyResult["passed"]` is false and the
    iteration budget is not spent, and it is the one that cannot be reached without a real
    command: the routing depends on an exit code, and the exit code depends on whether the file
    on disk has been corrected yet. The coding role here corrects it only on its second session,
    so the first `make test` genuinely fails.
    """
    repo_root = tmp_path / "work" / TASK_ID / "repo"
    sessions: list[str] = []

    def side_effect(role: str, prompt: str, cwd: Path | None) -> None:
        sessions.append(role)
        repo = Path(cwd) if cwd is not None else repo_root
        _planner(repo_root)(role, prompt, cwd)
        if role == "code" and sessions.count("code") == 1:
            # A first attempt that changes something outside `.agent/` -- so the no-op guard is
            # satisfied -- but does not fix the constant, so `make test` still exits non-zero.
            (repo / "answer.py").write_text("ANSWER = 41  # still wrong\n", encoding="utf-8")

    recorder = RoleRecorder(
        results={
            "plan": FakeRoleResult(final_text="wrote the specification"),
            "code": FakeRoleResult(final_text="edited the constant"),
            "review": FakeRoleResult(final_text="Reads correct now.\n\nVERDICT: PASS\n"),
        },
        default=FakeRoleResult(final_text="done"),
        side_effect=side_effect,
    )
    _patch_roles(monkeypatch, recorder)

    def fake_pull_request(*_args: Any, **_kwargs: Any) -> str:
        return "https://example.invalid/pr/1"

    monkeypatch.setattr(submit_module.github, "create_pull_request", fake_pull_request)

    # The wire, and only the wire. `push`'s own logic still runs: it reads `origin`, recognises
    # the clone source as local, rewrites the remote, and composes the refspec -- all of which
    # this records instead of sending.
    pushes: list[tuple[list[str], str | None]] = []

    def fake_wire(args: Any, *, cwd: Path, token: str | None) -> None:
        # part of the signature being stood in for; dropping it would let a caller that stopped
        # passing it go unnoticed here.
        pushes.append((list(args), token))

    monkeypatch.setattr(submit_module.git_ops, "_run_authenticated", fake_wire)

    compiled = graph_module.build_graph()
    final = await compiled.ainvoke(
        _state(origin, **{"noSubmit": False}), graph_module.graph_config(TASK_ID)
    )

    # The second pass through the plan node runs the **`replan`** role, not `plan` -- a different
    # prompt (`REPLAN_PROMPT`), which queries memory on the *failure* rather than on the task. The
    # whole sequence is asserted rather than a count, because the ordering is the contract: a
    # failed verification goes back to `plan` and never straight to `code`, since re-running the
    # coder against the spec that just failed is what that edge exists to prevent.
    assert recorder.roles == [
        "plan",
        "code",
        "review",
        "review",
        "review",
        "replan",
        "code",
        "review",
        "review",
        "review",
    ], f"unexpected role sequence: {recorder.roles}"
    assert final["iteration"] >= 1

    # The second attempt was measured green, and `submit` published it.
    assert final["verifyResult"]["passed"] is True
    assert final["verifyResult"]["verified"] is True
    assert final["prUrl"] == "https://example.invalid/pr/1"

    # `submit` pushes before it opens the pull request, and it pushed the right refspec.
    assert pushes, "`submit` reached `create_pull_request` without pushing anything first"
    args, _token = pushes[0]
    assert args == ["push", "--set-upstream", "origin", f"HEAD:refs/heads/agent/{TASK_ID}"], (
        f"unexpected push argv: {args}"
    )

    # And it rewrote the local clone source to the GitHub URL before pushing -- the behaviour that
    # makes a local clone publishable, and the reason a bare repository in `tmp_path` cannot stand
    # in for the remote. The PAT is never in that URL (invariant 4).
    origin_url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(tmp_path / "work" / TASK_ID / "repo"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin_url == "https://github.com/example/end-to-end.git", (
        f"push should have rewritten the local origin to the GitHub URL; got {origin_url}"
    )
    assert "@" not in origin_url, "a credential in the remote URL outlives the push (invariant 4)"


async def test_nothing_in_the_loop_builds_an_aws_client(
    monkeypatch: pytest.MonkeyPatch, origin: Path, tmp_path: Path
) -> None:
    """The offline claim, asserted rather than assumed.

    Every off-switch this file relies on is a branch in production code that a later change could
    quietly remove -- and the symptom would be this suite trying to reach AWS from a developer
    machine, failing on credentials, and reading as a broken test rather than as a lost guard. So
    `boto3.client` is replaced by something that raises: if any of the four paths starts building
    a client, the run stops here with a message naming the service.
    """
    import boto3

    def forbidden(service: str, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            f"the loop built a boto3 client for {service!r}. With no TASKS_TABLE, no "
            "CONFIG_TABLE and no memoryId, every AWS path is supposed to be "
            "switched off -- see this module's docstring for the four of them."
        )

    monkeypatch.setattr(boto3, "client", forbidden)
    recorder = _recorder(tmp_path / "work" / TASK_ID / "repo")
    _patch_roles(monkeypatch, recorder)

    compiled = graph_module.build_graph()
    final = await compiled.ainvoke(
        _state(origin, noSubmit=True), graph_module.graph_config(TASK_ID)
    )
    assert final["verifyResult"]["verified"] is True
