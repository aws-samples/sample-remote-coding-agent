# The entry points an agent looks for.
#
# `setup:` and `test:` are not arbitrary names. The remote coding agent sniffs a
# repository at checkout and will only use an entry point the repository itself
# declared: a Makefile `setup:`/`bootstrap:` target for preparation, and a Makefile
# `test:` target for verification. Rename either and the agent falls back to
# guessing, which for a Python project means bare `python3 -m pytest` against an
# interpreter with no dependencies installed -- reported as a red suite rather than
# as a missing environment.
#
# PYTHON prefers the venv so `make test` works whether or not it is activated.
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: setup test lint preflight deploy

# Idempotent: cheap on an already-prepared tree, which is what every verified run
# assumes. Keyed on pip rather than the interpreter because a `uv venv` has
# bin/python but no bin/pip, and skipping creation then fails on the next line.
#
# The venv path is resolved with `cd .venv && pwd -P` rather than passed literally:
# the agent's container pre-creates `.venv` as a symlink onto /tmp, and
# `python3 -m venv` on a symlink raises `ValueError: Unable to create directory`
# outright (`EnvBuilder.ensure_directories` checks `os.path.islink`, and `--clear`
# does not help -- the check runs after the clear). `pwd -P` resolves the link with
# no GNU-only `readlink -f`; the `|| echo .venv` arm covers a clean clone where the
# path does not exist yet.
#
# `--system-site-packages` is not cosmetic, it is the difference between fitting the
# container and not. The agent image already carries claude-agent-sdk 0.2.142, boto3
# and botocore 1.43.75, langgraph 1.2.11, bedrock-agentcore 1.22.0, mcp 1.29.0 and
# pydantic 2.13.4 -- every one of them inside its declared bound, so pip installs none
# of them and nothing resolves differently. Measured on the phase-7 tree: 1.1G without
# the flag, 637M with it, `make test` 669 passed either way. claude_agent_sdk alone is
# 318M of what the flag stops copying. The ~460M between those two numbers is most of a
# container's spare disk, and `setup` runs before verification on every run that verifies
# -- so that 460M is spent first and what still has to fit afterwards is the checkout,
# the `pip install -e`s and whatever the task under verification writes. If the image
# ever ships a version outside a declared bound, pip installs the right one into .venv
# and the venv's site-packages still shadows the system one -- the saving shrinks,
# correctness does not.
#
# `--no-cache-dir` on every install line: pip's wheel cache under $HOME needs roughly
# the install size again, transiently, which on the margin above is the difference
# between fitting and not. A nearly-full disk does not fail at the write that filled it,
# it fails at the next one -- so the cost of leaving the cache on is paid by whatever
# runs after `setup`, not by `setup`. This container happens to export
# PIP_NO_CACHE_DIR=1 already, so the flag changes nothing here; it is on the recipe so
# the guarantee lives in this file instead of in an environment variable no reader of it
# can see. Cost is a slower re-run on a developer machine.
setup:
	test -x .venv/bin/pip || python3 -m venv --system-site-packages "$$(cd .venv 2>/dev/null && pwd -P || echo .venv)"
	.venv/bin/python -m pip install --quiet --no-cache-dir --upgrade pip
	.venv/bin/python -m pip install --quiet --no-cache-dir -r requirements-dev.txt
	.venv/bin/python -m pip install --quiet --no-cache-dir -e ./agent -e ./mcp
	.venv/bin/python -m pip install --quiet --no-cache-dir -r api/requirements.txt -r infra/requirements.txt

test:
	$(PYTHON) -m pytest -q

# The JS linter, pinned for the same reason `CDK_VERSION` below is (invariant 34): `npx
# oxlint@latest` would run whatever was published last, so a green checkout could go red
# on a rule that did not exist when it was written. `?=` and never `:=` -- the five-targets
# test reads `^([A-Za-z][A-Za-z0-9_-]*):` over this file, so a `:=` at column 0 is a sixth
# target. One binary through `npx` and one 20-line `web/.oxlintrc.json`, which is NOT a build
# step: nothing is generated, `web/js` still ships to S3 exactly as it is written.
OXLINT_VERSION ?= 1.80.0
OXLINT ?= npx --yes oxlint@$(OXLINT_VERSION)

# The path list matches `CLAUDE.md`'s `## Verification` block. `api/` and `infra/` have
# no wheel, so they are linted by directory rather than through an install.
#
# The second line COMPLEMENTS `tests/test_web_console.py` rather than replacing anything in
# it: that file's `test_every_module_parses_as_an_es_module` checks the parse, this checks
# the semantics. `web/js` is 3,434 lines and until this line nothing anywhere read an
# identifier in it -- `respones.ok` parses as a perfectly good ES module and reaches an
# operator's browser. The twenty hand-rolled regex contracts in that file stay: one clock,
# no repeating timer, only `ui.js` may spell a refusal are decisions no linter has an
# opinion about. Needs node on PATH, like `preflight` and `deploy` do; unlike the parse
# test, which skips when node is missing, this one fails, because a lint that quietly
# checked nothing is the hole it exists to close.
#
# The fourth line is the type checker, and it is LAST on purpose: ruff is seconds and a
# cold mypy is tens of seconds, so a formatting slip reports first. The consequence,
# stated rather than worked around: on a machine with no node on PATH the `oxlint` line
# above fails and mypy never runs. `make lint` is one gate, so a partial pass is not a
# pass -- the fix is node, not a reordering. `mypy.ini` at the root holds the whole
# configuration, including why the path list here is these four trees and not `tests`.
lint:
	$(PYTHON) -m ruff check agent/src api/src infra mcp/src tests
	$(PYTHON) -m ruff format --check agent/src api/src infra mcp/src tests
	$(OXLINT) -c web/.oxlintrc.json web/js
	$(PYTHON) -m mypy --config-file mypy.ini agent/src api/src mcp/src infra

# ---------------------------------------------------------------------------------
# The operator front door. `preflight` is read-only and safe to run at any time;
# `deploy` is the whole deploy, and both are idempotent, so a re-run of either is the
# health check. Everything lives here rather than in a script: this file is the single
# owner of every operator command. Nothing wraps these targets any more -- the plugin used to
# ship a `setup` skill that ran `setup`, then `preflight`, then `deploy` and nothing else, and
# it was removed in plugin 0.3.0 because an operator who installed the plugin from the
# marketplace has no checkout for it to run them in. So a human runs them directly, in that
# order, and `README.md`'s deploy step is the one place that spells the order out.
# `tests/test_repo_contracts.py::test_the_makefile_declares_exactly_the_five_targets`
# pins the target set to exactly five, so a helper target is not available as a hiding place.

# The CDK CLI, pinned. Invariant 34 covers this line as much as it covers the plugin's
# `.mcp.json`: `npx aws-cdk@latest` would run whatever was published last on an operator's
# workstation with their own AWS credentials, and it would make every deploy depend on the
# npm registry resolving a moving tag. A global `cdk` on PATH is deliberately NOT preferred
# either -- then the version that deploys is whatever that machine happens to carry.
CDK_VERSION ?= 2.1138.0
CDK ?= npx --yes aws-cdk@$(CDK_VERSION)

