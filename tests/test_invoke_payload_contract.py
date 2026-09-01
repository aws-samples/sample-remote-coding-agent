"""The invocation payload's *field names* are the contract between the queue and the container.

`api/src/handlers/dispatcher.py::_payload` decides what a container is handed;
`agent/src/coding_agent/server.py::_REQUIRED` decides what it demands and
`graph._initial_state` decides what it reads. Nothing links the two but strings spelled
twice in two deployment units, and this is what that cost: the dispatcher sent `prompt`,
`_REQUIRED` demanded `description`, `tests/test_api_dispatcher.py` asserted `prompt` and
`tests/test_server.py` asserted `description`, so **both suites passed while every
invocation was broken**. On the first invocation this repository ever managed to reach a
running container the runtime answered 500 with `ValueError: the invocation payload is
missing description`.

So there is now one definition of the shape -- `tests/invoke_payload.py` -- and this file is
what makes the two halves fail *together*. A rename on either side fails here, a field the
container reads that nobody sends fails here, and a field the dispatcher sends that nothing
reads fails here until somebody writes down who consumes it and why (invariant 13b).

Both container modules are imported directly: unlike `provisioner.py` in
`tests/test_env_contract.py`, neither needs a region, so real calls into `server._validate`
and `graph._initial_state` are available and are strictly stronger evidence than a scan of
the source text. No handler module is imported at all -- that would be a collection error
rather than a test failure -- because the dispatcher's key set is asserted in
`tests/test_api_dispatcher.py`, where the moto and `FakeAgentCore` machinery already lives.
"""

from __future__ import annotations

import invoke_payload
from coding_agent import graph, server

# Sent by `dispatcher._payload` and deliberately not read by the container. Each entry is a
# reason, not an allowlist: the reason is what the next reader needs.
SENT_NOT_READ: dict[str, str] = {
    "repositoryUrl": (
        "`nodes/checkout.py:172` derives the clone URL as "
        "`https://github.com/{repository}.git` instead. Mapping this onto `cloneSource` "
        "would put an operator-supplied URL on the clone path, and `configurations.py` "
        "accepts a `git@github.com:` form there, which would clone with no SSH key where "
        "the current derivation works -- invariant 11 is about exactly that field."
    ),
    "defaultBranch": (
        "`nodes/checkout.py:179` resolves the base branch with "
        "`git_ops.default_branch(repo_dir)` off the clone. A KNOWN GAP, named rather than "
        "closed: an operator's `PATCH /configurations` override of `defaultBranch` "
        "(`configurations.EDITABLE_FIELDS`) does not reach the container, because mapping "
        "it onto `baseBranch` would fail `checkout` outright for a branch that does not "
        "exist in the clone, where detection currently succeeds."
    ),
    "resume": (
        "Correctly ignored. `run_task`'s own reading of the checkpoint decides whether a "
        "run is a resume, so a redelivered payload cannot lie about it either way -- see "
        '`tests/test_checkpoint.py:355`, which asserts `"resumed" not in payload`. That '
        "is why `_initial_state` writes `resumed=False` unconditionally."
    ),
    "sessionId": (
        "Already an `InvokeAgentRuntime` API parameter (`runtimeSessionId`, invariant 18), "
        "so the container is running *in* it and has no reason to read it back. It rides "
        "in the body for log correlation."
    ),
}

# Read by `graph._initial_state` when present, and sent by nothing today.
READ_NOT_SENT: dict[str, str] = {
    "cloneSource": (
        "The override channel `nodes/checkout.py` comments on, used by "
        "`local_run.py` to point the graph at a local working copy. No queued task sends "
        "it, and `repositoryUrl` deliberately does not become it -- see `SENT_NOT_READ`."
    ),
    "baseBranch": (
        "Written by `checkout` after it resolves the default branch off the clone, and "
        "readable from a payload as the same override channel. No queued task sends it, "
        "and `defaultBranch` deliberately does not become it -- see `SENT_NOT_READ`."
    ),
}

_MIN_REASON = 60


def test_the_container_requires_exactly_the_declared_required_fields() -> None:
    """`server._REQUIRED` is the gate every invocation passes through. The incident was
    exactly this set disagreeing with what the dispatcher sends."""
    assert set(server._REQUIRED) == set(invoke_payload.REQUIRED), (
        "agent/src/coding_agent/server.py::_REQUIRED and tests/invoke_payload.py::REQUIRED "
        "are the two halves of one wire contract; renaming a field needs both"
    )


def test_the_two_container_modules_agree_on_the_required_fields() -> None:
    """The half a wire-name-only fix leaves open.

    `server.py` validating one spelling while `graph.py` reads another is not a rejected
    invocation: `_validate` passes, the entrypoint answers `{"status": "accepted"}`, and the
    `KeyError` happens on the graph thread afterwards, where the only trace is a `FAILED`
    status.
    """
    assert set(graph.PAYLOAD_TO_STATE) == set(server._REQUIRED)


