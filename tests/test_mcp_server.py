"""The six tools, driven without a transport, a session or an event loop.

The registry is a tuple of frozen dataclasses whose `run` is `(client, arguments) -> str`,
and that is what makes this file possible: a recording stub stands in for `ApiClient`, so
"no tool can delete anything" is an assertion over every tool rather than a claim about
the ones somebody remembered to look at.

The one test that does touch AWS is the discovery test, and it touches moto.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, get_args

import boto3
import httpx
import pytest
from coding_agent.state import TaskStatus
from conftest import FAKE_UNLOGGED_PAT, REPO_ROOT
from moto import mock_aws
from rca_mcp import format as fmt
from rca_mcp.client import ApiClient, SetupRequired, resolve_endpoint
from rca_mcp.server import TOOLS, TOOLS_BY_NAME, call_tool

_REGION = "ap-southeast-1"
_API_URL = "https://abc123.execute-api.ap-southeast-1.amazonaws.com"
# The stack discovery reads when nothing is configured, so it must stay
# `client.DEFAULT_STACK_NAME`: `test_the_endpoint_is_discovered_when_nothing_is_configured`
# creates a stack of this name and then asserts the not-found message names it, so a stale
# literal here fails as "endpoint not discovered" rather than as a rename.
_STACK = "SampleCodingAgentApi"

# The six, in the order the registry declares them. A seventh -- in particular a tool over
# `GET /tasks/{taskId}/activity` -- is a decision, not an addition: it is the polling route
# and "never poll in a loop" is one of the ported guardrails.
_EXPECTED_NAMES = (
    "list_repositories",
    "register_repository",
    "get_repository",
    "start_task",
    "get_task",
    "list_tasks",
)

_CONFIG_ID = "cfg_1a2b3c"
_TASK_ID = "tsk_1700000000000_ab12cd"

# The shapes `api/src/handlers/{configurations,tasks}.py` actually return: the public
# field allow-list of a configuration, and a task view with its activity feed replaced by
# `activityCount` and its `logs` derived by `observability.log_locations`.
_CONFIG: dict[str, Any] = {
    "configId": _CONFIG_ID,
    "repository": "lazydragon/sample-remote-coding-agent",
    "repositoryUrl": "https://github.com/lazydragon/sample-remote-coding-agent",
    "status": "READY",
    "verifyCommand": "make test",
    "setupCommand": "make setup",
    "defaultBranch": "main",
    "secretArn": (
        "arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:rca/github/cfg_1a2b3c-AbCdEf"
    ),
    "executionRoleArn": "arn:aws:iam::111122223333:role/CodingAgentExecution-cfg_1a2b3c",
    "agentRuntimeId": "coding_agent_cfg_1a2b3c-9Zx",
    "createdAt": "2026-08-19T00:00:00+00:00",
    "updatedAt": "2026-08-19T00:04:00+00:00",
}

_TASK: dict[str, Any] = {
    "configId": _CONFIG_ID,
    "taskId": _TASK_ID,
    "repository": "lazydragon/sample-remote-coding-agent",
    "status": "SUCCEEDED_UNVERIFIED",
    "prompt": "add a --version flag to the CLI",
    "steps": ["checkout", "plan", "code", "verify", "review", "plan", "code", "verify", "submit"],
    "verifyResult": {
        "verified": False,
        "passed": False,
        "command": "",
        "summary": "no verify command was declared and none could be detected",
        "warning": "verification never ran",
    },
    "prUrl": "https://github.com/lazydragon/sample-remote-coding-agent/pull/7",
    "activityCount": 143,
    "logs": {
        "logGroup": "/aws/bedrock-agentcore/runtimes/coding_agent_cfg_1a2b3c-9Zx-DEFAULT",
        "logStream": "2026/08/19/[runtime-logs]session",
        "consoleUrl": "https://ap-southeast-1.console.aws.amazon.com/cloudwatch/home",
        "sessionId": "session",
    },
    "resumeCount": 1,
    "createdAt": "2026-08-19T01:00:00+00:00",
}

_CANNED: dict[tuple[str, str], Any] = {
    ("GET", "/configurations"): {
        "items": [_CONFIG, {**_CONFIG, "configId": "cfg_9z8y7x", "status": "CREATING"}],
        "count": 2,
        "nextCursor": "eyJjb25maWdJZCI6ICJjZmdfOXo4eTd4In0=",
    },
    ("POST", "/configurations"): {
        "configId": "cfg_9z8y7x",
        "repository": "example/other-repo",
        "status": "PENDING",
    },
    ("GET", f"/configurations/{_CONFIG_ID}"): _CONFIG,
    # `POST /tasks` answers with a `cardId` as well now -- invariant 38, every run has a card --
    # so the double answers with one: a stub that returns less than the API does is how a
    # renderer gets away with dropping a field. `_RENDERED["start_task"]` deliberately does not
    # name it yet, and `_TASK` deliberately has none, which keeps the cardless run an old task
    # item really is exercised by `get_task` and `list_tasks`. The `card:` line that would print
    # it belongs to `mcp/src/rca_mcp/format.py::task`, and by invariant 34 an edit under `mcp/`
    # travels with the re-pin that follows it, in a commit of its own.
    ("POST", "/tasks"): {
        **_TASK,
        "status": "QUEUED",
        "steps": [],
        "prUrl": None,
        "cardId": "crd_1700000000000_ab12cd",
    },
    ("GET", "/tasks"): {"tasks": [_TASK], "count": 1, "nextCursor": "eyJ0YXNrSWQiOiAiMSJ9"},
    ("GET", f"/tasks/{_TASK_ID}"): _TASK,
}

_TOKEN = FAKE_UNLOGGED_PAT

_ARGUMENTS: dict[str, dict[str, Any]] = {
    "list_repositories": {"limit": 5},
    "register_repository": {
        "repositoryUrl": "https://github.com/example/other-repo.git",
        "githubToken": _TOKEN,
        "verifyCommand": "make test",
    },
    "get_repository": {"configId": _CONFIG_ID},
    "start_task": {"configId": _CONFIG_ID, "prompt": "add a --version flag to the CLI"},
    "get_task": {"taskId": _TASK_ID, "configId": _CONFIG_ID},
    "list_tasks": {"configId": _CONFIG_ID, "status": "SUCCEEDED_UNVERIFIED", "limit": 10},
}


class RecordingClient:
    """Everything a tool may do to a client, and nothing else.

    Deliberately not an `ApiClient` subclass: the point of the `(client, arguments)`
    signature is that a tool cannot reach past `get` and `post`, and inheriting would
    quietly hand these tests a `request` method to fall through to.

    `provenance` is the third and last attribute, and it is a *report*, not a channel:
    `get` and `post` are still the only two things here that send anything. It exists so
    the endpoint line `call_tool` appends is exercised on every tool.
    """

    def __init__(self, canned: Mapping[tuple[str, str], Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._canned = dict(_CANNED if canned is None else canned)

    @property
    def methods(self) -> list[str]:
        return [method for method, _, _ in self.calls]

    def get(self, path: str, **query: Any) -> Any:
        self.calls.append(("GET", path, {k: v for k, v in query.items() if v is not None}))
        return self._answer("GET", path)

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        self.calls.append(("POST", path, dict(body)))
        return self._answer("POST", path)

    # A settled endpoint, so `call_tool`'s provenance line is exercised on every tool.
    # Returned from a constant, not recorded and never sent: this stub is what holds the
    # claim that a tool cannot reach past `get`/`post`, and a third attribute that touched
    # a transport would quietly weaken it.
    def provenance(self) -> tuple[str, str, str]:
        return _API_URL, "cache", _REGION

    def _answer(self, method: str, path: str) -> Any:
        assert (method, path) in self._canned, f"no canned payload for {method} {path}"
        return self._canned[(method, path)]


def _template(url: str) -> str:
    return json.dumps(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": {
                "Marker": {"Type": "AWS::SQS::Queue", "Properties": {"QueueName": "rca-marker"}}
            },
            "Outputs": {"ApiUrl": {"Value": url}},
        }
    )


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"{request.method} {request.url} reached the transport")


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A machine with nothing configured: no override, no cache."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("RCA_API_URL", raising=False)
    monkeypatch.delenv("RCA_STACK_NAME", raising=False)
    return tmp_path


def test_the_endpoint_is_discovered_when_nothing_is_configured(home: Path) -> None:
    """The two-step install, as one test.

    Nothing is configured and nothing is pasted: the endpoint comes from the stack's
    `ApiUrl` output. And when the stack is not there -- a plugin installed before the
    deploy, which is the ordering this design exists to fix -- the failure names
    `/sample-rca:setup` and reaches the *model* as tool output, not Claude Code as a dead
    server.
    """
    with mock_aws():
        boto3.client("cloudformation", region_name=_REGION).create_stack(
            StackName=_STACK, TemplateBody=_template(_API_URL)
        )
        assert resolve_endpoint() == (_API_URL, "cloudformation", _REGION)
        assert ApiClient(transport=httpx.MockTransport(_refuse)).endpoint() == _API_URL

    (home / ".rca" / "endpoint.json").unlink(missing_ok=True)
    with mock_aws():
        # Same account, same region, no stack.
        with pytest.raises(SetupRequired) as raised:
            resolve_endpoint()
        assert "/sample-rca:setup" in str(raised.value), (
            "a plugin installed before the deploy must be told what to run next; a bare "
            f"connection error there tells the operator nothing. Got {raised.value}"
        )
        assert _STACK in str(raised.value) and _REGION in str(raised.value)

        answer = call_tool(
            ApiClient(transport=httpx.MockTransport(_refuse)), "list_repositories", {}
        )
        assert answer.startswith("Error: ")
        assert "/sample-rca:setup" in answer, (
            "resolution is lazy and the message comes back as tool text, so the model can "
            f"act on it; raised through the transport it would be a broken server. Got {answer}"
        )
        assert "endpoint used:" not in answer, (
            "nothing was resolved, so there is no endpoint to report and the accessor "
            "must not have gone looking for one — resolution is lazy on purpose"
        )


def test_no_tool_can_delete_anything() -> None:
    """Three independent halves, none of which needs a session.

    The absence of teardown is what makes a broad, free-prose `/remote` skill safe to
    ship, so it is asserted from three directions: the allow-list, what the tools *say*
    they do, and what they actually send.
    """
    assert set(ApiClient.ALLOWED_METHODS) == {"GET", "POST"}

    destructive = ("delete", "destroy", "deregister", "unregister", "teardown", "remove")
    for spec in TOOLS:
        advertised = " ".join([spec.name, spec.description, json.dumps(spec.input_schema)]).lower()
        for word in destructive:
            assert word not in advertised, (
                f"{spec.name} advertises {word!r}. No tool here can remove anything -- the "
                "runtime, the execution role and the GitHub secret have to go first "
                "(invariant 22) -- and a description that hints otherwise gets a model to "
                "promise a teardown it cannot perform."
            )

    for module in ("server.py", "format.py"):
        source = (REPO_ROOT / "mcp/src/rca_mcp" / module).read_text(encoding="utf-8")
        constants = {
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not constants & {"DELETE", "PUT", "PATCH"}, (
            f"mcp/src/rca_mcp/{module} names a destructive method as a string constant. "
            "client.py is the only module allowed to spell one, and only inside the "
            "message explaining why it is refused."
        )

    client = RecordingClient()
    for spec in TOOLS:
        assert call_tool(client, spec.name, _ARGUMENTS[spec.name]).strip()
    assert len(client.calls) == len(TOOLS)
    assert set(client.methods) == {"GET", "POST"}, (
        f"every tool driven, and only GET and POST reached; got {sorted(set(client.methods))}"
    )

    # The third attribute must not weaken the two claims above. `provenance` is a local
    # report of an endpoint already settled: it records no call, and it sends nothing.
    before = list(client.calls)
    assert client.provenance() == (_API_URL, "cache", _REGION)
    assert client.calls == before, (
        "provenance reached the transport. It reports what is already settled; a tool "
        "must not be able to make a request through it"
    )


def test_the_registry_is_transport_free() -> None:
    """`TOOLS` is plain data and `call_tool` is the entry point over it.

    Streamable HTTP or an AgentCore Gateway target is then a new adapter beside `main()`
    rather than a rewrite -- and this file needs no session to drive a tool.
    """
    assert tuple(spec.name for spec in TOOLS) == _EXPECTED_NAMES
    assert set(TOOLS_BY_NAME) == set(_EXPECTED_NAMES)
    for spec in TOOLS:
        assert callable(spec.run)
        assert spec.input_schema["type"] == "object"

    tree = ast.parse((REPO_ROOT / "mcp/src/rca_mcp/server.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(alias.name for alias in node.names)
    assert "Server" in imported, "the registry is served by the low-level Server"
    assert not [name for name in imported if "stdio" in name], (
        "the stdio transport is imported inside `serve()`, not at module scope: importing "
        f"the registry must not drag in a transport. Got {imported}"
    )

    # The registry does not care what a client is, only that it has `get` and `post`.
    client = RecordingClient()
    assert not isinstance(client, ApiClient)
    assert _CONFIG_ID in call_tool(client, "list_repositories", {})


_RENDERED: dict[str, dict[str, Any]] = {
    "list_repositories": {
        "call": ("GET", "/configurations", {"limit": 5}),
        "contains": (
            _CONFIG_ID,
            "cfg_9z8y7x",
            "CREATING",
            "409 ConfigurationNotReady",
            "more pages",
        ),
    },
    "register_repository": {
        "call": (
            "POST",
            "/configurations",
            {
                "repositoryUrl": "https://github.com/example/other-repo.git",
                "githubToken": _TOKEN,
                "verifyCommand": "make test",
            },
        ),
        "contains": ("cfg_9z8y7x", "PENDING", "Secrets Manager", "get_repository"),
        "excludes": (_TOKEN,),
    },
    "get_repository": {
        "call": ("GET", f"/configurations/{_CONFIG_ID}", {}),
        "contains": (_CONFIG_ID, "READY", "make test", "rca/github/cfg_1a2b3c", "start_task"),
        # The secret's name, never the ARN's suffix and never the value.
        "excludes": ("AbCdEf", "arn:aws:secretsmanager"),
    },
    "start_task": {
        "call": (
            "POST",
            "/tasks",
            {"configId": _CONFIG_ID, "prompt": "add a --version flag to the CLI"},
        ),
        "contains": (_TASK_ID, "QUEUED", "add a --version flag", "steps: none recorded yet"),
    },
    "get_task": {
        "call": ("GET", f"/tasks/{_TASK_ID}", {"configId": _CONFIG_ID}),
        "contains": (
            _TASK_ID,
            "SUCCEEDED_UNVERIFIED",
            "revised",
            "draft",
            "not working code",
            "143",
            "pull/7",
        ),
    },
    "list_tasks": {
        "call": (
            "GET",
            "/tasks",
            {"configId": _CONFIG_ID, "status": "SUCCEEDED_UNVERIFIED", "limit": 10},
        ),
        "contains": (_TASK_ID, "SUCCEEDED_UNVERIFIED", "draft", "more pages"),
    },
}


@pytest.mark.parametrize("name", _EXPECTED_NAMES)
def test_every_tool_renders_text_from_a_stubbed_client(name: str) -> None:
    """One request each, and text that names the fields an operator asked about."""
    client = RecordingClient()
    expected = _RENDERED[name]

    answer = call_tool(client, name, _ARGUMENTS[name])

    assert client.calls == [expected["call"]], (
        f"{name} sends exactly one request; got {client.calls}"
    )
    for fragment in expected["contains"]:
        assert fragment in answer, f"{name} should render {fragment!r}; got:\n{answer}"
    for fragment in expected.get("excludes", ()):
        assert fragment not in answer, f"{name} must never render {fragment!r}; got:\n{answer}"

    # All six tools carry the endpoint, not register_repository alone: the walkthrough that
    # registered into the wrong fleet then listed zero configurations in the right one
    # needed *list_repositories* to say which deployment it had asked.
    line = fmt.endpoint_provenance(*client.provenance())
    assert line and answer.endswith(line), (
        f"{name} must name the endpoint it reached; got:\n{answer}"
    )
    assert _API_URL in line and _REGION in line and "~/.rca/endpoint.json" in line
    # The line is appended to every answer, including the one whose arguments carried the
    # operator's GitHub token. Asserted on the line itself, not only on the answer.
    assert _TOKEN not in line, "no part of the token may reach the provenance line"


def test_the_descriptions_carry_the_state_machine() -> None:
    """A description is where a skill's value lands, so the transitions live in it.

    A model that reads `SUCCEEDED_UNVERIFIED` as success gives a confidently wrong
    answer, and one that starts a task against a `CREATING` configuration gets a 409 it
    cannot explain. Neither is fixed by a better prompt at the call site.
    """
    for name in ("start_task", "register_repository"):
        description = TOOLS_BY_NAME[name].description
        for fragment in ("PENDING", "CREATING", "provisioning", "refused", "409", "READY"):
            assert fragment in description, f"{name} must say {fragment!r}"

    for name in ("get_task", "list_tasks"):
        description = TOOLS_BY_NAME[name].description
        assert "SUCCEEDED_UNVERIFIED" in description
        assert "verification never passed" in description
        assert "draft" in description
        assert "not as working code" in description or "not working code" in description

    get_task = TOOLS_BY_NAME["get_task"].description
    assert "plan" in get_task and "more than once" in get_task and "revised" in get_task, (
        "a second `plan` step means the agent changed its approach rather than retrying, "
        "which is the single most misread thing in a run's history"
    )

    register = TOOLS_BY_NAME["register_repository"].description
    assert "fine-grained" in register and "classic" in register, (
        "the control plane rejects classic personal access tokens, and a model that "
        "does not know that asks the operator for the wrong credential"
    )
    assert "echo" in register.lower()


def test_the_list_tasks_filter_names_every_container_status() -> None:
    """The filter vocabulary, derived from the container's own enum.

    `coding_agent.state.TaskStatus` is the authority and NOT
    `api/src/handlers/runs.py::TASK_STATUSES`, which is missing FAILED_VERIFICATION and
    CANCELLED today (a FOLLOW-UP another card owns this cycle) -- pinning to that copy
    would pin this description to the drift instead of to the vocabulary. A model that
    never reads FAILED_VERIFICATION here cannot suggest filtering by the very state
    invariant 6's verification veto produces, and nothing fails: the API does not validate
    `status` at all (`tasks.list_` passes it straight into a DynamoDB
    `FilterExpression`), so a filter for a status the description omits is simply a
    question nobody asks.
    """
    description = TOOLS_BY_NAME["list_tasks"].input_schema["properties"]["status"]["description"]
    named = set(re.findall(r"\b[A-Z][A-Z_]{2,}\b", description))
    assert named == set(get_args(TaskStatus)), (
        f"the list_tasks status filter advertises {sorted(named)}; the container writes "
        f"{sorted(get_args(TaskStatus))}. Missing: {sorted(set(get_args(TaskStatus)) - named)}."
    )


def test_an_unknown_tool_is_refused_by_name() -> None:
    """A typo is a `ValueError` naming the six, not a silent empty answer."""
    with pytest.raises(ValueError) as raised:
        call_tool(RecordingClient(), "delete_repository", {"configId": _CONFIG_ID})

    message = str(raised.value)
    assert "delete_repository" in message
    for name in _EXPECTED_NAMES:
        assert name in message, f"the refusal lists what this server does serve; missing {name}"
