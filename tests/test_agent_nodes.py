"""The six nodes, and the structural one-way veto they exist to make unforgeable.

The shape being pinned here is the whole reason `verify` and `review` are two modules:

- `verify` runs a command and returns a `CommandResult`. It judges **nothing** -- no
  `passed`, no `verified`, no `verifyResult`.
- `review` alone composes `verifyResult`, because it alone holds both the exit code and
  the role's verdict. `passed = exitCode == 0 and not vetoed`, and `verified = passed`
  only on the branch where a real command actually ran.
- `graph._after_review` is the only reader of `passed`; `submit`'s `draft=` and
  `state.terminal_status` key on `verified`.
- `review` digests the tree either side of its own session and hops back to `verify`
  when its own role edited the working tree -- a CONTENT digest, because the likeliest
  shape of that mistake changes no path at all. The hop is one boolean, spent once.
- the pull request body is where an unverified run explains itself, so the draft
  paragraph, the `## Verification` lines and the `## Artefacts` paths are pinned here
  against the real `submit`.

Every node is the real node here. The only things substituted are the role session
(`monkeypatch.setattr(<node>.subagents, "run_role", recorder)`, which works because a
node calls through the module object) and the two functions that would touch a network
(`git_ops.push` and the `github` module `submit` calls through). git itself is real:
half of what these tests assert is git's own `--porcelain` output.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from agent_helpers import FakeRoleResult, RoleRecorder, make_git_repo, no_aws, write
from coding_agent import agentcore_memory, config, git_ops, graph, subagents
from coding_agent import state as state_module
from coding_agent.nodes import checkout as checkout_module
from coding_agent.nodes import code as code_module
from coding_agent.nodes import plan as plan_module
from coding_agent.nodes import review as review_module
from coding_agent.nodes import submit as submit_module
from coding_agent.nodes import verify as verify_module
from conftest import REPO_ROOT

# Long enough to clear `plan._MIN_SPEC_CHARS`, and shaped like a real specification so
# `plan._named_paths` has backticked tokens to find.
_SPEC_TEXT = """## Goal

Change the value `src/thing.py` exports, and nothing else in the tree.

## Approach

One edit, in place, with no new module and no new dependency.

## Changes

- `src/thing.py` -- one unit, owning that one file. Nothing depends on it yet.

## Verification

`make test`, which was green against the unchanged tree before this specification
was written.

## Risks

Nothing else in the repository reads that value, so the change is inert if wrong.
"""

# A closing message that *looks* exactly like a specification: all five section headings,
# well over `plan._MIN_SPEC_CHARS`, and a backticked path whose parent directory exists in
# `_repo`, so `plan._named_paths` would pick it up were the message ever read as the spec.
_SPEC_IN_THE_CLOSING_MESSAGE = """## Goal

NARRATED-SENTINEL: I ran out of turns before writing the file, so here is the spec.

## Approach

Export the value from a new module instead of editing the existing one.

## Changes

- `src/from_the_closing_message.py` -- one unit, owning that one new module.

## Verification

`make test`, which was green against the unchanged tree before this was written.

## Risks

Nothing else in the repository imports the new module, so a wrong value is inert.
"""

_PACKAGE_JSON_WITH_TEST = json.dumps({"name": "widget", "scripts": {"test": "jest"}})
_PACKAGE_JSON_WITH_SETUP = json.dumps({"name": "widget", "scripts": {"setup": "npm ci"}})


def _repo(tmp_path: Path) -> Path:
    """A real one-commit repository with one source file, standing in for the clone."""
    return make_git_repo(
        tmp_path / "repo", files={"README.md": "seed\n", "src/thing.py": "value = 1\n"}
    )


def _command_result(**overrides: Any) -> dict[str, Any]:
    """A green `CommandResult`, with the fields a test cares about overridden."""
    return {"command": "make test", "exitCode": 0, "output": "ok\n", "warning": None, **overrides}


def _no_push(*args: Any, **kwargs: Any) -> None:
    """Stand in for `git_ops.push`. No test here may reach a git remote."""


class _RecordingGithub:
    """A stand-in for the `github` module `submit` calls through.

    Records the keyword arguments verbatim, which is the only way to assert what `draft=`
    was actually decided to be -- the value is not on the returned state.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_pull_request(self, repository: str, **kwargs: Any) -> str:
        self.calls.append({"repository": repository, **kwargs})
        return "https://github.com/owner/repo/pull/1"


