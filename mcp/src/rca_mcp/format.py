"""What the tools return: plain text, bounded, with the state machine spelled out.

Pure -- no boto3, no httpx, no filesystem -- so `tests/test_mcp_format.py` costs nothing
to run and `import rca_mcp.format` cannot fail because a credential chain is broken.

Two rules shape every renderer here.

**Bounded.** A model reads this, and one task view must not be able to flood a context
window: a prompt, a status reason, a verification summary and any command output are
truncated, and a list is capped. A run's activity feed is never rendered at all -- it is
hundreds of rows and it belongs to a route this package deliberately does not expose.

**A status code is not an explanation.** `SUCCEEDED_UNVERIFIED` reads like success and
is not: it means verification never passed and the pull request is a draft. The whole
reason these renderers exist rather than dumping JSON is that the difference has to be
in words, at the point of use.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# A pinned copy of `api/src/handlers/configurations.py`'s two patterns, variable names
# included. `mcp/` cannot import the original: the plugin runs this package with
# `uv run --directory mcp` / `uvx --from git+...#subdirectory=mcp`, which resolves
# `mcp/pyproject.toml` alone, and `api/` is a Lambda bundle that builds a DynamoDB
# resource at import. `tests/test_mcp_format.py::test_the_repository_url_regex_agrees_
# with_the_control_plane` compares the two pattern strings, so a change on either side
# is a red test rather than a 400 an operator meets with a token already in flight.
# If it goes red, re-copy the pattern -- never loosen the assertion.
_HTTPS_REPOSITORY = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?/?$"
)
_SSH_REPOSITORY = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?$"
)

# Field and list bounds. Generous enough to be useful, small enough that a whole view
# stays a screenful.
_MAX_FIELD = 300
_MAX_SHORT = 120
_MAX_ROWS = 25
_MAX_STEPS = 20

# A configuration in one of these is still provisioning, so a run against it is refused.
_PROVISIONING = ("PENDING", "CREATING")

# The two statuses that look terminal-and-fine and are not. The first is the control
# plane's (`api/src/handlers/runs.py`), the second the agent's own
# (`agent/src/coding_agent/state.py`); the two vocabularies differ, so both are named.
_UNVERIFIED = ("SUCCEEDED_UNVERIFIED", "FAILED_VERIFICATION")

_NOT_READY_NOTE = (
    "provisioning takes 2-5 minutes, and start_task against a configuration that is not "
    "READY is refused with 409 ConfigurationNotReady. Call get_repository to check before "
    "starting a task."
)

_DRAFT_NOTE = (
    "what that status means: verification never passed, so the pull request is a draft "
    "offered for review, not working code. Read the diff yourself before trusting it."
)

# `client.DEFAULT_STACK_NAME`, copied. `format.py` is pure -- no boto3, no httpx, no
# filesystem -- so it cannot import `client.py`, which builds a `boto3` session on the
# discovery arm; `import rca_mcp.format` staying free of a credential chain is what makes
# `tests/test_mcp_format.py` cost nothing. Held by
# `tests/test_mcp_format.py::test_the_stack_name_in_the_provenance_line_agrees_with_the_client`,
# because a copy with no test holding it is a copy that drifts. If that goes red,
# re-copy the name -- never loosen the assertion.
_DEFAULT_STACK_NAME = "SampleCodingAgentApi"


def repository_from_url(url: str) -> str:
    """`owner/repo` for a GitHub clone URL, or `ValueError` naming both accepted forms.

    Checked here, before the request, because `register_repository`'s body carries the
    operator's GitHub token: a typo'd URL should fail locally rather than have the token
    signed and sent to a route that answers 400 `InvalidRepositoryUrl`. The wording is
    the control plane's, so the two failures read the same.
    """
    match = _HTTPS_REPOSITORY.match(url) if isinstance(url, str) else None
    match = match or (_SSH_REPOSITORY.match(url) if isinstance(url, str) else None)
    if not match:
        raise ValueError(
            "repositoryUrl must be https://github.com/owner/repo(.git) or "
            f"git@github.com:owner/repo.git; got {url!r}"
        )
    return f"{match.group('owner')}/{match.group('repo')}"


def _truncate(value: Any, limit: int = _MAX_FIELD) -> str:
    """One line, at most `limit` characters, with a trailing ellipsis when cut."""
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else f"{text[:limit].rstrip()}…"


def _step_names(steps: Any) -> list[str]:
    """The step names, whatever shape the item stored them in: the graph writes plain
    strings, and a row that carries a mapping is read for its `step`/`name`."""
    names: list[str] = []
    for step in steps if isinstance(steps, Sequence) and not isinstance(steps, str) else []:
        if isinstance(step, Mapping):
            names.append(str(step.get("step") or step.get("name") or "?"))
        else:
            names.append(str(step))
    return names


def _steps_line(steps: Any) -> str:
    """The step list, and what a second `plan` in it means.

    `plan` twice is the interesting reading in this whole system: the loop routes back
    from review to *plan*, so the agent revised its approach rather than running the
    same one again. Saying that in words is the difference between "it is stuck" and
    "it changed its mind".
    """
    names = _step_names(steps)
    if not names:
        return "steps: none recorded yet"
    shown = names[:_MAX_STEPS]
    line = f"steps: {' -> '.join(shown)}"
    if len(names) > len(shown):
        line = f"{line} … ({len(names)} in total)"
    plans = names.count("plan")
    if plans > 1:
        line = (
            f"{line}\n  {plans} plan steps: the agent revised its approach after verification "
            "sent it back. It did not run the same plan a second time."
        )
    return line


def _secret_name(arn: Any) -> str:
    """The secret's *name*, never the ARN and never the value. The name is
    `rca/github/<configId>`, which is all anyone needs to find it in the console."""
    text = str(arn or "")
    if ":secret:" not in text:
        return "stored in Secrets Manager"
    tail = text.split(":secret:", 1)[1]
    # Secrets Manager appends `-AbCdEf` to the name in the ARN.
    return tail.rsplit("-", 1)[0] if "-" in tail else tail


def _config_line(config: Mapping[str, Any]) -> str:
    return (
        f"- {config.get('configId', '?')}  {config.get('repository') or 'unknown repository'}  "
        f"{config.get('status') or 'UNKNOWN'}"
    )


def repositories(payload: Mapping[str, Any]) -> str:
    """`GET /configurations`."""
    items = [item for item in (payload.get("items") or []) if isinstance(item, Mapping)]
    if not items:
        return (
            "no repositories are registered yet. register_repository takes a GitHub clone URL "
            "and a fine-grained personal access token, and only with the operator asking for it."
        )

    shown = items[:_MAX_ROWS]
    lines = [f"{len(items)} registered repository/ies:"]
    lines.extend(_config_line(item) for item in shown)
    if len(items) > len(shown):
        lines.append(f"… {len(items) - len(shown)} more in this page")
    if any(str(item.get("status")) in _PROVISIONING for item in items):
        lines.append(
            f"at least one of these is PENDING or CREATING, so it is still being built — {_NOT_READY_NOTE}"
        )
    elif any(item.get("status") != "READY" for item in items):
        lines.append(_NOT_READY_NOTE)
    if payload.get("nextCursor"):
        lines.append("there are more pages; ask for a larger limit if you need them.")
    return "\n".join(lines)


def repository(payload: Mapping[str, Any]) -> str:
    """`GET /configurations/{configId}`."""
    status = str(payload.get("status") or "UNKNOWN")
    lines = [
        f"configuration {payload.get('configId', '?')}",
        f"repository: {payload.get('repository') or 'unknown'} ({payload.get('repositoryUrl') or 'no url'})",
        f"status: {status}",
    ]
    if payload.get("statusReason"):
        lines.append(f"reason: {_truncate(payload.get('statusReason'))}")
    for field, label in (
        ("verifyCommand", "verify command"),
        ("setupCommand", "setup command"),
        ("defaultBranch", "base branch"),
    ):
        if payload.get(field):
            lines.append(f"{label}: {_truncate(payload.get(field), _MAX_SHORT)}")
    if payload.get("secretArn"):
        # The name, and never the token: the token is write-only by design and is not
        # readable from any route this package can reach.
        lines.append(
            f"github token: {_secret_name(payload.get('secretArn'))} (never readable again)"
        )
    if payload.get("agentRuntimeId"):
        lines.append(f"agent runtime: {payload.get('agentRuntimeId')}")
    # Reported in both states, unlike `agent runtime:` above. A model told nothing about
    # memory cannot tell "this repository recalls earlier tasks" from "the field was not in
    # the payload", and a repository registered before memory existed will never have one --
    # `api/src/handlers/configurations.py` publishes `memoryId` only when the item carries it.
    memory_id = payload.get("memoryId")
    if memory_id:
        lines.append(f"memory: {_truncate(memory_id, _MAX_SHORT)}")
    else:
        lines.append(
            "memory: none — this repository was registered before memory existed, or "
            "provisioning did not create one, so nothing from earlier tasks is recalled."
        )
    if status in _PROVISIONING:
        lines.append(f"{status} means it is still being built, not usable yet — {_NOT_READY_NOTE}")
    elif status != "READY":
        lines.append(_NOT_READY_NOTE)
    else:
        lines.append("READY, so start_task will be accepted against this configuration.")
    return "\n".join(lines)


def registered(payload: Mapping[str, Any]) -> str:
    """`POST /configurations` -- the 201, which is the start of provisioning and not
    the end of it. No echo of the token, in any form.

    A 201 carrying `replacedConfigIds` is a *replacement*: the repository's earlier
    configuration was `FAILED`, so the control plane retired it and registered this one.
    Saying so is not decoration -- without it a recovery reads as a first registration,
    and the operator is never told that a configuration they may be looking at is on its
    way out.
    """
    lines = [
        f"registered {payload.get('repository') or 'the repository'} as "
        f"{payload.get('configId', '?')}",
        f"status: {payload.get('status') or 'PENDING'} — the AgentCore runtime, its execution "
        "role and the GitHub secret are being provisioned now.",
    ]
    if payload.get("replacedConfigIds"):
        lines.append(
            "this replaced the earlier failed configuration "
            f"{', '.join(str(c) for c in payload['replacedConfigIds'])}; its runtime, its "
            "execution role and its GitHub secret are being cleaned up now."
        )
    lines.append(_NOT_READY_NOTE)
    lines.append(
        "the token went straight into Secrets Manager and is not readable from any tool "
        "here; it is not repeated back."
    )
    return "\n".join(lines)


# The four sources `ApiClient.provenance()` can report, and what each means to an
# operator who has to decide whether this is the deployment they meant. The `env` arm
# names the *loser* as well as the winner: `env` beating `cache` silently is the whole
# incident this line exists for.
_ENDPOINT_SOURCES = {
    "env": (
        "supplied by the RCA_API_URL override — the rca plugin's `api_url` userConfig "
        "setting is one of these, and it takes precedence over the endpoint document "
        "~/.rca/endpoint.json that `make deploy` writes. If this is not the deployment "
        "you meant, clear `api_url` and the endpoint document wins instead."
    ),
    "cache": (
        "read from the endpoint document ~/.rca/endpoint.json, which `make deploy` wrote "
        "at the end of the last deploy from this machine. No RCA_API_URL override is set."
    ),
    "cloudformation": (
        "discovered from the ApiUrl output of the CloudFormation stack "
        f"{_DEFAULT_STACK_NAME} (RCA_STACK_NAME names a different one), because neither "
        "an RCA_API_URL override nor a usable ~/.rca/endpoint.json was found."
    ),
    "explicit": (
        "passed to this server directly, so neither the RCA_API_URL override nor the "
        "endpoint document ~/.rca/endpoint.json was consulted."
    ),
}


def endpoint_provenance(url: str, source: str, region: str) -> str:
    """Which deployment a call actually reached: the API URL, the region, and where that
    URL came from — in words an operator can act on.

    `server.call_tool` appends this to *every* tool's answer, which is the point. A live
    walkthrough registered a repository into the wrong fleet and reported complete
    success: `make deploy` had written a fresh eu-west-1 endpoint into
    ~/.rca/endpoint.json, an `api_url` userConfig left over from an earlier deployment
    outranked it, and the registration went PROVISIONING then READY against the older
    deployment in another region while the new fleet listed zero configurations — with
    the operator's GitHub token in the other account's Secrets Manager. The precedence
    is legitimate; someone running two deployments needs it. What was missing was any
    statement of which endpoint was used, so that is what this adds.

    `("", "", "")` — nothing resolved yet, which is what a tool that failed *during*
    discovery reports — renders as the empty string and the caller appends nothing. No
    part of any credential appears here: the only inputs are a URL, a region and one of
    four fixed words.
    """
    if not url:
        return ""
    where = _ENDPOINT_SOURCES.get(source) or (
        f"from a source this renderer does not recognise ({source or 'none recorded'})."
    )
    place = (
        f"region {region}"
        if region
        else "the region this machine's ambient AWS credentials resolve to, since none was pinned"
    )
    return f"endpoint used: {url} in {place} — {where}"


def _status_note(status: str) -> str:
    """What a terminal status means, in words. The codes alone mislead."""
    if status in _UNVERIFIED:
        return _DRAFT_NOTE
    if status == "SUCCEEDED":
        return (
            "what that status means: a real verification command ran and the review judged it "
            "green, so the pull request is not a draft."
        )
    if status == "FAILED":
        return "what that status means: the run ended without a pull request."
    if status == "CANCELLED":
        return "what that status means: someone stopped this run deliberately."
    return (
        "this run is still going. Call get_task again when the operator asks — there is no "
        "polling tool here on purpose."
    )


def task(payload: Mapping[str, Any]) -> str:
    """`POST /tasks` and `GET /tasks/{taskId}` -- one run, in words."""
    status = str(payload.get("status") or "UNKNOWN")
    lines = [
        f"task {payload.get('taskId', '?')} on configuration {payload.get('configId', '?')}",
        f"repository: {payload.get('repository') or 'unknown'}",
        f"status: {status}",
        f"prompt: {_truncate(payload.get('prompt'))}",
    ]
    if payload.get("statusReason"):
        lines.append(f"reason: {_truncate(payload.get('statusReason'))}")
    lines.append(_steps_line(payload.get("steps")))

    verify = payload.get("verifyResult") if isinstance(payload.get("verifyResult"), Mapping) else {}
    if verify:
        lines.append(
            f"verification: verified={bool(verify.get('verified'))} passed={bool(verify.get('passed'))}"
        )
        if verify.get("command"):
            lines.append(f"  command: {_truncate(verify.get('command'), _MAX_SHORT)}")
        if verify.get("summary"):
            lines.append(f"  summary: {_truncate(verify.get('summary'))}")
        if verify.get("warning"):
            lines.append(f"  warning: {_truncate(verify.get('warning'), _MAX_SHORT)}")
    if payload.get("prUrl"):
        lines.append(f"pull request: {payload.get('prUrl')}")
    if payload.get("resumeCount"):
        lines.append(f"resumed {payload.get('resumeCount')} time(s) by the watchdog")
    if payload.get("activityCount"):
        lines.append(
            f"activity: {payload.get('activityCount')} rows recorded (the feed itself is not "
            "exposed here — it is the route a console polls)"
        )
    raw_logs = payload.get("logs")
    logs: Mapping[str, Any] = raw_logs if isinstance(raw_logs, Mapping) else {}
    if logs.get("logGroup"):
        lines.append(f"logs: {logs.get('logGroup')} / {logs.get('logStream')}")
    lines.append(_status_note(status))
    return "\n".join(lines)


def tasks(payload: Mapping[str, Any]) -> str:
    """`GET /tasks?configId=…`."""
    rows = [row for row in (payload.get("tasks") or []) if isinstance(row, Mapping)]
    if not rows:
        return (
            "no tasks match. A configuration with no runs yet answers this way, and so does a "
            "status filter nothing is in."
        )

    shown = rows[:_MAX_ROWS]
    lines = [f"{len(rows)} task(s), newest first:"]
    for row in shown:
        lines.append(
            f"- {row.get('taskId', '?')}  {row.get('status') or 'UNKNOWN'}  "
            f"{_truncate(row.get('prompt'), _MAX_SHORT)}"
        )
        if row.get("prUrl"):
            lines.append(f"    {row.get('prUrl')}")
    if len(rows) > len(shown):
        lines.append(f"… {len(rows) - len(shown)} more in this page")
    if any(str(row.get("status")) in _UNVERIFIED for row in rows):
        lines.append(_DRAFT_NOTE)
    if payload.get("nextCursor"):
        lines.append("there are more pages; ask for a larger limit if you need them.")
    return "\n".join(lines)
