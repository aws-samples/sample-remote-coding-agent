"""Contracts on the repository's own files: the Makefile's target names, the stack prefix it
mirrors from `infra/app.py`, the wait its `deploy` recipe puts in front of the account stack,
the way its outputs reader reports a stack it cannot find, every dependency's upper bound,
pytest's distribution mode, and the agent image's pins. Each of the seven is an invariant that
fails silently or confusingly when broken."""

from __future__ import annotations

import configparser
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import app
import pytest
from coding_agent import subagents
from conftest import REPO_ROOT
from stacks import account_stack

# A requirement is a name, optional extras, then everything else is the specifier.
# Deliberately not `packaging.requirements`: packaging is only a transitive
# dependency of pytest and nothing here declares it, so importing it would make this
# test depend on pip's resolution of somebody else's manifest.
_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)(?P<extras>\[[^\]]*\])?(?P<specifier>.*)$")

_MANIFESTS = [
    "requirements-dev.txt",
    "api/requirements.txt",
    "infra/requirements.txt",
    "agent/pyproject.toml",
    "mcp/pyproject.toml",
]

# `agent/Dockerfile` is deliberately NOT a sixth manifest: `_declared_requirements` parses
# `.toml` with tomllib and everything else one requirement per line, and an npm `@version`
# is neither. The image's pins are held by the sibling tests below instead.
_DOCKERFILE = "agent/Dockerfile"
_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_ARG = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)\s*$", re.MULTILINE)
# Every spelling of "whatever is newest at build time". None of these has a legitimate use in
# this file, so the check is a plain substring scan rather than a parse.
_MOVING_REFERENCES = ("@latest", ":latest", "@main", "@master", "@next", "@beta", "@canary")


