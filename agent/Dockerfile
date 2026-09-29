# The AgentCore Runtime image: Python 3.13, the pinned Claude Code CLI, git and make.
#
# THE BUILD CONTEXT IS `agent/`, NOT THE REPOSITORY ROOT. `infra/stacks/agent_stack.py`
# builds this with `DockerImageAsset(directory=<repo>/agent)`, so every COPY source below
# is relative to this directory -- `COPY skills ...`, never the same path with an `agent/`
# prefix in front of it, which is a build that dies on `"/agent/skills": not found`.
#
# Everything installed here is pinned to an exact version (invariant 34). Three of these
# execute code with real privilege inside every container that runs an untrusted
# repository's test suite, so a moving reference is a supply-chain hole and an image
# rebuild is a silent behaviour change.
# `tests/test_repo_contracts.py::test_the_agent_image_*` is what holds the shape of that,
# including the absence of every spelling of "whichever version is newest at build time" --
# so those spellings do not appear in this file, not even inside a comment.

# ---- Node, for the Claude Code CLI ---------------------------------------------------
# The CLI is an npm package, so this image needs a Node runtime. It is copied out of the
# official Node image rather than installed with `apt-get install nodejs npm`, because
# Debian's `nodejs` is whichever version that release froze: unpinned from here, invisible
# to a grep for pins, and on some releases older than the CLI accepts. Bookworm on both
# stages, so the copied binary and the base image share a glibc.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:22-bookworm-slim AS node

# ---- the runtime image ----------------------------------------------------------------
# Invariant 2, ARM64 everywhere, and silent when wrong: CloudFormation accepts an x86 image
# and AgentCore then never reaches READY, with no logs to say why. This flag must agree with
# `ecr_assets.Platform.LINUX_ARM64` in `infra/stacks/agent_stack.py`.
# ECR Public rather than Docker Hub on both stages: an anonymous Hub pull is rate limited,
# and the worst place to discover that is 20 minutes into a first deploy.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim-bookworm

# Invariant 34: exact versions, never a floating channel tag and never a branch name.
#
# `AWS_DOCS_MCP_VERSION` MUST EQUAL `subagents.AWS_DOCS_VERSION`. `subagents.AWS_DOCS_SERVER`
# runs `uvx awslabs.aws-documentation-mcp-server==<version>` at run time and is meant to
# resolve the environment this build already cached; a drift between the two turns that into
# a registry fetch at task time -- or, offline, into a tool that silently does not appear.
# `test_the_agent_image_installs_the_pinned_aws_documentation_server` asserts the pair.
ARG CLAUDE_CODE_VERSION=2.0.76
ARG AWS_DOCS_MCP_VERSION=1.1.30
ARG UV_VERSION=0.9.2

ENV PYTHONUNBUFFERED=1

# `git` and `make` because this container clones an untrusted repository and then runs that
# repository's OWN declared setup and verify commands (`coding_agent/commands.py`), and this
# repository's own are `make setup` and `make test` (invariant 35). Bookworm ships git 2.39,
# which is the floor invariant 25's `safe.directory` exemption assumes -- it supports no glob,
# so the exemption is an exact path.
RUN apt-get update \
 && apt-get install --no-install-recommends -y git make ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Only the two paths the CLI needs, so the image does not grow a second toolchain. `npm` and
# `npx` are the symlinks the Node image ships; they are recreated because they point into a
# directory that is copied and not the one they came from. `node --version && npm --version`
# closes the same RUN so a moved layout fails the build rather than the first `npm install`.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version \
 && npm --version

# Invariant 3: the CLI must be on PATH, and the smoke check is IN THE SAME `RUN` as the
# install on purpose. Split across two layers, a broken install is a first task that dies
# with `claude: not found`; here it is a failed build, in front of whoever caused it.
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && claude --version

# The AWS documentation MCP server, INSTALLED AT BUILD TIME at a pinned version (invariant 34
# and invariant 10b, documentation tools only). `uvx` at run time then resolves what is already
# here; it must not be the thing that goes and gets a version. `uv` itself comes from PyPI at
# an exact version rather than from a third container image, so there is one fewer registry
# reference to keep pinned. The cache is world-readable because the run-time `uvx` may not be
# the user that built it.
#
# `UV_OFFLINE` is deliberately NOT set: `subagents.py` documents a missing documentation
# server as an absent tool and never an error, and an offline hard failure would turn a cache
# miss into exactly the error that contract rules out.
ENV UV_CACHE_DIR=/opt/uv-cache
RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
 && UV_TOOL_BIN_DIR=/usr/local/bin \
    uv tool install "awslabs.aws-documentation-mcp-server==${AWS_DOCS_MCP_VERSION}" \
 && chmod -R a+rX "${UV_CACHE_DIR}"

# The agent itself. Manifest before source, so editing a module does not re-resolve five
# dependencies. Bounds on all of them are `agent/pyproject.toml`'s job and invariant 29's
# test already reads that file.
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# The two Agent Skills as ONE plugin root. `config.skill_paths()` reads `AGENT_SKILL_PATHS`
# and falls back to the in-tree `agent/skills`; that directory itself contains
# `skills/<name>/SKILL.md`, and the nesting IS the plugin-root layout `tests/test_skills.py`
# pins -- so copy the root, not its contents. Copying the contents flattens it and both
# skills then silently never load, because the CLI reports a missing skill as nothing at all.
COPY skills /app/skills
ENV AGENT_SKILL_PATHS=/app/skills

# THREE THINGS THIS FILE DELIBERATELY DOES NOT DO.
#
# It sets no environment variable for the agent's work root (invariant 26): a Dockerfile
# `ENV` cannot be *unset* by a runtime's `environmentVariables`, only overridden, which is
# why that value is forwarded by `api/src/handlers/provisioner.py` and read by `config`.
# One added here "for local convenience" makes the runtime's value unremovable.
#
# It ships no Claude Code CLI configuration file: nothing under the CLI's dot-directory in
# `/root`, no JSON settings file, and nothing copied to either. Invariant 10 and
# `subagents.build_options`: such a file loads REGARDLESS of `setting_sources=[]`, so it is
# the one file that can widen every role's session from inside the image.
#
# It sets no telemetry environment variable either, and above all not
# `CLAUDE_CODE_ENABLE_TELEMETRY` -- the one variable that turns the CLI's OTLP exporter on.
# That is fleet policy, pinned to `0` by `api/src/handlers/provisioner.py`'s
# `_fleet_environment` so a deploy converges it onto runtimes provisioned before it existed
# (invariant 24), and invariant 26 is why it cannot live here: an `ENV` is overridable by a
# runtime's `environmentVariables` but not removable, so an exporter switched on in this file
# could never be switched off again without an image rebuild. This image once carried an ADOT
# collector behind a build arg for CloudWatch Coding Agent Insights; `CLAUDE.md` records why
# that feature was removed rather than left wired up.

# AgentCore reaches `/invocations` and `/ping` on 8080; `bedrock_agentcore`'s `app.run()`
# serves both, and `server.py` already carries the `if __name__ == "__main__"` guard.
EXPOSE 8080
CMD ["python", "-m", "coding_agent.server"]