async def _review(
    repo: Path,
    command_result: dict[str, Any],
    result: FakeRoleResult,
    monkeypatch: pytest.MonkeyPatch,
    *,
    side_effect: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    """Run the real `review` node over `command_result` with a canned role result.

    `side_effect` is handed to `RoleRecorder`, which calls it with `(role, prompt, cwd)`
    before returning -- that is how a "role" makes the edit this node is about to detect.
    It fires for the VERDICT session only: the two advisory passes run through the same
    `run_role` with a slash command as the prompt, and "the review role edited the tree"
    means the session that produced the verdict. A test about an advisory pass editing the
    tree builds its own recorder.
    `extra` is merged into the state, so a caller can put `iteration`, `reviewRerun` or a
    previous `verifyResult` on it. Nothing is added here by default: the four positional
    callers rely on the node's own `state.get("iteration", 0)`.
    """

    def verdict_session_only(role: str, prompt: str, cwd: Path) -> None:
        if side_effect is not None and not prompt.startswith("/"):
            side_effect(role, prompt, cwd)

    recorder = RoleRecorder(results={"review": result}, side_effect=verdict_session_only)
    monkeypatch.setattr(review_module.subagents, "run_role", recorder)
    return await review_module.review(
        {
            "repoDir": str(repo),
            "repository": "owner/repo",
            "description": "change the exported value",
            "baseBranch": "main",
            "commandResult": command_result,
            **extra,
        }
    )


def _work_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point both filesystem roots `checkout` uses inside `tmp_path`.

    Call after `no_aws`, which deletes them: this container really does ship a
    `WORK_ROOT` and a `VENV_ROOT`, and a `checkout` test that used them would clone into
    the live session's own work directory.
    """
    monkeypatch.setenv("WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("VENV_ROOT", str(tmp_path / "venv"))


async def _checkout(source: Path, **extra: Any) -> dict[str, Any]:
    """Run the real `checkout` node against a local clone source -- never a URL."""
    state: dict[str, Any] = {
        "taskId": "t-1",
        "repository": "owner/repo",
        "cloneSource": str(source),
        **extra,
    }
    return await checkout_module.checkout(state)


# --------------------------------------------------------------------------------------
# verify judges nothing
# --------------------------------------------------------------------------------------


async def test_verify_judges_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`verify` returns a measurement only, and `verifyResult` is produced in one file."""
    no_aws(monkeypatch)

    update = await verify_module.verify({"repoDir": str(tmp_path), "verifyCommand": "exit 0"})

    # (a) the returned update carries nothing that could be read as a verdict.
    assert set(update) == {"commandResult"}
    assert update["commandResult"]["exitCode"] == 0
    for forbidden in ("passed", "verified", "verifyResult"):
        assert forbidden not in update, (
            f"`verify` returned {forbidden!r}: it holds no role verdict and no diff, so "
            "anything it called a verdict would be an exit code renamed (invariant 6)"
        )
        assert forbidden not in update["commandResult"]

    # (b) the same asymmetry, read off the source. The predicate is the dict-literal key
    # `"verifyResult":` -- the *production* form -- and not the bare string: `submit.py`
    # and `plan.py` both legitimately `state.get("verifyResult")`, which is a read and
    # carries no colon inside the quoted key. So this scan finds writers, not readers.
    nodes_dir = REPO_ROOT / "agent/src/coding_agent/nodes"
    producers = [
        path.name
        for path in sorted(nodes_dir.glob("*.py"))
        if '"verifyResult":' in path.read_text(encoding="utf-8")
    ]
    assert producers == ["review.py"], (
        "`verifyResult` is composed in exactly one file, because that file is the only "
        f"one holding both the exit code and the verdict. Writers found: {producers}"
    )
    # And the predicate is not vacuous: the readers really are spelled the other way.
    assert 'state.get("verifyResult")' in (nodes_dir / "submit.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# the one-way veto
# --------------------------------------------------------------------------------------


async def test_a_non_zero_exit_cannot_be_upgraded_by_a_passing_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A red exit stays red however green the review role's verdict is."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)

    update = await _review(
        repo,
        _command_result(exitCode=1, output="1 failed\n"),
        FakeRoleResult(final_text="The diff is covered.\n\nVERDICT: PASS"),
        monkeypatch,
    )
    vr = update["verifyResult"]

    assert vr["vetoed"] is False, "the role did pass it; the exit code is what refuses"
    assert vr["passed"] is False
    assert vr["verified"] is False, (
        "`passed = exitCode == 0 and not vetoed` is the whole one-way veto: there is no "
        "branch in which a verdict of PASS raises a non-zero exit code to green"
    )
    assert vr["exitCode"] == 1


async def test_a_missing_verdict_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closing message with no `VERDICT:` line is a veto, green exit code or not."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)

    update = await _review(
        repo,
        _command_result(),
        FakeRoleResult(final_text="I read the diff and it looks reasonable to me."),
        monkeypatch,
    )
    vr = update["verifyResult"]

    assert vr["vetoed"] is True
    assert vr["passed"] is False
    assert vr["verified"] is False, (
        "an unjudged change must not become a non-draft pull request; the alternative to "
        "this direction is a PR whose reviewer never actually spoke"
    )


async def test_the_verdict_is_read_from_final_text_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 7: the verdict comes off `final_text`, never off the transcript."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)

    update = await _review(
        repo,
        _command_result(),
        FakeRoleResult(
            # The transcript narrates a failure it later resolves. Reading `text` would
            # veto a run whose `final_text` is green.
            text="Two tests are failing, so VERDICT: FAIL is where this is heading.",
            final_text="Both were fixtures, not the change.\n\nVERDICT: PASS",
        ),
        monkeypatch,
    )
    vr = update["verifyResult"]

    assert vr["vetoed"] is False
    assert vr["passed"] is True
    assert vr["verified"] is True


async def test_the_reviewers_closing_message_survives_on_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closing message is kept as `summary`, not discarded once the verdict is parsed.

    `summary` is how the reviewer's own words reach the checkpoint, `store.record_result`
    and the pull request -- it is what a human reads to find out why the PR came back a
    draft. Parsing `VERDICT:` out of `final_text` (invariant 7) and then throwing the rest
    away would leave the reason nowhere at all.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)

    head = "HEAD-SENTINEL: the opening paragraph, which is not the reason."
    tail = "TAIL-SENTINEL: the one hunk with no test is in `src/thing.py`."
    filler = "I checked another hunk and found the test that covers it.\n" * 200
    final_text = f"{head}\n{filler}\n{tail}\n\nVERDICT: PASS"
    assert len(final_text) > review_module._MAX_SUMMARY, "otherwise the bound is untested"

    update = await _review(
        repo, _command_result(), FakeRoleResult(final_text=final_text), monkeypatch
    )
    summary = update["verifyResult"]["summary"]

    assert len(summary) <= review_module._MAX_SUMMARY, (
        "unbounded, this is a DynamoDB item attribute and a PR body: a role that narrated "
        "for 200 turns would put its whole transcript into both"
    )
    # The TAIL is what is kept, because the verdict line and the paragraph giving the
    # reason are at the end of the message. The head is the part that may be lost.
    assert tail in summary
    assert head not in summary
    # And it is `final_text` that was kept, not the transcript: `FakeRoleResult.text`
    # deliberately carries a `VERDICT: FAIL` a node reading the wrong field would surface.
    assert "exploring the repository" not in summary
    assert update["verifyResult"]["vetoed"] is False


# --------------------------------------------------------------------------------------
# the one permitted hop back to `verify`
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "edits_the_source",
    [True, False],
    ids=["edits-the-source-in-place", "writes-only-its-own-analysis"],
)
async def test_a_reviewer_that_edits_the_tree_in_place_asks_for_one_more_measurement(
    edits_the_source: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review role holds `Write`, `Edit` and `Bash`, so "judge, do not fix" is a prompt
    instruction and not an allowlist fact. An edit after the last measurement therefore
    has to be *detected*, and the detection is a content digest of the non-`.agent/` diff.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    # The coder's uncommitted change, already in the tree when `review` starts.
    write(repo / "src" / "thing.py", "value = 2\n")

    def review_the_tree(role: str, prompt: str, cwd: Path) -> None:
        write(Path(cwd) / ".agent" / "review" / "iteration-0.md", "I checked every hunk.\n")
        if edits_the_source:
            # In place, in a file the coder already touched. No path is added or removed.
            write(Path(cwd) / "src" / "thing.py", "value = 3\n")

    def source_paths() -> list[str]:
        return [p for p in git_ops.changed_paths(repo) if not p.startswith(".agent/")]

    before = source_paths()
    # The measurement on the state before the role ran, which is what makes the router
    # assertion below meaningful: it says `passed`, and the flag still overrules it.
    state: dict[str, Any] = {
        "iteration": 0,
        "verifyResult": {"passed": True, "verified": True, "summary": "the previous run"},
    }
    update = await _review(
        repo,
        _command_result(),
        FakeRoleResult(final_text="Read the diff against main.\n\nVERDICT: PASS"),
        monkeypatch,
        side_effect=review_the_tree,
        **state,
    )
    after = source_paths()

    if edits_the_source:
        # This identity is the whole point of the design. A digest over the *set of
        # changed paths* would have seen nothing here, because an in-place edit to a file
        # the coder already touched adds no path and removes none -- which is the likeliest
        # shape of a helpful reviewer's mistake.
        assert after == before == ["src/thing.py"]
        # Exactly the flag and nothing else. `_after_review` reads `reviewRerun` before it
        # reads `verifyResult` precisely because the exit code on the state now describes a
        # tree nobody ran, so a node that returned a verdict alongside the flag would be
        # composing one against a measurement it had just invalidated.
        assert update == {"reviewRerun": True}
        # The real router, with no stub between it and the node.
        assert graph._after_review({**state, **update}) == "verify", (
            "the stale `verifyResult` on the state says `passed`, so anything that read it "
            "first would route straight to `submit` on an unmeasured tree"
        )
    else:
        # The reviewer's own analysis under `.agent/` is not the work being reviewed, so it
        # must not cost a whole extra verification run.
        assert "reviewRerun" in update and update["reviewRerun"] is False
        assert update["verifyResult"]["passed"] is True
        assert update["iteration"] == 1, "`iteration` advances here and nowhere else"


async def test_a_second_edit_leaves_the_verdict_standing_against_the_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The bound on the hop is one boolean: a second edit is loud, and the verdict stands.

    A role that edits the tree on every pass could otherwise alternate `verify` and
    `review` until LangGraph's recursion guard fired -- an opaque `GraphRecursionError`
    instead of the draft pull request the unverified path exists to produce.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / "src" / "thing.py", "value = 2\n")

    # Different bytes on every call. Rewriting identical content leaves
    # `source_diff_digest` unmoved, the branch under test never runs and the assertions
    # below pass vacuously -- verified both ways in this container.
    edits = {"n": 0}

    def edit_the_source_again(role: str, prompt: str, cwd: Path) -> None:
        edits["n"] += 1
        write(Path(cwd) / "src" / "thing.py", f"value = {10 + edits['n']}\n")

    verdict = FakeRoleResult(final_text="I could not resist fixing it.\n\nVERDICT: PASS")

    # First pass: the flag is unspent, so the node asks for one more measurement.
    first = await _review(
        repo,
        _command_result(),
        verdict,
        monkeypatch,
        side_effect=edit_the_source_again,
        iteration=0,
    )
    assert first == {"reviewRerun": True}

    # Second pass: `iteration` did NOT advance on the hop, and the flag is now spent.
    state: dict[str, Any] = {"iteration": 0, "reviewRerun": True}
    with caplog.at_level(logging.ERROR):
        update = await _review(
            repo,
            _command_result(),
            verdict,
            monkeypatch,
            side_effect=edit_the_source_again,
            **state,
        )

    assert edits["n"] == 2, "the role really did edit the tree on both passes"
    assert "twice" in caplog.text
    assert "the closest measurement" in caplog.text, (
        "the second edit is unfixable from here, so the only thing left is to say so "
        "loudly enough that a human reading the run can see what happened"
    )
    # The verdict is composed anyway, against the second run.
    assert update["verifyResult"]["passed"] is True
    assert update["reviewRerun"] is False, "cleared on the same update that composes the verdict"
    assert update["iteration"] == 1
    assert update["reviewPath"]
    assert graph._after_review({**state, **update}) != "verify", (
        "one boolean is the whole bound: a pathological role cannot make the hop loop"
    )