def _declared_requirements(manifest: str) -> list[str]:
    path = REPO_ROOT / manifest
    if path.suffix == ".toml":
        data = tomllib.loads(path.read_text())
        project = data["project"]
        requirements = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            requirements.extend(extra)
        return requirements
    return [
        line.split(" #")[0].strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_the_makefile_declares_exactly_the_five_targets() -> None:
    declared = set(
        re.findall(
            r"^([A-Za-z][A-Za-z0-9_-]*):", (REPO_ROOT / "Makefile").read_text(), re.MULTILINE
        )
    )
    assert declared == {"setup", "test", "lint", "preflight", "deploy"}, (
        "invariant 35: `setup:` and `test:` are this repository's declared "
        "setupCommand and verifyCommand. An agent uses only an entry point the "
        "repository declared, so renaming `setup` switches verification off "
        "silently -- one draft PR at a time -- while renaming `test` falls back to "
        "bare `python3 -m pytest` and reports a missing environment as a red suite. "
        f"Got {sorted(declared)}"
    )


def test_the_makefile_mirrors_the_stack_prefix_infra_owns() -> None:
    """`STACK_PREFIX` is one knob, the three stack names derive from it, and it is forwarded.

    A Makefile variable that advertises a stack name the deploy does not actually use is
    the confusing failure this test exists to prevent: `make deploy CONSOLE_STACK=Foo` used
    to deploy `SampleCodingAgentConsole` anyway and then die in the console-upload step
    reading outputs for a stack nothing wrote -- a traceback out of a JSON read, from a
    variable this file itself offered. So the default has to be the one `infra/app.py`
    owns, the three names have to derive from the knob rather than repeat a literal, and
    the knob has to reach `cdk deploy` as `-c stackPrefix=`.
    """
    makefile = (REPO_ROOT / "Makefile").read_text()

    default = re.search(r"^STACK_PREFIX \?= (\S+)$", makefile, re.MULTILINE)
    assert default is not None, (
        "Makefile declares no `STACK_PREFIX ?= <prefix>`. It is the one overridable stack "
        "variable: the three names below it are derived, so there is nothing else to set."
    )
    assert default.group(1) == app.DEFAULT_STACK_PREFIX, (
        f"Makefile defaults STACK_PREFIX to {default.group(1)!r} but `infra/app.py` defaults "
        f"to {app.DEFAULT_STACK_PREFIX!r}. `cdk deploy` names the stacks from app.py's "
        "prefix and every later step reads outputs from this file's, so a drift is a deploy "
        "that succeeds and then fails reading back stacks nothing wrote."
    )

    for variable in ("API_STACK", "CONSOLE_STACK", "ACCOUNT_STACK"):
        assignment = re.search(rf"^{variable} \??= (.+)$", makefile, re.MULTILINE)
        assert assignment is not None, f"Makefile no longer defines {variable}"
        value = assignment.group(1)
        assert "$(STACK_PREFIX)" in value and "SampleCodingAgent" not in value, (
            f"Makefile defines {variable} as {value!r}, which does not derive from "
            "$(STACK_PREFIX). A literal here is a name that keeps saying "
            "`SampleCodingAgent` while `-c stackPrefix=` renamed the stacks, and the "
            "symptom is a crash in the console-upload step rather than a second deployment."
        )

    assert "-c stackPrefix=$(STACK_PREFIX)" in makefile, (
        "the `$(CDK) deploy --all` line no longer forwards `-c stackPrefix=$(STACK_PREFIX)`. "
        "Unforwarded, STACK_PREFIX renames only the stacks this file READS -- the deploy "
        "still writes the default ones -- which is exactly the advertised-knob-that-does-"
        "nothing this variable replaced."
    )


def _deploy_recipe() -> str:
    """The tab-indented recipe lines of the `deploy` target, tabs included.

    Local to this file on purpose, in the shape of `_defined_program` below: this is the only
    place in *this* file that reads a recipe, and a shared helper would invite reading the
    other targets the same way when nothing else needs to.
    """
    lines = (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("deploy:"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("\t"):
            break
        body.append(line)
    return "\n".join(body)


def test_the_deploy_waits_out_a_busy_account_stack() -> None:
    """`deploy` polls the account stack's state before `cdk deploy --all`, and bounds the wait.

    Three consecutive deploys were refused outright with `Stack SampleCodingAgentAccount is in
    UPDATE_COMPLETE_CLEANUP_IN_PROGRESS state and can not be updated` -- a message that says
    nothing about waiting, so the operator's next move was a guess. The wait turns that into a
    printed sentence; being bounded is what keeps it from turning a busy stack into a hang.
    """
    recipe = _deploy_recipe()
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    for probe in ("describe-stacks", "Stacks[0].StackStatus"):
        assert probe in recipe, (
            f"the deploy recipe does not {probe!r}. The wait has to read the stack's actual "
            "state: anything else is a sleep that guesses."
        )
    assert f"--region {account_stack.WAF_REGION}" in recipe, (
        f"the wait does not poll --region {account_stack.WAF_REGION}. Asserted against the "
        "constant rather than the literal so the recipe and `account_stack.WAF_REGION` cannot "
        "drift: the account stack is only ever created in the WAF's region, and a poll of the "
        "deploy region finds nothing and waves a busy stack straight through."
    )
    assert "does not exist" in recipe, (
        "the wait does not tolerate a missing stack. `describe-stacks` exits non-zero with "
        "`... does not exist` for a stack nothing has created yet, so without this arm the "
        "very first deploy on a fresh account fails inside the wait that was meant to help it."
    )
    for probe in ("_IN_PROGRESS", "sleep", "ACCOUNT_WAIT_SECONDS"):
        assert probe in recipe, (
            f"the deploy recipe does not mention {probe!r}: a transient state has to be "
            "waited out, and the waiting has to be bounded."
        )
    assert "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS" in makefile, (
        "the Makefile never names UPDATE_COMPLETE_CLEANUP_IN_PROGRESS, which is the state "
        "actually hit. An operator holding CloudFormation's message greps for that word."
    )
    for terminal in (
        "REVIEW_IN_PROGRESS",
        "ROLLBACK_FAILED",
        "UPDATE_ROLLBACK_FAILED",
        "DELETE_FAILED",
    ):
        assert terminal in recipe, (
            f"the wait does not case {terminal}. A terminal state never clears, so waiting on "
            "it spends the whole budget and then reports a timeout instead of the real problem."
        )

    bound = re.search(r"^ACCOUNT_WAIT_SECONDS \?= (\d+)$", makefile, re.MULTILINE)
    assert bound is not None, (
        "Makefile declares no `ACCOUNT_WAIT_SECONDS ?= <seconds>`. `?=` and never `:=`: "
        "`test_the_makefile_declares_exactly_the_five_targets` reads a `:=` at column 0 as a "
        "sixth target."
    )
    assert int(bound.group(1)) > 0, (
        f"ACCOUNT_WAIT_SECONDS is {bound.group(1)}, so the wait either never polls or never "
        "gives up; an unbounded loop inside a deploy is worse than a clear failure."
    )

    assert recipe.index("describe-stacks") < recipe.index("$(CDK) deploy --all"), (
        "the account-stack poll comes after `$(CDK) deploy --all`. A wait is worthless after "
        "the command it protects -- cdk is what fails on a stack mid-operation."
    )
    assert recipe.index("REVIEW_IN_PROGRESS") < recipe.index("*_IN_PROGRESS"), (
        "REVIEW_IN_PROGRESS is cased after the `*_IN_PROGRESS` glob it matches, so an "
        "abandoned change set is treated as transient: it burns the whole wait budget and is "
        "then reported as a timeout rather than as the change set someone left behind."
    )


def test_the_deploy_reports_the_orphaned_shared_account_stack() -> None:
    """The pre-split `$(STACK_PREFIX)Account` stack is named, with the command to delete it.

    It still holds a live web ACL and a budget, so the deploy reports it rather than deleting
    it -- another fleet's distribution may still reference that ACL. It is a closing message
    and not a precondition, so it has to come after the deploy that made the old stack idle.
    """
    recipe = _deploy_recipe()
    assert "$(ACCOUNT_STACK)" in recipe and "delete-stack" in recipe, (
        "the deploy never names $(ACCOUNT_STACK) with a `delete-stack` command. After the "
        "account stack became one instance per consuming region, the shared stack from any "
        "earlier deploy is orphaned and nothing else in the tree will ever mention it again."
    )
    assert recipe.index("delete-stack") > recipe.index("$(CDK) deploy --all"), (
        "the orphan notice is printed before `$(CDK) deploy --all`, where it reads as a "
        "prerequisite. Nothing is orphaned until this deploy has created the per-region stack."
    )


def _defined_program(name: str, tmp_path: Path) -> Path:
    """The body of a `define <name> ... endef` Makefile block, written out as a script.

    Local to this test on purpose: it is the only place a `define`d program is run outside
    make, and a shared helper would invite reading the other one the same way when nothing
    else needs to.
    """
    makefile = (REPO_ROOT / "Makefile").read_text()
    block = re.search(rf"^define {name}$\n(.*?)^endef$", makefile, re.MULTILINE | re.DOTALL)
    assert block is not None, f"Makefile no longer defines a `define {name}` block"
    assert f"export {name}" in makefile, (
        f"Makefile defines {name} but never exports it. `$${name}` in a recipe is a SHELL "
        "variable, so without the export every read expands to the empty string silently."
    )
    program = tmp_path / f"{name.lower()}.py"
    program.write_text(block.group(1))
    return program


def test_the_outputs_reader_names_a_missing_stack_instead_of_raising(tmp_path: Path) -> None:
    """`json_output` must report a stack the outputs file does not carry as a sentence.

    A bare `KeyError: 'FooConsole'` is what this used to be, and it arrived twenty minutes
    into a deploy from a stack name `STACK_PREFIX` chose -- so the traceback named a
    variable rather than the mistake. Only the value may reach stdout, because every call
    site reads this through `$$(...)` and would otherwise write the diagnostic into
    `web/config.js`.
    """
    program = _defined_program("RCA_OUTPUT", tmp_path)
    outputs = tmp_path / "stack-outputs.json"
    outputs.write_text(json.dumps({"SampleCodingAgentConsole": {"Region": "ap-southeast-1"}}))

    def run(stack: str, key: str) -> subprocess.CompletedProcess[str]:
        # A list argv: `sys.executable` and a program this test wrote to `tmp_path`.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        return subprocess.run(
            [sys.executable, str(program), str(outputs), stack, key],
            capture_output=True,
            text=True,
        )

    good = run("SampleCodingAgentConsole", "Region")
    assert good.returncode == 0, f"the good case failed: {good.stderr}"
    assert good.stdout == "ap-southeast-1\n", (
        f"the good case printed {good.stdout!r}. The eleven call sites capture stdout with "
        "command substitution, so anything but the value alone lands in a config file, a "
        "bucket name or an AWS CLI argument."
    )
    assert good.stderr == "", f"the good case wrote {good.stderr!r} to stderr"

    missing_stack = run("FooConsole", "Region")
    assert missing_stack.returncode != 0, (
        "reading an absent stack exited 0. The call sites run under `set -eu`, so a zero "
        "exit here lets the deploy carry on with an empty bucket name."
    )
    assert missing_stack.stdout == "", (
        f"the missing-stack case printed {missing_stack.stdout!r} on stdout, which is the "
        "stream the caller captures as a value."
    )
    assert "FooConsole" in missing_stack.stderr and (
        "SampleCodingAgentConsole" in missing_stack.stderr
    ), (
        "the message must name the stack it looked for AND the stacks the file does carry -- "
        "that pair is what tells an operator the prefix, not the deploy, is what went wrong. "
        f"Got: {missing_stack.stderr!r}"
    )
    assert "Traceback" not in missing_stack.stderr and "KeyError" not in missing_stack.stderr, (
        "the outputs reader raised instead of failing with a sentence. A `KeyError` out of a "
        "one-line JSON read is the original bug: `make deploy CONSOLE_STACK=Foo` advertised a "
        "knob, ignored it, and then died in the console-upload step with a Python traceback "
        f"naming the variable. Got: {missing_stack.stderr!r}"
    )

    missing_key = run("SampleCodingAgentConsole", "ConsoleBucket")
    assert missing_key.returncode != 0, "reading an absent output key exited 0"
    assert "ConsoleBucket" in missing_key.stderr, (
        "a stack that exists but lacks the output key must have the key named -- it is a "
        f"different mistake from a wrong prefix. Got: {missing_key.stderr!r}"
    )
    assert "Traceback" not in missing_key.stderr, missing_key.stderr


@pytest.mark.parametrize("manifest", _MANIFESTS)
def test_every_declared_dependency_carries_an_upper_bound(manifest: str) -> None:
    requirements = _declared_requirements(manifest)
    assert requirements, f"{manifest} declares nothing; the parser is probably wrong"
    for requirement in requirements:
        match = _REQUIREMENT.match(requirement)
        assert match is not None, f"{manifest}: cannot parse {requirement!r}"
        assert "<" in match.group("specifier"), (
            f"invariant 29: {manifest} declares {requirement!r} with no upper bound. "
            "`mcp>=1.2.0` resolved to 2.0.0, which removed a method; `pytest>=8.0` "
            "resolved to 9 and turned a green checkout red. setupCommand runs before "
            "verification on every run that verifies, so an unbounded floor fails "
            "runs for reasons nothing in the diff explains."
        )


def test_pytest_ini_keeps_the_loadfile_distribution() -> None:
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    parser.read(REPO_ROOT / "pytest.ini")
    section = parser["pytest"]

    assert "--dist loadfile" in section["addopts"], (
        "handler modules bind table names at import, so several test files "
        "`importlib.reload` them; only `loadfile` keeps a file's tests on one xdist "
        "worker, which is what makes the reload order a within-file contract"
    )
    assert "-n auto" in section["addopts"]
    assert section["asyncio_mode"] == "auto"
    assert section["testpaths"] == "tests"
    assert section["pythonpath"].split() == ["agent/src", "api/src", "mcp/src", "tests", "infra"]


def _dockerfile_args() -> dict[str, str]:
    """The `ARG NAME=default` table, i.e. the versions a plain `docker build` would use."""
    return dict(_ARG.findall((REPO_ROOT / _DOCKERFILE).read_text()))


def _dockerfile() -> str:
    """The Dockerfile with its ARG defaults substituted -- what the build actually runs.

    Substituted rather than pattern-matched around `${...}`, so the test reads the same
    string the build does and an indirection cannot hide a moving reference.
    """
    text = (REPO_ROOT / _DOCKERFILE).read_text()
    for name, value in _dockerfile_args().items():
        text = text.replace(f"${{{name}}}", value).replace(f"${name}", value)
    return text


def test_the_agent_image_exists_where_the_asset_builds_it() -> None:
    assert (REPO_ROOT / _DOCKERFILE).is_file(), (
        f"{_DOCKERFILE} is missing. `infra/stacks/agent_stack.py` builds "
        "`DockerImageAsset(directory=<repo>/agent)`, so without this file `cdk synth` fails "
        "on a real machine -- and nothing in this suite builds an image, so a green run here "
        "is exactly what a missing Dockerfile looks like."
    )


def test_every_stage_of_the_agent_image_is_arm64() -> None:
    froms = [
        line
        for line in (REPO_ROOT / _DOCKERFILE).read_text().splitlines()
        if line.startswith("FROM")
    ]
    assert froms, f"{_DOCKERFILE} declares no stage; the parser is probably wrong"
    for line in froms:
        assert "--platform=linux/arm64" in line, (
            f"invariant 2: {line!r} does not pin ARM64, and this one is silent -- "
            "CloudFormation accepts an x86 image and AgentCore then never reaches READY, "
            "with no logs to say why. It must agree with `ecr_assets.Platform.LINUX_ARM64` "
            "in `infra/stacks/agent_stack.py`."
        )


def test_the_agent_image_pins_the_claude_cli_and_smoke_checks_it() -> None:
    text = _dockerfile()
    references = re.findall(r"@anthropic-ai/claude-code@(\S+?)[\"'\s]", text)
    assert len(references) == 1, (
        "invariant 34: the Claude Code CLI must be installed exactly once, at an exact "
        f"version. Found {references}, and a second reference is a second version that "
        "moves independently of the first."
    )
    assert _EXACT_VERSION.match(references[0]), (
        f"invariant 34: the CLI is installed at {references[0]!r}, which is not an exact "
        "version. This CLI executes model-chosen commands inside a container running an "
        "untrusted repository's test suite, so a rebuild that silently changes it is a "
        "supply-chain hole."
    )

    # A `RUN` that continues over a trailing backslash is one shell command; joining them
    # is the only way to tell "same RUN" from "next layer".
    logical = re.sub(r"\\\n\s*", " ", text).splitlines()
    install = [line for line in logical if "@anthropic-ai/claude-code@" in line]
    assert len(install) == 1 and "claude --version" in install[0], (
        "invariant 3: `claude --version` must close the SAME `RUN` that installs the CLI. "
        "In a separate layer, a broken install stops being a failed build in front of "
        f"whoever caused it and becomes a first task that dies with `claude: not found`. "
        f"Got {install}"
    )


def test_the_agent_image_installs_the_pinned_aws_documentation_server() -> None:
    assert subagents.AWS_DOCS_REQUIREMENT in _dockerfile(), (
        f"{_DOCKERFILE} must install {subagents.AWS_DOCS_REQUIREMENT!r} at build time: "
        "`subagents.AWS_DOCS_SERVER` runs `uvx <package>==<version>` at run time and is "
        "meant to resolve the environment this build already cached."
    )
    assert _dockerfile_args()["AWS_DOCS_MCP_VERSION"] == subagents.AWS_DOCS_VERSION, (
        "invariant 34: the image's `AWS_DOCS_MCP_VERSION` and `subagents.AWS_DOCS_VERSION` "
        "must be the same version. A drift turns the run-time `uvx` into a registry fetch "
        "from inside the container -- or, with no egress, into a documentation tool that "
        "silently does not appear, because a missing MCP server is an absent tool and "
        "never an error."
    )


def test_the_agent_image_installs_nothing_from_a_moving_reference() -> None:
    raw = (REPO_ROOT / _DOCKERFILE).read_text()
    for moving in _MOVING_REFERENCES:
        assert moving not in raw, (
            f"invariant 34: {_DOCKERFILE} contains {moving!r}. A moving reference makes the "
            "image irreproducible and lets a compromised release land in every container "
            "without a diff in this repository. The scan covers comments too, so a reason "
            "for a pin has to be written without spelling the thing it rules out."
        )

    for name, value in _dockerfile_args().items():
        if not name.endswith("_VERSION"):
            continue
        assert _EXACT_VERSION.match(value), (
            f"invariant 34: `ARG {name}={value}` is not an exact version. Every version this "
            "image installs is a reviewed edit to one `ARG`, not something that moves by "
            "itself."
        )

    stages: set[str] = set()
    images: list[str] = []
    for line in raw.splitlines():
        if line.startswith("FROM"):
            operands = [token for token in line.split()[1:] if not token.startswith("--")]
            images.append(operands[0])
            if len(operands) == 3 and operands[1].upper() == "AS":
                stages.add(operands[2])
        elif line.startswith("COPY"):
            # A `--from=` that names a stage declared above is an intra-build reference, not
            # an image; anything else is a registry pull and needs a version tag.
            images.extend(
                token.split("=", 1)[1]
                for token in line.split()
                if token.startswith("--from=") and token.split("=", 1)[1] not in stages
            )

    assert images, f"{_DOCKERFILE} references no image; the parser is probably wrong"
    for image in images:
        _, _, tag = image.rpartition(":")
        assert tag and tag[0].isdigit(), (
            f"invariant 34: the image reference {image!r} carries no version tag. A tag whose "
            "first character is not a digit is not a version, it is a channel, and a channel "
            "moves under the build."
        )


def test_the_agent_image_bakes_the_skills_and_sets_no_work_root() -> None:
    raw = (REPO_ROOT / _DOCKERFILE).read_text()

    assert "COPY skills /app/skills" in raw and "AGENT_SKILL_PATHS=/app/skills" in raw, (
        f"{_DOCKERFILE} must copy `agent/skills` whole and point `AGENT_SKILL_PATHS` at it. "
        "That directory is one plugin root containing `skills/<name>/SKILL.md`, and the "
        "nesting IS the layout `tests/test_skills.py` pins -- copy its contents instead and "
        "both skills silently never load, because the CLI reports a missing skill as nothing."
    )
    assert "COPY agent/skills" not in raw, (
        "the build context is `agent/`, so a `COPY agent/skills` source resolves to "
        '`agent/agent/skills`: the first real deploy dies on `"/agent/skills": not found`.'
    )
    assert "WORK_ROOT" not in raw, (
        "invariant 26: the image must set no work-root environment variable. A Dockerfile "
        "`ENV` cannot be *unset* by a runtime's `environmentVariables`, only overridden, so "
        "one added here for local convenience makes the runtime's value unremovable."
    )
    for config_file in (".claude", "claude.json"):
        assert config_file not in raw, (
            f"invariant 10: {_DOCKERFILE} mentions {config_file!r}. A CLI configuration file "
            "in the image loads REGARDLESS of `setting_sources=[]`, which makes it the one "
            "file that can widen every role's session from inside the image."
        )


def test_the_agent_image_carries_no_metrics_collector() -> None:
    """Nothing in the image, and nothing in the tree, exports the CLI's OTel metrics.

    This replaces two tests that asserted the *presence* of an ADOT collector behind a build
    arg, and its deploy-time default. Coding Agent Insights was removed: AWS documents two
    supported paths -- a bearer token the CLI posts with directly, and a vendor gateway -- and
    this repository could take neither. A bearer token is a long-term IAM-user credential,
    which this container's whole credential story (one repository-scoped PAT, never in the
    environment) refuses; the gateway is a vendor endpoint invariant 33 blocks by name. What
    was built instead -- an in-container collector SigV4-signing to
    `monitoring.<region>.amazonaws.com/v1/metrics` -- is a third path that was measured end to
    end and produced no metric in any namespace.

    So this asserts the absence, in every place the feature had a limb, because each of them
    fails silently on its own: a collector with no permission exports nothing, exporter
    variables with no collector retry against a closed port, and a `cloudwatch:PutMetricData`
    grant with neither is the fifth thing stolen container credentials reach. `CLAUDE.md`
    carries the whole argument; this is the mechanical half.
    """
    raw = (REPO_ROOT / _DOCKERFILE).read_text()
    args = _dockerfile_args()

    assert "ENABLE_OTEL_COLLECTOR" not in args and "ADOT_COLLECTOR_VERSION" not in args, (
        f"{_DOCKERFILE} declares a collector build arg again ({sorted(args)}). The collector was "
        f"removed with the feature it served; reintroducing it means reintroducing an exporter "
        f"endpoint, the CloudWatch grant and the boundary widening that ceilings it."
    )
    # Instructions only, so the paragraph at the bottom of the Dockerfile that explains why no
    # collector is here does not fail the test that keeps it true.
    instructions = [
        line
        for line in re.sub(r"\\\n\s*", " ", raw).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    offending = [line for line in instructions if "otel" in line.lower()]
    assert offending == [], (
        f"{_DOCKERFILE} carries {offending}, an OTel collector or `otel` path again. The ~1.3 GB "
        f"ARM64 image is built on the operator's own machine against AgentCore's 2 GB cap "
        f"(invariant 2), and nothing in this suite can measure an image."
    )

    for line in raw.splitlines():
        if not line.startswith("ENV "):
            continue
        assert "OTEL_" not in line and "CLAUDE_CODE_ENABLE_TELEMETRY" not in line, (
            f"invariant 26: {_DOCKERFILE} sets {line!r}. `CLAUDE_CODE_ENABLE_TELEMETRY` is fleet "
            "policy in `provisioner._fleet_environment`, pinned to `0` and converged on every "
            "provision; an `ENV` here can be overridden by a runtime's `environmentVariables` "
            "but never unset, so an exporter switched on in the image could not be switched off "
            "without a rebuild."
        )

    # The collector's own configuration directory, which `COPY otel /opt/otel` used to place.
    assert not (REPO_ROOT / "agent" / "otel").exists(), (
        "agent/otel/ is back. It held the collector configuration -- a `sigv4auth` extension, a "
        "loopback OTLP receiver and an `otlphttp` exporter -- and nothing reads it now."
    )


def test_mypy_excludes_the_synthesized_cloud_assembly() -> None:
    """`make lint` must survive a tree that has been deployed from.

    `infra` is one of mypy's four declared bases, `make deploy` synthesizes into
    `infra/cdk.out/`, and an `ecr_assets.DockerImageAsset` copies the whole agent source tree
    in there as `asset.<hash>/src/coding_agent/`. Without an exclude mypy finds every module
    in that package twice and stops with "Source file found twice under different module
    names", having checked nothing.

    Worth pinning because of how it presents rather than how it breaks: `cdk.out/` is in
    `.gitignore`, so a clean tree passes, the identical tree one deploy later fails, and
    `git status` shows nothing that could explain it. The message names an `__init__.py` in a
    directory the operator has never heard of.
    """
    config = (REPO_ROOT / "mypy.ini").read_text(encoding="utf-8")
    excludes = [
        line.split("=", 1)[1].strip() for line in config.splitlines() if line.startswith("exclude")
    ]
    assert excludes, (
        "mypy.ini declares no `exclude`, so `make lint` fails on any tree somebody has run "
        "`make deploy` (or `cdk synth`) in -- see this test's docstring for why that is "
        "invisible from `git status`"
    )
    pattern = re.compile(excludes[0].replace("(?x)", ""), re.VERBOSE)
    assert pattern.search("infra/cdk.out/asset.abc123/src/coding_agent/state.py"), (
        f"mypy.ini's exclude {excludes[0]!r} does not match a synthesized asset path. The "
        f"duplicate it has to keep out is the agent source the Docker image asset copies into "
        f"cdk.out, not cdk.out's own JSON."
    )
    # The exclude must not reach the tree it is meant to leave alone: `agent/src` is checked
    # through its own base, and an over-broad pattern here would silently stop checking it.
    assert not pattern.search("agent/src/coding_agent/state.py"), (
        f"mypy.ini's exclude {excludes[0]!r} also matches the real agent source, so the copy "
        f"is not what is being skipped -- the package is"
    )


def test_the_console_upload_sets_a_cache_directive() -> None:
    """`make deploy`'s S3 sync must send `Cache-Control`, or a console change can be
    invisible to the operator who deployed it.

    Without a directive these objects carry none at all: browsers cache heuristically, ES
    module imports especially so, and `create-invalidation` clears CloudFront's copy and never
    the browser's. This is not hypothetical -- a register-page change was verified live at the
    origin (`LastModified`, `ETag`) and at the edge, with the invalidation `Completed`, while
    the browser still rendered a sentence that existed nowhere server-side.

    The assertion is on the *directive*, not on the value: `no-cache` is the right answer while
    `web/js` ships unbuilt with stable filenames, and content-hashed assets would want
    something else entirely. What must not happen is sending nothing.
    """
    recipe = (REPO_ROOT / "Makefile").read_text()
    sync = [
        line for line in re.sub(r"\\\n\s*", " ", recipe).splitlines() if "aws s3 sync web/" in line
    ]
    assert len(sync) == 1, (
        f"expected exactly one console upload in the Makefile; found {len(sync)}. Two would "
        "drift, and this test could then pass while the one that runs sends no directive."
    )
    assert "--cache-control" in sync[0], (
        "the console upload must set --cache-control. Without it every object ships with no "
        "cache directive, so an operator can hard-refresh and still be served the previous "
        f"JavaScript with nothing to indicate it. Got: {sync[0]!r}"
    )
