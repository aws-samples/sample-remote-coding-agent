"""The three AgentCore figures, and the two vocabularies that must agree with them.

Every assertion here is about a *number or a string that was measured against the live
service* and cannot be re-derived from anything in the repository:

- `filesystemConfigurations[0].sessionStorage.mountPath = /mnt/workspace` -- without a
  session-storage mount the container's filesystem is gone on the next invoke, so a
  resume re-clones and re-plans and the run loops. `MountPath`'s own pattern allows
  exactly one level under `/mnt`, and botocore does **not** enforce a `pattern`
  client-side, so `/mnt/work/space` is a bare server-side `ValidationException`.
- `lifecycleConfiguration.maxLifetime = 28800` -- the service default is 2 hours and a
  real task ran 3h34m; the failure looks like the container crashing.
- one `StopRuntimeSession` per terminal task -- a session left running bills for the
  rest of `maxLifetime`, which with the figure above is eight hours per task.

The figures are asserted twice over: as constants, and as what `_desired_runtime`
actually sends when the live runtime disagrees. That second half is invariant 24 -- the
classification carry-forward-vs-converge -- and it is the one that cannot be reviewed by
reading `provisioner.py`, because a carried-forward field looks identical to a converged
one at the call site.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import boto3
import pytest
from coding_agent.commands import VERIFY_TIMEOUT
from conftest import FAKE_AWS_CREDENTIALS, REPO_ROOT
from test_api_fakes import load_handlers as _load

# The one client in this file, and it is never called: it is here as the authority on
# what the real API's own shapes allow.
_MODEL = boto3.client(
    "bedrock-agentcore-control",
    region_name="ap-southeast-1",
    # Built at import, so `conftest._aws_env` has not run yet and the environment carries
    # nothing: this client needs credentials of its own to exist at all.
    **FAKE_AWS_CREDENTIALS,
).meta.service_model
_UPDATE_SHAPE = _MODEL.operation_model("UpdateAgentRuntime").input_shape

CONFIG = {
    "configId": "cfg_0123456789ab",
    "repository": "owner/repo",
    "defaultBranch": "main",
    "verifyCommand": "make test",
    "executionRoleArn": "arn:aws:iam::123456789012:role/rca/rca-cfg-0123456789ab",
}


@pytest.fixture
def figures(aws: Any) -> tuple[Any, Any, Any]:
    """`(provisioner, watchdog, runs)`, loaded inside `moto`'s mock."""
    _common, provisioner, watchdog, runs = _load("provisioner", "watchdog", "runs")
    return provisioner, watchdog, runs


# -- the mount --------------------------------------------------------------------


def test_the_mount_path_is_exactly_one_level_under_mnt(figures: tuple[Any, Any, Any]) -> None:
    provisioner, _watchdog, _runs = figures
    assert provisioner.MOUNT_PATH == "/mnt/workspace"

    mount_shape = (
        _UPDATE_SHAPE.members["filesystemConfigurations"]
        .member.members["sessionStorage"]
        .members["mountPath"]
    )
    pattern = mount_shape.metadata["pattern"]
    assert pattern == "/mnt/[a-zA-Z0-9._-]+/?", (
        "MountPath's own pattern changed; re-read it before trusting the guard below"
    )
    assert provisioner.observability.is_valid_mount_path(provisioner.MOUNT_PATH)
    # The guard exists because botocore does not enforce `pattern` client-side: a
    # two-level path reaches the service and comes back as a bare ValidationException.
    for rejected in ("/mnt/work/space", "/workspace", "/mnt/", "/mnt/work space", "/tmp/workspace"):
        assert not provisioner.observability.is_valid_mount_path(rejected)


def test_the_work_root_lives_under_the_mount(figures: tuple[Any, Any, Any]) -> None:
    """Beside the mount rather than under it is the same bug as no mount at all: the
    work tree is what a resume needs to still be there."""
    provisioner, _watchdog, _runs = figures
    assert provisioner.WORK_ROOT.startswith(f"{provisioner.MOUNT_PATH}/")


