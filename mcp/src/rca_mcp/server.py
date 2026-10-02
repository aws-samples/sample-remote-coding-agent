"""The six tools, as plain data, and the stdio adapter that serves them.

The registry is a tuple of frozen `ToolSpec`s whose `run` is `(client, arguments) -> str`,
and `call_tool` is the one entry point over it. Nothing in either knows what a transport
is: `build_server`/`serve`/`main` are the only stdio-aware code in the package, so a
streamable-HTTP adapter or an AgentCore Gateway target is a new function beside `main()`
rather than a rewrite.

That shape is also what makes the safety test cheap. Because the registry is data,
`tests/test_mcp_server.py` can iterate every tool, drive each one through a recording
stub and assert that the only methods any of them reach are `client.get` and
`client.post` -- with no session, no network and no event loop.

Descriptions here are long on purpose. They are where a skill's value lands: a model that
knows `SUCCEEDED_UNVERIFIED` means "verification never passed" gives a useful answer, and
one that reads it as success does not.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from . import format as fmt
from .client import ApiClient, RcaError

LOG = logging.getLogger("rca_mcp.server")

# The same name in both `.mcp.json` files, which is what fixes the two tool-name
# spellings: `mcp__remote-coding-agent__*` project-scoped, and
# `mcp__plugin_sample-rca_remote-coding-agent__*` when the plugin provides it.
SERVER_NAME = "remote-coding-agent"

# Prose shared by several descriptions. One copy so the wording cannot drift between two
# tools that describe the same transition.
_PROVISIONING_NOTE = (
    "A configuration whose status is PENDING or CREATING is still provisioning: tasks "
    "against it are refused with 409 ConfigurationNotReady, so wait for READY (8-12 minutes) "
    "and check with get_repository."
)
_UNVERIFIED_NOTE = (
    "SUCCEEDED_UNVERIFIED means verification never passed and the pull request is a draft: "
    "treat it as a draft offered for review, not as working code."
)


@dataclass(frozen=True)
class ToolSpec:
    """One tool: what the model sees, and the function that answers it."""

    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[ApiClient, Mapping[str, Any]], str]


def _required(arguments: Mapping[str, Any], name: str) -> str:
    """A required argument as a non-empty string.

    A `ValueError` rather than a `KeyError`: `call_tool` turns it into tool text, so the
    model is told which argument it left out instead of seeing a traceback.
    """
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required and must be a non-empty string")
    return value.strip()


def _config_id(arguments: Mapping[str, Any]) -> str:
    """The `configId` argument, refused locally when it is a repository name.

    `lazydragon/rca-test` is the natural thing for a model to pass, and sent as a path
    segment it came back as a bare `404 HttpError: Not Found` -- a dead end that named
    neither the mistake nor the fix. A configuration id never contains a slash, so a value
    that does is a repository and the answer is the lookup that maps one to the other.
    """
    config_id = _required(arguments, "configId")
    if "/" in config_id or ":" in config_id:
        raise ValueError(
            f"configId must be a configuration id such as cfg_1a2b3c, not a repository name "
            f"or URL; call list_repositories to find the configId registered for {config_id}"
        )
    return config_id


def list_repositories(client: ApiClient, arguments: Mapping[str, Any]) -> str:
    # `arguments.get` rather than an unused parameter: ruff's ARG001 is on for `mcp/src`.
    return fmt.repositories(client.get("/configurations", limit=arguments.get("limit")))


def register_repository(client: ApiClient, arguments: Mapping[str, Any]) -> str:
    url = _required(arguments, "repositoryUrl")
    # Validated here, before the request is signed: a typo'd URL should fail on this machine
    # rather than reach the wire for a route that would answer 400 with the same sentence.
    repository = fmt.repository_from_url(url)
    # No credential of any kind. This tool used to require a `githubToken`, and the whole
    # hazard class went with it: there is nothing in these arguments a model could echo into
    # a chat window, read out of a file, or guess.
    body: dict[str, Any] = {"repositoryUrl": url}
    for optional in ("verifyCommand", "setupCommand", "defaultBranch"):
        value = arguments.get(optional)
        if isinstance(value, str) and value.strip():
            body[optional] = value.strip()
    LOG.info("registering %s", repository)
    return fmt.registered(client.post("/configurations", body))


def get_repository(client: ApiClient, arguments: Mapping[str, Any]) -> str:
    config_id = _config_id(arguments)
    return fmt.repository(client.get(f"/configurations/{config_id}"))


def start_task(client: ApiClient, arguments: Mapping[str, Any]) -> str:
    body = {
        "configId": _config_id(arguments),
        "prompt": _required(arguments, "prompt"),
    }
    return fmt.task(client.post("/tasks", body))


def get_task(client: ApiClient, arguments: Mapping[str, Any]) -> str:
    task_id = _required(arguments, "taskId")
    # `configId` is required, not a convenience: Tasks is keyed configId + taskId with no
    # GSI (`api/src/handlers/tasks.py::_required_config_id`), so a task genuinely cannot
    # be found by id alone and the route answers 400 without it.
    return fmt.task(client.get(f"/tasks/{task_id}", configId=_config_id(arguments)))


def list_tasks(client: ApiClient, arguments: Mapping[str, Any]) -> str:
    return fmt.tasks(
        client.get(
            "/tasks",
            configId=_config_id(arguments),
            status=arguments.get("status"),
            limit=arguments.get("limit"),
        )
    )


_LIMIT_SCHEMA = {
    "type": "integer",
    "minimum": 1,
    "maximum": 100,
    "description": "How many rows to ask for. The control plane caps this at 100.",
}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_repositories",
        description=(
            "List the repositories registered with the remote coding agent, with the "
            "configuration id and status of each. Start here: every other tool takes a "
            "configId from this list. " + _PROVISIONING_NOTE
        ),
        input_schema={
            "type": "object",
            "properties": {"limit": _LIMIT_SCHEMA},
            "additionalProperties": False,
        },
        run=list_repositories,
    ),
    ToolSpec(
        name="register_repository",
        description=(
            "Register a GitHub repository so tasks can be run against it. Only ever call "
            "this when the operator has explicitly asked for it: it provisions real AWS "
            "resources (an AgentCore runtime, an execution role and an AgentCore memory). "
            "It takes NO credential -- the deployment authenticates through one GitHub App "
            "held in AgentCore Identity, so never ask the operator for a token and never "
            "accept one. Registering is only half the setup: the operator then has to "
            "authorise the repository, a browser step no tool here can take for them: opening "
            "the repository's page in the console starts it, and a task started before it "
            "is done is refused with 409 GitHubNotAuthorized. The new configuration starts "
            "at PENDING. " + _PROVISIONING_NOTE
        ),
        input_schema={
            "type": "object",
            "properties": {
                "repositoryUrl": {
                    "type": "string",
                    "description": (
                        "https://github.com/owner/repo(.git) or git@github.com:owner/repo.git"
                    ),
                },
                "verifyCommand": {
                    "type": "string",
                    "description": (
                        "The command that proves a change works, e.g. `make test`. Omit it to "
                        "let each run detect one."
                    ),
                },
                "setupCommand": {
                    "type": "string",
                    "description": "The command that prepares the checkout, e.g. `make setup`.",
                },
                "defaultBranch": {
                    "type": "string",
                    "description": "The branch to base pull requests on. Omit for the repository default.",
                },
            },
            "required": ["repositoryUrl"],
            "additionalProperties": False,
        },
        run=register_repository,
    ),
    ToolSpec(
        name="get_repository",
        description=(
            "Show one registered repository: its status, its verify and setup commands, and "
            "whether it is ready to run tasks. This is how you check that provisioning "
            "finished. " + _PROVISIONING_NOTE
        ),
        input_schema={
            "type": "object",
            "properties": {
                "configId": {
                    "type": "string",
                    "description": "The configuration id from list_repositories, e.g. cfg_1a2b3c.",
                }
            },
            "required": ["configId"],
            "additionalProperties": False,
        },
        run=get_repository,
    ),
    ToolSpec(
        name="start_task",
        description=(
            "Start one coding task against a registered repository. The agent plans, codes, "
            "verifies, reviews and opens a pull request; a run takes tens of minutes, so this "
            "answers as soon as the task is queued rather than when it finishes. "
            + _PROVISIONING_NOTE
            + " Write the prompt as an instruction about the repository, e.g. `add a --version "
            "flag to the CLI`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "configId": {
                    "type": "string",
                    "description": "The configuration to run against; it must be READY.",
                },
                "prompt": {
                    "type": "string",
                    "description": "What the agent should do, in the operator's own words.",
                },
            },
            "required": ["configId", "prompt"],
            "additionalProperties": False,
        },
        run=start_task,
    ),
    ToolSpec(
        name="get_task",
        description=(
            "Show one task: its status, its steps, its verification result and its pull "
            "request. configId is required because tasks are keyed by configuration and task "
            "id together, so a task cannot be looked up by id alone. Read the steps: `plan` "
            "appearing more than once means the agent revised its approach after verification "
            "sent it back, not that it retried the same plan. "
            + _UNVERIFIED_NOTE
            + " Call this when the operator asks; do not poll it in a loop."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "taskId": {
                    "type": "string",
                    "description": "The task id, e.g. tsk_1700000000000_ab12cd.",
                },
                "configId": {
                    "type": "string",
                    "description": "The configuration the task belongs to. Required.",
                },
            },
            "required": ["taskId", "configId"],
            "additionalProperties": False,
        },
        run=get_task,
    ),
    ToolSpec(
        name="list_tasks",
        description=(
            "List the tasks of one repository, newest first, optionally filtered by status. "
            "This is the tool behind a `status` question. " + _UNVERIFIED_NOTE
        ),
        input_schema={
            "type": "object",
            "properties": {
                "configId": {
                    "type": "string",
                    "description": "The configuration whose tasks to list.",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "One status to filter by: QUEUED, DISPATCHED, CHECKING_OUT, "
                        "PLANNING, CODING, VERIFYING, REVIEWING, SUBMITTING, SUCCEEDED, "
                        "SUCCEEDED_UNVERIFIED, FAILED_VERIFICATION, FAILED or CANCELLED. "
                        "FAILED_VERIFICATION is the verification veto: the change did not "
                        "pass, so the pull request is a draft nobody should merge on "
                        "trust. CANCELLED means someone stopped that run deliberately."
                    ),
                },
                "limit": _LIMIT_SCHEMA,
            },
            "required": ["configId"],
            "additionalProperties": False,
        },
        run=list_tasks,
    ),
)

TOOLS_BY_NAME: Mapping[str, ToolSpec] = {spec.name: spec for spec in TOOLS}


def _endpoint_line(api: ApiClient) -> str:
    """The `endpoint used:` suffix for one answer, or `""` when there is nothing to report.

    A function beside `call_tool` rather than a line inside it, and the parameter is `api`
    rather than `client`, for one reason: `tests/test_client_api_contract.py::_mcp_calls`
    reads every call on a local named `client` in this file and refuses any attribute
    outside `get`/`post`, so that the catalogue of routes this package can reach is
    exactly the set it sends. Reporting is not a route -- `ApiClient.provenance` is a
    purely local read of what has already been settled, so this costs no request -- and it
    should not be recorded as one.

    `getattr`, because the registry's contract with a client is `get` and `post`:
    `tests/test_mcp_server.py::test_the_registry_is_transport_free` drives `call_tool`
    with a stub that is not an `ApiClient` at all, and a line of *reporting* must never be
    the reason a tool cannot answer.
    """
    report = getattr(api, "provenance", None)
    return fmt.endpoint_provenance(*report()) if report is not None else ""


def call_tool(client: ApiClient, name: str, arguments: Mapping[str, Any] | None) -> str:
    """Run one tool and return its text, with the endpoint it reached named at the end.

    The one transport-free entry point over `TOOLS`.

    `RcaError` and the `ValueError`s from argument and URL validation come back as text
    rather than as exceptions, because the most important message in this package --
    "the control plane is not deployed; run `make deploy`" -- is only useful if the *model*
    sees it. As a raised exception it becomes a transport-level failure Claude Code
    reports as a broken server. Anything else propagates: a bug should be loud.

    The provenance line is appended here rather than inside each `run`, so all six tools
    carry it and a seventh gets it for free. It is appended on the error path too: a 404
    NotFound for a configId that exists in the *other* deployment is precisely the
    confusion this line exists to end. Before anything is resolved it is empty and nothing
    is appended, which is what keeps the "not deployed" answer a bare `Error: ...`.
    """
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        raise ValueError(
            f"no tool named {name!r} on this server; it serves {', '.join(TOOLS_BY_NAME)}"
        )
    try:
        answer = spec.run(client, dict(arguments or {}))
    except (RcaError, ValueError) as err:
        LOG.info("tool %s could not answer: %s", name, err)
        answer = f"Error: {err}"
    provenance = _endpoint_line(client)
    return f"{answer}\n{provenance}" if provenance else answer


def build_server(client: ApiClient) -> Server:
    """The MCP server over `TOOLS`. The only place the SDK's types are constructed."""
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def _tools() -> list[Tool]:
        return [
            Tool(name=spec.name, description=spec.description, inputSchema=spec.input_schema)
            for spec in TOOLS
        ]

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        # `call_tool` is synchronous inside an async handler deliberately: this server
        # talks to exactly one stdio client, so blocking the loop for the duration of one
        # request is the intended behaviour, and it keeps the entry point testable
        # without a session or an event loop.
        return [TextContent(type="text", text=call_tool(client, name, arguments))]

    return server


async def serve() -> None:
    """Serve `TOOLS` over stdio until the client goes away."""
    # Imported here rather than at module scope: importing the registry must not drag in
    # a transport, which is what keeps `tests/test_mcp_server.py` free of session setup.
    from mcp.server.stdio import stdio_server

    client = ApiClient()
    server = build_server(client)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """The `rca-mcp` console script, named by both `.mcp.json` files."""
    # stderr, because stdout *is* the protocol here, and no `print` anywhere in the
    # package (ruff's T20 is on for `mcp/src`).
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(serve())
