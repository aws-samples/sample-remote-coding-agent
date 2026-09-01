# ruff: noqa: T201 -- this is a CLI; its printed output is the product, not a debug aid.
"""Run the loop on a developer machine against a local git repository.

The cheapest full exercise of `plan -> code -> verify -> review`: no AgentCore runtime, no
DynamoDB, no Secrets Manager. `store.py` is a no-op with no table configured, so nothing
here needs AWS at all -- only Bedrock credentials for the roles themselves.

It walks the nodes **by hand** rather than compiling the graph. That is deliberate and it
has a cost: walking by hand never exercises a conditional edge, so the routing it prints
is not the routing that runs in production. `tests/test_graph.py` covers the real edges,
and this module covers the nodes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from . import state as state_module
from .nodes.checkout import checkout
from .nodes.code import code
from .nodes.plan import plan
from .nodes.review import review
from .nodes.submit import submit
from .nodes.verify import verify
from .state import TaskState

log = logging.getLogger(__name__)


def _task_id(args: argparse.Namespace) -> str:
    """A deterministic id from the arguments -- no wall clock, so a test can predict it and
    a re-run reuses the same work directory instead of orphaning the last one."""
    digest = hashlib.sha256(f"{args.repo}\x00{args.task}".encode()).hexdigest()
    return f"local-{digest[:8]}"


def _repository(repo: str) -> str:
    """`owner/repo` for a remote, or `local/<dirname>` for a path.

    A local path is used as the *clone source* only and is never put on the state's
    `repository` field, because that field reaches the roles' prompts and a role handed a
    path outside its `cwd` followed it (invariant 11).
    """
    candidate = Path(repo).expanduser()
    if candidate.exists():
        return f"local/{candidate.resolve().name}"
    return repo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m coding_agent.local_run",
        description="Run the coding loop locally against a git repository.",
    )
    parser.add_argument("--repo", required=True, help="a local path, or owner/repo on GitHub")
    parser.add_argument("--task", required=True, help="what the agent should do")
    parser.add_argument(
        "--no-submit", action="store_true", help="stop before committing or pushing"
    )
    parser.add_argument("--work-root", default=None, help="where to work (default: a temp dir)")
    parser.add_argument("--verify-command", default=None, help="override command detection")
    parser.add_argument("--setup-command", default=None, help="override command detection")
    parser.add_argument("--task-id", default=None, help="default: local-<8 hex of the arguments>")
    return parser


def _initial_state(args: argparse.Namespace) -> TaskState:
    repo = Path(args.repo).expanduser()
    state: TaskState = {
        "taskId": args.task_id or _task_id(args),
        "configId": "local",
        "repository": _repository(args.repo),
        "description": args.task,
        "iteration": 0,
        "reviewRerun": False,
        "resumed": False,
        "noSubmit": bool(args.no_submit),
    }
    if repo.exists():
        state["cloneSource"] = str(repo.resolve())
    if args.verify_command is not None:
        state["verifyCommand"] = args.verify_command
    if args.setup_command is not None:
        state["setupCommand"] = args.setup_command
    return state


def _show(node: str, update: dict[str, Any]) -> None:
    print(f"\n=== {node} ===")
    for key in sorted(update):
        value = update[key]
        rendered = json.dumps(value) if isinstance(value, dict | list) else str(value)
        if len(rendered) > 400:
            rendered = rendered[:400] + " …"
        print(f"  {key}: {rendered}")


def _merged(state: TaskState, update: Mapping[str, Any]) -> TaskState:
    """The one place this walk narrows a node update back to a `TaskState`.

    A function rather than four `{**state, **update}` expressions because the narrowing is
    an assumption worth stating once. A node returns `dict[str, Any]` and it has to: an
    update may carry `roleCosts`, which is not a `TaskState` key, so a node CANNOT be typed
    as returning a partial `TaskState` and the merge genuinely cannot be expressed to a type
    checker. It is the same assumption LangGraph makes when it merges an update into the
    compiled state in `graph.py`; the difference is only that this walk does it by hand, so
    here it is written down.
    """
    merged: dict[str, Any] = {**state, **update}
    return cast("TaskState", merged)


async def _walk(state: TaskState, *, no_submit: bool) -> TaskState:
    for node, fn in (("checkout", checkout), ("plan", plan), ("code", code), ("verify", verify)):
        update = await fn(state)
        state = _merged(state, update)
        _show(node, update)

    update = await review(state)
    state = _merged(state, update)
    _show("review", update)

    if state.get("reviewRerun"):
        # The one permitted hop. Hard-coded here because this walk has no router; the real
        # graph reaches it through `graph._after_review`.
        print("\n--- the review role edited the tree; re-measuring it once ---")
        for node, fn in (("verify", verify), ("review", review)):
            update = await fn(state)
            state = _merged(state, update)
            _show(node, update)

    vr = state.get("verifyResult") or {}
    print("\n=== verifyResult ===")
    print(json.dumps(dict(vr), indent=2, default=str))
    print(f"\nterminal status: {state_module.terminal_status(vr)}")

    if no_submit:
        print("\n--no-submit: stopping before the commit, the push and the pull request")
        return state
    update = await submit(state)
    state = _merged(state, update)
    _show("submit", update)
    return state


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)

    work_root = args.work_root or tempfile.mkdtemp(prefix="coding-agent-")
    # `checkout` reads WORK_ROOT through `config.load_config()`, so setting it here is how
    # the CLI's flag reaches the node without a second configuration path.
    os.environ["WORK_ROOT"] = str(work_root)
    state = _initial_state(args)
    print(f"task {state['taskId']} on {state['repository']} in {work_root}")
    if not state.get("noSubmit") and not os.environ.get("GITHUB_TOKEN"):
        # The developer-machine path. The container fetches the token from Secrets Manager
        # at submit time and never puts it in the environment (invariant 4); locally there
        # is no secret to fetch, so GITHUB_TOKEN is the only source.
        print("warning: GITHUB_TOKEN is unset, so --no-submit is the only mode that will finish")

    final = asyncio.run(_walk(state, no_submit=bool(args.no_submit)))
    return 0 if state_module.terminal_status(final.get("verifyResult")) != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