def test_no_agent_module_names_a_different_mount(figures: tuple[Any, Any, Any]) -> None:
    """The container and the runtime spec are separate deployment units that cannot
    import each other, so the mount is written twice. This is the reconciliation.

    Vacuous until phase 2 lands `agent/src/coding_agent/**`, and meaningful the moment
    it does -- which is why it globs rather than naming a module.
    """
    provisioner, _watchdog, _runs = figures
    agreed = (provisioner.MOUNT_PATH, provisioner.WORK_ROOT)
    for path in sorted((REPO_ROOT / "agent/src/coding_agent").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literal = node.value
                if literal.startswith("/mnt") and not literal.startswith(agreed):
                    pytest.fail(
                        f"{Path(path).relative_to(REPO_ROOT)}:{node.lineno} names {literal!r}, "
                        f"which is not under {provisioner.MOUNT_PATH}. The runtime mounts session "
                        f"storage there and nowhere else, so a path outside it is lost on the next "
                        f"invoke and the resume re-clones."
                    )


def test_only_session_storage_is_ever_sent_as_a_filesystem_configuration(
    figures: tuple[Any, Any, Any],
) -> None:
    """`FilesystemConfiguration` is a four-way union (`sessionStorage`,
    `efsAccessPoint`, `s3FilesAccessPoint`, `capacityProviderVolume`) and only the first
    survives a session. An EFS or S3 member appearing here means a resume reads a
    filesystem that no longer holds the work tree."""
    provisioner, _watchdog, _runs = figures
    union = set(_UPDATE_SHAPE.members["filesystemConfigurations"].member.members)
    assert "sessionStorage" in union and len(union) > 1, (
        "the union collapsed to one member; this test is asserting nothing"
    )

    desired = provisioner._desired_runtime(None, CONFIG)
    (entry,) = desired["filesystemConfigurations"]
    assert set(entry) == {"sessionStorage"}, f"only sessionStorage may be sent, got {sorted(entry)}"
    assert entry["sessionStorage"] == {"mountPath": provisioner.MOUNT_PATH}


# -- the lifetime -----------------------------------------------------------------


def test_the_max_lifetime_is_eight_hours_and_the_service_accepts_it(
    figures: tuple[Any, Any, Any],
) -> None:
    provisioner, _watchdog, _runs = figures
    assert provisioner.MAX_LIFETIME_SECONDS == 28800

    bounds = _UPDATE_SHAPE.members["lifecycleConfiguration"].members["maxLifetime"].metadata
    assert bounds["min"] <= provisioner.MAX_LIFETIME_SECONDS <= bounds["max"], (
        f"maxLifetime must be within the service's own {bounds['min']}..{bounds['max']}; "
        f"outside it every CreateAgentRuntime fails validation and the whole fleet is "
        f"unprovisionable"
    )


# -- carried forward vs converged -------------------------------------------------


def _drifted_runtime() -> dict[str, Any]:
    """A live runtime that predates all three figures and carries two things nobody here
    knows about: an idle timeout an operator raised, and an environment key a later
    version of the agent added."""
    return {
        "agentRuntimeId": "rca_cfg_0123456789ab-ABC123",
        "agentRuntimeArn": (
            "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/rca_cfg_0123456789ab-ABC123"
        ),
        "roleArn": "arn:aws:iam::123456789012:role/rca/rca-cfg-0123456789ab",
        "status": "READY",
        "description": "a description an operator edited by hand",
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "networkConfiguration": {"networkMode": "PUBLIC"},
        # No session storage at all, and the service default lifetime.
        "lifecycleConfiguration": {"maxLifetime": 7200, "idleRuntimeSessionTimeout": 1800},
        "environmentVariables": {"A_KEY_THIS_CODE_HAS_NEVER_HEARD_OF": "keep me"},
        "agentRuntimeArtifact": {
            "containerConfiguration": {
                "containerUri": "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/rca:old"
            }
        },
    }


def test_all_three_figures_converge_rather_than_carrying_forward(
    figures: tuple[Any, Any, Any],
) -> None:
    """The invariant-24 test. `UpdateAgentRuntime` is a full replace, so a field omitted
    is a field deleted -- which makes carry-forward the safe-looking default and exactly
    wrong for a fleet figure. Both runtimes on the deploy this was measured against
    predated session storage and the 8-hour lifetime; carried forward, neither would ever
    have got them, and no number of re-issued runtime updates would have fixed it."""
    provisioner, _watchdog, _runs = figures
    live = _drifted_runtime()
    desired = provisioner._desired_runtime(live, CONFIG)

    assert desired["lifecycleConfiguration"]["maxLifetime"] == provisioner.MAX_LIFETIME_SECONDS, (
        "maxLifetime is a fleet figure and must be overwritten, not carried forward"
    )
    assert desired["filesystemConfigurations"] == [
        {"sessionStorage": {"mountPath": provisioner.MOUNT_PATH}}
    ], "the mount is a fleet figure: a runtime that predates session storage must gain it"
    assert desired["environmentVariables"]["WORK_ROOT"] == provisioner.WORK_ROOT

    # And the update is recognised as *needed*: a guard that compared the image alone
    # would short-circuit here and the mount would stay unreachable forever.
    assert not provisioner._matches_intent(live, desired)


def test_the_fields_that_are_carried_forward_actually_are(figures: tuple[Any, Any, Any]) -> None:
    """The other half of the same classification. An operator's idle timeout and an
    environment key this version has never heard of survive an update; converging them
    to a default would silently undo a deliberate change on every deploy."""
    provisioner, _watchdog, _runs = figures
    live = _drifted_runtime()
    desired = provisioner._desired_runtime(live, CONFIG)

    assert desired["lifecycleConfiguration"]["idleRuntimeSessionTimeout"] == 1800
    assert desired["description"] == live["description"]
    assert desired["roleArn"] == live["roleArn"]
    assert desired["environmentVariables"]["A_KEY_THIS_CODE_HAS_NEVER_HEARD_OF"] == "keep me"
    # An update names the runtime by id and must not carry a name: `UpdateAgentRuntime`
    # rejects `agentRuntimeName` outright.
    assert desired["agentRuntimeId"] == live["agentRuntimeId"]
    assert "agentRuntimeName" not in desired


def test_a_converged_runtime_is_left_alone(figures: tuple[Any, Any, Any]) -> None:
    """The stopping condition: every SQS redelivery of a `CHECK` or `UPDATE` would
    otherwise issue another update, and each one bounces the runtime."""
    provisioner, _watchdog, _runs = figures
    desired = provisioner._desired_runtime(None, CONFIG)
    already = {
        **{key: value for key, value in desired.items() if key != "agentRuntimeName"},
        "agentRuntimeId": "rca_cfg_0123456789ab-ABC123",
        "status": "READY",
        "agentRuntimeVersion": "4",
    }
    assert provisioner._matches_intent(already, provisioner._desired_runtime(already, CONFIG))


def test_every_field_the_update_sends_is_a_field_the_update_accepts(
    figures: tuple[Any, Any, Any],
) -> None:
    """A full-replace call is only safe if it is complete, and "complete" is defined by
    the service model rather than by this repository."""
    provisioner, _watchdog, _runs = figures
    desired = provisioner._desired_runtime(_drifted_runtime(), CONFIG)
    unknown = set(desired) - set(_UPDATE_SHAPE.members)
    assert not unknown, f"UpdateAgentRuntime does not accept {sorted(unknown)}"
    for required in ("agentRuntimeId", "agentRuntimeArtifact", "roleArn", "networkConfiguration"):
        assert required in desired, f"UpdateAgentRuntime requires {required} on every call"


# -- the session, and the two vocabularies ----------------------------------------


def test_stopping_a_session_has_exactly_one_owner() -> None:
    """One `StopRuntimeSession` per terminal task, and therefore one call site. A second
    caller is how a session gets stopped twice (a 409 that reads like a service fault) or
    not at all -- and an unstopped session bills for the remaining `maxLifetime`, which
    is eight hours."""
    handlers = REPO_ROOT / "api/src/handlers"
    call_sites = {
        path.stem: [
            node.lineno
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "stop_runtime_session"
        ]
        for path in sorted(handlers.glob("*.py"))
    }
    called_by = {module: lines for module, lines in call_sites.items() if lines}
    assert list(called_by) == ["watchdog"], (
        f"StopRuntimeSession is called from {sorted(called_by)}; the watchdog is the one owner "
        f"of session teardown because it is the one sweep that knows a task reached a terminal "
        f"status"
    )
    assert len(called_by["watchdog"]) == 1, "one call site, retried in place, not two"


def test_the_watchdog_and_the_task_vocabulary_cannot_drift(figures: tuple[Any, Any, Any]) -> None:
    """`RESUMABLE_STATUSES` is the non-terminal half of `TASK_STATUSES`, spelled out
    rather than negated. Pinned here because a status in *neither* set is invisible to
    the sweep: a task in it is never resumed and never failed, so it stays live forever
    with its session billing."""
    _provisioner, watchdog, runs = figures
    assert set(runs.TASK_STATUSES) - runs.TERMINAL_STATUSES == set(watchdog.RESUMABLE_STATUSES)
    assert set(runs.TASK_STATUSES) >= runs.TERMINAL_STATUSES
    assert not watchdog.RESUMABLE_STATUSES & runs.TERMINAL_STATUSES


def test_the_quiet_threshold_clears_the_agents_verify_timeout(
    figures: tuple[Any, Any, Any],
) -> None:
    """Invariant 27. `verify` makes no model call, so a *healthy* verification emits no
    activity rows for as long as the suite runs; a threshold at or below the agent's own
    `VERIFY_TIMEOUT` re-invokes a task that is working perfectly well, and a second
    container against the same session is two branches racing."""
    provisioner, watchdog, _runs = figures
    # Read from the agent package rather than written twice: invariant 27's two halves are
    # `commands.VERIFY_TIMEOUT` and `watchdog.QUIET_THRESHOLD_SECONDS`, and a test that
    # hardcoded the first could not see the agent raise it.
    assert watchdog.QUIET_THRESHOLD_SECONDS == 2700
    # And it stays well inside the runtime's own lifetime, or the sweep could never
    # observe a quiet task before the session was killed underneath it.
    assert VERIFY_TIMEOUT < watchdog.QUIET_THRESHOLD_SECONDS < provisioner.MAX_LIFETIME_SECONDS
