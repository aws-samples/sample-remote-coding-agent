"""The container half of invariant 32b: the interlock, the attributes, and the config.

Every test here runs with **no collector, no socket and no network**. That is not an
accident of the environment, it is the property `telemetry` is built for: the module reads
no environment variable at all and answers "is there a collector?" by probing two paths
whose defaults are injectable, so the whole of it is exercisable on a machine that has
never seen an ADOT binary. The one test that starts a process starts a two-line shell
script that sleeps and is terminated here, so nothing in this file binds a port.

The asymmetry these tests exist to pin: **the container can only turn the exporter off,
never on.** `api/src/handlers/provisioner.py`'s `_fleet_environment()` owns
`CLAUDE_CODE_ENABLE_TELEMETRY` as fleet policy; `telemetry.exporter_env` is an interlock
against the one window in which that policy can be wrong -- a runtime created against an
image built before the flag was flipped, where `CLAUDE_CODE_ENABLE_TELEMETRY=1` and no
collector means the CLI retries OTLP exports against a dead port on every turn of every
task. So `exporter_env` returning `"1"` in any state at all is a bug this file fails on.

`agent/otel/collector.yaml` is asserted against as **text** rather than through PyYAML for
everything that is load-bearing. PyYAML is not in `requirements-dev.txt` -- it arrives, if
it arrives, as a transitive dependency of `moto[cloudformation]` -- and no test in this
repository adds a dependency to make an assertion, so the structural check that really
needs a parser is the one test allowed to skip. `tests/test_env_contract.py` and
`tests/test_api_route_contract.py` already establish the read-the-source-text idiom.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
from pathlib import Path

import pytest
from coding_agent import telemetry
from coding_agent.state import TaskState
from conftest import REPO_ROOT

TELEMETRY_SOURCE = REPO_ROOT / "agent/src/coding_agent/telemetry.py"
COLLECTOR_CONFIG = REPO_ROOT / "agent/otel/collector.yaml"

# The three dimensions `resource_attributes` may emit and no others. Pinned as an
# allow-list rather than a deny-list, which is invariant 11's spirit: a state field added
# later must be *chosen* to become a metric dimension, never inherited into one.
EXPECTED_ATTRIBUTES = ("team.id", "task.id", "role")

# A state carrying both of the fields that must never leave the container. `cloneSource` is
# a filesystem path on a local run and `secretArn` names the PAT's secret; a metric
# dimension is published to CloudWatch and read by anyone with console access.
FULL_STATE: TaskState = {
    "taskId": "task-abc123",
    "configId": "cfg_deadbeef",
    "repository": "owner/repo",
    "cloneSource": "/mnt/workspace/work/task-abc123/repo",
    "secretArn": "arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:rca-pat-AbCdEf",
}

# `us-east-1` and friends. A baked region in the collector config is a wrong-region
# signature, which CloudWatch rejects rather than redirects.
_REGION_RE = re.compile(r"\b[a-z]{2}(?:-[a-z]+){1,2}-\d\b")


def _install_fake_collector(tmp_path: Path) -> tuple[Path, Path]:
    """An executable "collector" that sleeps, and a configuration file beside it.

    A real script rather than an empty file because `start_collector` really `Popen`s it,
    and `sleep` is what keeps the child alive long enough to assert on its argument list
    before the test terminates it. It binds nothing and prints nothing.
    """
    binary = tmp_path / "aws-otel-collector"
    binary.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    binary.chmod(0o755)
    config = tmp_path / "collector.yaml"
    config.write_text("service:\n  pipelines: {}\n", encoding="utf-8")
    return binary, config


def test_the_collector_is_installed_only_when_both_paths_are_there(tmp_path: Path) -> None:
    """Both paths present and the binary executable, or it counts as absent.

    An unpack that landed the binary somewhere unexpected, or a `COPY otel /opt/otel` that
    never happened, must read as *not installed* rather than as a `Popen` that raises after
    the fleet has already told the CLI to export.
    """
    binary, config = _install_fake_collector(tmp_path)
    assert telemetry.collector_installed(binary=binary, config=config) is True

    assert telemetry.collector_installed(binary=tmp_path / "absent", config=config) is False
    assert telemetry.collector_installed(binary=binary, config=tmp_path / "absent.yaml") is False

    # Present but not executable: the case a `dpkg-deb -x` into the wrong tree produces.
    binary.chmod(0o644)
    assert telemetry.collector_installed(binary=binary, config=config) is False

    # And the shipped defaults, on a machine that has no collector: this is the state every
    # test run and every `local_run` is in, and it is what makes the interlock fire.
    assert telemetry.collector_installed() is False, (
        f"{telemetry.COLLECTOR_BINARY} exists here, so this image was built with "
        "`ENABLE_OTEL_COLLECTOR=1`. That is not the shipped default, and two assertions in "
        "this file plus `tests/test_subagents.py`'s expected `options.env` depend on it."
    )


def test_no_collector_means_the_exporter_is_explicitly_switched_off(tmp_path: Path) -> None:
    """The no-retry-storm guarantee, exactly: one variable, set to `"0"`, and nothing else.

    `ClaudeAgentOptions.env` merges **over** `os.environ` and cannot unset a variable, so a
    fleet environment saying `CLAUDE_CODE_ENABLE_TELEMETRY=1` on an image with no collector
    can only be beaten by writing the `"0"` out. Without it the CLI retries OTLP exports
    against a port nothing is listening on, every turn, for the whole of every task.

    Nothing else may be in the dict: `OTEL_RESOURCE_ATTRIBUTES` on a container that exports
    nothing is a variable with no reader, and it would make the two states harder to tell
    apart than they are.
    """
    absent = tmp_path / "nothing-here"

    for state in (None, FULL_STATE):
        env = telemetry.exporter_env("code", state, binary=absent, config=absent)
        assert env == {"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}, state

    # The defaults, which is the path `subagents.build_options` really takes here.
    assert telemetry.exporter_env("plan", FULL_STATE) == {"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}


def test_the_container_can_only_switch_the_exporter_off_never_on(tmp_path: Path) -> None:
    """`exporter_env` never returns `CLAUDE_CODE_ENABLE_TELEMETRY: "1"`, in any state.

    That asymmetry is the whole design. Turning the exporter *on* is the fleet's decision,
    converged on every provision and auditable in a `GetAgentRuntime` response; a container
    that could also turn it on would be a second owner of one setting, and two owners of
    one setting disagree. So this is asserted over every combination of installed and
    state -- and then over the source, because a value that is never returned today is one
    edit away from being returned tomorrow.
    """
    binary, config = _install_fake_collector(tmp_path)
    absent = tmp_path / "nothing-here"

    for probe_binary, probe_config in ((binary, config), (absent, absent)):
        for role in ("plan", "replan", "code", "review"):
            for state in (None, FULL_STATE, {}, {"repository": "owner/repo"}):
                env = telemetry.exporter_env(role, state, binary=probe_binary, config=probe_config)
                assert env.get("CLAUDE_CODE_ENABLE_TELEMETRY") != "1", (role, state, probe_binary)

    # The source contract, read with `ast` so a mention in a docstring -- and the module's
    # own docstring does mention the variable -- cannot satisfy or break it. Only a real
    # dict literal binding the name to `"1"` counts.
    tree = ast.parse(TELEMETRY_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            named = isinstance(key, ast.Constant) and key.value == "CLAUDE_CODE_ENABLE_TELEMETRY"
            enabled = isinstance(value, ast.Constant) and value.value == "1"
            assert not (named and enabled), (
                f"{TELEMETRY_SOURCE.name}:{node.lineno} enables the exporter from inside the "
                "container. Only `provisioner._fleet_environment()` may do that: this module "
                "is an interlock and may only ever switch the exporter off."
            )


def test_an_installed_collector_contributes_the_per_turn_dimensions(tmp_path: Path) -> None:
    """With a collector and a state, one `OTEL_RESOURCE_ATTRIBUTES` and no master switch."""
    binary, config = _install_fake_collector(tmp_path)

    env = telemetry.exporter_env("code", FULL_STATE, binary=binary, config=config)

    assert set(env) == {"OTEL_RESOURCE_ATTRIBUTES"}
    attributes = env["OTEL_RESOURCE_ATTRIBUTES"]
    for key in EXPECTED_ATTRIBUTES:
        assert f"{key}=" in attributes, key
    assert "team.id=owner/repo" in attributes
    assert "task.id=task-abc123" in attributes
    assert "role=code" in attributes
    # W3C Baggage: three pairs is two separators and three `=`, so an empty value or a
    # dropped pair shows up here rather than as a dimension nobody notices is missing.
    assert attributes.count(",") == 2
    assert attributes.count("=") == 3
    assert ",," not in attributes
    assert not attributes.startswith(",") and not attributes.endswith(",")

    # No state, no attributes -- and emphatically not a placeholder. Several existing call
    # sites build options with no state at all, and `task.id=unknown` would aggregate every
    # one of them into a bucket that looks like a real task.
    assert telemetry.exporter_env("code", None, binary=binary, config=config) == {}
    assert telemetry.exporter_env("code", {}, binary=binary, config=config) == {}
    # A state with no `taskId` is the same case: there is nothing to attribute a turn to.
    no_task: TaskState = {"repository": "owner/repo"}
    assert telemetry.exporter_env("code", no_task, binary=binary, config=config) == {}


def test_the_attributes_name_exactly_three_keys_and_no_secret_field() -> None:
    """An allow-list of dimensions, and nothing off the state that must stay in the clone.

    `configId` is deliberately absent: invariant 32b's prose and its code sketch disagree
    about `team.id`, and this resolves it in favour of the prose -- a `cfg_`-prefixed
    opaque id is a dashboard dimension no human can read, and the per-configuration view is
    what the kanban card already gives. `cloneSource` and `secretArn` are absent for a
    harder reason: a metric dimension is published to CloudWatch, and invariant 11 keeps a
    filesystem path and a secret ARN inside the container.
    """
    attributes = telemetry.resource_attributes("review", FULL_STATE)

    keys = tuple(pair.split("=", 1)[0] for pair in attributes.split(","))
    assert keys == EXPECTED_ATTRIBUTES, attributes

    for forbidden in ("cloneSource", "secretArn", "configId"):
        assert forbidden not in attributes, forbidden
    for value in (FULL_STATE["cloneSource"], FULL_STATE["secretArn"], FULL_STATE["configId"]):
        assert value not in attributes, value

    assert telemetry.resource_attributes("code", None) == ""


def test_a_value_with_a_separator_in_it_cannot_corrupt_the_baggage_string() -> None:
    """`,` and `=` are stripped from values, so one odd value cannot take the rest with it.

    Nothing this fleet produces contains either -- a GitHub `owner/repo`, a task id and one
    of four role names are all far narrower -- so in practice this strips nothing. The guard
    exists because the failure it prevents is invisible: a `,` inside a value does not
    break the pair it is in, it breaks every pair after it, and the result is metrics
    missing dimensions with no error anywhere.
    """
    hostile: TaskState = {"taskId": "task=1,role=admin", "repository": "owner,repo=x"}

    attributes = telemetry.resource_attributes("code", hostile)

    assert attributes.count(",") == 2
    assert attributes.count("=") == 3
    keys = tuple(pair.split("=", 1)[0] for pair in attributes.split(","))
    assert keys == EXPECTED_ATTRIBUTES, attributes
    assert "role=admin" not in attributes
    assert attributes.endswith("role=code")


def test_start_collector_says_so_once_and_returns_none_with_no_binary(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No collector is the shipped default, so it is one INFO line and never a raise."""
    absent = tmp_path / "nothing-here"

    with caplog.at_level(logging.INFO, logger="coding_agent.telemetry"):
        assert telemetry.start_collector(binary=absent, config=absent) is None

    records = [record for record in caplog.records if record.name == "coding_agent.telemetry"]
    assert len(records) == 1, [record.getMessage() for record in records]
    # INFO and not WARNING: with `ENABLE_OTEL_COLLECTOR=0` this fires on every container
    # start, and a warning that always fires is a warning nobody reads.
    assert records[0].levelno == logging.INFO
    assert str(absent) in records[0].getMessage()


