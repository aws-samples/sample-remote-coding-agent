"""The renderers: the pinned URL regex, and the wordings that stop a status misleading.

`format.py` is pure, so nothing here needs moto, a transport or an environment. What it
does need is the pin: the repository-URL patterns are a *copy* of the control plane's, and
a copy with no test holding it is a copy that drifts.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conftest import FAKE_UNLOGGED_PAT, REPO_ROOT
from rca_mcp import format as fmt

# Importing `client` from a *test* is fine -- boto3 is installed here; it is `format.py`
# itself that must stay import-pure, which is why the stack name is a copy at all.
from rca_mcp.client import DEFAULT_STACK_NAME

_MCP_FORMAT = REPO_ROOT / "mcp/src/rca_mcp/format.py"
_API_CONFIGURATIONS = REPO_ROOT / "api/src/handlers/configurations.py"
_PATTERN_NAMES = ("_HTTPS_REPOSITORY", "_SSH_REPOSITORY")


def _compiled_pattern(path: Path, name: str) -> str:
    """The pattern string `name = re.compile(<pattern>)` was built from.

    Read with `ast` rather than by importing either module: `api/src/handlers/_common.py`
    builds `boto3.resource("dynamodb")` at import time and raises `NoRegionError`
    (`tests/test_api_route_contract.py` records the same constraint), and `mcp/` must not
    import `api/` at all.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        call = node.value
        assert isinstance(call, ast.Call) and call.args, f"{path}: {name} is not a re.compile call"
        return str(ast.literal_eval(call.args[0]))
    raise AssertionError(f"{path} declares no {name}")


@pytest.mark.parametrize("name", _PATTERN_NAMES)
def test_the_repository_url_regex_agrees_with_the_control_plane(name: str) -> None:
    """The two patterns are byte-identical, and stay that way by test rather than by hope.

    `mcp/` cannot import the original. The plugin runs this package as
    `uvx --from git+...#subdirectory=mcp` and a checkout runs it as
    `uv run --directory mcp`, both of which resolve `mcp/pyproject.toml` alone; `api/` is
    a Lambda bundle and `agent/` is a separate distribution. So the copy exists on
    purpose, and if this goes red the fix is to re-copy the pattern -- never to loosen the
    assertion, and never to edit `api/` to match.
    """
    assert _compiled_pattern(_MCP_FORMAT, name) == _compiled_pattern(_API_CONFIGURATIONS, name), (
        f"{name} has drifted from api/src/handlers/configurations.py. The local copy "
        "exists so a typo'd URL fails on the operator's machine, before their GitHub "
        "token is signed and sent to a route that answers 400 InvalidRepositoryUrl."
    )


def test_repository_from_url_accepts_both_clone_forms_and_names_both_on_failure() -> None:
    both = {
        "https://github.com/example-org/sample-remote-coding-agent": (
            "example-org/sample-remote-coding-agent"
        ),
        "https://github.com/example-org/sample-remote-coding-agent.git": (
            "example-org/sample-remote-coding-agent"
        ),
        "https://github.com/example-org/sample-remote-coding-agent/": (
            "example-org/sample-remote-coding-agent"
        ),
        "git@github.com:example-org/sample-remote-coding-agent.git": (
            "example-org/sample-remote-coding-agent"
        ),
    }
    for url, slug in both.items():
        assert fmt.repository_from_url(url) == slug

    # The interesting failure is a URL that is *nearly* right, so the message names both
    # accepted forms rather than saying "invalid".
    for wrong in (
        "https://gitlab.com/example/repo",
        "github.com/example/repo",
        "https://github.com/example-org",
        "",
    ):
        with pytest.raises(ValueError) as raised:
            fmt.repository_from_url(wrong)
        message = str(raised.value)
        assert "https://github.com/owner/repo" in message
        assert "git@github.com:owner/repo.git" in message