# --------------------------------------------------------------------------------------
# unverified is not failed
# --------------------------------------------------------------------------------------


async def test_no_command_configured_is_passed_but_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No verification command gives `passed: True, verified: False` and a warning."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)

    measured = await verify_module.verify({"repoDir": str(repo), "verifyCommand": ""})
    cr = measured["commandResult"]
    assert cr == {
        "command": "",
        "exitCode": None,
        "output": "",
        "warning": "no verification command was detected in this repository",
    }

    update = await _review(repo, cr, FakeRoleResult(final_text="VERDICT: PASS"), monkeypatch)
    vr = update["verifyResult"]

    assert vr["passed"] is True, "nothing ran, so there is nothing for the loop to re-plan"
    assert vr["verified"] is False, "`passed: True` here is safe only because `verified` is False"
    assert vr["warning"]
    assert state_module.terminal_status(vr) == "SUCCEEDED_UNVERIFIED"


async def test_a_setup_failure_is_unverified_not_failed_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed setup ends SUCCEEDED_UNVERIFIED, never FAILED_VERIFICATION."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)

    measured = await verify_module.verify(
        {"repoDir": str(repo), "setupCommand": "exit 3", "verifyCommand": "make test"}
    )
    cr = measured["commandResult"]
    assert cr["exitCode"] is None, "`None` is how `nothing ran` is spelled, and it is not 0"
    assert cr["warning"] == "the setup command failed; nothing was verified"

    update = await _review(repo, cr, FakeRoleResult(final_text="VERDICT: FAIL"), monkeypatch)
    vr = update["verifyResult"]

    assert vr["passed"] is True
    assert vr["verified"] is False
    assert state_module.terminal_status(vr) == "SUCCEEDED_UNVERIFIED", (
        "FAILED_VERIFICATION here would send the loop back to re-plan a change nothing "
        "was wrong with, burning an iteration on a broken environment"
    )