# The CDK CLI needs a supported Node; 18 is end-of-life and the failure it produces is a
# stack trace out of the CLI's own bundle rather than a sentence about Node.
NODE_MINIMUM ?= 20

# `infra/app.py` owns the five suffixes and the default prefix -- including why the prefix
# carries a `Sample` -- and this file mirrors the prefix and derives the three names it needs
# to read outputs back. STACK_PREFIX is the one knob: it is forwarded to `cdk deploy` as
# `-c stackPrefix=` below, so `make deploy EMAIL=... STACK_PREFIX=Foo` deploys FooApi,
# FooConsole, FooAccount and the data and runtime stacks, and then reads THOSE stacks'
# outputs. Hence `?=` on the prefix and plain `=` on the three derivations: override the
# prefix, never a derived name, or the deploy reads back a stack nothing wrote.
# `tests/test_layout_infra.py` and `tests/test_repo_contracts.py` keep the two halves
# agreeing, and `mcp/src/rca_mcp/client.py` pins `DEFAULT_STACK_NAME` to the DEFAULT
# `API_STACK_NAME`, so a renamed fleet is found through `RCA_STACK_NAME` instead.
#
# BOOTSTRAP_STACK stays unprefixed: `CDKToolkit` is the CDK CLI's own stack, not one of ours.
STACK_PREFIX ?= SampleCodingAgent
# The regions this deployment can exist in, because `provisioner.PLATFORM_VERSION` is V2
# and V2 is offered in exactly these five. Not a knob: there is no configuration under
# which a non-V2 region is acceptable, so this check is unconditional.
#
# It matters more than it looks. `CreateAgentRuntime` in another region does **not** reject
# `platformVersion: V2` -- it accepts the parameter and hands back a V1 runtime -- so a
# wrong region produces no error at deploy time, none at run time, and a `GetAgentRuntime`
# that truthfully reports V1. The deploy succeeds and the only thing it was for did not
# happen. That is strictly worse than the two failures preflight already catches, both of
# which eventually say something.
#
# Checked against a literal list rather than by asking the service, for the same reason the
# note above this one exists: no read-only call distinguishes "V2 is offered here" from "V2
# is not", and preflight may not make a mutating one. The list goes stale when AWS adds a
# region, and that is the safe direction -- it refuses a deploy that would have worked,
# loudly, rather than accepting one that silently will not.
V2_REGIONS = us-east-1 us-east-2 us-west-2 eu-west-1 ap-northeast-1

BOOTSTRAP_STACK ?= CDKToolkit
API_STACK = $(STACK_PREFIX)Api
CONSOLE_STACK = $(STACK_PREFIX)Console
# The PRE-SPLIT, shared us-east-1 stack name: the console's WAF web ACL and the Bedrock
# spend budget used to live in one stack per *account*, so every regional deploy of the same
# fleet updated it. `infra/app.py::stack_names` now instances that stack per consuming
# region, so this name is what a deploy from BEFORE that split left behind -- the deploy
# resolves the name it actually builds inside the recipe from `infra/app.py::stack_names`
# and closes by reporting this one as orphaned if it still exists. Kept as a variable
# because that notice, and nothing else, names it; nothing reads an output from it.
ACCOUNT_STACK = $(STACK_PREFIX)Account

# The bound on the wait the deploy does on the account stack before `cdk deploy` (the block
# in `deploy:` below). 900s because the churn that made the wait necessary -- CDK's
# `ExportsWriter` custom resource being deleted by the other region's template and reported
# as `DELETE_FAILED (skipped)` -- took about six minutes of CloudFormation retry, and
# `UPDATE_COMPLETE_CLEANUP_IN_PROGRESS` after it refuses the next deploy outright. The bound
# is the point: an unbounded loop inside a deploy is worse than a clear failure, because a
# hang is indistinguishable from the 15-25 minute image build. `?=` and never `:=` -- the
# five-targets test reads `^([A-Za-z][A-Za-z0-9_-]*):` over this file, so a `:=` at column 0
# is a sixth target.
ACCOUNT_WAIT_SECONDS ?= 900
ACCOUNT_WAIT_INTERVAL ?= 15

# Extra `-c key=value` flags forwarded verbatim to the one `cdk deploy` below, and empty by
# default. It exists because every other knob in `infra/app.py` is deliberately a context
# value -- `planModelId`, `codeModelId`, `haikuModelId`, `monthlyBudgetUsd`,
# `consoleDomainPrefix`, `codeGoalEnabled` -- and until this variable there was no way to set
# any of them *through this target*. The alternative was a bare `cdk deploy`, which skips
# steps 3 to 6: no `web/config.js`, no upload (so the console URL serves S3's own
# `AccessDenied` XML), no first operator, no `endpoint.json`, no registration.
#
# Two cases make it load-bearing rather than convenient. Pinning a geo-scoped profile is the
# one thing that buys data residency (see CLAUDE.md, "Data residency"), which nobody should
# have to leave this target to do -- and a geo prefix is geography, not decoration, so a
# profile pinned to a region the deploy does not run in is a real hazard: `config.MODEL_RE`
# validates the *pattern*, so a wrong-geography id passes start-up validation and fails at
# invocation time instead. The three defaults are all `global.` precisely so that hazard
# needs an explicit override to reach. Example:
#
#   make deploy EMAIL=you@example.com \
#     CDK_CONTEXT='-c haikuModelId=us.anthropic.claude-haiku-4-5-20251001-v1:0'
#
# Exported because `preflight`'s model-profile check reads it out of the environment:
# that check resolves which model ids this deploy will actually use, and if it could not
# see this variable it would validate the `infra/app.py` defaults and fail on a profile
# the deploy was about to override. Preflight uses it twice now: that check READS it, and
# the synth check (check 10) FORWARDS it onto its own `cdk synth` line, because a synth
# that ignored it would validate an app the deploy is not going to build -- the whole point
# of this variable is that it changes the composition.
CDK_CONTEXT ?=
export CDK_CONTEXT

# `~/.rca` is the same directory `mcp/src/rca_mcp/client.py::cache_path()` reads, which is
# what makes step 5 below land where a running MCP server already looks. The raw outputs
# file lives there too, and not in the tree: a deploy must not leave an untracked JSON file
# in the working copy (`web/config.js` is the one exception, and web/.gitignore covers it).
RCA_HOME ?= $(HOME)/.rca
OUTPUTS ?= $(RCA_HOME)/stack-outputs.json

# One JSON read, spelled once: `$$($(json_output) <StackName> <OutputKey>)`. `cdk deploy
# --outputs-file` is what makes this a file read rather than a `describe-stacks` per value.
#
# A program rather than two chained subscripts because of what the subscripts did when they
# missed: a bare `KeyError: 'FooConsole'` traceback, twenty minutes into a deploy, out of a
# stack name STACK_PREFIX chose. So this names the stack it could not find, lists the stacks
# the file does carry, and says which prefix to re-run with.
#
# Every diagnostic goes to stderr and only the value to stdout: the helper is read through
# `$$(...)`, so a sentence on stdout does not become a message, it becomes the contents of
# `web/config.js` or the region in a Cognito call. The non-zero exit is load-bearing too --
# the call sites run under `set -eu`, which is what makes a failed read abort the recipe
# instead of assigning an empty string.
#
# `if key not in outputs` rather than a falsy check: `stackLastUpdated` is legitimately
# empty on a stack that has never been updated.
#
# Every statement is inside a function, for the same reason `RCA_REGISTER` below is: a
# column-0 `try:` or `else:` reads as a declared Makefile target to the regex in
# `tests/test_repo_contracts.py`.
define RCA_OUTPUT
import json
import sys