@pytest.mark.parametrize("status", ("SUCCEEDED_UNVERIFIED", "FAILED_VERIFICATION"))
def test_an_unverified_task_is_reported_as_a_draft_not_working_code(status: str) -> None:
    """Both spellings of "verification never passed" read as a draft.

    `SUCCEEDED_UNVERIFIED` is the control plane's and `FAILED_VERIFICATION` the agent's
    own; the two vocabularies differ, and the second is the one a renderer that only knew
    the first would report as an unremarkable in-progress run.
    """
    text = fmt.task(
        {
            "taskId": "tsk_1700000000000_ab12cd",
            "configId": "cfg_1a2b3c",
            "status": status,
            "prompt": "add a --version flag",
            "prUrl": "https://github.com/example-org/sample-remote-coding-agent/pull/7",
            "verifyResult": {"verified": False, "passed": False},
        }
    )

    assert "draft" in text
    assert "not working code" in text
    assert "verification never passed" in text
    assert "not a draft" not in text, (
        f"{status} must not be able to read as success; the whole reason these renderers "
        f"exist rather than dumping JSON is that the code alone misleads. Got:\n{text}"
    )


def test_a_second_plan_step_reads_as_a_revision() -> None:
    """`plan` twice is the loop routing review back to planning, not a retry."""
    text = fmt.task(
        {
            "taskId": "tsk_1700000000000_ab12cd",
            "configId": "cfg_1a2b3c",
            "status": "SUCCEEDED",
            "steps": ["checkout", "plan", "code", "verify", "review", "plan", "code"],
        }
    )

    assert "2 plan steps" in text
    assert "revised" in text
    assert "did not run the same plan a second time" in text, (
        "'it revised its approach' and 'it retried' are different reports of the same "
        f"step list, and only one of them is true. Got:\n{text}"
    )


def test_a_provisioning_repository_says_tasks_are_refused() -> None:
    """A configuration that is not READY refuses tasks, and the renderer says so."""
    for status in ("PENDING", "CREATING"):
        text = fmt.repository(
            {
                "configId": "cfg_1a2b3c",
                "repository": "example-org/sample-remote-coding-agent",
                "status": status,
            }
        )
        assert "409 ConfigurationNotReady" in text
        assert "8-12 minutes" in text
        assert "get_repository" in text
        assert "still being built" in text

    listed = fmt.repositories(
        {"items": [{"configId": "cfg_1a2b3c", "repository": "a/b", "status": "PENDING"}]}
    )
    assert "409 ConfigurationNotReady" in listed

    ready = fmt.repository({"configId": "cfg_1a2b3c", "repository": "a/b", "status": "READY"})
    assert "409" not in ready
    assert "start_task will be accepted" in ready


def test_a_repository_reports_its_memory_or_says_it_has_none() -> None:
    """Both arms render: a model cannot infer memory from a line that is not there.

    Unlike `agent runtime:` above, the absent case is reported rather than omitted. A
    repository registered before memory existed will never have one, so "no line" would be
    indistinguishable from "this repository recalls earlier tasks" to the only reader this
    text has.
    """
    payload = {
        "configId": "cfg_1a2b3c",
        "repository": "example-org/sample-remote-coding-agent",
        "repositoryUrl": "https://github.com/example-org/sample-remote-coding-agent",
        "status": "READY",
        "agentRuntimeId": "rt_abc123",
    }

    remembers = fmt.repository({**payload, "memoryId": "rca_owner_repo_mem-AbCdEf"})
    assert "memory: rca_owner_repo_mem-AbCdEf" in remembers
    assert "409" not in remembers, (
        f"a READY repository accepts tasks whether or not it has memory. Got:\n{remembers}"
    )
    assert "start_task will be accepted" in remembers
    assert len(remembers) < 1_000, f"one repository view is a screenful; got {len(remembers)}"

    forgets = fmt.repository(payload)
    assert "memory: none" in forgets, (
        "a payload without `memoryId` -- every repository registered before memory existed "
        f"-- must still say so in words. Got:\n{forgets}"
    )
    assert "None" not in forgets, f"the absent arm is a sentence, not a repr. Got:\n{forgets}"
    assert "start_task will be accepted" in forgets

    bounded = fmt.repository({**payload, "memoryId": "m" * 5_000})
    assert len(bounded) < 1_000, (
        f"a 5 000-character memoryId rendered {len(bounded)} characters. Every field this "
        "renderer reports goes through `_truncate`; a new one has to as well."
    )
    assert "…" in bounded, "a truncated field ends in an ellipsis, so the reader knows it was cut"


