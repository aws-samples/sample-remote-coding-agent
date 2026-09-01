"""The in-container ADOT collector, and the interlock that can only switch the CLI off.

Three small functions and two paths, and the whole of CLAUDE.md invariant 32b's container
half. The Claude Code CLI exports OTLP metrics to `127.0.0.1:4318`; the collector this
module starts SigV4-signs them with the execution role's credentials and POSTs them to
CloudWatch, which reads them back as Coding Agent Insights. `agent/otel/collector.yaml` is
the collector's own configuration and explains its three load-bearing lines; this file
decides *whether* any of it runs.

WHY THE CONTAINER GETS THE LAST WORD ON THE EXPORTER. The on/off switch belongs to
`api/src/handlers/provisioner.py`'s `_fleet_environment()`: that is fleet policy, it
converges on every provision (invariant 24), it is auditable in a `GetAgentRuntime`
response, and changing it needs no image rebuild -- which is exactly why invariant 26
forbids the Dockerfile `ENV` alternative. But the fleet environment cannot know whether
*this* image has a collector in it, and there is a real window in which it is wrong:
`CreateAgentRuntime` for a new configuration reads `AGENT_IMAGE_URI` as forwarded by the
API stack, while the image itself is built by the agent stack, so a runtime can be created
against an image from before the flag was flipped. With `CLAUDE_CODE_ENABLE_TELEMETRY=1`
and no collector, the CLI retries OTLP exports against a port nothing is listening on, on
every turn, for the whole of every task.

`ClaudeAgentOptions.env` merges **over** `os.environ` for the CLI subprocess: it can
override a variable but it cannot unset one. So `exporter_env` writes
`CLAUDE_CODE_ENABLE_TELEMETRY: "0"` explicitly whenever the collector binary is absent
from this filesystem, and `subagents.build_options` splats the result into `env=`. The
rule is one sentence and it is asymmetric: **the container can only turn the exporter
off, never on.** That asymmetry is what keeps this from being a second place for the same
setting to disagree -- the fleet variable is the policy and this is an interlock. It is
the same reasoning already written above `CLAUDE_CODE_DISABLE_AUTO_MEMORY` in
`subagents.py`, duplicated so it holds on a local run and on a runtime whose environment
has not converged yet; it also means every `python -m coding_agent.local_run` and every
test run has telemetry explicitly off, with no collector, no socket and no retry.

NO ENVIRONMENT READ LIVES HERE, and that is a contract rather than a coincidence: every
`os.environ` read under `agent/src/coding_agent/` must be forwarded by
`provisioner._fleet_environment()` or declared in `tests/test_env_contract.py`'s
`CONTAINER_DEFAULTS`. The installed-or-not question is answered by probing the filesystem,
which is the only source that cannot be wrong about what this image contains.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from .state import TaskState

log = logging.getLogger(__name__)

# Where `agent/Dockerfile`'s conditional `RUN` unpacks the collector, and where its
# `COPY otel /opt/otel` puts the configuration. `/opt` and not the session-storage mount:
# both are properties of the image, and a path under the mount would vanish between
# invocations. The binary is the one that decides -- the configuration is copied in
# unconditionally because it is about a kilobyte and a configuration with no binary is
# inert, so `ENABLE_OTEL_COLLECTOR` has exactly one observable effect.
COLLECTOR_BINARY = Path("/opt/aws/aws-otel-collector/bin/aws-otel-collector")
COLLECTOR_CONFIG = Path("/opt/otel/collector.yaml")

# The two W3C-Baggage separators, which is the format `OTEL_RESOURCE_ATTRIBUTES` takes:
# `,` between pairs and `=` between a key and its value. Either one inside a *value*
# corrupts every pair after it and not just its own.
_BAGGAGE_SEPARATORS = (",", "=")


def collector_installed(
    *, binary: Path = COLLECTOR_BINARY, config: Path = COLLECTOR_CONFIG
) -> bool:
    """Whether this image really carries a runnable collector.

    Both paths present and the binary executable -- an unpack that landed the file
    somewhere unexpected, or a `COPY` that never happened, must read as *not installed*
    rather than as a `Popen` that raises later. The paths are keyword arguments with the
    module constants as defaults so a test can point them at a `tmp_path` without
    monkeypatching a module global or, worse, needing a real collector.
    """
    return binary.is_file() and os.access(binary, os.X_OK) and config.is_file()


def _baggage_value(value: str) -> str:
    """`value` with the two W3C-Baggage separators removed and the ends trimmed.

    Nothing this fleet puts in an attribute can contain either separator -- a GitHub
    `owner/repo`, a task id and one of four role names are all far narrower than that --
    so in practice this strips nothing. It exists so that one unexpected value cannot
    take the whole attribute string down with it, which is a failure that would show up
    as missing dimensions on every metric rather than as an error anywhere.
    """
    for separator in _BAGGAGE_SEPARATORS:
        value = value.replace(separator, "")
    return value.strip()


def resource_attributes(role: str, state: TaskState | None) -> str:
    """The `OTEL_RESOURCE_ATTRIBUTES` value that gives one role's turn its dimensions.

    PER-TURN ATTRIBUTION, and exactly three dimensions:

    | attribute | value | why |
    | --- | --- | --- |
    | `team.id` | `state["repository"]` | the thing an operator compares: per-repository cost |
    | `task.id` | `state["taskId"]` | the one task a turn belongs to |
    | `role` | `role` | a dimension Coding Agent Insights already recognises |

    `CLAUDE.md` invariant 32b is the reference here. Its prose says the repository is the natural
    mapping for `team.id` and its code sketch writes `team.id={config_id}` -- and this
    resolves it in favour of the prose: a `cfg_`-prefixed opaque id is a dashboard
    dimension no human can read, and the per-configuration view is what the kanban card
    already gives. `configId` is therefore deliberately absent, and so is everything else
    on the state: `cloneSource` in particular is a filesystem path on a local run
    (invariant 11) and has no business on a metric.

    Empty when there is no `state` or no `taskId` on it, rather than filled with
    placeholders. Several callers build options with no state at all, and a metric
    dimensioned `task.id=unknown` is worse than an undimensioned one -- it aggregates
    every such turn into one bucket that looks like a real task.

    This is **additive**. The per-role `ResultMessage.total_cost_usd` -> DynamoDB ->
    kanban-card attribution is not touched, not rerouted and not replaced: that one is
    per-task and this one is fleet-wide, and they answer different questions.
    """
    if not state or not state.get("taskId"):
        return ""
    pairs = (
        ("team.id", _baggage_value(state.get("repository", ""))),
        ("task.id", _baggage_value(state.get("taskId", ""))),
        ("role", _baggage_value(role)),
    )
    # A pair whose value stripped to nothing is dropped rather than emitted blank: a
    # trailing `=` with nothing after it is how a stray separator becomes a whole
    # attribute string CloudWatch reads differently than it was written.
    return ",".join(f"{key}={value}" for key, value in pairs if value)


def exporter_env(
    role: str,
    state: TaskState | None,
    *,
    binary: Path = COLLECTOR_BINARY,
    config: Path = COLLECTOR_CONFIG,
) -> dict[str, str]:
    """The per-`query()` environment overlay for the exporter. The interlock lives here.

    **This never returns `CLAUDE_CODE_ENABLE_TELEMETRY: "1"`.** Turning the exporter *on*
    is the fleet's decision -- `provisioner._fleet_environment()` owns that variable, and
    it is the only thing in this design that enables OTLP export at all. All this
    function can do is turn it *off*, which it does whenever the collector binary is
    absent from this filesystem: `ClaudeAgentOptions.env` merges over `os.environ` and
    cannot unset a variable, so the suppression has to be written out explicitly. That is
    the no-retry-storm guarantee, and it is why a runtime created against an image from
    before the flag flip is a wrong environment rather than a broken task.

    With a collector present it contributes the resource attributes and nothing else: the
    master switch, the protocol, the endpoint and the interval are all fleet policy, and a
    second writer for any of them would be a second place for them to disagree.
    """
    if not collector_installed(binary=binary, config=config):
        return {"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}
    attributes = resource_attributes(role, state)
    return {"OTEL_RESOURCE_ATTRIBUTES": attributes} if attributes else {}


def start_collector(
    *, binary: Path = COLLECTOR_BINARY, config: Path = COLLECTOR_CONFIG
) -> subprocess.Popen[bytes] | None:
    """Start the collector as a child of the server process, or return `None`.

    Deliberately unsupervised: no restart loop, no readiness poll, no `atexit` handler.
    If the collector dies, metrics stop and nothing else is affected -- the CLI's exports
    fail against the loopback and the task carries on -- so a supervisor is scope this
    change does not need, and one that restarted a collector failing on a bad
    configuration would hide the reason in a loop.

    stdout and stderr are inherited rather than captured, so the collector's own
    diagnostics land in the runtime's log group beside everything else; a captured pipe
    with nobody reading it is a collector that blocks once it fills.

    `OSError` is caught and `None` returned because an unstartable collector must not
    cost a task: this is called from `__main__` before the app serves anything, and a
    raise there is a container that never becomes healthy over an observability feature
    that is off by default.
    """
    if not collector_installed(binary=binary, config=config):
        # INFO and not a warning: with `ENABLE_OTEL_COLLECTOR=0` -- the shipped default --
        # this is the expected path on every single container start, and a warning that
        # fires every time is a warning nobody reads.
        log.info(
            "no ADOT collector at %s, so nothing exports metrics from this container; "
            "`exporter_env` switches the CLI's exporter off to match",
            binary,
        )
        return None
    try:
        # A list argv and no shell, and both elements are this module's own constants
        # (`COLLECTOR_BINARY`, `COLLECTOR_CONFIG`) or a test's substitute for them -- no
        # part of this command line comes from the repository being worked on.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        process = subprocess.Popen([str(binary), "--config", str(config)])
    except OSError:
        log.warning("could not start the ADOT collector at %s", binary, exc_info=True)
        return None
    log.info("started the ADOT collector at %s as pid %d", binary, process.pid)
    return process
