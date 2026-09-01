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

# The AWS Distro for OpenTelemetry collector, which signs this container's OTel metrics with
# the execution role and hands them to CloudWatch (invariant 32b). `0` IS THE SAFETY PROPERTY
# OF THIS FILE: with it off, the `RUN` below executes nothing, no collector lands in the
# image, and this build produces exactly the image it produced before the collector existed.
# `infra/stacks/agent_stack.py` passes `ENABLE_OTEL_COLLECTOR=1` only when the deploy carries
# `-c otelMetricsEnabled=true`, and nobody should flip that until somebody has built this
# image on a machine with a Docker daemon and checked it against AgentCore's 2 GB cap
# (invariant 2) -- the collector's own artifact is not small, and no test in this repository
# can measure an image.
ARG ENABLE_OTEL_COLLECTOR=0
ARG ADOT_COLLECTOR_VERSION=0.49.0

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

# The collector, and ONLY when `ENABLE_OTEL_COLLECTOR=1` -- see the `ARG` above for why the
# default is the point. It arrives as the pinned Debian package AWS publishes rather than as a
# `COPY --from=` of the ADOT image, because every published tag of that ECR Public repository
# is `v`-prefixed and `test_the_agent_image_installs_nothing_from_a_moving_reference` requires
# a tag whose first character is a digit; weakening that guard for one dependency's tagging
# habit costs more than a download does. So the `v` lives in the URL and the `ARG` stays
# digits-only.
#
# Fetched with the base image's own `python3` because this stage has Python and
# `ca-certificates` already, so the download needs no new apt package -- and unpacked with
# `dpkg-deb -x` rather than installed with `dpkg -i` because the package's maintainer scripts
# create an `aoc` user and install a systemd unit, and this container has no systemd and runs
# the collector as its own child process (`coding_agent/telemetry.py`). `dpkg-deb -x` unpacks
# the data archive and runs none of them.
#
# `--version` closes the SAME `RUN`, for the reason the CLI install above gives: a package
# whose internal layout moved would otherwise produce an image whose collector silently never
# starts, and "no metrics appeared" is a much worse place to discover that than a failed build.
RUN if [ "${ENABLE_OTEL_COLLECTOR}" = "1" ]; then \
      set -eu; \
      python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/aoc.deb')" \
        "https://aws-otel-collector.s3.amazonaws.com/debian/arm64/v${ADOT_COLLECTOR_VERSION}/aws-otel-collector.deb"; \
      dpkg-deb -x /tmp/aoc.deb /; \
      rm -f /tmp/aoc.deb; \
      /opt/aws/aws-otel-collector/bin/aws-otel-collector --version; \
    fi

# The two Agent Skills as ONE plugin root. `config.skill_paths()` reads `AGENT_SKILL_PATHS`
# and falls back to the in-tree `agent/skills`; that directory itself contains
# `skills/<name>/SKILL.md`, and the nesting IS the plugin-root layout `tests/test_skills.py`
# pins -- so copy the root, not its contents. Copying the contents flattens it and both
# skills then silently never load, because the CLI reports a missing skill as nothing at all.
COPY skills /app/skills
ENV AGENT_SKILL_PATHS=/app/skills

# The collector's configuration, copied unconditionally even when the binary above was not
# installed: it is about a kilobyte, and a configuration with nothing to read it is inert.
# `coding_agent/telemetry.py` requires both this file and the binary before it starts anything,
# so the binary stays the one thing that decides whether this container exports at all.
COPY otel /opt/otel

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
# It sets no OTel environment variable either -- not the exporter's endpoint, not its protocol
# and above all not `CLAUDE_CODE_ENABLE_TELEMETRY`. Those are fleet policy, forwarded by
# `api/src/handlers/provisioner.py`'s `_fleet_environment` so that a deploy converges them
# across runtimes provisioned before they existed (invariant 24), and invariant 26 is why they
# cannot live here: an `ENV` is overridable by a runtime's `environmentVariables` but not
# removable, so an exporter switched on in this file could never be switched off again without
# an image rebuild.

# AgentCore reaches `/invocations` and `/ping` on 8080; `bedrock_agentcore`'s `app.run()`
# serves both, and `server.py` already carries the `if __name__ == "__main__"` guard.
EXPOSE 8080
CMD ["python", "-m", "coding_agent.server"]