def fail(*lines):
    for line in lines:
        print(line, file=sys.stderr)
    return 1

def main(path, stack, key):
    try:
        document = json.load(open(path))
    except OSError as err:
        return fail(
            f"deploy: FAILED -- cannot read {path}: {err}",
            "  fix: run 'make deploy EMAIL=...'; 'cdk deploy --outputs-file' writes it.",
        )
    outputs = document.get(stack)
    if outputs is None:
        return fail(
            f"deploy: FAILED -- {path} carries no outputs for a stack named {stack}.",
            f"  it carries: {', '.join(sorted(document)) or '(no stacks at all)'}",
            "  why: STACK_PREFIX names the stacks this deploy reads back, so a prefix",
            "       changed between 'cdk deploy' and here reads a stack nothing wrote.",
            "  fix: re-run 'make deploy EMAIL=... STACK_PREFIX=<the prefix you deployed>'.",
        )
    if key not in outputs:
        return fail(
            f"deploy: FAILED -- stack {stack} has no output named {key} in {path}.",
            f"  it carries: {', '.join(sorted(outputs)) or '(no outputs at all)'}",
        )
    print(outputs[key])
    return 0

sys.exit(main(*sys.argv[1:4]))
endef
export RCA_OUTPUT

json_output = $(PYTHON) -c "$$RCA_OUTPUT" "$(OUTPUTS)"

# Step 6 of the deploy, as a program rather than a shell pipeline, for one reason: the
# GitHub token. `getpass` keeps it off the terminal, and running it here keeps it out of a
# command line, out of `argv` and out of the environment. The control plane is IAM-authorized
# `execute-api`, so the `aws` CLI cannot sign this request at all -- the in-tree
# `rca_mcp.client.ApiClient` is the only signer on this machine, and `POST /configurations`
# is inside its `ALLOWED_METHODS`.
#
# Every statement is inside a function on purpose. A column-0 `try:` or `else:` in this
# block reads as a declared Makefile target to the regex in
# `tests/test_repo_contracts.py`, which turns that test red for a reason that looks like a
# naming complaint.
define RCA_REGISTER
import getpass
import sys
from rca_mcp import client as rca_client
from rca_mcp import format as rca_format

def registered_already(api, slug):
    """The configuration for this repository, or None.

    Listing first is what makes a re-run of `make deploy` silent: an already-registered
    repository must never be asked for a token a second time.
    """
    for item in api.get("/configurations").get("items") or []:
        if item.get("repository") == slug:
            return item
    return None

def main(origin):
    try:
        slug = rca_format.repository_from_url(origin)
    except ValueError as err:
        print(f"deploy: nothing was registered -- {err}")
        return 0
    try:
        api = rca_client.ApiClient()
        existing = registered_already(api, slug)
        if existing is not None:
            print(
                f"deploy: {slug} is already registered as {existing.get('configId')} "
                f"(status {existing.get('status')}), so no token was asked for."
            )
            return 0
        if not sys.stdin.isatty():
            print(
                f"deploy: {slug} is not registered yet, and there is no terminal here to "
                "read a token from, so nothing was registered. This is what happens when "
                "Claude Code runs this target: ask it to register the repository with "
                "register_repository, which takes the token without putting it on a "
                "command line."
            )
            return 0
        print(f"deploy: registering {slug} needs one value this deploy cannot discover -- a")
        print("        fine-grained GitHub PAT scoped to that one repository, with contents")
        print("        write and pull requests write. It is not echoed, and blank skips.")
        token = getpass.getpass("GitHub token: ").strip()
        if not token:
            print(
                f"deploy: no token given, so {slug} was not registered. Re-run "
                "'make deploy EMAIL=...' when you have one, or ask Claude Code to run "
                "register_repository."
            )
            return 0
        print(
            rca_format.registered(
                api.post("/configurations", {"repositoryUrl": origin, "githubToken": token})
            )
        )
        return 0
    except (rca_client.RcaError, rca_client.ApiError) as err:
        print(f"deploy: registration did not happen: {err}")
        return 1
    except Exception as err:
        """The broad arm is the point, not laziness.

        An unreachable endpoint raises `httpx.ConnectError`, which is not an `RcaError`
        at all, and the traceback for it is fifty lines of httpcore after a twenty-minute
        deploy that actually succeeded. The caller in `deploy:` turns any non-zero exit
        into one sentence and two retries, so a name and a message is all this needs.
        """
        print(f"deploy: registration did not happen: {type(err).__name__}: {err}")
        return 1

sys.exit(main(sys.argv[1]))
endef
export RCA_REGISTER

# The inference-profile ids the deploy forwards into every runtime, recovered from
# `infra/app.py` TEXTUALLY rather than by importing it. `infra/app.py` imports `aws_cdk`,
# and `deploy` runs `preflight` BEFORE its `import aws_cdk, rca_mcp` guard -- so a
# preflight that imports it turns "run 'make setup' first" into an import traceback on the
# one machine that has not run setup yet. Re-typing the three ids here instead was the other
# option and is worse: it duplicates the literals and loses the "overridable by context"
# property the deploy actually has.
#
# Same three constraints as RCA_REGISTER above, plus one: every statement is inside a
# function (a column-0 `try:` reads as a declared Makefile target), no `#` anywhere (make
# would treat it as a comment and truncate the program), and no bare `$`.
define RCA_MODEL_IDS
import json
import os
import pathlib
import re
import shlex
import sys

def context_overrides():
    """The context a `cdk deploy` from this tree would actually see.

    Three sources, merged in the order the CDK CLI resolves them, so the last one wins:
    the two context files, then `CDK_CONTEXT`. `cdk.context.json` IS a context map;
    `cdk.json` carries one under "context"; `CDK_CONTEXT` is the deploy target's own
    pass-through of extra `-c key=value` flags, and it has to be read here or this check
    validates a model id the deploy was never going to use -- passing on the `global.`
    defaults while `make deploy CDK_CONTEXT='-c haikuModelId=apac....'` was about to forward
    a profile this region does not offer, or failing on a geo-scoped pin the override had
    already corrected. Two owners of one fact is the bug; this makes it one.
    """
    merged = {}
    for name, key in (
        ("infra/cdk.json", "context"),
        ("cdk.context.json", None),
        ("infra/cdk.context.json", None),
    ):
        path = pathlib.Path(name)
        if not path.exists():
            continue
        loaded = json.loads(path.read_text(encoding="utf-8"))
        merged.update((loaded.get(key) or {}) if key else (loaded or {}))

    # `-c key=value` and `--context key=value`, in either the split or the joined
    # spelling. `shlex` so a quoted value survives, and anything unparseable is ignored
    # rather than raised: this is a preflight check, and refusing to run because a flag
    # was odd would be worse than checking the default.
    flags = shlex.split(os.environ.get("CDK_CONTEXT", ""))
    for index, flag in enumerate(flags):
        pair = ""
        if flag in ("-c", "--context") and index + 1 < len(flags):
            pair = flags[index + 1]
        elif flag.startswith("--context="):
            pair = flag[len("--context=") :]
        elif flag.startswith("-c") and len(flag) > 2:
            pair = flag[2:]
        key, equals, value = pair.partition("=")
        if equals and key:
            merged[key] = value
    return merged