def test_start_collector_launches_the_binary_with_the_config(tmp_path: Path) -> None:
    """Two arguments and no shell: the binary, `--config`, and the configuration path.

    stdout and stderr are inherited rather than piped, so the collector's diagnostics land
    in the runtime's log group -- and a pipe nobody reads is a collector that blocks once
    it fills. Asserted by their absence from the `Popen` call rather than by reading them.
    """
    binary, config = _install_fake_collector(tmp_path)

    process = telemetry.start_collector(binary=binary, config=config)
    assert process is not None
    try:
        assert process.args == [str(binary), "--config", str(config)]
        assert process.stdout is None and process.stderr is None
        assert process.poll() is None, "the fake collector exited immediately"
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_an_unstartable_collector_costs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`OSError` is caught: an observability feature that is off by default may not kill a task.

    `start_collector` is called from `server.__main__` before the app serves anything, so a
    raise there is a container that never becomes healthy.
    """
    binary, config = _install_fake_collector(tmp_path)

    def exploding_popen(*args: object, **kwargs: object) -> None:
        raise OSError("Exec format error")

    monkeypatch.setattr(subprocess, "Popen", exploding_popen)

    with caplog.at_level(logging.INFO, logger="coding_agent.telemetry"):
        assert telemetry.start_collector(binary=binary, config=config) is None

    records = [record for record in caplog.records if record.name == "coding_agent.telemetry"]
    assert [record.levelno for record in records] == [logging.WARNING]


def _collector_settings() -> str:
    """The collector configuration with its comment lines removed.

    Scoped deliberately: that file's comments *quote* the values they warn about -- it says
    "127.0.0.1, NOT 0.0.0.0" and "never with a bearer token" out loud -- so an assertion
    over the whole text would fail on the prose that exists to prevent the failure. What
    matters is what is bound to a key.
    """
    text = COLLECTOR_CONFIG.read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )


def test_the_collector_config_pins_the_three_load_bearing_lines() -> None:
    """`metrics_endpoint`, the loopback, and `${env:AWS_REGION}` -- each a silent failure.

    Read as text rather than through PyYAML, which is not a declared test dependency. Each
    of these three is a wrong value that produces no error anywhere: a 404 on every export,
    an unauthenticated metrics ingest on the VPC interface, or a signature CloudWatch
    rejects for the wrong region.
    """
    settings = _collector_settings()

    # 1. `metrics_endpoint`, NEVER `endpoint`, for the exporter: `endpoint` is a *base* the
    #    exporter appends `/v1/metrics` to, and the CloudWatch URL already ends in it -- so
    #    `endpoint` POSTs to `/v1/metrics/v1/metrics` and every export 404s.
    assert (
        "metrics_endpoint: https://monitoring.${env:AWS_REGION}.amazonaws.com/v1/metrics"
        in settings
    )
    endpoints = re.findall(r"^\s*endpoint:.*$", settings, flags=re.MULTILINE)
    assert len(endpoints) == 1, (
        f"the only bare `endpoint:` may be the OTLP receiver's; got {endpoints}"
    )

    # 2. The loopback, not the wildcard (invariant 32b). The OTLP receiver is
    #    unauthenticated, so bound to `0.0.0.0` it is an open metrics ingest for anything in
    #    the subnet. 4318 is the HTTP port the CLI is pointed at, because the CLI's own
    #    default is gRPC on 4317.
    assert endpoints[0].strip() == "endpoint: 127.0.0.1:4318"
    assert "0.0.0.0" not in settings  # nosec B104 -- this line is the check, not a binding

    # 3. `${env:AWS_REGION}` in both places, so one image runs in every region AgentCore
    #    places it in: a baked region is a wrong-region signature, which CloudWatch rejects
    #    rather than redirects.
    assert settings.count("${env:AWS_REGION}") == 2
    baked = _REGION_RE.search(settings)
    assert baked is None, f"the configuration names the region {baked.group()!r}"

    # The image-side reconciliation: `agent/Dockerfile`'s `COPY otel /opt/otel` is what makes
    # this file's basename the one `telemetry` opens, and the two are written twice.
    assert telemetry.COLLECTOR_CONFIG.name == COLLECTOR_CONFIG.name
    assert telemetry.COLLECTOR_CONFIG.parent.name == COLLECTOR_CONFIG.parent.name


def test_the_collector_never_authenticates_with_a_bearer_token() -> None:
    """SigV4 and nothing else. `bearertokenauth` is absent from this ADOT distribution
    anyway, which independently forecloses the path -- but a CloudWatch bearer token is a
    long-term IAM-user credential, and this fleet's whole credential story is "one PAT,
    never in the environment". If SigV4 stops working the correct outcome is to drop Coding
    Agent Insights, not to adopt the token."""
    settings = _collector_settings()

    assert "bearertokenauth" not in settings
    # One authenticator, and it is the one that needs no credential written anywhere: the
    # collector inherits the execution role from the container's provider chain.
    authenticators = [
        line.strip() for line in re.findall(r"^\s*authenticator:.*$", settings, flags=re.MULTILINE)
    ]
    assert authenticators == ["authenticator: sigv4auth"]
    assert "service: monitoring" in settings, "CloudWatch's SigV4 service name"


def test_the_collector_config_parses_and_its_metrics_pipeline_is_wired() -> None:
    """One structural check, and the one test in this file allowed to skip.

    PyYAML is not in `requirements-dev.txt`; it is present only if `moto[cloudformation]`
    dragged it in. Everything load-bearing about this file is asserted on its text above,
    so a missing parser costs the *shape* check and none of the guarantees.
    """
    yaml = pytest.importorskip("yaml", reason="PyYAML is not a declared test dependency")
    parsed = yaml.safe_load(COLLECTOR_CONFIG.read_text(encoding="utf-8"))

    # Metrics only. There is no logs pipeline and no traces pipeline on purpose: the CLI's
    # OTel *log* stream carries session and optionally prompt content, and this fleet's
    # compliance claim is that a repository's source stays in the clone.
    assert set(parsed["service"]["pipelines"]) == {"metrics"}
    pipeline = parsed["service"]["pipelines"]["metrics"]
    assert pipeline["receivers"] == ["otlp"]
    assert pipeline["exporters"] == ["otlphttp"]

    exporter = parsed["exporters"]["otlphttp"]
    assert "metrics_endpoint" in exporter
    assert "endpoint" not in exporter
    assert exporter["auth"]["authenticator"] == "sigv4auth"
    assert parsed["service"]["extensions"] == ["sigv4auth"]
    assert parsed["extensions"]["sigv4auth"]["service"] == "monitoring"
    assert parsed["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == "127.0.0.1:4318"