# --------------------------------------------------------------------------------------
# submit
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command_result", "final_text"),
    [
        (
            {"command": "", "exitCode": None, "output": "", "warning": "nothing was detected"},
            "VERDICT: PASS",
        ),
        (
            {
                "command": "make test",
                "exitCode": None,
                "output": "",
                "warning": "the setup command failed; nothing was verified",
            },
            "VERDICT: PASS",
        ),
        (_command_result(), "VERDICT: PASS"),
    ],
    ids=["no-command", "setup-failed", "green-and-judged"],
)
async def test_the_draft_flag_keys_on_verified(
    command_result: dict[str, Any],
    final_text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`draft=` is the negation of `verified` in all three `verifyResult` shapes."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    # An uncommitted source change, written before `review` digests the tree so the
    # digest does not move and the one permitted re-run hop is not taken.
    write(repo / "src" / "thing.py", "value = 2\n")

    vr = (await _review(repo, command_result, FakeRoleResult(final_text=final_text), monkeypatch))[
        "verifyResult"
    ]

    fake_github = _RecordingGithub()
    monkeypatch.setattr(submit_module, "github", fake_github)
    monkeypatch.setattr(submit_module.git_ops, "push", _no_push)

    update = await submit_module.submit(
        {
            "taskId": "t-1",
            "repoDir": str(repo),
            "repository": "owner/repo",
            "branch": "agent/t-1",
            "baseBranch": "main",
            "description": "change the exported value",
            "verifyResult": vr,
        }
    )

    assert len(fake_github.calls) == 1
    assert fake_github.calls[0]["draft"] is not vr["verified"], (
        "a `passed: True, verified: False` run must still be a draft: `passed` says only "
        "that the loop has nothing more to do, never that anything was checked"
    )
    assert update["status"] == state_module.terminal_status(vr)


async def test_submit_refuses_a_tree_with_only_a_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pull request containing only a specification must never exist (invariant 5)."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / ".agent" / "spec" / "iteration-0.md", _SPEC_TEXT)

    fake_github = _RecordingGithub()
    monkeypatch.setattr(submit_module, "github", fake_github)
    monkeypatch.setattr(submit_module.git_ops, "push", _no_push)

    with pytest.raises(submit_module.SubmitError):
        await submit_module.submit(
            {
                "taskId": "t-1",
                "repoDir": str(repo),
                "repository": "owner/repo",
                "branch": "agent/t-1",
                "baseBranch": "main",
                "description": "change the exported value",
                "verifyResult": {"passed": True, "verified": True},
            }
        )

    assert fake_github.calls == [], "the guard runs before anything is committed or pushed"


async def _verify_review_submit(
    repo: Path,
    command_result: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Drive the real `review` then the real `submit` over one measurement.

    Returns `(verifyResult, submit update, the recorded create_pull_request call)`. The
    two nodes are chained through the state the graph would carry between them --
    `iteration`, `specPath` and `reviewPath` -- because the pull request body is composed
    from exactly those fields and nothing else.
    """
    spec_path = write(repo / ".agent" / "spec" / "iteration-0.md", _SPEC_TEXT)
    review_update = await _review(
        repo,
        command_result,
        FakeRoleResult(final_text="Every hunk has a test that reaches it.\n\nVERDICT: PASS"),
        monkeypatch,
        iteration=0,
        specPath=str(spec_path),
    )
    assert "reviewRerun" not in review_update or review_update["reviewRerun"] is False, (
        "the source change was written before `review` ran, so the digest did not move"
    )

    fake_github = _RecordingGithub()
    monkeypatch.setattr(submit_module, "github", fake_github)
    monkeypatch.setattr(submit_module.git_ops, "push", _no_push)

    update = await submit_module.submit(
        {
            "taskId": "t-1",
            "repoDir": str(repo),
            "repository": "owner/repo",
            "branch": "agent/t-1",
            "baseBranch": "main",
            "description": "change the exported value",
            "verifyResult": review_update["verifyResult"],
            "iteration": review_update["iteration"],
            "specPath": str(spec_path),
            "reviewPath": review_update["reviewPath"],
        }
    )
    assert len(fake_github.calls) == 1
    return review_update["verifyResult"], update, fake_github.calls[0]


async def test_an_unverified_run_says_in_the_pull_request_why_it_is_a_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository declaring no verification command ends as a draft that explains itself.

    The whole chain, with the real nodes: `verify` measures nothing, `review` composes the
    one `verifyResult`, `submit` opens the pull request. A human who receives this PR has
    only the body to tell them that nothing checked the change.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    # The coder's change, written before `review` digests the tree so the digest does not
    # move and the one permitted re-run hop is not taken.
    write(repo / "src" / "thing.py", "value = 2\n")

    measured = await verify_module.verify({"repoDir": str(repo), "verifyCommand": ""})
    vr, update, call = await _verify_review_submit(repo, measured["commandResult"], monkeypatch)

    assert call["draft"] is True, "a `verified: False` run is a draft however green the verdict"
    assert update["status"] == "SUCCEEDED_UNVERIFIED"
    assert state_module.terminal_status(vr) == "SUCCEEDED_UNVERIFIED"

    body = call["body"]
    assert "draft because the change was not verified" in body, (
        "a draft with no explanation is indistinguishable from a draft somebody forgot to "
        "finish, and the reason is the only thing that tells a human what to do next"
    )
    assert "No verification command was detected" in body
    assert "Attempts: 1 of 3" in body, "`iteration` advanced once, in `review` and nowhere else"
    # `## Artefacts` names both files for the *same* iteration: the spec the change was
    # implemented from and the analysis that judged it.
    assert "## Artefacts" in body
    assert f"- Specification: `{repo / '.agent' / 'spec' / 'iteration-0.md'}`" in body
    assert f"- Review: `{repo / '.agent' / 'review' / 'iteration-0.md'}`" in body
    # Nothing ran, so there was no exit code to report and the body must not invent one.
    assert "Exit code" not in body


async def test_a_judged_green_run_opens_a_real_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The converse, so the draft test above cannot be passing for a trivial reason.

    A real command that exited 0 and a reviewer who judged it green is the one shape that
    produces a non-draft pull request, and the body reports the measurement rather than
    apologising for its absence.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / "src" / "thing.py", "value = 2\n")

    # `_command_result()` rather than a real `verify` run: this temporary repository has no
    # `make test` to execute, and what is under test is what `review` and `submit` do with
    # a green measurement, not `commands` (which `tests/test_commands.py` owns).
    vr, update, call = await _verify_review_submit(repo, _command_result(), monkeypatch)

    assert call["draft"] is False
    assert update["status"] == "SUCCEEDED"
    assert state_module.terminal_status(vr) == "SUCCEEDED"

    body = call["body"]
    assert "- Command: `make test`" in body
    assert "Exit code: `0`" in body
    assert "draft because the change was not verified" not in body


# --------------------------------------------------------------------------------------
# the two advisory passes, which never touch `verified`
# --------------------------------------------------------------------------------------


# One `/security-review` finding in the shape the command emits.
_SECURITY_FINDING = """## Vuln 1: Command injection in the branch helper: `src/thing.py:12`

* **Severity: HIGH**
* **Category:** `command-injection`
* **Confidence:** 9/10

The branch name is interpolated into a `shell=True` call, so a branch named `; rm -rf /`
runs as a command.
"""

_CODE_REVIEW_FINDING = """I read the diff against main.

Findings:
- `src/thing.py` changes an exported value with no test that reads it back.
- The docstring still describes the old value.
"""


class _AdvisoryRoles:
    """A `run_role` stand-in that tells the verdict session from the advisory passes.

    All three sessions run as the `review` role, so the prompt is the only thing that
    distinguishes them -- `RoleRecorder` keys on the role and cannot express that. The two
    advisory commands are matched exactly; anything else is the verdict session.
    """

    def __init__(
        self,
        *,
        verdict: str,
        security: str = "",
        code_review: str = "",
        raises: tuple[str, ...] = (),
        error: type[Exception] = RuntimeError,
        side_effect: Any = None,
    ) -> None:
        self.verdict = verdict
        self.replies = {
            review_module.SECURITY_COMMAND: security,
            review_module.CODE_REVIEW_COMMAND: code_review,
        }
        self.raises = raises
        # `error` is what a prompt in `raises` fails with. The default is the arbitrary
        # exception the node must survive; `subagents.RoleError` is the one the node has
        # to *record*, and the two branches are told apart by nothing else.
        self.error = error
        self.side_effect = side_effect
        self.prompts: list[str] = []

    async def __call__(self, role: str, prompt: str, **kwargs: Any) -> FakeRoleResult:
        self.prompts.append(prompt)
        if self.side_effect is not None:
            self.side_effect(role, prompt, kwargs.get("cwd"))
        if prompt in self.raises:
            raise self.error(f"the {prompt} pass fell over")
        if prompt in self.replies:
            return FakeRoleResult(final_text=self.replies[prompt])
        return FakeRoleResult(final_text=self.verdict)


async def _review_with(
    repo: Path,
    command_result: dict[str, Any],
    roles: _AdvisoryRoles,
    monkeypatch: pytest.MonkeyPatch,
    *,
    origin_head: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """Run the real `review` node with the three sessions answered separately.

    `origin_head` is patched rather than faked in git: a `git init` repository really has
    no `refs/remotes/origin/HEAD`, so the *default* here is the one that needs patching.
    """
    monkeypatch.setattr(review_module.subagents, "run_role", roles)
    monkeypatch.setattr(review_module.git_ops, "has_origin_head", lambda _repo: origin_head)
    return await review_module.review(
        {
            "repoDir": str(repo),
            "repository": "owner/repo",
            "description": "change the exported value",
            "baseBranch": "main",
            "commandResult": command_result,
            **extra,
        }
    )


@pytest.mark.parametrize("exit_code", [0, 1], ids=["green-exit", "red-exit"])
async def test_a_security_finding_cannot_flip_verified(
    exit_code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 6b: `verified` has one writer, and an advisory finding is not it.

    A full HIGH-severity finding alongside `VERDICT: PASS` and a green exit code still
    produces `verified: True`; the same finding alongside a red exit code still produces
    `FAILED_VERIFICATION`. The advisory output moves neither bit, in either direction.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / "src" / "thing.py", "value = 2\n")

    roles = _AdvisoryRoles(
        verdict="Every hunk has a test.\n\nVERDICT: PASS",
        security=_SECURITY_FINDING,
        code_review=_CODE_REVIEW_FINDING,
    )
    update = await _review_with(repo, _command_result(exitCode=exit_code), roles, monkeypatch)
    vr = update["verifyResult"]

    assert update["securityFindings"] > 0, "the pass really did report something"
    assert update["codeReviewFindings"] > 0
    assert vr["passed"] is (exit_code == 0)
    assert vr["verified"] is (exit_code == 0), (
        "`verified` means a real command ran and was judged green, and it has exactly one "
        "writer: a security finding that could flip it would make the one signal `submit`, "
        "the PR body and `terminal_status` all key on mean two different things"
    )
    assert state_module.terminal_status(vr) == (
        "SUCCEEDED" if exit_code == 0 else "FAILED_VERIFICATION"
    )
    # Both files are on disk under `.agent/`, which is not the work being reviewed.
    assert Path(update["securityReviewPath"]).is_file()
    assert Path(update["codeReviewPath"]).is_file()


async def test_an_advisory_pass_that_raises_cannot_fail_the_review_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A pass that falls over is a warning and an empty section, never a failed node.

    An advisory pass that could raise would give a finding a back-door route to the
    pass/fail signal: the node dies, the graph reports `FAILED`, and a security finding has
    decided the outcome after all.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / "src" / "thing.py", "value = 2\n")

    roles = _AdvisoryRoles(
        verdict="Read the diff against main.\n\nVERDICT: PASS",
        raises=(review_module.SECURITY_COMMAND, review_module.CODE_REVIEW_COMMAND),
    )
    with caplog.at_level(logging.WARNING):
        update = await _review_with(repo, _command_result(), roles, monkeypatch)

    assert update["verifyResult"]["verified"] is True
    assert update["securityFindings"] == 0
    assert update["codeReviewFindings"] == 0
    assert "cannot fail this node" in caplog.text


async def test_an_empty_security_review_is_recorded_as_did_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty result is "the pass did not run", never "no findings".

    That is the whole hazard: an empty `result` arrives with `subtype: "success"` and
    `is_error: false`, so a body reading "0 findings" would be a confident green on a pass
    that never made a model call. `num_turns` cannot be used instead -- `/code-review` was
    observed returning 0 turns while producing 1234 characters of real findings.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / "src" / "thing.py", "value = 2\n")

    roles = _AdvisoryRoles(verdict="VERDICT: PASS", security="", code_review="")
    update = await _review_with(repo, _command_result(), roles, monkeypatch)

    assert review_module.SECURITY_COMMAND in roles.prompts, "it really was dispatched"
    assert update["securityFindings"] == 0
    text = Path(update["securityReviewPath"]).read_text(encoding="utf-8")
    assert "DID NOT RUN" in text
    assert "no findings" not in text.lower(), (
        "0 findings on a pass that never ran is exactly the confident-looking green this "
        "branch exists to prevent"
    )


async def test_a_role_error_from_an_advisory_pass_is_recorded_as_did_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same artefact, when the emptiness is caught one layer down instead.

    `run_role` raises `subagents.RoleError` on a session that produced no text at all, so
    the empty advisory pass now fails before `_advisory_pass` ever sees a body. Without a
    clause for it ahead of the generic `except Exception`, the raise is swallowed as "the
    pass failed", no file is written, and an omitted section reads as "nothing to say" --
    which is the confident-looking green the produced-nothing artefact exists to prevent.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / "src" / "thing.py", "value = 2\n")

    roles = _AdvisoryRoles(
        verdict="VERDICT: PASS",
        raises=(review_module.SECURITY_COMMAND,),
        error=subagents.RoleError,
    )
    update = await _review_with(repo, _command_result(), roles, monkeypatch)

    assert review_module.SECURITY_COMMAND in roles.prompts, "it really was dispatched"
    assert update["securityFindings"] == 0
    text = Path(update["securityReviewPath"]).read_text(encoding="utf-8")
    assert "DID NOT RUN" in text
    assert "no findings" not in text.lower()


async def test_the_security_pass_is_skipped_without_an_origin_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `refs/remotes/origin/HEAD` means the pass is not dispatched, and the file says so.

    `/security-review` opens with `!git log --no-decorate origin/HEAD...`, so without that
    ref it aborts before any model call and returns an empty success in about 100 ms.
    `/code-review` needs no upstream and runs anyway.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / "src" / "thing.py", "value = 2\n")
    # The real answer for a `git init` repository, asserted rather than assumed: this is
    # also `local_run.py`'s situation.
    assert git_ops.has_origin_head(repo) is False

    roles = _AdvisoryRoles(verdict="VERDICT: PASS", code_review=_CODE_REVIEW_FINDING)
    update = await _review_with(repo, _command_result(), roles, monkeypatch, origin_head=False)

    assert review_module.SECURITY_COMMAND not in roles.prompts
    assert review_module.CODE_REVIEW_COMMAND in roles.prompts
    assert update["securityFindings"] == 0
    text = Path(update["securityReviewPath"]).read_text(encoding="utf-8")
    assert "refs/remotes/origin/HEAD" in text, "the recorded reason, on disk"
    assert "not run" in text
    assert update["codeReviewFindings"] > 0


async def test_the_advisory_sections_carry_no_absolute_clone_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/code-review` prints absolute paths, and `repoDir` must not reach a PR body."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / "src" / "thing.py", "value = 2\n")

    roles = _AdvisoryRoles(
        verdict="VERDICT: PASS",
        security=_SECURITY_FINDING,
        code_review=f"Findings:\n- {repo / 'src' / 'thing.py'}:1 changes an exported value.\n",
    )
    update = await _review_with(repo, _command_result(), roles, monkeypatch)

    on_disk = Path(update["codeReviewPath"]).read_text(encoding="utf-8")
    assert str(repo) not in on_disk
    assert "./src/thing.py" in on_disk, "scrubbed to a repository-relative path, not deleted"

    body = submit_module._pr_body({**update, "description": "change the exported value"})
    # From the first advisory heading on: `## Artefacts` above it names the artefact paths
    # as they are on disk, which is a deliberate and separate decision. What must not carry
    # the clone path is the text an advisory pass wrote.
    advisory = body[body.index("## Security review (advisory)") :]
    assert str(repo) not in advisory, (
        "the container's work directory is an implementation detail of the runtime and "
        "means nothing to whoever reads the pull request"
    )


async def test_the_advisory_passes_do_not_spend_the_rerun_hop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An advisory pass that edits the tree is loud, and `reviewRerun` stays unspent.

    The hop budget is one boolean spent once per iteration (invariant 6c) and it belongs to
    the review role. A pass that cannot move `verified` must not be able to spend it.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / "src" / "thing.py", "value = 2\n")

    def fix_it_while_reviewing(role: str, prompt: str, cwd: Any) -> None:
        if prompt == review_module.CODE_REVIEW_COMMAND:
            write(Path(cwd) / "src" / "thing.py", "value = 99\n")

    roles = _AdvisoryRoles(
        verdict="VERDICT: PASS",
        code_review=_CODE_REVIEW_FINDING,
        side_effect=fix_it_while_reviewing,
    )
    with caplog.at_level(logging.ERROR):
        update = await _review_with(repo, _command_result(), roles, monkeypatch, iteration=0)

    assert update["reviewRerun"] is False
    assert update["verifyResult"]["verified"] is True
    assert "advisory pass" in caplog.text
    # The warning lands beside the findings, where a human reading them will see it.
    assert "WARNING" in Path(update["codeReviewPath"]).read_text(encoding="utf-8")


async def test_the_pull_request_body_labels_both_passes_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sections appear with their counts, and each says it did not affect `verified`.

    A security section in a pull request body reads like a gate unless it says otherwise,
    and this one is not a gate: whether a high-severity finding should force a draft is
    deliberately deferred, because it needs severity calibration nobody here has done.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / "src" / "thing.py", "value = 2\n")

    roles = _AdvisoryRoles(
        verdict="VERDICT: PASS",
        security=_SECURITY_FINDING,
        code_review=_CODE_REVIEW_FINDING,
    )
    update = await _review_with(repo, _command_result(), roles, monkeypatch)

    body = submit_module._pr_body({**update, "description": "change the exported value"})

    for heading in ("## Security review (advisory)", "## Code review (advisory)"):
        assert heading in body, heading
    assert body.count("did not affect `verified`") == 2
    assert f"Findings (heuristic count): {update['securityFindings']}" in body
    assert "Command injection in the branch helper" in body

    # A run that never did the passes omits the sections entirely rather than reporting
    # zero: a heading with a count of nothing reads as a pass that found nothing.
    without = submit_module._pr_body({"description": "change the exported value"})
    assert "advisory" not in without


# --------------------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------------------


async def test_the_spec_is_read_back_from_the_per_iteration_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Iteration 1 runs `replan` and reads back `.agent/spec/iteration-1.md`."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)

    def write_the_spec(role: str, prompt: str, cwd: Path) -> None:
        write(Path(cwd) / ".agent" / "spec" / "iteration-1.md", _SPEC_TEXT)

    recorder = RoleRecorder(side_effect=write_the_spec)
    monkeypatch.setattr(plan_module.subagents, "run_role", recorder)

    update = await plan_module.plan(
        {
            "repoDir": str(repo),
            "repository": "owner/repo",
            "description": "change the exported value",
            "baseBranch": "main",
            "verifyCommand": "make test",
            "iteration": 1,
        }
    )

    assert recorder.roles == ["replan"], (
        "an attempt after a failed verification is not a fresh plan"
    )
    assert Path(update["specPath"]) == repo / ".agent" / "spec" / "iteration-1.md"
    assert update["specText"] == _SPEC_TEXT.strip()
    assert ".agent/spec/iteration-1.md" in recorder.calls[0]["prompt"], (
        "a per-iteration path, so a node reading it can tell `never written` from "
        "`last iteration's` -- which is what the whole re-plan loop turns on"
    )
    assert "src/thing.py" in update["planChangedPaths"]


@pytest.mark.parametrize("written", [None, "too short."], ids=["nothing", "ten-characters"])
async def test_a_planner_that_writes_nothing_raises(
    written: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planner that produced no usable specification fails loudly, never quietly."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)

    def maybe_write_the_spec(role: str, prompt: str, cwd: Path) -> None:
        if written is not None:
            write(Path(cwd) / ".agent" / "spec" / "iteration-0.md", written)

    recorder = RoleRecorder(
        default=FakeRoleResult(final_text="I explored the repository for a while."),
        side_effect=maybe_write_the_spec,
    )
    monkeypatch.setattr(plan_module.subagents, "run_role", recorder)

    with pytest.raises(plan_module.PlanError):
        await plan_module.plan(
            {
                "repoDir": str(repo),
                "repository": "owner/repo",
                "description": "change the exported value",
                "baseBranch": "main",
                "iteration": 0,
            }
        )


@pytest.mark.parametrize(
    "writes_the_file",
    [True, False],
    ids=["the-file-wins-over-the-closing-message", "a-narrated-spec-is-not-a-spec"],
)
async def test_the_spec_is_never_taken_out_of_the_closing_message(
    writes_the_file: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The specification is the file the planning role wrote, never what it said instead.

    There is no fallback from the closing message to the file, and this is the pair that
    pins both halves: when both exist the file wins, and when only the message exists the
    node raises. A message read as a spec would be a spec that never reached the disk the
    coder, the reviewer and the next iteration's re-plan prompt all read it off.

    Complements `test_a_planner_that_writes_nothing_raises`, which covers a file that is
    missing or too short rather than a message that looks complete.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)

    def maybe_write_the_spec(role: str, prompt: str, cwd: Path) -> None:
        if writes_the_file:
            write(Path(cwd) / ".agent" / "spec" / "iteration-0.md", _SPEC_TEXT)

    recorder = RoleRecorder(
        default=FakeRoleResult(final_text=_SPEC_IN_THE_CLOSING_MESSAGE),
        side_effect=maybe_write_the_spec,
    )
    monkeypatch.setattr(plan_module.subagents, "run_role", recorder)
    state: dict[str, Any] = {
        "repoDir": str(repo),
        "repository": "owner/repo",
        "description": "change the exported value",
        "baseBranch": "main",
        "iteration": 0,
    }

    if writes_the_file:
        update = await plan_module.plan(state)

        assert update["specText"] == _SPEC_TEXT.strip()
        assert "NARRATED-SENTINEL" not in update["specText"]
        # The snapshot the coder's no-op guard is measured against comes off the file too.
        # `src/` exists in this repository, so `_named_paths` would have accepted this token
        # happily -- it is absent because the text it appears in was never read.
        assert "src/from_the_closing_message.py" not in update["planChangedPaths"]
        assert "src/thing.py" in update["planChangedPaths"]
    else:
        with caplog.at_level(logging.INFO), pytest.raises(plan_module.PlanError) as excinfo:
            await plan_module.plan(state)

        assert ".agent/spec/iteration-0.md" in str(excinfo.value), (
            "the path is what a human needs: the failure is `nothing is there`, and the "
            "message has to say where `there` was"
        )
        # The role did run, and the log says so. That is how a human reading the run tells a
        # role that narrated a specification from one that never started.
        assert "plan role finished" in caplog.text


# --------------------------------------------------------------------------------------
# code -- the no-op guard, both ways
# --------------------------------------------------------------------------------------


async def test_a_code_role_that_changed_only_agent_files_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 5: a run that implemented nothing must fail rather than ship a spec."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / ".agent" / "spec" / "iteration-0.md", _SPEC_TEXT)

    def write_only_a_note(role: str, prompt: str, cwd: Path) -> None:
        write(Path(cwd) / ".agent" / "memory" / "code.md", "I found the file interesting.\n")

    recorder = RoleRecorder(side_effect=write_only_a_note)
    monkeypatch.setattr(code_module.subagents, "run_role", recorder)

    with pytest.raises(code_module.CodeError):
        await code_module.code(
            {
                "repoDir": str(repo),
                "repository": "owner/repo",
                "description": "change the exported value",
                "specPath": str(repo / ".agent/spec/iteration-0.md"),
                "specText": _SPEC_TEXT,
                "verifyCommand": "make test",
            }
        )


async def test_a_code_role_that_changed_a_source_file_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction of the guard: a real source edit is returned, `.agent/` is not."""
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / ".agent" / "spec" / "iteration-0.md", _SPEC_TEXT)

    def write_the_change(role: str, prompt: str, cwd: Path) -> None:
        write(Path(cwd) / "src" / "thing.py", "value = 2\n")

    recorder = RoleRecorder(side_effect=write_the_change)
    monkeypatch.setattr(code_module.subagents, "run_role", recorder)

    update = await code_module.code(
        {
            "repoDir": str(repo),
            "repository": "owner/repo",
            "description": "change the exported value",
            "specPath": str(repo / ".agent/spec/iteration-0.md"),
            "specText": _SPEC_TEXT,
            "verifyCommand": "make test",
            "planChangedPaths": ["src/thing.py"],
        }
    )

    assert update["codeChangedPaths"] == ["src/thing.py"], (
        "`git_ops.has_changes()` is unconditionally true by now -- the plan node wrote a "
        "spec -- so the guard filters `changed_paths` instead"
    )
    assert recorder.roles == ["code"]


@pytest.mark.parametrize(
    "planned",
    [["src/thing.py"], []],
    ids=["the-spec-named-one-file", "the-spec-named-nothing"],
)
async def test_the_coder_may_touch_paths_the_specification_did_not_name(
    planned: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invariant 5's other direction: extra paths are information, never a failure.

    A specification cannot enumerate every file a correct change touches -- a new import,
    a fixture, a generated manifest -- so a guard that failed on an unlisted path would
    refuse most correct changes. The failing direction, a run that changed nothing outside
    `.agent/`, is `test_a_code_role_that_changed_only_agent_files_raises`.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / ".agent" / "spec" / "iteration-0.md", _SPEC_TEXT)

    def write_more_than_was_planned(role: str, prompt: str, cwd: Path) -> None:
        write(Path(cwd) / "src" / "thing.py", "value = 2\n")
        write(Path(cwd) / "src" / "helper.py", "def helper() -> int:\n    return 2\n")
        write(Path(cwd) / "tests" / "test_thing.py", "def test_it() -> None:\n    assert True\n")

    recorder = RoleRecorder(side_effect=write_more_than_was_planned)
    monkeypatch.setattr(code_module.subagents, "run_role", recorder)

    with caplog.at_level(logging.WARNING):
        update = await code_module.code(
            {
                "repoDir": str(repo),
                "repository": "owner/repo",
                "description": "change the exported value",
                "specPath": str(repo / ".agent/spec/iteration-0.md"),
                "specText": _SPEC_TEXT,
                "verifyCommand": "make test",
                "planChangedPaths": planned,
            }
        )

    # A set, not a list: the order is git's and nothing depends on it.
    assert set(update["codeChangedPaths"]) == {
        "src/thing.py",
        "src/helper.py",
        "tests/test_thing.py",
    }
    assert all(not p.startswith(".agent/") for p in update["codeChangedPaths"])

    if planned:
        assert "did not name" in caplog.text
        assert "src/helper.py" in caplog.text, (
            "a warning that did not name the paths would tell a human nothing they could act on"
        )
    else:
        # `code.py` guards on `planned and extra`, so a planner that enumerated no paths at
        # all produces no noise -- every path would otherwise be `extra`.
        assert "did not name" not in caplog.text


def _code_cfg(*, goal_enabled: bool) -> config.AgentConfig:
    """A fleet configuration with the goal switch pinned, reading no environment."""
    return config.AgentConfig(
        model="sonnet",
        roles=config.ROLES,
        work_root=Path("/mnt/workspace/work"),
        venv_root=Path("/tmp/venv"),
        skill_paths=(),
        fallback_model="sonnet",
        goal_enabled=goal_enabled,
    )


def test_the_goal_condition_is_never_the_verification_verdict() -> None:
    """Invariant 6b, mechanised on the one string that could quietly break it.

    A condition shaped like "the tests pass" would put a second writer on the pass/fail
    signal and invite a nine-minute suite to be re-run an unbounded number of times inside
    one session, with nothing for the watchdog to see. `verify` runs the command and
    `review` composes the verdict. The scan covers the whole system prompt and not just
    the condition, because the system prompt is what the session actually receives.
    """
    for forbidden in ("test", "tests", "pass", "passes", "green", "suite", "exit code", "verif"):
        for name, text in (
            ("condition", code_module.GOAL_CONDITION.lower()),
            ("system prompt", code_module.GOAL_SYSTEM_PROMPT.lower()),
        ):
            assert forbidden not in text, (
                f"the goal {name} names {forbidden!r}: it is drawn from the specification "
                "and must never be the verification verdict (invariant 6b)"
            )

    # And it does say what it is drawn from, so the role has something to evaluate.
    assert "## Changes" in code_module.GOAL_CONDITION
    # One owner of the words: the system prompt is built from the condition itself.
    assert code_module.GOAL_CONDITION in code_module.GOAL_SYSTEM_PROMPT


@pytest.mark.parametrize("goal_enabled", [True, False], ids=["goal-on", "goal-off"])
async def test_the_coding_instructions_survive_the_goal_switch(
    goal_enabled: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The goal rides in the system prompt, and the loop must work without it either way.

    The switch moves `system_prompt` and nothing else: whatever it is set to, the session
    input still carries the coding instructions.
    """
    no_aws(monkeypatch)
    repo = _repo(tmp_path)
    write(repo / ".agent" / "spec" / "iteration-0.md", _SPEC_TEXT)

    def write_the_change(role: str, prompt: str, cwd: Path) -> None:
        write(Path(cwd) / "src" / "thing.py", "value = 2\n")

    recorder = RoleRecorder(side_effect=write_the_change)
    monkeypatch.setattr(code_module.subagents, "run_role", recorder)

    update = await code_module.code(
        {
            "repoDir": str(repo),
            "repository": "owner/repo",
            "description": "change the exported value",
            "specPath": str(repo / ".agent/spec/iteration-0.md"),
            "specText": _SPEC_TEXT,
            "verifyCommand": "make test",
        },
        _code_cfg(goal_enabled=goal_enabled),
    )

    # The node still does its job either way: the switch changes the system prompt alone.
    assert update["codeChangedPaths"] == ["src/thing.py"]
    prompt = recorder.calls[0]["prompt"]
    # The bug this replaces: with the switch on, a `/goal` first line made the whole
    # input a command invocation and none of this reached the model -- 0 turns, $0.00,
    # subtype "success", and a no-op guard blaming the model.
    assert "Change the value `src/thing.py` exports" in prompt  # the specification text
    assert "Run `make test` before you finish and leave it passing." in prompt
    assert "/goal" not in prompt
    assert prompt.startswith("You are the implementation role")

    system_prompt = recorder.calls[0]["system_prompt"]
    if goal_enabled:
        assert system_prompt == code_module.GOAL_SYSTEM_PROMPT
        assert code_module.GOAL_CONDITION in system_prompt
    else:
        assert system_prompt is None


# --------------------------------------------------------------------------------------
# what this repository has learned reaches every role prompt
# --------------------------------------------------------------------------------------


# One plausible episodic record, and a sentinel: no other part of any of the four prompts
# can produce this sentence, so `_RECALLED_TEXT in prompt` says the recalled text really
# arrived rather than that some paragraph happens to talk about memory.
_RECALLED_TEXT = "`make setup` is a hard prerequisite in a fresh container"

# The three ids `TaskMemory.for_state` reads, and all three are required: `.enabled` is
# `memory_id and config_id and task_id`, so a state carrying two of them is exactly as
# memoryless as one carrying none. A module constant rather than a function -- unlike
# `tests/test_agent_memory.py::_memory_state`, nothing here mutates what it is handed.
_MEMORY_KEYS = {"taskId": "t-1", "configId": "cfg-1", "memoryId": "mem-abc"}


class _RecallingClient:
    """A `bedrock-agentcore` data-plane client answering one record and capturing the query.

    `searches` is the point rather than an extra: a prompt could carry recalled text off a
    stale slot or a hard-coded default, so the recorded call is the only evidence that the
    node actually asked memory anything. No `create_event` -- nothing in this section
    writes, and a method nothing calls would invite an assertion about a write no node makes.
    """

    def __init__(self) -> None:
        self.searches: list[dict[str, Any]] = []

    def retrieve_memory_records(self, **kwargs: Any) -> dict[str, Any]:
        self.searches.append(kwargs)
        return {"memoryRecordSummaries": [{"content": {"text": _RECALLED_TEXT}}]}


def _install_recall(monkeypatch: pytest.MonkeyPatch) -> _RecallingClient:
    """Patch the memory module's own `boto3.client`, the way `tests/test_agent_memory.py` does.

    Never global `boto3`: the reference `agentcore_memory` holds is the one a call goes
    through, and patching that one is what proves the client is built inside the call rather
    than at import.
    """
    client = _RecallingClient()

    def _fake_client(name: str, **kwargs: Any) -> Any:
        assert name == "bedrock-agentcore", (
            f"the data plane is the only client the container may build; got {name!r} -- the "
            "control plane (`bedrock-agentcore-control`) creates memory resources and is the "
            "provisioner's, not a node's"
        )
        return client

    monkeypatch.setattr(agentcore_memory.boto3, "client", _fake_client)
    return client


def _forbid_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make building a memory client a test failure rather than a slow no-op.

    `no_aws` clears the environment; it does not stop a client being constructed. A task
    with no memory resource must reach AWS *not at all* -- `recall` returns on
    `not memory.enabled`, before anything could build one -- and this is what asserts that,
    rather than only that the prompt came out with a placeholder in it.
    """

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an AWS client was built for a task that has no memory")

    monkeypatch.setattr(agentcore_memory.boto3, "client", _explode)


async def _role_prompt(
    role: str,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    **extra: Any,
) -> str:
    """Run the node that owns `role` and return the prompt its first session received.

    One helper for four roles because the assertion is the same for all four and the setup
    is not: each node reads something back afterwards, so the recorder's `side_effect` has
    to write it or the node raises before a test can look at the prompt. `plan`/`replan`
    write the per-iteration specification, `code` writes a source file so the no-op guard
    passes, and `review` reads only the tree it was handed.

    `run_role` is patched on the *module object* (`plan_module.subagents` and friends),
    which is why every node calls through the module rather than importing the function.
    `calls[0]` is the session under test in every case: on `review` it is the verdict
    session, and any advisory pass is a later call carrying a slash command rather than a
    prompt -- here that is `calls[1]` alone, since `/security-review` is skipped in a
    repository with no `refs/remotes/origin/HEAD`.
    """
    state: dict[str, Any] = {
        "repoDir": str(repo),
        "repository": "owner/repo",
        "description": "change the exported value",
        "baseBranch": "main",
        "verifyCommand": "make test",
        **extra,
    }

    if role in ("plan", "replan"):
        # The iteration is what selects the role, and `replan` is the one that only renders
        # on iteration >= 1 -- the prompt an unfilled slot would raise `KeyError` from in
        # production and nowhere else.
        iteration = 1 if role == "replan" else 0

        def write_the_spec(_role: str, prompt: str, cwd: Path) -> None:
            write(Path(cwd) / ".agent" / "spec" / f"iteration-{iteration}.md", _SPEC_TEXT)

        recorder = RoleRecorder(side_effect=write_the_spec)
        monkeypatch.setattr(plan_module.subagents, "run_role", recorder)
        state["iteration"] = iteration
        if role == "replan":
            # The failure the re-plan is about, because it is also the second half of the
            # re-plan's recall query.
            state["verifyResult"] = {"command": "make test", "warning": "src/thing.py failed"}
        await plan_module.plan(state)
    elif role == "code":
        spec = write(repo / ".agent" / "spec" / "iteration-0.md", _SPEC_TEXT)

        def write_the_change(_role: str, prompt: str, cwd: Path) -> None:
            write(Path(cwd) / "src" / "thing.py", "value = 2\n")

        recorder = RoleRecorder(side_effect=write_the_change)
        monkeypatch.setattr(code_module.subagents, "run_role", recorder)
        # `_code_cfg` rather than the environment, so this reads no host configuration; the
        # goal switch is irrelevant here and off is the cheaper half.
        await code_module.code(
            {**state, "specPath": str(spec), "specText": _SPEC_TEXT},
            _code_cfg(goal_enabled=False),
        )
    else:
        recorder = RoleRecorder()
        monkeypatch.setattr(review_module.subagents, "run_role", recorder)
        # `_repo`'s clone has no `refs/remotes/origin/HEAD`, so `/security-review` is
        # skipped rather than dispatched -- existing behaviour, and nothing here needs it.
        await review_module.review({**state, "commandResult": _command_result()})

    assert recorder.roles[0] == role, (
        f"the node ran {recorder.roles[0]!r} rather than {role!r}, so the prompt below is "
        "not the one this parametrisation is about"
    )
    return str(recorder.calls[0]["prompt"])


@pytest.mark.parametrize("role", ["plan", "replan", "code", "review"])
async def test_every_role_prompt_carries_what_the_repository_has_learned(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All four prompts paste the repository's episodic memory in, and promise the tool.

    Pasted by the node rather than left to a model's decision to call
    `mcp__memory__memory_search`: a role that never searches never learns, and there is no
    signal anywhere in a run to say it did not.
    """
    no_aws(monkeypatch)
    client = _install_recall(monkeypatch)
    repo = _repo(tmp_path)

    prompt = await _role_prompt(role, repo, monkeypatch, **_MEMORY_KEYS)

    assert _RECALLED_TEXT in prompt, f"the {role} prompt does not carry the recalled record"
    assert "mcp__memory__" in prompt, (
        "the tool sentence is generated per task now, so a run that really has a memory "
        "resource must say so -- a role never told about the server never calls it"
    )
    assert client.searches, (
        "a prompt built with no search at all would satisfy the assertion above off a stale "
        "slot or a hard-coded string; the recorded call is the only evidence of a real recall"
    )
    query = client.searches[0]["searchCriteria"]["searchQuery"]
    assert 0 < len(query) <= agentcore_memory.MAX_QUERY_CHARS, (
        "`RetrieveMemoryRecords` is a semantic search and not a grep, so an uncapped query "
        f"is a whole specification embedding to noise; this one sent {len(query)} characters"
    )
    if role == "replan":
        assert "src/thing.py failed" in query, (
            "the re-plan asks memory about the FAILURE and not about the task in general: a "
            "query about the task returns the records the attempt that just failed already had"
        )
    if role == "code":
        # The specification's first line. The coder's query is the description plus
        # `specText`, and `## Goal` is well inside the 400-character cap.
        assert "## Goal" in query, "the coder recalls against what it is about to implement"


@pytest.mark.parametrize("role", ["plan", "replan", "code", "review"])
async def test_a_memoryless_run_renders_every_prompt_and_reaches_no_aws(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task with no memory resource renders all four prompts and builds no client at all.

    Two of the three assertions are in the machinery rather than on a line below. Reaching
    the node at all is the `KeyError` check: `str.format` ignores an *extra* keyword
    argument but raises on a slot with no value, so a `{recalled_memory}` added to one
    prompt and left unfilled in another fails here and nowhere else -- `replan` is in the
    parametrisation for exactly that. And `_forbid_aws` is the no-AWS check.

    The placeholder is a stated sentence and never `""`, for the same reason
    `format_records` says "No memory records matched.": a model cannot tell an empty slot
    from a substitution that broke, and it will explain the blank to itself either way.
    """
    no_aws(monkeypatch)
    _forbid_aws(monkeypatch)
    repo = _repo(tmp_path)

    prompt = await _role_prompt(role, repo, monkeypatch)

    assert "(nothing recorded yet)" in prompt, (
        f"the {role} prompt left the recalled-memory slot blank, which a model reads as a "
        "broken prompt rather than as a repository with nothing recorded yet"
    )
    assert "mcp__memory__" not in prompt, (
        "there is no memory server on this run, and a sentence promising one buys turns "
        "spent discovering a tool the role does not have"
    )
    assert _RECALLED_TEXT not in prompt, "nothing was recalled, so nothing may be quoted"


# --------------------------------------------------------------------------------------
# checkout
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "verify_command", "setup_command"),
    [
        ({"Makefile": "test:\n\t@true\n"}, "make test", ""),
        ({"package.json": _PACKAGE_JSON_WITH_TEST}, "npm test", ""),
        ({"README.md": "this repository declares nothing\n"}, "", ""),
        ({"Makefile": "install:\n\t@true\n"}, "", ""),
        ({"Makefile": ".PHONY: test\nbuild:\n\t@true\n"}, "", ""),
        ({"Makefile": "setup:\n\t@true\ntest:\n\t@true\n"}, "make test", "make setup"),
        ({"package.json": _PACKAGE_JSON_WITH_SETUP}, "", "npm run setup"),
    ],
    ids=[
        "makefile-test",
        "package-json-test",
        "nothing-declared",
        "makefile-install-only",
        "phony-test-only",
        "makefile-setup-and-test",
        "package-json-setup",
    ],
)
async def test_detection_only_uses_declared_entry_points(
    files: dict[str, str],
    verify_command: str,
    setup_command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant 35: only an entry point the repository declared, and never `install:`."""
    no_aws(monkeypatch)
    _work_roots(tmp_path, monkeypatch)
    source = make_git_repo(tmp_path / "src", files=files)

    update = await _checkout(source)

    assert update["verifyCommand"] == verify_command, (
        "guessing `python -m pytest` against an interpreter with no dependencies "
        "installed reports a missing environment as a red suite"
    )
    assert update["setupCommand"] == setup_command, (
        "`install:` in the wild means anything from `install dependencies` to `install "
        "this program onto the system`, and a wrong setup command switches verification "
        "off silently, one draft pull request at a time"
    )


async def test_a_clone_provided_agent_directory_is_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository shipping its own `.agent/` has it moved aside, not read as our state."""
    no_aws(monkeypatch)
    _work_roots(tmp_path, monkeypatch)
    source = make_git_repo(
        tmp_path / "src",
        files={"README.md": "seed\n", ".agent/notes.md": "repository content\n"},
    )

    update = await _checkout(source)
    repo_dir = Path(update["repoDir"])

    assert (repo_dir / ".agent.repo" / "notes.md").read_text(
        encoding="utf-8"
    ) == "repository content\n"
    assert not (repo_dir / ".agent" / "notes.md").exists(), (
        "the clone's `.agent/` would otherwise be read as this task's specification and "
        "memory -- and would be written over by them"
    )
    assert (repo_dir / ".agent" / "memory").is_dir(), "this task's own .agent/ is created fresh"
    assert (repo_dir / ".agent" / "spec").is_dir()


async def test_a_resumed_checkout_does_not_requarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a resume the `.agent/` present is ours, and moving it would destroy the resume."""
    no_aws(monkeypatch)
    _work_roots(tmp_path, monkeypatch)
    source = make_git_repo(
        tmp_path / "src",
        files={"README.md": "seed\n", ".agent/memory/plan.md": "what the first attempt learned\n"},
    )

    update = await _checkout(source, resumed=True, iteration=2, reviewRerun=True)
    repo_dir = Path(update["repoDir"])

    assert (repo_dir / ".agent" / "memory" / "plan.md").is_file(), (
        "the spec and the memory a resume exists to reuse live here; quarantining them "
        "would throw away the hours the checkpoint was kept for"
    )
    assert not (repo_dir / ".agent.repo").exists()
    # And the loop budget is not handed back either.
    assert "iteration" not in update
    assert "reviewRerun" not in update


async def test_the_linked_venv_is_not_a_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly prepared workspace has no uncommitted change at all (invariant 5).

    `_link_venv` creates `repo/.venv`, and a repository whose own `.gitignore` does not
    mention it -- the clone source here deliberately has no `.gitignore` -- reported that
    symlink as an untracked change. That single path satisfied the no-op guard in both
    `nodes/code.py` and `nodes/submit.py`, and `git add -A` committed the symlink into the
    pull request. `checkout` now writes the pattern into `.git/info/exclude`, which is
    per-clone and untracked, so the fix cannot appear in the diff either.
    """
    no_aws(monkeypatch)
    _work_roots(tmp_path, monkeypatch)
    source = make_git_repo(tmp_path / "src", files={"README.md": "seed\n"})

    update = await _checkout(source)
    repo_dir = Path(update["repoDir"])

    assert (repo_dir / ".venv").is_symlink(), "the venv link is still created"
    assert git_ops.changed_paths(repo_dir) == [], "nothing the agent itself did is a change"
    assert not submit_module.has_source_changes(repo_dir)

    # Idempotent: a resumed checkout must not append the pattern a second time.
    exclude = repo_dir / ".git" / "info" / "exclude"
    once = exclude.read_text(encoding="utf-8")
    await _checkout(source, resumed=True)
    assert exclude.read_text(encoding="utf-8") == once