def main():
    app = pathlib.Path("infra/app.py").read_text(encoding="utf-8")
    overrides = context_overrides()
    resolved = []
    for const, key in (
        ("_DEFAULT_PLAN_MODEL", "planModelId"),
        ("_DEFAULT_CODE_MODEL", "codeModelId"),
        ("_DEFAULT_HAIKU_MODEL", "haikuModelId"),
    ):
        found = re.search(rf'^{const} = "([^"]+)"', app, re.MULTILINE)
        if found is None:
            print(f"{const} is no longer spelled that way in infra/app.py", file=sys.stderr)
            return 1
        value = overrides.get(key) or found.group(1)
        if value not in resolved:
            resolved.append(value)
    print("\n".join(resolved))
    return 0

sys.exit(main())
endef
export RCA_MODEL_IDS

# Ten checks. The first nine are cheapest and most-often-wrong first; the tenth is ordered
# by COST rather than by likelihood. It is the `cdk synth` of the whole five-stack app, and
# on a machine with no image layers cached it builds the agent's ~1.3 GB ARM64 image -- the
# same build `cdk deploy` needs anyway, so the total does not grow, but it is minutes rather
# than seconds and a missing region or a broken credential helper must not cost a container
# build to discover. Hence: everything cheap, then the build. One recipe line per check, so
# Make aborts the target at the first failure -- which is the behaviour asked for: a named
# fix instead of a stack trace, and no later check running against a broken earlier one.
#
# What this target does NOT prove: permissions. A credential that can DescribeStacks but
# cannot create an ECR repository passes every check here and fails a few minutes into
# `cdk deploy` with CloudFormation's own message. Proving otherwise needs a dry-run deploy,
# which needs an account. The synth check does not change that: it proves the app COMPOSES
# and that the image BUILDS -- which is the only thing in this repository that ever calls
# `infra/app.py::main()` -- and it still proves nothing about whether these credentials may
# create the resources that composition describes.
#
# "Read-only" here means read-only against AWS, not side-effect-free locally. Check 9 does
# the `docker login` the CDK CLI itself does, so `~/.docker/config.json` gains a credential
# for this account's ECR registry -- exactly what `cdk deploy` would leave there. Check 10
# writes `infra/cdk.out/` and may write `infra/cdk.context.json`; `.gitignore` already
# carries `cdk.out/` and `infra/cdk.context.json`, so preflight still leaves no tracked file
# behind and remains safe to re-run at any time.
#
# What check 7 asks is narrower than it looks, and it now says so out loud. The question
# `list-agent-runtimes` answers is "does the AgentCore endpoint resolve in this region at
# all", and an AccessDeniedException still answers THAT -- so the catch-all arm stays a pass.
# What it is not is availability of `CreateAgentRuntime`, which is what the deploy actually
# needs: eu-west-1 answered this same call and then refused the create 26 minutes into a
# deploy. Telling the two apart needs a mutating call, which this target may not make (it is
# documented safe to run at any time, and the read-only-verb assertion in
# `tests/test_plugin_manifest.py` enforces it), so the check prints what it established and
# names what it did not instead of implying the stronger claim.
preflight:
	@command -v docker >/dev/null 2>&1 || { printf '%s\n' \
	  "preflight: FAILED -- docker is not on PATH." \
	  "  fix: install Docker Desktop (macOS, Windows) or the docker engine (Linux) and" \
	  "       start it. The agent's ~1.3 GB ARM64 image is built on THIS machine by" \
	  "       'cdk deploy', so there is no deploy without a running daemon." >&2; exit 1; }
	@docker info >/dev/null 2>&1 || { printf '%s\n' \
	  "preflight: FAILED -- docker is installed but its daemon is not answering." \
	  "  fix: start Docker Desktop, or 'sudo systemctl start docker' on Linux, wait for" \
	  "       it to report running, then re-run 'make preflight'." >&2; exit 1; }
	@echo "preflight: ok      docker daemon is answering"
	@command -v uv >/dev/null 2>&1 || { printf '%s\n' \
	  "preflight: FAILED -- uv is not on PATH." \
	  "  fix: curl -LsSf https://astral.sh/uv/install.sh | sh   (or 'brew install uv')." \
	  "       Claude Code starts this deployment's MCP server with uvx, so the plugin" \
	  "       cannot reach the control plane without it." >&2; exit 1; }
	@echo "preflight: ok      uv $$(uv --version 2>/dev/null | awk '{print $$2}')"
	@set -eu; \
	version=$$(command -v node >/dev/null 2>&1 && node --version || true); \
	major=$$(printf '%s' "$$version" | sed -e 's/^v//' -e 's/\..*//'); \
	test -n "$$major" && test "$$major" -ge $(NODE_MINIMUM) 2>/dev/null || { printf '%s\n' \
	  "preflight: FAILED -- node $${version:-is not on PATH} is not the v$(NODE_MINIMUM)-or-newer the CDK CLI needs." \
	  "  fix: install Node $(NODE_MINIMUM)+ (https://nodejs.org, 'brew install node', or nvm)." \
	  "       The CDK CLI is run as '$(CDK)', so npx has to work." >&2; exit 1; }; \
	printf 'preflight: ok      node %s\n' "$$version"
	@command -v aws >/dev/null 2>&1 || { printf '%s\n' \
	  "preflight: FAILED -- the aws CLI is not on PATH." \
	  "  fix: install AWS CLI v2 (https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)." \
	  "       The deploy uses it for the caller identity, the first console operator and" \
	  "       the console upload." >&2; exit 1; }
	@set -eu; \
	account=$$(aws sts get-caller-identity --query Account --output text 2>&1) || { printf '%s\n' \
	  "preflight: FAILED -- aws sts get-caller-identity did not answer:" \
	  "    $$account" \
	  "  fix: export AWS_PROFILE, or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, or run" \
	  "       'aws sso login', then re-run 'make preflight'." >&2; exit 1; }; \
	region=$${AWS_REGION:-$${AWS_DEFAULT_REGION:-$$(aws configure get region 2>/dev/null || true)}}; \
	test -n "$$region" || { printf '%s\n' \
	  "preflight: FAILED -- no AWS region resolves on this machine." \
	  "  fix: export AWS_REGION=ap-southeast-1 (or set 'region' in ~/.aws/config). Every" \
	  "       stack, the ECR image and the Cognito domain are region-scoped, so this is" \
	  "       not a value the deploy may guess." >&2; exit 1; }; \
	printf 'preflight: ok      aws account %s, region %s\n' "$$account" "$$region"; \
	if bootstrap=$$(aws cloudformation describe-stacks --stack-name $(BOOTSTRAP_STACK) \
	    --region "$$region" --query 'Stacks[0].StackStatus' --output text 2>&1); then \
	  case "$$bootstrap" in \
	    *_COMPLETE) printf 'preflight: ok      cdk bootstrap done (%s is %s)\n' "$(BOOTSTRAP_STACK)" "$$bootstrap" ;; \
	    *) printf '%s\n' \
	      "preflight: FAILED -- $(BOOTSTRAP_STACK) in $$region is $$bootstrap, not a completed state." \
	      "  fix: finish or delete that stack in the CloudFormation console, then run this" \
	      "       one command yourself:" >&2; \
	      printf '\n         %s bootstrap aws://%s/%s\n\n' "$(CDK)" "$$account" "$$region" >&2; \
	      exit 1 ;; \
	  esac; \
	else \
	  case "$$bootstrap" in \
	    *"does not exist"*) printf '%s\n' \
	      "preflight: FAILED -- $$region in account $$account has never been cdk bootstrapped." \
	      "  fix: run this one command yourself, once per account and region:" >&2; \
	      printf '\n         %s bootstrap aws://%s/%s\n\n' "$(CDK)" "$$account" "$$region" >&2; \
	      printf '%s\n' \
	      "       It is deliberately printed and not run: bootstrap creates the deploy role" \
	      "       and the assets bucket, which is a different privilege level from a deploy," \
	      "       and it is needed once rather than on every run." >&2; exit 1 ;; \
	    *) printf '%s\n' \
	      "preflight: FAILED -- could not read the $(BOOTSTRAP_STACK) stack in $$region:" \
	      "    $$bootstrap" \
	      "  fix: this is a credentials or permissions problem and not a missing bootstrap." \
	      "       A deploy needs cloudformation:DescribeStacks at minimum." >&2; exit 1 ;; \
	  esac; \
	fi
	@set -eu; \
	region=$${AWS_REGION:-$${AWS_DEFAULT_REGION:-$$(aws configure get region 2>/dev/null || true)}}; \
	if runtimes=$$(aws bedrock-agentcore-control list-agent-runtimes \
	    --region "$$region" --max-results 1 2>&1); then \
	  printf 'preflight: ok      the bedrock-agentcore-control endpoint resolves and answers in %s\n' "$$region"; \
	else \
	  case "$$runtimes" in \
	    *"Invalid choice"*|*"invalid choice"*) printf '%s\n' \
	      "preflight: FAILED -- this aws CLI has never heard of bedrock-agentcore-control." \
	      "  fix: upgrade to a current AWS CLI v2. The deploy creates an AgentCore runtime" \
	      "       per configuration, so a CLI that cannot name the service cannot check the" \
	      "       one thing this deploy needs most." >&2; exit 1 ;; \
	    *"Could not connect to the endpoint URL"*|*EndpointConnectionError*) printf '%s\n' \
	      "preflight: FAILED -- Bedrock AgentCore is not available in $$region." \
	      "  fix: export AWS_REGION to a region that offers Bedrock AgentCore, then re-run" \
	      "       'make preflight'. Without it there is nothing for this deploy to create a" \
	      "       runtime on, and that failure otherwise arrives AFTER the ~1.3 GB ARM64" \
	      "       image has been built and pushed." >&2; exit 1 ;; \
	    *) printf '%s\n' \
	      "preflight: ok      the bedrock-agentcore-control endpoint exists in $$region -- it" \
	      "preflight: ok      answered, with an error, which is an answer:" \
	      "    $$runtimes" ;; \
	  esac; \
	fi; \
	printf '%s\n' \
	  "preflight: note    that is the ENDPOINT resolving, not permission or enablement for" \
	  "preflight: note    CreateAgentRuntime, which is what the deploy needs. eu-west-1 answered" \
	  "preflight: note    this same call and then refused the create 26 minutes into a deploy. No" \
	  "preflight: note    read-only call can tell the two apart and preflight may not make a" \
	  "preflight: note    mutating one, so this check does not claim it."
	@set -eu; \
	region=$${AWS_REGION:-$${AWS_DEFAULT_REGION:-$$(aws configure get region 2>/dev/null || true)}}; \
	for offered in $(V2_REGIONS); do \
	  if [ "$$offered" = "$$region" ]; then \
	    printf 'preflight: ok      AgentCore Runtime V2 is offered in %s\n' "$$region"; \
	    exit 0; \
	  fi; \
	done; \
	printf '%s\n' \
	  "preflight: FAILED -- AgentCore Runtime V2 is not offered in $$region, and this" \
	  "                     deployment runs on V2 only." \
	  "  fix: export AWS_REGION to one of $(V2_REGIONS)" \
	  "       and re-run 'make preflight'. CreateAgentRuntime does NOT reject V2 in a region" \
	  "       that does not offer it -- it accepts the parameter and gives you V1 runtimes --" \
	  "       so without this check the deploy succeeds, every cold start is the slow one it" \
	  "       was meant to fix, and nothing anywhere reports it." >&2; \
	exit 1
	@set -eu; \
	region=$${AWS_REGION:-$${AWS_DEFAULT_REGION:-$$(aws configure get region 2>/dev/null || true)}}; \
	profiles=$$(aws bedrock list-inference-profiles --region "$$region" \
	    --query 'inferenceProfileSummaries[].[inferenceProfileId,status]' --output text 2>&1) || { \
	  printf '%s\n' \
	  "preflight: FAILED -- aws bedrock list-inference-profiles did not answer in $$region:" \
	  "    $$profiles" \
	  "  fix: if that is an AccessDeniedException, grant bedrock:ListInferenceProfiles --" \
	  "       preflight cannot tell whether this deploy's models exist in the region" \
	  "       without it. Otherwise Bedrock itself is not available in $$region, and no" \
	  "       AWS_REGION this deploy can use is a region without Bedrock." >&2; exit 1; }; \
	missing=""; \
	for id in $$($(PYTHON) -c "$$RCA_MODEL_IDS"); do \
	  status=$$(printf '%s\n' "$$profiles" | awk -v id="$$id" '$$1 == id { print $$2 }'); \
	  test "$$status" = ACTIVE || missing="$$missing $$id"; \
	done; \
	test -z "$$missing" || { printf '%s\n' \
	  "preflight: FAILED -- these inference profiles are not ACTIVE in $$region:" \
	  "   $$missing" \
	  "  fix: a geo prefix is geography, not decoration -- 'apac.' profiles exist only in" \
	  "       APAC and 'us.'/'eu.' only in their own geography, while MODEL_RE in" \
	  "       agent/src/coding_agent/config.py validates an id's PATTERN and not its" \
	  "       region. So a wrong-region id passes the container's own start-up" \
	  "       validate_model and fails at INVOCATION time instead, which is a silent" \
	  "       runtime failure rather than a deploy-time one. Three ways out, best first:" \
	  "       (1) pass the right ids through CDK_CONTEXT, the only one of the three that" \
	  "           edits no tracked file -- preflight reads it back and re-checks the ids" \
	  "           the deploy will really use:" \
	  "" \
	  "             make deploy CDK_CONTEXT='-c haikuModelId=us.anthropic.claude-haiku-4-5-20251001-v1:0'" \
	  "" \
	  "           and the same for planModelId / codeModelId." \
	  "       (2) export AWS_REGION to a region that offers these profiles." \
	  "       (3) add planModelId / codeModelId / haikuModelId to the 'context' map in" \
	  "           infra/cdk.json -- this one edits a tracked file, so it is last." \
	  "       To see what this region does have, run:" >&2; \
	  printf '\n         aws bedrock list-inference-profiles --region %s \\\n' "$$region" >&2; \
	  printf '           --query %s --output text\n\n' "'inferenceProfileSummaries[].[inferenceProfileId,status]'" >&2; \
	  exit 1; }; \
	printf 'preflight: ok      inference profiles ACTIVE in %s:\n' "$$region"; \
	for id in $$($(PYTHON) -c "$$RCA_MODEL_IDS"); do printf 'preflight: ok        %s\n' "$$id"; done
	@set -eu; \
	account=$$(aws sts get-caller-identity --query Account --output text); \
	region=$${AWS_REGION:-$${AWS_DEFAULT_REGION:-$$(aws configure get region 2>/dev/null || true)}}; \
	registry="$$account.dkr.ecr.$$region.amazonaws.com"; \
	password=$$(aws ecr get-login-password --region "$$region" 2>&1) || { printf '%s\n' \
	  "preflight: FAILED -- aws ecr get-login-password did not answer in $$region:" \
	  "    $$password" \
	  "  fix: if that is an AccessDeniedException, grant ecr:GetAuthorizationToken --" \
	  "       'cdk deploy' publishes the agent image to this account's ECR and cannot" \
	  "       authenticate without it." >&2; exit 1; }; \
	login=$$(printf '%s' "$$password" | docker login --username AWS --password-stdin "$$registry" 2>&1) || { printf '%s\n' \
	  "preflight: FAILED -- docker login to $$registry did not succeed:" \
	  "    $$login" \
	  "  fix: look at credHelpers and credsStore in ~/.docker/config.json. 'error saving" \
	  "       credentials ... not implemented' means a credHelpers entry maps this registry" \
	  "       to docker-credential-ecr-login, which implements 'get' and deliberately not" \
	  "       'store', while the CDK CLI runs 'docker login' regardless. Drop that" \
	  "       registry's credHelpers entry, or point credsStore at a helper that stores." \
	  "       Otherwise the publish step dies about eight seconds after synth, with the" \
	  "       ~1.3 GB ARM64 image already built." >&2; exit 1; }; \
	printf 'preflight: ok      docker login to %s succeeded\n' "$$registry"
	@set -eu; \
	$(PYTHON) -c 'import aws_cdk' >/dev/null 2>&1 || { printf '%s\n' \
	  "preflight: FAILED -- this checkout's virtualenv has no aws-cdk-lib, so nothing can synth." \
	  "  fix: run 'make setup' first. It is idempotent." >&2; exit 1; }; \
	account=$$(aws sts get-caller-identity --query Account --output text); \
	region=$${AWS_REGION:-$${AWS_DEFAULT_REGION:-$$(aws configure get region 2>/dev/null || true)}}; \
	printf 'preflight: ..      cdk synth of all five stacks; on a cold machine this builds the ~1.3 GB ARM64 image\n'; \
	if output=$$(cd infra && PATH="$(CURDIR)/.venv/bin:$$PATH" $(CDK) synth --quiet \
	    -c account="$$account" -c region="$$region" \
	    -c stackPrefix=$(STACK_PREFIX) -c budgetAlertEmail=$(EMAIL) $(CDK_CONTEXT) 2>&1); then \
	  printf 'preflight: ok      cdk synth composed all five stacks for account %s in %s\n' "$$account" "$$region"; \
	else \
	  printf '%s\n' \
	  "preflight: FAILED -- cdk synth of the five-stack app did not succeed:" \
	  "$$output" \
	  "  fix: this is the app itself, not your machine, unless the output above names" \
	  "       docker (then the daemon died mid-build) or an import (then run 'make setup')." \
	  "       CrossRegionReferencesRequireExplicitRegion here means the two -c flags this" \
	  "       line passes stopped reaching infra/app.py's try_get_context('account') and" \
	  "       ('region'): the CDK CLI injects neither, it exports CDK_DEFAULT_ACCOUNT and" \
	  "       CDK_DEFAULT_REGION as environment variables instead, and AccountStack and" \
	  "       WebStack both set cross_region_references=True, which refuses an" \
	  "       environment-agnostic stack." >&2; exit 1; \
	fi
	@echo "preflight: all checks passed -- 'make deploy EMAIL=you@example.com' is the deploy."
	@echo "preflight: what it cannot check is whether these credentials may create every"
	@echo "preflight: resource; that failure arrives from CloudFormation during the deploy."