def test_the_canonical_payload_passes_the_container_gate() -> None:
    """Not set algebra: the real gate, run against the real payload, both shapes."""
    server._validate(invoke_payload.payload())
    server._validate(invoke_payload.required_only())


def test_the_graph_turns_the_wire_payload_into_the_state_the_nodes_read() -> None:
    """The one translation point. `prompt` on the wire becomes `description` in the state,
    because `description` is what three role prompts and the commit subject interpolate."""
    payload = invoke_payload.payload()
    state = graph._initial_state(payload)

    assert state["description"] == payload["prompt"]
    for wire, key in graph.PAYLOAD_TO_STATE.items():
        assert state[key] == payload[wire], f"{wire} did not reach the state as {key}"
    assert state["secretArn"] == payload["secretArn"]
    assert state["verifyCommand"] == payload["verifyCommand"]
    assert state["setupCommand"] == payload["setupCommand"]
    # The payload has no say in this one; `run_task` re-asserts it off the checkpoint.
    assert state["resumed"] is False

    # The cold path of a task whose configuration carries no commands: it must not require
    # an optional field, and it must not invent one either -- an empty `verifyCommand` in
    # the state would have `verify` run a command nobody configured.
    bare = graph._initial_state(invoke_payload.required_only())
    assert bare["description"] == invoke_payload.payload()["prompt"]
    assert not set(bare) & set(graph.OPTIONAL_PAYLOAD_FIELDS)


def test_every_field_the_dispatcher_sends_is_classified() -> None:
    """Every field on the wire is either mapped onto a state key, read as an optional, or
    declared as sent-and-unread with a reason. This is what stops the seam going unowned
    again."""
    mapped = set(graph.PAYLOAD_TO_STATE)
    optional = set(graph.OPTIONAL_PAYLOAD_FIELDS)
    declared = set(SENT_NOT_READ)
    sent = set(invoke_payload.FIELDS)

    unclassified = sent - (mapped | optional | declared)
    assert not unclassified, (
        f"{sorted(unclassified)} is sent by api/src/handlers/dispatcher.py::_payload and "
        "classified nowhere. A field added there has to be either mapped onto a `TaskState` "
        "key in graph.PAYLOAD_TO_STATE/OPTIONAL_PAYLOAD_FIELDS, or declared in this file's "
        "SENT_NOT_READ with a reason saying who consumes it instead."
    )
    assert (mapped | optional | declared) - set(READ_NOT_SENT) == sent

    for left, right, names in (
        (mapped, optional, "PAYLOAD_TO_STATE and OPTIONAL_PAYLOAD_FIELDS"),
        (mapped, declared, "PAYLOAD_TO_STATE and SENT_NOT_READ"),
        (optional, declared, "OPTIONAL_PAYLOAD_FIELDS and SENT_NOT_READ"),
    ):
        assert not left & right, f"{sorted(left & right)} is in both {names}"


def test_every_field_the_container_reads_is_sent_or_declared() -> None:
    """The direction that would have caught `secretArn`, which the container read from the
    payload and the dispatcher sent under no spelling at all -- so every container clone
    was anonymous and no pull request could be opened."""
    read = set(graph.PAYLOAD_TO_STATE) | set(graph.OPTIONAL_PAYLOAD_FIELDS)
    sent = set(invoke_payload.FIELDS)

    unsent = read - sent - set(READ_NOT_SENT)
    assert not unsent, (
        f"{sorted(unsent)} is read out of the invocation payload by "
        "agent/src/coding_agent/graph.py and sent by nothing: either "
        "api/src/handlers/dispatcher.py::_payload has to send it, or it has to be declared "
        "in READ_NOT_SENT with a reason"
    )

    # A stale declaration fails rather than lingering: once a field is genuinely sent, or
    # is no longer read, its entry here is a lie about the system.
    for field, reason in READ_NOT_SENT.items():
        assert field in graph.OPTIONAL_PAYLOAD_FIELDS, f"{field} is not read at all: {reason}"
        assert field not in sent, f"{field} is sent now, so it does not belong in READ_NOT_SENT"


def test_the_declaration_tables_carry_a_reason() -> None:
    """The only thing keeping the two tables from becoming a silent allowlist."""
    for table, name in ((SENT_NOT_READ, "SENT_NOT_READ"), (READ_NOT_SENT, "READ_NOT_SENT")):
        for field, reason in table.items():
            assert isinstance(reason, str)
            assert len(reason.strip()) >= _MIN_REASON, (
                f"{name}[{field!r}] needs a reason a reader can act on, naming the file "
                "that makes it true"
            )
