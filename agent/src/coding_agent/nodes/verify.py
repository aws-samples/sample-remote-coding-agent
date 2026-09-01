"""Runs the repository's own setup and verification commands. **This node judges nothing.**

That sentence is the module's whole contract. It returns a `CommandResult` -- command,
exit code, output, warning -- and holds no verdict, no diff and no model output it could
form one from. `nodes/review.py` is the only place `verifyResult` is composed, because
it is the only node holding both the exit code and the review role's verdict.

So the asymmetry is structural rather than remembered (invariant 6): this file does not
import `subagents`, and `nodes/review.py` does not import `commands`. Neither can do the
other's job even by accident.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .. import activity, commands
from ..state import TaskState

log = logging.getLogger(__name__)


async def verify(state: TaskState) -> dict[str, Any]:
    """Run setup then verification, and return what happened."""
    repo = Path(state["repoDir"])

    setup = state.get("setupCommand") or ""
    if setup and not state.get("reviewRerun"):
        # Skipped on the re-run hop: that hop re-measures a tree whose dependencies were
        # installed minutes ago, and SETUP_TIMEOUT is 900 s of nothing happening.
        log.info("running setup command %r", setup)
        setup_result = await asyncio.to_thread(commands.run_setup, setup, cwd=repo)
        # An activity row the moment setup finishes, so the watchdog's quiet threshold
        # never has to clear SETUP_TIMEOUT + VERIFY_TIMEOUT (invariant 27) -- the two
        # silences are separated by this write rather than added together.
        activity.record(
            state,
            [
                {
                    "at": activity.utcnow_iso(),
                    "node": "verify",
                    "kind": "setup",
                    "detail": setup,
                    "exitCode": setup_result["exitCode"],
                }
            ],
        )
        if setup_result["exitCode"] != 0:
            # Setup failing means nothing was verified -- it does NOT mean verification
            # failed. `review` turns this shape into unverified-and-draft, never into
            # FAILED_VERIFICATION, because re-planning a change nothing was wrong with
            # burns an iteration on a broken environment.
            log.error(
                "setup command failed with exit %s; nothing was verified", setup_result["exitCode"]
            )
            return {
                "commandResult": {
                    "command": setup,
                    "exitCode": None,
                    "output": setup_result["output"],
                    "warning": "the setup command failed; nothing was verified",
                }
            }
    elif setup:
        log.info(
            "skipping setup on the review re-run: the tree's dependencies are already installed"
        )

    verify_cmd = state.get("verifyCommand") or ""
    if not verify_cmd:
        # `exitCode: None` is how "nothing ran" is spelled, and it is what makes the PR a
        # draft two nodes later.
        return {
            "commandResult": {
                "command": "",
                "exitCode": None,
                "output": "",
                "warning": "no verification command was detected in this repository",
            }
        }

    log.info("running verification command %r", verify_cmd)
    return {"commandResult": await asyncio.to_thread(commands.run_command, verify_cmd, cwd=repo)}