# The whole deploy, in the order a first-time operator needs it: check, warn, deploy,
# console, first operator, three facts, register. Idempotent throughout -- a second run is
# a no-op `cdk deploy`, a re-print, and no prompt -- so a re-run of `make deploy` doubles as
# "is my deployment healthy?".
#
# The console upload comes straight after `cdk deploy` and ahead of the Cognito call
# because the distribution is live from the moment `cdk deploy` returns: any step between
# the two that can `exit 1` -- and `admin-create-user` does, on anything but
# `UsernameExistsException` -- leaves a live CloudFront distribution over an empty S3
# origin, which answers the console URL with S3's own
# `<Error><Code>AccessDenied</Code></Error>` XML rather than anything about the real cause.
#
# `preflight` is called from the recipe rather than declared as a prerequisite so the
# EMAIL guard fires first: a missing EMAIL should not cost five AWS calls to discover.
#
# `-c account=` and `-c region=` are what make the app synthesize at all, and they are the
# least obvious two flags here. `infra/app.py` reads both from context, and the CDK CLI does
# not put them there -- it exports `CDK_DEFAULT_ACCOUNT`/`CDK_DEFAULT_REGION` as environment
# variables and injects no such context keys -- so without these the four in-region stacks
# are environment-agnostic and `AccountStack`'s `cross_region_references=True` refuses them
# with `CrossRegionReferencesRequireExplicitRegion` before a single resource is considered.
# They are resolved here rather than in `preflight` because preflight only *prints* the
# account and region it found, and a printed value is not a passed one. The region falls back
# the same way preflight's does, so the two cannot disagree about which region this is.
#
# `-c budgetAlertEmail=$(EMAIL)` forwards the address this target already requires rather
# than asking for it twice: the Bedrock spend budget's notifications go to the same person
# who becomes the first console operator, and a second flag an operator has to remember is
# a budget with no alarm on every deploy that forgets it.
#
# `-c stackPrefix=$(STACK_PREFIX)` is forwarded the same way and for the same reason: the
# variable is what the later steps read outputs from, so leaving it unforwarded is the
# whole bug -- `cdk deploy` writes `SampleCodingAgent*` outputs and the console step then
# looks for a stack named after the prefix nobody passed on. It is passed explicitly even
# at its default so a `cdk diff` records the value that produced the template.
#
# Both flags have to stay on the `$(CDK) deploy` line and not on a comment line of their
# own inside the recipe -- `tests/test_plugin_manifest.py::_recipe` reads the recipe up to
# the first line that is not tab-indented, so a column-0 comment in the middle of it
# truncates what that test sees, and a tab-indented one is a recipe line make hands to the
# shell and prints.
#
# The block between the 15-25 minute warning and `cdk deploy` waits the account stack out.
# It exists because `cdk` refuses a stack mid-operation with `Stack ... is in
# UPDATE_COMPLETE_CLEANUP_IN_PROGRESS state and can not be updated` -- a message that says
# nothing about waiting, so the operator's next move is a guess. Three consecutive deploys
# were blocked by exactly that state on a live account. The wait polls the state instead of
# guessing, prints what it is waiting for, exits non-zero on a state `cdk` can never update
# in place (`REVIEW_IN_PROGRESS`, `*_ROLLBACK_FAILED`, `DELETE_FAILED`, `CREATE_FAILED`), and
# treats a stack that does not exist as normal -- a first deploy is the case where it does
# not. `us-east-1` is written out as a literal in that block rather than taken from a
# variable, so the text contract in `tests/test_repo_contracts.py` can assert the deploy
# polls that region; it must stay equal to `infra/stacks/account_stack.py::WAF_REGION`, which
# is what that test compares it against.
deploy:
	@test -n "$(EMAIL)" || { printf '%s\n' \
	  "deploy: EMAIL is required -- 'make deploy EMAIL=you@example.com'." \
	  "  why: self-signup is off on the console's user pool (AllowAdminCreateUserOnly)," \
	  "       so without a first operator there is no way into the board at all." >&2; exit 1; }
	@$(MAKE) --no-print-directory preflight
	@$(PYTHON) -c 'import aws_cdk, rca_mcp' >/dev/null 2>&1 || { printf '%s\n' \
	  "deploy: FAILED -- this checkout's virtualenv is missing aws-cdk-lib or rca_mcp." \
	  "  fix: run 'make setup' first. It is idempotent." >&2; exit 1; }
	@printf '%s\n' \
	  "deploy: cdk deploy --all -- $(API_STACK), $(CONSOLE_STACK), the per-region" \
	  "        $(ACCOUNT_STACK)<Region> stack, and the data and runtime stacks." \
	  "        The FIRST deploy takes 15-25 MINUTES, almost all of it building and pushing" \
	  "        the ~1.3 GB ARM64 agent image to ECR. A long silence here is the image, not" \
	  "        a hang. A later deploy with nothing to change takes about a minute."
	@set -eu; \
	region=$${AWS_REGION:-$${AWS_DEFAULT_REGION:-$$(aws configure get region 2>/dev/null || true)}}; \
	stack=$$(PYTHONPATH=infra $(PYTHON) -c \
	  'import app, sys; print(app.stack_names(sys.argv[1], sys.argv[2]).account)' \
	  "$(STACK_PREFIX)" "$$region"); \
	budget=$$(( $(ACCOUNT_WAIT_SECONDS) / $(ACCOUNT_WAIT_INTERVAL) )); \
	attempt=0; \
	while :; do \
	  if ! status=$$(aws cloudformation describe-stacks --stack-name "$$stack" \
	      --region us-east-1 --query 'Stacks[0].StackStatus' --output text 2>&1); then \
	    case "$$status" in \
	      *"does not exist"*) printf 'deploy: ok      %s does not exist in %s yet; this deploy creates it\n' \
	        "$$stack" us-east-1; break ;; \
	      *) printf '%s\n' \
	        "deploy: FAILED -- could not read the state of $$stack in us-east-1:" \
	        "    $$status" \
	        "  why: the account stack holds the console's CloudFront-scoped WAF web ACL and" \
	        "       the Bedrock budget, and both are only creatable from us-east-1." \
	        "  fix: check these credentials may cloudformation:DescribeStacks in us-east-1," \
	        "       and that the session has not expired." >&2; exit 1 ;; \
	    esac; \
	  fi; \
	  case "$$status" in \
	    REVIEW_IN_PROGRESS|ROLLBACK_FAILED|UPDATE_ROLLBACK_FAILED|DELETE_FAILED|CREATE_FAILED) \
	      printf '%s\n' \
	      "deploy: FAILED -- $$stack in us-east-1 is $$status, and cdk cannot update it in place." \
	      "  fix: REVIEW_IN_PROGRESS is an abandoned change set -- delete it, or delete the" \
	      "       stack. *_ROLLBACK_FAILED needs continue-update-rollback. DELETE_FAILED and" \
	      "       CREATE_FAILED need the named resource removed by hand. Then re-run" \
	      "       'make deploy EMAIL=...'." >&2; exit 1 ;; \
	    *_IN_PROGRESS) \
	      attempt=$$((attempt + 1)); \
	      if [ "$$attempt" -gt "$$budget" ]; then \
	        printf '%s\n' \
	        "deploy: FAILED -- $$stack in us-east-1 is still $$status after $(ACCOUNT_WAIT_SECONDS)s." \
	        "  why: cdk refuses a stack mid-operation with 'is in $$status state and can not" \
	        "       be updated', so waiting is the fix -- but not for ever." \
	        "  fix: watch it in the us-east-1 CloudFormation console, then re-run" \
	        "       'make deploy EMAIL=...'. Raise the bound with" \
	        "       'make deploy EMAIL=... ACCOUNT_WAIT_SECONDS=1800' if it is genuinely slow." >&2; \
	        exit 1; \
	      fi; \
	      printf 'deploy: ..      %s in %s is %s; waiting %ss (%s/%s)\n' \
	        "$$stack" us-east-1 "$$status" "$(ACCOUNT_WAIT_INTERVAL)" "$$attempt" "$$budget"; \
	      sleep $(ACCOUNT_WAIT_INTERVAL) ;; \
	    *) printf 'deploy: ok      %s in %s is %s\n' "$$stack" us-east-1 "$$status"; break ;; \
	  esac; \
	done
	@mkdir -p "$(RCA_HOME)"
	@set -eu; \
	account=$$(aws sts get-caller-identity --query Account --output text); \
	region=$${AWS_REGION:-$${AWS_DEFAULT_REGION:-$$(aws configure get region)}}; \
	cd infra && PATH="$(CURDIR)/.venv/bin:$$PATH" $(CDK) deploy --all \
	  --require-approval never --outputs-file "$(OUTPUTS)" \
	  -c account="$$account" -c region="$$region" \
	  -c stackPrefix=$(STACK_PREFIX) -c budgetAlertEmail=$(EMAIL) $(CDK_CONTEXT)
	@set -eu; \
	region=$$($(json_output) $(CONSOLE_STACK) Region); \
	bucket=$$($(json_output) $(CONSOLE_STACK) ConsoleBucket); \
	distribution=$$($(json_output) $(CONSOLE_STACK) DistributionId); \
	pool=$$($(json_output) $(CONSOLE_STACK) UserPoolId); \
	client=$$($(json_output) $(CONSOLE_STACK) UserPoolClientId); \
	domain=$$($(json_output) $(CONSOLE_STACK) CognitoDomain); \
	printf '%s\n' \
	  "// Written by 'make deploy' from $(CONSOLE_STACK)'s own outputs. Never committed:" \
	  "// it is not a secret, but it names one live deployment (web/.gitignore)." \
	  "window.RCA_CONFIG = {" \
	  '  apiBase: "/api",' \
	  "  region: \"$$region\"," \
	  "  userPoolId: \"$$pool\"," \
	  "  clientId: \"$$client\"," \
	  "  cognitoDomain: \"$$domain\"," \
	  "};" > web/config.js; \
	aws s3 sync web/ "s3://$$bucket/" --region "$$region" --delete \
	  --exclude '.gitignore' --exclude 'config.example.js'; \
	aws cloudfront create-invalidation --distribution-id "$$distribution" --paths '/*' >/dev/null; \
	printf 'deploy: wrote web/config.js and uploaded web/ to %s, cache invalidated\n' "$$bucket"; \
	aws s3api head-object --bucket "$$bucket" --key index.html --region "$$region" \
	  >/dev/null 2>&1 || { printf '%s\n' \
	  "deploy: FAILED -- the console bucket has no index.html after the upload." \
	  "  why: the bucket is BLOCK_ALL and the OAC grant is s3:GetObject only, so S3" \
	  "       answers a missing key with 403 AccessDenied rather than 404. CloudFront" \
	  "       cannot serve an error page it also cannot fetch, so the console URL" \
	  "       returns S3's own <Error><Code>AccessDenied</Code></Error> XML." \
	  "  fix: re-run 'make deploy EMAIL=...'; the upload is idempotent. If it fails" \
	  "       again, these credentials cannot s3:PutObject into $$bucket." >&2; exit 1; }
	@set -eu; \
	region=$$($(json_output) $(CONSOLE_STACK) Region); \
	pool=$$($(json_output) $(CONSOLE_STACK) UserPoolId); \
	if message=$$(aws cognito-idp admin-create-user --region "$$region" --user-pool-id "$$pool" \
	    --username '$(EMAIL)' --user-attributes Name=email,Value='$(EMAIL)' \
	    Name=email_verified,Value=true --desired-delivery-mediums EMAIL 2>&1 >/dev/null); then \
	  printf '%s\n' \
	    "deploy: $(EMAIL) is the first console operator. Cognito has emailed a temporary" \
	    "        password, and the hosted UI forces a change on first sign-in."; \
	elif printf '%s' "$$message" | grep -q UsernameExistsException; then \
	  printf '%s\n' \
	    "deploy: $(EMAIL) is already a console operator, so its password was left alone." ; \
	else \
	  printf '%s\n' \
	    "deploy: FAILED -- could not create the console operator:" \
	    "    $$message" \
	    "  fix: check the address, and that these credentials may call" \
	    "       cognito-idp:AdminCreateUser on $$pool, then re-run 'make deploy EMAIL=...'." >&2; \
	  exit 1; \
	fi
	@set -eu; \
	api=$$($(json_output) $(API_STACK) ApiUrl); \
	region=$$($(json_output) $(CONSOLE_STACK) Region); \
	stamp=$$(aws cloudformation describe-stacks --stack-name $(API_STACK) --region "$$region" \
	  --query 'Stacks[0].LastUpdatedTime' --output text 2>/dev/null || true); \
	case "$$stamp" in None) stamp="" ;; esac; \
	printf '%s\n' \
	  "{" \
	  "  \"apiUrl\": \"$$api\"," \
	  "  \"stackName\": \"$(API_STACK)\"," \
	  "  \"region\": \"$$region\"," \
	  "  \"stackLastUpdated\": \"$$stamp\"," \
	  "  \"writtenBy\": \"make deploy\"" \
	  "}" > "$(RCA_HOME)/endpoint.json.tmp"; \
	mv "$(RCA_HOME)/endpoint.json.tmp" "$(RCA_HOME)/endpoint.json"
	@set -eu; \
	printf '\n'; \
	printf 'console: %s\n' "$$($(json_output) $(CONSOLE_STACK) ConsoleUrl)"; \
	printf 'api:     %s\n' "$$($(json_output) $(API_STACK) ApiUrl)"; \
	printf 'region:  %s\n' "$$($(json_output) $(CONSOLE_STACK) Region)"; \
	printf '%s\n' \
	  "" \
	  "$(RCA_HOME)/endpoint.json carries that API URL now, so a running MCP server picks it" \
	  "up with no restart and no URL is pasted into a config field anywhere." \
	  "One exception: a plugin userConfig override -- api_url, aws_region or stack_name --" \
	  "takes precedence over the document just written, so a plugin configured with one keeps" \
	  "using it and will not see this deployment until that field is cleared." \
	  "$(EMAIL) signs in at the console URL with the temporary password Cognito emailed."
	@set -eu; \
	origin=$$(git config --get remote.origin.url 2>/dev/null || true); \
	case "$$origin" in \
	  *github.com*) $(PYTHON) -c "$$RCA_REGISTER" "$$origin" || printf '%s\n' \
	    "deploy: the deployment is up, but registering this repository did not finish." \
	    "  fix: re-run 'make deploy EMAIL=...' from a terminal, or ask Claude Code to" \
	    "       register it -- register_repository takes the token without putting it on a" \
	    "       command line." >&2 ;; \
	  "") printf '%s\n' \
	    "deploy: this checkout has no 'origin' remote, so no repository was registered." \
	    "        Register one from Claude Code with register_repository." ;; \
	  *) printf '%s\n' \
	    "deploy: origin ($$origin) is not a GitHub remote, so nothing was registered." \
	    "        Only GitHub repositories can be registered." ;; \
	esac
	@set -eu; \
	region=$${AWS_REGION:-$${AWS_DEFAULT_REGION:-$$(aws configure get region 2>/dev/null || true)}}; \
	stack=$$(PYTHONPATH=infra $(PYTHON) -c \
	  'import app, sys; print(app.stack_names(sys.argv[1], sys.argv[2]).account)' \
	  "$(STACK_PREFIX)" "$$region"); \
	if [ "$$stack" != "$(ACCOUNT_STACK)" ] && \
	   aws cloudformation describe-stacks --stack-name $(ACCOUNT_STACK) --region us-east-1 \
	     >/dev/null 2>&1; then \
	  printf '%s\n' \
	    "" \
	    "deploy: $(ACCOUNT_STACK) in us-east-1 is left over from a deploy before the account" \
	    "        stack became one stack per consuming region. This deploy owns $$stack" \
	    "        instead, and nothing reads $(ACCOUNT_STACK) any more. Delete it once no" \
	    "        region still references it -- check that no other fleet's console" \
	    "        distribution names its web ACL first, then:" \
	    "          aws cloudformation delete-stack --stack-name $(ACCOUNT_STACK) --region us-east-1"; \
	fi
