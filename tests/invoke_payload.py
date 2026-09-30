"""The single definition of the AgentCore invocation payload's shape.

`api/src/handlers/dispatcher.py::_payload` writes this payload and
`agent/src/coding_agent/server.py` + `agent/src/coding_agent/graph.py` read it -- two
deployment units with nothing between them but a string spelled twice. Before this module
existed each side was tested against its own spelling: the dispatcher sent `prompt`,
`server._REQUIRED` demanded `description`, both suites were green, and every invocation
that reached a running container answered 500. So both suites assert against *this*
module, and `tests/test_invoke_payload_contract.py` holds the two halves to it -- a rename
on either side fails a test rather than a deployment (invariant 13b).

Deliberately not a `conftest.py` fixture: `tests/conftest.py` is jointly owned in this
wave. Deliberately importing nothing but `__future__` and `typing`, so any test module can
import it at collection time -- the precedent is `tests/agent_helpers.py`.
"""

from __future__ import annotations

from typing import Any

# Wire names. The graph-state name for `prompt` is `description`, and
# `graph._initial_state` is the one translation point.
REQUIRED: tuple[str, ...] = ("taskId", "configId", "prompt", "repository")

# Sent whenever the stored configuration carries them, which in practice is always: the
# dispatcher writes `""` rather than omitting a key. Whether the container *reads* each of
# these is the question `tests/test_invoke_payload_contract.py` answers, field by field.
OPTIONAL: tuple[str, ...] = (
    "repositoryUrl",
    "defaultBranch",
    "credentialProviderName",
    "verifyCommand",
    "setupCommand",
    "resume",
    "sessionId",
)

FIELDS: tuple[str, ...] = REQUIRED + OPTIONAL


def payload(**overrides: Any) -> dict[str, Any]:
    """Every field the dispatcher sends, with a plausible value for each.

    The shapes are the ones already seeded across the suite -- a `tsk_`-prefixed task id, a
    credential provider name of the shape `configurations.py` mints, and a `sessionId` over
    botocore's client-side 33-character minimum (invariant 18) -- so nothing here looks
    invented.
    """
    complete: dict[str, Any] = {
        "taskId": "tsk_1700000000000_abcdef",
        "configId": "cfg_0123456789ab",
        "prompt": "make the tests pass",
        "repository": "acme/widgets",
        "repositoryUrl": "https://github.com/acme/widgets",
        "defaultBranch": "main",
        "credentialProviderName": "rca_cfg_0123456789ab",
        "verifyCommand": "make test",
        "setupCommand": "make setup",
        "resume": False,
        # `observability.session_id("tsk_1700000000000_abcdef")` (invariant 18), written
        # out rather than imported: this module imports nothing, so the container-side
        # tests can use it without the `api/src` path setup `handlers.*` needs.
        "sessionId": "tsk_1700000000000_abcdef-822b04fc976432ceb11bc5cbfab01400",
    }
    return {**complete, **overrides}


def required_only(**overrides: Any) -> dict[str, Any]:
    """Every required field and nothing optional -- the cold path of a task whose
    configuration carries no commands.

    Filtered out of `payload()` rather than written out a second time, so the two cannot
    drift apart.
    """
    complete = payload()
    return {**{field: complete[field] for field in REQUIRED}, **overrides}