def test_a_replacement_registration_says_the_failed_one_is_being_cleaned_up() -> None:
    """A 201 with `replacedConfigIds` is a recovery, and reads as one.

    `POST /configurations` against a repository whose only configuration is `FAILED`
    retires the stale one and registers a replacement. `registered` is the only thing the
    chat surface prints for a 201, so without this line a recovery is indistinguishable
    from a first registration -- and the operator is never told that the configuration
    they may still be looking at is being torn down.
    """
    replacement = fmt.registered(
        {
            "configId": "cfg_9z8y7x",
            "repository": "example-org/sample-remote-coding-agent",
            "status": "PENDING",
            "replacedConfigIds": ["cfg_1a2b3c"],
        }
    )

    assert "cfg_9z8y7x" in replacement
    assert "cfg_1a2b3c" in replacement
    assert "replaced the earlier failed configuration" in replacement
    assert "cleaned up" in replacement

    # A first registration says nothing about a replacement: there is none, and a line
    # about a torn-down configuration would be a fiction.
    first = fmt.registered(
        {
            "configId": "cfg_9z8y7x",
            "repository": "example-org/sample-remote-coding-agent",
            "status": "PENDING",
        }
    )
    assert "replaced" not in first
    assert "409 ConfigurationNotReady" in first, (
        "a 201 is the start of provisioning, not the end of it -- the not-ready note "
        f"stays on both paths. Got:\n{first}"
    )
    # The 201 has to tell the model the setup is not finished. A registration that reads as
    # complete leaves the operator starting a task that is refused, with nothing having said
    # why -- authorising is a browser step and no tool in this package can take it.
    assert "authorise" in first.lower()
    assert "409 GitHubNotAuthorized" in first


_PROVENANCE_URL = "https://abc123.execute-api.eu-west-1.amazonaws.com"
_PROVENANCE_REGION = "eu-west-1"

# The wording each source arm has to carry, so an operator reading one line knows which
# knob produced the endpoint and which knob to clear.
_PROVENANCE_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "env": (
        "RCA_API_URL",
        "api_url",
        "userConfig",
        "~/.rca/endpoint.json",
        "make deploy",
    ),
    "cache": ("~/.rca/endpoint.json", "make deploy"),
    "cloudformation": ("ApiUrl", "SampleCodingAgentApi"),
    "explicit": ("directly",),
}


def test_the_stack_name_in_the_provenance_line_agrees_with_the_client() -> None:
    """`format._DEFAULT_STACK_NAME` is a copy, held by a test rather than by hope.

    `format.py` is pure -- no boto3, no httpx, no filesystem -- so it cannot import
    `client.py` to name the stack the endpoint is discovered from. Same rule as the
    repository-URL patterns above: if this goes red, re-copy the name; never loosen the
    assertion, and never rename the stack to match a stale line of prose.
    """
    assert fmt._DEFAULT_STACK_NAME == DEFAULT_STACK_NAME, (
        "the stack name in the provenance line has drifted from client.DEFAULT_STACK_NAME, "
        "so every tool now tells the operator to look at a stack that is not the one "
        "discovery reads"
    )


@pytest.mark.parametrize("source", sorted(_PROVENANCE_FRAGMENTS))
def test_the_provenance_line_names_the_url_the_region_and_the_source(source: str) -> None:
    """Which deployment a call reached, in one line, for each of the four sources.

    The `env` arm is the incident: `make deploy` had written a fresh eu-west-1 endpoint
    into `~/.rca/endpoint.json`, an `api_url` userConfig left over from an earlier
    deployment outranked it, and a registration went PROVISIONING then READY against the
    older deployment in another region while the new fleet listed zero configurations. So
    that arm has to name the *loser* as well as the winner -- an operator who cannot see
    what to clear cannot act on the line.
    """
    line = fmt.endpoint_provenance(_PROVENANCE_URL, source, _PROVENANCE_REGION)

    assert _PROVENANCE_URL in line and _PROVENANCE_REGION in line, (
        f"the {source} line must name the endpoint and the region; got:\n{line}"
    )
    for fragment in _PROVENANCE_FRAGMENTS[source]:
        assert fragment in line, f"the {source} line must say {fragment!r}; got:\n{line}"

    if source == "env":
        assert "~/.rca/endpoint.json" in line and ("precedence" in line or "wins" in line), (
            "the override beating the endpoint document silently is the whole reason this "
            f"line exists, so the line says so out loud; got:\n{line}"
        )


def test_an_unresolved_endpoint_renders_no_line() -> None:
    """Nothing settled means nothing to report, and a region alone is not a report.

    A tool that failed *during* discovery reports `("", "", "")`, and `call_tool` appends
    nothing -- which is what keeps the "/sample-rca:setup" answer a bare `Error: ...`. A
    region with no URL comes from `AWS_REGION` rather than from any resolution, and
    printing it would be an empty field dressed as a fact.
    """
    assert fmt.endpoint_provenance("", "", "") == ""
    assert fmt.endpoint_provenance("", "", "ap-southeast-1") == ""


def test_an_empty_region_reads_as_the_ambient_credentials() -> None:
    """No region pinned is a sentence, never a blank."""
    line = fmt.endpoint_provenance(_PROVENANCE_URL, "cache", "")

    assert "ambient" in line and "credentials" in line, (
        f"an unpinned region resolves from this machine's credentials; got:\n{line}"
    )
    assert "in  " not in line, f"an empty field was rendered where a region goes:\n{line}"
    assert not line.rstrip().endswith("in"), f"the sentence stops mid-clause:\n{line}"


def test_the_provenance_line_carries_no_credential() -> None:
    """Belt and braces beside the server-side assertion: three inputs, and no others.

    The line is appended to every answer, including `register_repository`'s -- the one
    call whose arguments carry the operator's GitHub token.
    """
    token = FAKE_UNLOGGED_PAT

    for source in (*_PROVENANCE_FRAGMENTS, "somethingelse"):
        line = fmt.endpoint_provenance(_PROVENANCE_URL, source, _PROVENANCE_REGION)
        assert token not in line
        assert "github_pat" not in line


def test_an_unknown_source_still_names_the_endpoint() -> None:
    """A fifth source word degrades to a vaguer sentence, not to silence.

    If `resolve_endpoint` ever grows an arm, the endpoint an operator reached is still the
    most useful half of this line, so it is rendered either way.
    """
    line = fmt.endpoint_provenance(_PROVENANCE_URL, "some_new_arm", _PROVENANCE_REGION)

    assert _PROVENANCE_URL in line and _PROVENANCE_REGION in line
    assert "some_new_arm" in line, (
        f"the unrecognised source is named, so the reader can go and look it up; got:\n{line}"
    )


def test_rendered_text_is_bounded() -> None:
    """A model reads this: one task view must not be able to flood a context window."""
    text = fmt.task(
        {
            "taskId": "tsk_1700000000000_ab12cd",
            "configId": "cfg_1a2b3c",
            "status": "CODING",
            "prompt": "x" * 20_000,
            "statusReason": "y" * 20_000,
            "verifyResult": {"verified": True, "passed": False, "summary": "z" * 20_000},
        }
    )

    assert len(text) < 2_000, f"a 20 000-character prompt rendered {len(text)} characters"
    assert "…" in text, "a truncated field ends in an ellipsis, so the reader knows it was cut"
    assert "xxx" in text, "bounded, but not empty: the beginning of the prompt is still useful"

    listed = fmt.tasks(
        {
            "tasks": [
                {"taskId": f"tsk_{index}", "status": "SUCCEEDED", "prompt": "p" * 5_000}
                for index in range(200)
            ]
        }
    )
    assert len(listed) < 6_000, f"a 200-row page rendered {len(listed)} characters"
    assert "more in this page" in listed
