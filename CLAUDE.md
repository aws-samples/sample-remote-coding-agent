# CLAUDE.md

Notes for agents changing this code, and the reference for everything expensive to
work out from the source: the layout and who owns what, the commands, the invariants
with the incident behind each, what was probed against the pinned SDK, and the
boundary claim with every place it is qualified. `README.md` keeps only what this is
and how to run it from nothing to nothing.

## Verification

```bash
make setup     # idempotent; safe to run when the venv already exists
make test
make lint
```

`make test` is the whole gate. It is fast, so run it after each change rather than at
the end.

## Decisions that look arbitrary and are not

- **`make setup` resolves `.venv` before handing it to `venv`.** The recipe runs
  `python3 -m venv "$(cd .venv && pwd -P || echo .venv)"`, because the agent's
  container pre-creates `.venv` as a symlink onto `/tmp` and `python3 -m venv` raises
  `ValueError: Unable to create directory` on a symlink regardless of what it points
  at. `--clear` does not help; the `islink` check runs after the clear. `pwd -P`
  resolves it without GNU-only `readlink -f`. The idempotence guard is on
  `.venv/bin/pip`, not `bin/python`, because a `uv venv` has the interpreter and no
  pip.
- **`make setup` comes before `make test` in a fresh container, always.** The `.venv`
  symlink is pre-created but points at an *empty* directory, so a first `make test`
  fails with `/usr/local/bin/python3: No module named pytest` and `make lint` with
  `No module named ruff`. That is a missing environment, not a red suite — and it reads
  as a red suite, which is how it costs an iteration. Run `make setup` (~2 min, and
  idempotent) first, then treat any failure as yours.
- **`--dist loadfile` in `pytest.ini` is load-bearing.** Handler modules bind their
  table names at import, so several test files `importlib.reload` them; the reload
  order is a within-file contract and only `loadfile` keeps a file's tests on one
  xdist worker. To run something serially use `-n0`
  (`python -m pytest -q -n0 tests/test_graph.py`) — **not** `-p no:xdist`, which
  unregisters the plugin that owns `-n`/`--dist` while `addopts` still passes them,
  and aborts with `INTERNALERROR` before collection.
- **There is no `mcp/__init__.py`.** Under `python -m pytest` the repository root is
  `sys.path[0]`, so a regular top-level `mcp` package there beats the installed MCP
  SDK outright and every `import mcp.server` fails with `No module named
  'mcp.server'` — which reads as a missing dependency, not a shadowed one. Without
  the `__init__.py`, `mcp/` is only a namespace portion and the real SDK wins.
- **There is no `infra/stacks/__init__.py`.** `pythonpath` already contains `infra`,
  so `stacks` resolves as a namespace package with `__file__ is None`. Adding the
  file makes its import depend on which directory pytest started from. The empty
  `infra/stacks/.gitkeep` is only there because git drops a directory with nothing
  tracked in it, which would make the namespace-package test fail on a clean clone
  for a reason nobody would guess.
- **Dependencies carry upper bounds.** An unbounded floor lets a major release land
  and break a build that changed nothing — `mcp>=1.2.0` resolved to 2.0.0, which
  removed a method, and `pytest>=8.0` resolved to 9 and turned a green checkout red.
  `setup` runs before verification on every run that verifies, so the failure has
  nothing in the diff to explain it. `tests/test_repo_contracts.py` enforces this
  mechanically across all five manifests.

## Conventions

`from __future__ import annotations` at the top of every module. Modern typing
(`str | None`, `list[tuple[str, int]]`). Comments explain *why*, not what. When you
change behaviour a comment describes, update the comment.

## Layout -- who owns what

| Path | What it is |
|---|---|
| `Makefile` | `setup`, `test`, `lint`, `preflight`, `deploy` — the single owner of every operator command |
| `pytest.ini` | `pythonpath` across all four packages, `asyncio_mode = auto`, `-n auto --dist loadfile` |
| `ruff.toml` | the lint selection |
| `mypy.ini` | the type-check configuration, and the recorded baseline finding count |
| `requirements-dev.txt` | the repository's own test toolchain, shared by all four packages |
| `agent/` | the AgentCore Runtime container: `Dockerfile`, `pyproject.toml`, `src/coding_agent/`, `skills/` |
| `api/` | the control-plane Lambda bundle: `requirements.txt`, `src/handlers/` |
| `infra/` | the CDK app: `app.py`, `cdk.json`, `requirements.txt`, `stacks/` |
| `mcp/` | the stdio MCP server Claude Code loads: `pyproject.toml`, `src/rca_mcp/` |
| `tests/` | `conftest.py` (moto fixtures, fake credentials) plus one file per area |

Each of these has exactly one owner, and each has a test behind it, because a second
one is the failure mode:

- `runs.queue_run` is the only definition of "queue a run" -- `tasks.create` and
  `cards.move` both go through it, so the slash command and the drag are the same code
  path. `api/src/handlers/cards.py::create_claimed` is the other half: it is the one way a
  card comes into being from `POST /tasks`, so both entry points produce a card as well as
  a run, and neither of them writes a card item of its own (invariant 38).
- `nodes/submit.py` is the sole publisher: nothing else writes to a git remote or the
  GitHub API.
- `nodes/review.py` is the only place `verifyResult` is composed (invariant 6).
- `observability.session_id` is the one owner of session ids -- a second session means
  two branches and two pull requests (invariant 18).
- `_common` is the only place a boto3 client is built.
- `git_ops` is every git invocation in the container.
- `agentcore_memory` is the one owner of the keying rule (D6) and of every
  `bedrock-agentcore` data-plane call in the container -- `memory_tools`' two tools and
  `graph._record_memory` both go through it, so the model-driven write and the automatic one
  key an event the same way. Not to be confused with `memory`, which is the filesystem
  `.agent/memory/` digest and a different feature wearing the same word.
- `subagents.build_options` is the one option surface for all four roles.
- `api/src/handlers/cards.py::STORED_COLUMNS`/`BOARD_COLUMNS` is the one definition of
  the board, mirrored in `web/js/transitions.js` and `web/js/status.js`.
- `tests/invoke_payload.py` is the one definition of the AgentCore invocation payload's
  field names, and both halves of the contract -- the dispatcher that writes it and the
  container that reads it -- are tested against it (invariant 13b).

## Commands

**Deploy this into an AWS account with nothing else in it.** It is free, it takes no
code, and it caps every other finding in this file: the worst case becomes "the demo
account" rather than "the account the demo happened to share". This system runs code it
did not write — an arbitrary repository's own `setupCommand` and test command — so the
account boundary is the outermost fence and the only one no bug inside this repository
can widen.

```bash
make setup                       # create .venv (inheriting the image's packages) and install all four
make test                        # the suite
make lint                        # ruff, oxlint, mypy
make preflight                   # prerequisite checks only; read-only, safe at any time
make deploy EMAIL=you@example.com  # the whole deploy, first console operator included
make deploy EMAIL=you@example.com STACK_PREFIX=Foo  # the same deploy as a second, separate fleet
```

`make preflight` checks the prerequisites and nothing else: a Docker daemon that
answers (the agent's ~1.3 GB ARM64 image is built on *this* machine), `uv` on `PATH`,
a Node new enough for the CDK CLI, `aws sts get-caller-identity` — printing the
account and the region it resolved — whether `cdk bootstrap` has been run in that
account and region, whether **Bedrock AgentCore answers in that region at all**, and
whether **every inference profile this deploy forwards into a runtime is `ACTIVE`
there**. The last two are the ones that would otherwise cost the whole deploy: both
failures land after the ARM64 image has been built and pushed, and the profile one is
worse than late — `MODEL_RE` in `agent/src/coding_agent/config.py` validates an id's
*pattern* and not its *geography*, so a wrong-region profile passes the container's
start-up `validate_model` and fails at invocation time instead: a silent runtime failure
rather than a deploy-time one. All three defaults in `infra/app.py` are `global.` for
exactly that reason, so reaching the hazard now takes an explicit geo-scoped
`-c planModelId=` / `-c codeModelId=` / `-c haikuModelId=` override — which is also the
one thing that buys residency, so the check earns its AWS call either way.
`_DEFAULT_HAIKU_MODEL` was `apac.` and is the worked example: a default that only
resolves in one geography failed every deploy outside APAC. Each failure stops at that check with a
`preflight: FAILED --` line and an indented `fix:` naming the action. A missing
bootstrap is **printed as the exact command, never run for you**: it is one command,
once per account and region, and at a different privilege level from a deploy.

`make deploy EMAIL=you@example.com` runs `preflight`, then deploys the five stacks —
the data, runtime, API and console stacks, plus an account stack pinned to `us-east-1`
carrying the console's WAF web ACL and the monthly Bedrock spend budget, which is where
`EMAIL` also receives the budget notifications — behind a printed 15-25 minute warning
(almost all of it building and pushing that image), uploads the console from the stacks'
own outputs, creates `EMAIL` as the first console operator because self-signup is off,
and prints exactly three facts — the console URL, the API URL and the region. The
console upload runs immediately after the stacks, so no step that can fail for its own
reasons — and `admin-create-user` can — sits between a live CloudFront distribution and
a populated origin; it then asserts `index.html` actually landed and stops with a
`deploy: FAILED --` line and a `fix:` if it did not. A console URL answering
`<Error><Code>AccessDenied</Code></Error>` means exactly one thing: the bucket has no
`index.html` (the OAC grant is `s3:GetObject` only, so S3 answers a missing key with
`403` and not `404`), and the fix is to re-run `make deploy EMAIL=...` rather than to go
looking at OAC or IAM. It also writes `~/.rca/endpoint.json`, which
is where the MCP server already looks, so a running server needs no restart and
nothing is pasted into a config field. Finally, when `origin` is a GitHub remote, it
offers to register this repository, asking for the PAT — the one value that genuinely
cannot be discovered. Both targets are idempotent, so a re-run is the health check.

An `RCA_API_URL` override outranks that endpoint document, and the `sample-rca` plugin's `api_url`
`userConfig` field is one of these — see `plugins/sample-rca/.mcp.json`. A live walkthrough hit
exactly that: a stale `api_url` from an earlier deployment beat the freshly written
`~/.rca/endpoint.json`, so `register_repository` reported `PROVISIONING` and then `READY`
against an older deployment in another region while the new fleet's `GET /configurations`
returned nothing, and the operator's PAT went into the other account's Secrets Manager.
The precedence is deliberate — someone running two deployments needs it — and it stays;
what was missing was any statement of which endpoint had been used. So every tool's answer
now ends with an `endpoint used:` line naming the URL, the region and which of the four
sources supplied it (the override, the endpoint document, discovery from the stack's
`ApiUrl` output, or an endpoint passed in directly). It is a purely local read of what has
already been settled — `mcp/src/rca_mcp/client.py::provenance` — rendered by
`mcp/src/rca_mcp/format.py::endpoint_provenance` and appended once in
`mcp/src/rca_mcp/server.py::call_tool`, so no tool pays a `describe_stacks` for it and a
tool that failed during discovery prints no line at all.

**The plugin name and the stack prefix are both qualified, and that is not cosmetic.**
Claude Code derives MCP tool names as `mcp__plugin_<pluginName>_<serverName>__<tool>`, so two
installed plugins that share a name collapse onto the same tool names and only one of them
resolves — silently, and not necessarily this one. A stack name collides the same way one layer
down, per account and region. So the plugin is `sample-rca` rather than a bare generic acronym,
and `DEFAULT_STACK_PREFIX` carries `Sample`; `tests/test_plugin_manifest.py` holds the plugin
name there by refusing an unqualified one. What makes a collision expensive rather than merely
confusing is that every symptom looks like success: `register_repository` answers with a
`configId`, then `PROVISIONING`, then `READY`, while the fleet you just deployed lists zero
configurations — because the tool that answered belongs to something else, and the GitHub token
went wherever it points. Nothing in this repository can reach
`~/.claude/plugins/installed_plugins.json`, so an operator who installed an unqualified id has to
remove that entry themselves; a version bump only tells the marketplace the entry changed.

`STACK_PREFIX` is the one stack-name knob, and it renames all five stacks at once:
`make deploy EMAIL=... STACK_PREFIX=Foo` is a whole second fleet — `FooData`, `FooRuntime`,
`FooApi`, `FooConsole`, `FooAccount` — in the same account. The Makefile derives the three
names it reads outputs from out of it *and* forwards it to the CDK as
`-c stackPrefix=$(STACK_PREFIX)`, which is what makes the deploy read back the stacks it
just wrote; `infra/app.py` owns the five suffixes and the `SampleCodingAgent` default. Two
caveats. A second fleet **in the same region** also needs `-c consoleDomainPrefix=<unique>`,
because a Cognito hosted-UI domain prefix is unique per region and both fleets would ask
for `rca-console` — the second one fails at `CreateUserPoolDomain`. And the MCP plugin
follows a renamed fleet through its existing optional `stack_name` user config, i.e.
`RCA_STACK_NAME`: `mcp/src/rca_mcp/client.py::DEFAULT_STACK_NAME` is pinned to the
*default* `SampleCodingAgentApi` and stays there. `APP_NAME = "rca"` is not part of the
prefix, so two fleets in one account still share the `/rca/` IAM path and the
`rca/github/*` secret namespace.

### What the walkthrough touches, and where it lives

`README.md`'s walkthrough is deliberately free of source paths and of conditional branches:
its reader is an operator mid-setup who has not read this code and wants the next command.
Everything the walkthrough used to attribute inline lives here, in the order an operator meets it.

- **The image is built on the operator's own machine.** `infra/stacks/agent_stack.py` declares the
  agent image as an `ecr_assets.DockerImageAsset`, so `make deploy` builds the ~1.3 GB ARM64 image
  locally. No build service stands in, and that build is most of the 15-25 minutes.
- **The first console operator's password is never printed.** `make deploy` runs
  `admin-create-user` with `--desired-delivery-mediums EMAIL` and no `--temporary-password`, so
  Cognito generates it, emails it, and nothing recovers it from the deploy output. A second
  `make deploy` reports the operator already exists and its password "was left alone" — it does
  **not** resend, so `--message-action RESEND` is the recovery, with the `UserPoolId` output of the
  console stack.
- **Registration is a console page, not a plugin tool.** With zero configurations `web/js/app.js`
  routes straight to `#/register`; afterwards the sidebar's **+ Add repository**
  (`web/js/views/sidebar.js`) is the link. `web/js/views/register.js` `POST`s `/configurations` and
  on `201` clears the token field, says provisioning takes a couple of minutes, and redirects to
  the board. A task started before the runtime reaches `READY` is refused with
  `409 ConfigurationNotReady`.
- **The slash command and the drag are one code path** — `tasks.create` and `cards.move` both call
  `runs.queue_run`. The board is live-polled by `web/js/activity.js`, and the task view links to
  that run's log group, `/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`.
- **Two spellings.** `/sample-rca:remote` is the plugin's, available wherever the plugin is
  installed; `.claude/commands/remote.md` is the un-namespaced `/remote`, which resolves only in a
  checkout of this repository. If some other namespace answers, it is not this system — see the
  collision note above.
- **The plugin ships two skills, and neither one deploys.** `/sample-rca:remote` dispatches and
  follows a task; `/sample-rca:scaffold` turns an empty repository into one the agent can run
  tasks against. Deploying is `make deploy EMAIL=...` from a checkout — the targets exist nowhere
  else, so wrapping them in a skill only helped an operator who already had the Makefile.
  `/sample-rca:setup` was that wrapper and it is gone as of plugin 0.3.0; `mcp/src/rca_mcp/client.py`
  still names it in the message it emits when no deployment answers, because moving the sha
  `plugins/sample-rca/.mcp.json` pins needs a commit that is already on `origin/main`.
- **Deregistration is console-only.** `DELETE /configurations/{configId}` is reachable from neither
  the plugin (`ApiClient.ALLOWED_METHODS` is `{"GET", "POST"}`) nor the `aws` CLI, which cannot
  SigV4-sign an arbitrary `execute-api` URL. It returns `202` and stays `DELETING` while
  `provisioner._delete` removes the runtime, the `/rca/` role and its policy, the secret, and the
  item **last** — **1-3 min** each. A `cdk destroy` first orphans all three.
- **The five names the teardown step spells out** are `infra/app.py::stack_names` at the default
  prefix. The account stack is one per consuming region, suffixed as `infra/app.py::region_suffix`
  spells it (`eu-west-1` → `…AccountEuWest1`); `STACK_PREFIX=Foo` renames all five, so a renamed
  fleet substitutes `Foo…` throughout that command.

### Tearing a fleet down

The order is the whole instruction, and `README.md` carries only the order — the commands live
here. **Destroying the stacks first orphans the AgentCore runtime, the per-configuration IAM
role at `/rca/` and the Secrets Manager secret**: nothing in CloudFormation owns them, they are
created per API call, and the runtime keeps costing money. This sequence has been run end to end
once, against a fleet with nothing registered in it, which is what corrected the command below.

1. **Deregister every repository.** In the console: the repository's settings page →
   **Deregister** → two clicks, the second armed only after a moment
   (`web/js/views/register.js`). This is console-or-curl only: `DELETE
   /configurations/{configId}` is a method no plugin tool can send
   (`ApiClient.ALLOWED_METHODS` is `{"GET", "POST"}`) and the `aws` CLI cannot SigV4-sign an
   arbitrary `execute-api` URL. It returns `202`, then stays `DELETING` while
   `provisioner._delete` removes the runtime, the role and its policy, the secret, and the item
   **last**. **1–3 min** each.
2. **Destroy the five stacks by name**, with the same pinned CLI `make deploy` uses:

   ```bash
   cd infra && PATH="$PWD/../.venv/bin:$PATH" npx --yes aws-cdk@2.1138.0 destroy \
     -c account=<account> -c region=<region> \
     SampleCodingAgentConsole SampleCodingAgentApi SampleCodingAgentRuntime \
     SampleCodingAgentData SampleCodingAgentAccountEuWest1
   ```

   **Both of the first two lines' additions are load-bearing, and this command failed without
   them.** `cdk.json` runs the app as `python3 app.py`, so a bare invocation uses whatever
   `python3` is on `PATH` and dies with `ModuleNotFoundError: No module named 'aws_cdk'`; the
   `PATH` prefix is exactly what `make deploy` does for the same reason. And `infra/app.py` reads
   the account and region from **context**, which the CDK CLI does not inject — it exports
   `CDK_DEFAULT_ACCOUNT`/`CDK_DEFAULT_REGION` as environment variables and no matching context
   keys — so without `-c account=` and `-c region=` the four in-region stacks synthesize
   environment-agnostic and `AccountStack`'s `cross_region_references=True` refuses them. Run
   `... ls -c account=… -c region=…` first if you want to see the five names it resolves before
   deleting anything. Add `--force` only to skip the interactive confirmation.

   **10–20 min**, mostly disabling and deleting the CloudFront distribution. The names are
   `infra/app.py::stack_names` at the default prefix; the account stack is one per consuming
   region, suffixed as `infra/app.py::region_suffix` spells it (`eu-west-1` →
   `…AccountEuWest1`), and `STACK_PREFIX=Foo` renames all five. `destroy --all` is equivalent
   **only** where this account and region hold one fleet — otherwise it also takes the other
   fleet's stacks and any unsuffixed `SampleCodingAgentAccount` left by a pre-per-region-split
   deploy.

### What deliberately survives a destroy

Destroying the five stacks leaves these behind on purpose. Capture the names you need *before*
the destroy — the stack that knew them is gone afterwards.

| Survives the destroy | Why | Delete it with |
|---|---|---|
| Three DynamoDB tables — Configurations, Tasks, Cards | `RemovalPolicy.RETAIN` (invariant 22), point-in-time recovery on | names are generated: `aws cloudformation list-stack-resources --stack-name SampleCodingAgentData` (a `TableV2` is an `AWS::DynamoDB::GlobalTable`), then `aws dynamodb delete-table` per table |
| The Cognito user pool | the pool *is* the operator list, so `cdk destroy` must not delete everyone's account and MFA registration. `FeaturePlan.PLUS` bills per monthly active user. | the hosted-UI domain first or the pool delete fails: `aws cognito-idp delete-user-pool-domain`, then `aws cognito-idp delete-user-pool` |
| The access-log bucket | CloudFront access logs plus the console bucket's S3 server-access logs under `s3-access/` — what says which sessions preceded a misused token | `aws s3 rb s3://<name> --force` |
| The ECR images, ~1.3 GB each | the agent image is a CDK asset, so it lives in the bootstrap repository owned by the `CDKToolkit` stack, which no `cdk destroy` of the five touches | `aws ecr batch-delete-image`, **not** `delete-repository --force` — every CDK app in the account shares it |
| The AgentCore runtime log groups | created by the service, in no stack; they hold the run transcripts | `aws logs delete-log-group --log-group-name /aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT` |

The DNS query log group is the exception: `/aws/route53/rca-agent-dns` is `RemovalPolicy.DESTROY`,
since its fixed name would collide at the next `CreateLogGroup`. It is the evidence for the egress
claim, so export it **before** the destroy. Lambda log groups go with the stack.

All of that is a **choice**, not an accident — destroying the tables takes every task's history.

### Why the Makefile targets are named that way

The remote coding agent sniffs a repository when it checks out, and it will only use
an entry point the repository itself **declared** — a Makefile `setup:` or
`bootstrap:` target for preparation, and a Makefile `test:` target for verification.
It deliberately guesses at nothing else.

Rename either and the agent falls back to a bare `python3 -m pytest` against an
interpreter with no dependencies installed. That fails with `No module named pytest`,
which reads as a broken test suite rather than as a missing environment — so a
perfectly good change gets reported red, and the agent may revise code that was never
wrong.

`setup` is also idempotent, because it runs before verification on *every* run,
including runs where the environment is already built.

## Invariants

These were expensive to discover -- each entry names the incident behind it, and
several of them fail *silently*, so a reader who skips them cannot tell they have
reintroduced one. Each gets a comment and a regression test.

1. **Never run the graph on the invocation thread** — `add_async_task` + a
   thread, return in ms. A blocked ping thread is reaped at 15 min, silently.
2. **ARM64 everywhere** — Dockerfile, `Platform.LINUX_ARM64`, `Architecture.ARM_64`.
3. **The Claude Code CLI must be on `PATH`** — keep the build-time `claude --version`.
4. **The PAT never enters the container env, the `origin` URL, or a command
   line** — all network git through `_run_authenticated` / `_askpass`;
   `_base_env()` omits the token. Every coding subagent has `Bash`.
4b. **The commit identity is configurable, and the default is known to fail a
   real check** — `git_ops`' default is `agentcore-coding-agent
   <agent@localhost>`, and `agent@localhost` matches no GitHub account, so
   GitHub resolves **no author** for the commit (`.author` is `null` in the
   commits API). Anything gating on commit attribution then refuses the pull
   request: a Vercel preview answers `Deployment was blocked` and never builds,
   and a DCO or signed-commit policy fails the same way. It is expensive because
   the pull request goes red on a *deployment* error while the diff is perfect —
   the cause is the author of the commit, not anything in it, and no test in
   this repository can see it. The override is the deploy-time context pair
   `-c gitAuthorName=` / `-c gitAuthorEmail=`, forwarded as `AGENT_GIT_AUTHOR_NAME`
   / `AGENT_GIT_AUTHOR_EMAIL`. It is **fleet policy**, so a runtime provisioned
   before it existed converges on the next update (invariant 24), and both keys
   are always sent — `""` included — because a key omitted from that group is
   *carried forward*, so sending the empty string is the only thing that makes
   clearing the context key actually restore the default. Not git's own
   `GIT_AUTHOR_*`: those are read directly by every git process and would
   outrank the exported config group, so the two could disagree with the
   environment silently winning. A **dedicated bot account** is the right value
   rather than a human's address — the commits were not written by a person and
   the audit trail should not say they were.
5. **Nothing under `.agent/` counts as work** — `has_changes()` is
   unconditionally true once the plan node writes a spec. Keep the two-way split
   in `code` (nothing outside `.agent/` raises; nothing beyond
   `planChangedPaths` warns) and `submit`'s independent guard.
6. **The veto is one-way, and `passed` is not `verified`** — and the asymmetry is
   **structural**: `verify` holds the exit code and has no verdict to apply,
   `review` holds the verdict and cannot re-run the command, and `review` alone
   composes `verifyResult`. See "The graph" below for the three signals and who
   may read each.
6b. **A `/goal` condition must never be the verification verdict, and nothing
   advisory may touch `verified`** — the goal evaluator and `/security-review` are
   both *model* judgements. `verified` means "a real command ran and was judged
   green" and has exactly one writer. A goal of "the tests pass" or a security
   finding that flips that bit reintroduces the second writer the whole split
   exists to remove.
6c. **A review that edits the tree costs one command re-run, and exactly one** —
   `source_diff_digest` either side of the review session; if it moved, route back
   to `verify`. The bound is one boolean on `TaskState` (therefore in the
   checkpoint) because `iteration` does not advance on the hop. A second edit is a
   loud `log.error` and the verdict stands against the second run.
7. **Read `final_text`, not `text`** — `text` is the whole transcript, narration
   included.
8. **Do not lower the `code` role's effort** — at `medium`, **Opus 5** reported
   complete with zero source edits against 214 insertions at `high`. That
   measurement was made on Opus, and `code` now runs on **Sonnet** (see
   "Model routing"), so it does not transfer: effort stays `high` there, and Sonnet-at-high
   is an open question with a named comparison to run, not a settled default. The
   no-op guard is what makes the experiment safe — under-performance here raises
   rather than passing quietly.
9. **`permission_mode="acceptEdits"`, never `"plan"`** — `"plan"` returns only
   narration and the spec never reaches the coder.
10. **`setting_sources=[]` *and* `strict_mcp_config=True`** — a checked-in
    `.claude/` must not widen permissions, and no MCP server may arrive from the
    clone, from user/global settings, or from a plugin root. Two mechanisms because
    they fail differently: `setting_sources=[]` is a consequence, the flag is a
    statement. Paired with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, the one input that
    loads *regardless* of `setting_sources`.
10b. **No MCP tool may call an AWS API, with one qualified exception.** The
    documentation server has no credential path; a general-purpose AWS MCP server
    would run under the container's execution role and hand a prompt-injected agent
    an AWS client. Only `mcp__aws-docs__*` is allowlisted, and only for `plan` and
    `code`. The exception is the in-process `memory` server, whose two tools do
    reach `CreateEvent` and `RetrieveMemoryRecords`. What makes that a different
    shape rather than a hole is mechanical and not asserted: exactly two fixed
    operations exist in `agent/src/coding_agent/memory_tools.py`, and the model
    **cannot name** the memory, the actor, the session or the namespace, because no
    such parameter exists in either schema and all four are **closed over** from the
    state. The absence is what is enforced, by
    `tests/test_agent_memory.py::test_no_tool_lets_a_model_name_the_memory_it_reads`
    rather than by this sentence. The general rule stands unchanged for anything
    general-purpose; see "AgentCore Memory" below.
10c. **A role-side commit is denied by a hook, not by a prompt** — `PreToolUse` on
    `Bash` rejects `git commit`. It replaces the one rule that had no mechanical
    enforcement, and it fires inside subagents too, so the implementer is covered
    by the same registration. A role-side commit empties `git_ops.diff` and makes
    `changed_paths` look clean, which the no-op guard reads as "nothing was
    implemented" — silent and confusing, which is why a sentence was never enough.
11. **Never hand a subagent a path outside its `cwd`** — `owner/repo`, not
    `repositoryUrl`; on a local run that is a `file://` path and subagents
    followed it.
12. **`RECURSION_LIMIT` above the worst-case node count** — derive it from
    `MAX_ITERATIONS`, and remember the worst-case iteration is **six** node visits
    (`plan`, `code`, `verify`, `review`, plus the one permitted `verify`/`review`
    re-run, which `iteration` does not count). LangGraph's own guard firing first
    replaces a draft PR with an opaque `GraphRecursionError`.
13. **Every observable transition is written to DynamoDB** — a new node needs a
    `_NODE_STATUS` entry, a `TaskStatus` literal, a `watchdog.RESUMABLE_STATUSES`
    line and a `web/js/status.js` line. `review`/`REVIEWING` is the worked example
    and the reason to price this: **four edits across three deployment units** for
    one node, and the console one is parsed line by line by
    `tests/test_web_status_vocabulary.py`, so its formatting is load-bearing. The
    re-run hop is a real node visit and is instrumented like any other. Activity
    writes **skip `updatedAt`**: that field is `lastFinished`'s sort key *and* the
    watchdog's only liveness clock.
13b. **The invocation payload's field names are a contract across two deployment
    units** — `api/src/handlers/dispatcher.py::_payload` writes it, `server._REQUIRED`
    and `graph.PAYLOAD_TO_STATE`/`OPTIONAL_PAYLOAD_FIELDS` read it. The dispatcher sent
    `prompt` while the container demanded `description` for a whole phase and **both
    suites stayed green**, because each half asserted its own spelling; the first
    invocation that ever reached a running container answered 500 with
    `ValueError: the invocation payload is missing description`. The wire name is
    `prompt` — the public field of `POST /tasks` and the stored Tasks attribute — the
    graph-state name is `description`, and `graph._initial_state` is the one translation
    point. `tests/invoke_payload.py` is the single definition of the shape and
    `tests/test_invoke_payload_contract.py` holds both halves to it, so a field the
    dispatcher sends which nothing in the container reads fails a test until it is
    classified there with a written reason. Beside 13 rather than folded into it: 13's
    subject is a *node* and its four-edit tax, this one is a *payload* and its two-unit
    tax, and they are reintroduced by different edits.
14. **Do not create a custom AgentCore endpoint** — `ENDPOINT_NAME = "DEFAULT"`.
15. **IAM is eventually consistent** — what may be retried at all is an **allowlist**
    (`provisioner._TRANSIENT_CREATE_FAILURES`): an error code, the operation that produced
    it, and for propagation a closed set of pinned message shapes. Everything else is
    terminal — but the allowlist has a **floor**: a message it does not recognise, out of
    `CreateAgentRuntime`, is retried **exactly once** and is then terminal with the vendor's
    own words in `statusReason`. The floor is there because a gap in the enumeration once
    cost the ability to provision at all: the live propagation message says the trust policy
    allows `assumption` by this service, all six pinned shapes were built around `assume`
    and `assumed`, so nothing matched and every `POST /configurations` failed on its first
    attempt. So the next phrasing the vendor invents costs one slow success, never an
    immediate permanent failure. The floor's own label claims no cause — nothing was
    classified, so the report may say only that the failure was unrecognised and was retried
    — and it is gated on the operation, because a permanent `iam:CreateRole` denial reaches
    the same handler and stays terminal on its first attempt. The budget
    is wall-clock — `_TRANSIENT_BUDGET_SECONDS`, 90 seconds, carried as a `retryDeadline` in
    the message body — not a count of attempts; retries are new SQS messages.
16. **`iam:TagRole` must not carry the `iam:PermissionsBoundary` condition** —
    a `CreateRole` passing `Tags` triggers a separate `TagRole` authorization
    whose context lacks that key, and the whole `CreateRole` is denied.
17. **Keep `iam:CreateRole` fenced three ways** — path, boundary, `PassRole`
    limited to `bedrock-agentcore.amazonaws.com`.
18. **Session ids need ≥33 chars and must be deterministic** — one owner
    (`observability.session_id`); a second session means two branches and two PRs.
19. **Dispatch and resume are claim-based** — conditional updates; resume
    additionally pins `resumeCount` and carries `attribute_exists(taskId)`,
    since `update_item` *creates* an absent item.
20. **`bedrock-agentcore` needs a newer boto3 than Lambda ships** — bundle
    `api/requirements.txt`, floor `botocore>=1.43.0`.
21. **`global.` inference profiles need a non-region-scoped model ARN.**
22. **Tables are `RETAIN`** — `DELETE /configurations/{id}` is the teardown path,
    not `cdk destroy`. A `FAILED` configuration does not hold its repository name:
    `POST /configurations` retires it (`DELETING` + a `DELETE` message) and registers
    a replacement, because `ApiClient.ALLOWED_METHODS` is `{"GET", "POST"}` and a
    permanent 409 could otherwise only be cleared in the console. `DELETING` is *not*
    retired — its teardown is already in flight.
23. **`_set_config` conditional on `attribute_exists`; `_create`/`_check` return
    early for `DELETING`** — otherwise a late `CHECK` resurrects a torn-down config.
24. **`UpdateAgentRuntime` is a full replace** — read the live runtime back and
    send `roleArn`, `networkConfiguration`, `protocolConfiguration`,
    `description`, `lifecycleConfiguration`, `filesystemConfigurations` and
    `environmentVariables`. *Carry forward* per-configuration values (`CONFIG_ID`
    and anything this code has never heard of); *converge* fleet policy. Compare
    per-key, or every SQS redelivery becomes an update.
25. **The mount is writable but not owned by the container user** — `git` dies
    with `fatal: detected dubious ownership` on a directory this process just
    created. Exempt the exact path via `GIT_CONFIG_COUNT`/`KEY_<n>`/`VALUE_<n>`
    in `os.environ`, because that is the only channel reaching both
    `_base_env()` (which builds its env from nothing, so it copies the whole
    `GIT_CONFIG_*` group back explicitly) and every subagent's inherited `Bash`.
    git 2.39 supports **no glob** in `safe.directory`; never use `*`. Probe the
    root with a real throwaway `git init` — `mkdir` succeeding is what let three
    tasks die 30 s later.
26. **A Dockerfile `ENV` cannot be unset by a runtime's `environmentVariables`** —
    only overridden. `WORK_ROOT` is therefore forwarded by the provisioner and
    **not** baked into the image.
27. **The watchdog's quiet threshold must clear `VERIFY_TIMEOUT`** —
    `verify` makes no model call, so a healthy verification is silent for up to
    30 min. `SETUP_TIMEOUT (900) <= VERIFY_TIMEOUT (1800)`, and setup writes an
    activity row when it finishes, so the two never add up. `REVIEWING`'s silence
    is a *subagent's*, and a subagent streams activity rows, so this bound stays
    `verify`'s alone — adding the review node does not move it.
28. **A deliberately cancelled task carries `cancelledAt`** and the watchdog
    *resolves* it rather than skipping it — a skip logs the same task forever.
    The attribute must stay in `_SCAN_PROJECTION` or the branch is dead code.
    The order is `cancelledAt` first and `StopRuntimeSession` second —
    `tasks.cancel` writes the mark, then calls `watchdog.stop_session` —
    because a stop that lands with no mark leaves a dead session behind an
    item that still looks live, which is a task the sweep tries to resume.
29. **Every dependency declaration carries an upper bound at the next major** —
    `mcp>=1.2.0,<2` removed a method and served nothing; `pytest>=8.0` resolved
    to 9 and turned a green checkout red. `setupCommand` runs before verification
    in every run that verifies, so an unbounded floor fails runs for reasons that
    have nothing to do with the code.
30. **`network_mode` is VPC, and `_update` must carry it forward** — invariant 24
    says `networkConfiguration` is carried forward from the live runtime, and it
    now carries a VPC config. Getting that wrong silently returns a runtime to
    public egress, which is invisible until something exfiltrates. Classify it as
    *fleet policy* and **converge** it instead, beside `lifecycleConfiguration`.
31. **The container writes no configuration item and reads only its own** —
    `dynamodb:LeadingKeys` on both table statements, `GetItem` only on
    Configurations. Otherwise a compromised container edits another repository's
    `verifyCommand`, which is remote code execution in that repository's container.
32. **The repository's own command runs with an environment built from nothing** —
    no `AWS_*`, no GitHub token, only `PATH`, `HOME`, `LANG`, `CI` and
    `git_config_env()`. One private `_run` for both the verify and setup commands,
    so the two cannot drift, and the absence is pinned by a parametrised test.
32b. **Telemetry goes out through the loopback, and no handler gains a CloudWatch
    grant** — the container exports OTel *metrics* to `localhost:4318`, where an
    in-container ADOT collector SigV4-signs with the execution role. **Never a
    bearer token**: that is a long-term IAM-user credential, and this container's
    whole credential story is "one PAT, never in the environment". And Coding Agent
    Insights is read in the CloudWatch console by a human, never proxied through
    `api/`, so note 30's read-only-over-DynamoDB posture is untouched.
    The mechanism now exists and is **switched off**: one context key,
    `-c otelMetricsEnabled=true`, gates the collector in the image
    (`ENABLE_OTEL_COLLECTOR`, default `0`), the exporter variables in
    `provisioner._fleet_environment`, the `cloudwatch:PutMetricData` grant on the
    execution role and the matching widening of the permissions boundary -- all four
    or none, because an image with a collector and no permission is a broken deploy.
    `agent/otel/collector.yaml`'s `sigv4auth` extension signs for service
    `monitoring` in `${env:AWS_REGION}`, with no credential written anywhere. The
    container gets the last word and only in one direction:
    `telemetry.exporter_env` returns `CLAUDE_CODE_ENABLE_TELEMETRY=0` whenever the
    collector binary is absent from this filesystem, and never returns `1` -- so a
    runtime whose fleet policy has not converged cannot retry against a port nothing
    is listening on, and turning the exporter *on* stays the fleet's decision.
33. **No role may reach the network, and no vendor endpoint may be reached** —
    `WebFetch`/`WebSearch` stay out of `ALLOWED_TOOLS`, the CLI's suppression
    variables stay in `_container_environment`, and the DNS Firewall deny list
    stays in the VPC. All three are asserted, because the whole compliance claim
    rests on them and a "let the agent read docs" change would undo the first one
    in a single line.
34. **Nothing is installed from a moving reference** — the plugin's `.mcp.json`
    pins a tag or sha, never `@main`; the Dockerfile pins the Claude Code CLI
    version, never `@latest`; and the AWS documentation MCP server is installed at
    **build time at a pinned version**, never fetched by `uvx …@latest` at run
    time. All three execute code with real privilege: the first on the operator's
    workstation with their AWS credentials, the other two inside every container.
    A runtime fetch would also make every task depend on a registry being up.
    Moving the plugin's pin is its own commit, and it has to be:
    `tests/test_plugin_manifest.py::test_the_pinned_reference_is_not_older_than_the_fix_it_ships`
    wants the pinned sha to be at or after every commit reachable from `HEAD` that touched
    `mcp/`, and a commit's sha does not exist until the commit does. So a change under
    `mcp/` lands as **two commits pushed together** — the change, then the re-pin naming
    it — and a lone commit that edits `mcp/` is a red suite with nothing in the working
    tree to fix. Plan any `mcp/` change that way; the alternative a hurried reader reaches
    for is a pin on a sha that is local-only or invented, which passes the shape check,
    installs nothing from GitHub, and is exactly the silence invariant 34 exists to stop.
35. **The suite only runs from `.venv`** — so `verifyCommand` for this repo is
    `make test`, not `python -m pytest -q`, and `setupCommand` is `make setup`.
    A wrong *verify* command is a red exit the loop can react to; a wrong *setup*
    command switches verification off silently, one draft PR at a time. Detection
    is therefore stricter for setup: only a declared entry point
    (`Makefile` `setup:`/`bootstrap:`, `package.json` `scripts.setup`), never
    `install:`, and `""` otherwise.
36. **A replacement registration retires the stale `FAILED` item *before* it mints the
    new secret** — `configId` is random, so a replacement gets a new role name and a new
    secret name; the other order orphans an IAM role at `/rca/` and a Secrets Manager
    secret holding a live GitHub token when the retirement write fails. The stale secret
    is never reused for a new token.
37. **A VPC-mode runtime needs `iam:CreateServiceLinkedRole`, and a permanent
    authorization failure is terminal and reported as itself** — `CreateAgentRuntime` with
    `networkMode: VPC` (invariant 30) makes AgentCore create a service-linked role of its
    own, so the provisioner's grant carries `iam:CreateServiceLinkedRole` on
    `resources=["*"]` fenced by **`StringLike`** on `iam:AWSServiceName`
    (`bedrock-agentcore.amazonaws.com` and `*.bedrock-agentcore.amazonaws.com`), never
    `StringEquals`: the names are subdomain-qualified — the one already in the account is
    `AWSServiceRoleForBedrockAgentCoreRuntimeIdentity` for
    `runtime-identity.bedrock-agentcore.amazonaws.com` — and the VPC-mode one is
    documented nowhere reachable, so a single guessed name just fails again. The wildcard
    is suffix-anchored on the literal `bedrock-agentcore.amazonaws.com`, so the grant
    cannot reach any other service; a service-linked role's trust policy and managed
    policy are fixed by AWS, so this is not the widening invariant 17 fences. It is a
    *separate* statement, because `MintExecutionRoles`'s `iam:PermissionsBoundary`
    condition would never match a role that carries no boundary (invariant 16's lesson).
    The other half is the report: retryability is an **allowlist** of error code plus the
    operation that produced it plus, for propagation, a closed set of pinned message shapes,
    and everything unrecognised is retried exactly once — the allowlist's floor — and is then
    terminal with the vendor's own words in `statusReason` — inside a wall-clock budget rather
    than a count of attempts. The floor is what keeps a gap in the enumeration cheap: the live
    propagation message says the trust policy allows `assumption` by this service and every
    pinned shape said `assume`, so a missing shape cost the ability to create any
    configuration at all; one retry makes the next unenumerated phrasing cost one slow success
    instead. It claims no cause of its own, and it does not reach an `iam:CreateRole` denial
    raised into the same handler. What
    this replaced was keyword matching on the vendor's prose, whose default was *retry*:
    anything containing `assume` and none of four permanent-cause phrases, so every
    permanent cause nobody had met yet was retryable by omission. That turned `Failed
    creating service linked role` into ten attempts over 26 minutes that then blamed an
    execution role that was never the problem — the third such lie. A
    permanent cause gets a named predicate, `FAILED`, and a `statusReason` naming itself,
    and it returns rather than raises: a raise leaves the item `PENDING` with no reason
    and only a DLQ body, and the operator reads `statusReason`.
38. **Every run has a card** — `api/src/handlers/tasks.py::create` reads the configuration,
    then creates the card, then queues the run, then records the run on the card, in that
    order. The card is `api/src/handlers/cards.py::create_claimed`'s: born `ACTIVE` with
    `runCount = 1` in one `put_item`, because the id is freshly minted so there is no
    concurrent drop to fence out and the counter is a claim token whose one claim is spent.
    There is **no request field that opts out**, and that is the invariant rather than an
    omission: an invariant with a flag on it is not an invariant, and a run nobody can see
    on the board is the failure this closed — `POST /tasks` is what the MCP `start_task`
    tool calls, so before this a run started from Claude Code was invisible in the console
    by construction. A queue that fails releases the card to `BACKLOG` rather than deleting
    it (`cards.release`, the same repair `cards._dispatch` performs), so a
    `409 ConfigurationNotReady` leaves the operator's prompt as a card they can drag once
    the repository is `READY`. Nothing is stored to keep the column current:
    `cards.derived_view` projects it off the run's own status on every read. What notices
    this breaking is
    `tests/test_api_tasks.py::test_a_run_started_from_the_api_lands_on_the_board_in_planning`,
    which asserts a `POST /tasks` run draws in `Planning` and then follows the run through
    `Coding` to `Done`. Not retroactive: task items written before this carry no `cardId`
    and no migration invents one.

## AgentCore Memory — one per registered repository

The one AgentCore surface here beyond the Runtime, and the only feature in this repository
that a model can add to. Two labels the code cites by name and nothing defined until now
are defined here: **D6**, the keying rule, and **D11**, the degradation rule. A bare `(D6)`
in a comment is only a pointer; this section is what it points at.

**Shape and lifecycle.** `api/src/handlers/provisioner.py::_ensure_memory` calls the
**control-plane** client's `create_memory` once per configuration, named `rca_mem_<configId>`
by `provisioner._memory_name` — `[^a-zA-Z0-9]` replaced by `_` and truncated to 48
characters, because `CreateMemory`'s `name` pattern is `[a-zA-Z][a-zA-Z0-9_]{0,47}` and a
hyphen in a config id is a server-side `ValidationException` that reads like a service
problem. It passes `eventExpiryDuration=_MEMORY_EVENT_EXPIRY_DAYS`, which is **90** days;
`clientToken=config_id`, so a transient retry inside the service's idempotency window cannot
mint a second memory; and `tags` as a **map**, `{"rca:configId": ..., "rca:repository": ...}`
— not `create_role`'s list of `{"Key": ..., "Value": ...}` pairs, because `CreateMemory`'s
`tags` member really is a map in the service model.

**Three things about memory are unmeasured, and this is the only place that says so.** No live
run has shown a memory written by one task being recalled by another, so the feature is
implemented and undemonstrated. The two strategies' extraction quality is a default nobody has
judged. And the **90**-day `eventExpiryDuration` is a default too: a memory silently empties
after a quarter, and no run has been alive long enough to see it happen. `README.md` used to
carry this in a `## Known gaps` section; that section was removed, so an admission that is not
written here is written nowhere.

It runs **before** `provisioner._ensure_role`, because the inline policy that role gets has to
name the memory's exact ARN. It is idempotent the way `_ensure_role` is: a stored `memoryId`
on the configuration item wins and no API call is made. That ordering is load-bearing in a way
worth spelling out — a `CREATE` retried inside the transient budget re-enters with an item
that by then carries a `memoryId`, a second `CreateMemory` on that name answers
`ConflictException`, and a function that returned `""` there would let the single write at the
end of the create path **overwrite a live memory id with an empty string**, silently
un-granting the role's memory statement on the next provision.

A `ClientError` is a `LOG.warning` and `""`, never a raise, so a memory that could not be
created costs that one repository its memory and nothing else: the configuration still reaches
`READY` and the container degrades to a no-op. `api/src/handlers/provisioner.py::_delete_memory`
tears it down on `DELETE /configurations/{configId}`, after the runtime and before the inline
policy — nothing the runtime does can reach a memory that is gone, and the policy granting
`UseOwnMemory` outlives what it names. A memory therefore has exactly the lifetime of the
registration, and `cdk destroy` is not the teardown path.

**Two strategies, two namespaces.** One `create_memory` call configures both, and the
strategies are what fan a single written event out to two places:

| Strategy | Name | Namespace | What it holds |
|---|---|---|---|
| `semanticMemoryStrategy` | `repository_episodic` | `/rca/{actorId}/episodic` | What this repository has learned across every task: **one** namespace shared by all of its sessions. |
| `summaryMemoryStrategy` | `session_summary` | `/rca/{actorId}/session/{sessionId}` | One rolling summary per task session, so a resumed run reads back what its own run has already done. |

Both templates are spelled once in `api/src/handlers/provisioner.py` and again in
`agent/src/coding_agent/agentcore_memory.py`, and
`tests/test_agent_memory.py::test_the_namespace_templates_are_the_provisioners_spelling` is
what keeps the two spellings identical. A mismatch does not fail:
`retrieve_memory_records` returns nothing, forever, with no error and no warning — an agent
that never remembers.

**D6 — the keying rule, and it is the whole design.** On the write, in `TaskMemory.record`:
`actorId` is the **configuration id** and `sessionId` is the **task id**. Say it in that
order and on one line, because the pairing is the one thing the API does not suggest and
cannot be inferred from either template. What it buys: every task the repository ever runs
writes its events under the same `actorId`, so the episodic strategy extracts them into **one
namespace shared by all of that repository's sessions** — which is what makes the episodic
memory a *repository's* memory rather than a task's, and what makes a lesson from one task
readable by the next — while the summary strategy keeps a per-run namespace beside it. What
going backwards costs: nothing raises, both templates still resolve, and they resolve to
somewhere no reader looks.

Two details worth having, because the call signatures are not symmetrical. `create_event`
passes **no** `namespace` at all — the strategies do that fan-out. And
`retrieve_memory_records` passes **neither** `actorId` nor `sessionId`; the ids reach the read
side only through the namespace templates, resolved by `TaskMemory._namespace`, where an
unrecognised scope is a warning and a fall back to episodic, because a *model* supplies that
value and an unrecognised one must be cheap.

**How the memory id reaches the container: not through the environment.** Neither
`agent/src/coding_agent/agentcore_memory.py` nor `agent/src/coding_agent/memory_tools.py`
reads a variable. The id travels configuration item → `agentcore_memory.resolve_memory_id` →
`TaskState`'s `memoryId` (`agent/src/coding_agent/state.py`) → `TaskMemory`, resolved
**exactly once** in `graph.run_task` and applied truthy-only, so the *absence* of the key is
the disabled signal rather than a second falsy spelling of it. Resolving it in a node would be
one `GetItem` per node visit for a value that cannot change mid-task; resolving it in
`subagents.build_options` would make that function impure. `TaskMemory.enabled` requires all
three of memory id, configuration id and task id, because `CreateEvent` needs the actor and
both templates need both ids.

**D11 — degradation, never an exception.** Every AWS call in
`agent/src/coding_agent/agentcore_memory.py` is `except (ClientError, BotoCoreError)` →
`log.warning` → an empty result: `TaskMemory.record` returns `False`, `search` returns `[]`,
`agentcore_memory.resolve_memory_id` returns `""`. The client is built *inside* each `try` and
never at import, so a missing credential or region degrades the same way rather than killing
the module. A repository registered before memory existed, and
`python -m coding_agent.local_run` on a machine with no tables at all, both run the whole loop
and **build no client**. Memory is an optional feature reached from inside a model's tool call,
and a run whose code, verification and pull request all succeeded must not die because a memory
write failed. The read-back route deliberately does *not* follow this rule — see below.

**Two tools, on keys the model cannot name.** `agent/src/coding_agent/memory_tools.py::build_server`
is the repository's first **in-process** MCP server (`create_sdk_mcp_server`, named
`memory_tools.MEMORY_SERVER_NAME` = `memory`), and in-process rather than stdio is not a
convenience: a subprocess cannot hold the task's state, and the only way to hand it the
container's credentials would be an `env` key — the thing the documentation server's missing
`env` exists to prevent. It exposes exactly two tools:

- `memory_search` — `query` (the only required one), `scope` from the two-value enum
  `episodic`/`session` defaulting to `episodic`, and `top_k` bounded 1–20 defaulting to
  `DEFAULT_TOP_K` = 5. Reaches `RetrieveMemoryRecords`.
- `memory_record` — `text`, required, and nothing else. Reaches `CreateEvent`.

No `memoryId`, `actorId`, `sessionId`, `namespace`, `configId` or `taskId` parameter exists in
either schema; all of them are closed over from the state. That absence is the mechanical half
of invariant 10b's qualification, and it is asserted by
`tests/test_agent_memory.py::test_no_tool_lets_a_model_name_the_memory_it_reads` rather than
trusted to prose — **do not add a parameter that could name a memory.** The schemas are the
explicit JSON Schema form and not the SDK's dict-of-types shorthand, which is a trap and not a
style choice: the shorthand marks *every* key `required`.

`subagents.build_options` adds `memory_tools.MEMORY_TOOLS` = `mcp__memory__*` to **every**
role's tools when the task has a memory — not just the roles that get the documentation server
— merges the server into `mcp_servers`, threads it into the fan-out `implementer`, and drops
the server entirely when `build_server` returns `None`. A memoryless run therefore gets no
server and no tool name at all, rather than two tools that fail on every call and spend a
role's turns discovering it.

**Recording is automatic, so nothing depends on a model choosing to call a tool.**
`graph._record_memory` writes one event per node visit, with the note `graph._memory_note`
composes capped at `_MEMORY_TEXT_LIMIT` = **500** characters — not the 2000 a `FAILED` status
allows, because a memory record is *prompt surface* a future task pastes into a role's
context, and a node that pasted a command-output tail in here would spend that run's tokens
re-reading this run's noise. `verify`'s note carries the command and the exit code off
`commandResult` (not `verifyResult`, which during a `verify` visit still describes the previous
measurement); `review`'s carries `passed=` and `verified=`, both halves, because a run with no
command configured is `passed` and *not* `verified`; `submit`'s carries `verified=` and the PR
URL. The worst run there is makes 22 node visits, so at most 22 `CreateEvent` calls per task —
the ceiling is derived from `MAX_ITERATIONS` and needs no counter. The write never reaches the
graph state.

**Recall goes into the prompt, not into a tool a role has to remember.** `{recalled_memory}`
is a slot in four templates — `PLAN_PROMPT` and `REPLAN_PROMPT` in `nodes/plan.py`,
`CODE_PROMPT` in `nodes/code.py`, `REVIEW_PROMPT` in `nodes/review.py` — each filled with
`agentcore_memory.recall(state, query)` or the literal `(nothing recorded yet)`, with the query
capped at 400 characters because `RetrieveMemoryRecords` is a semantic search and a whole
specification pasted in as the query embeds to noise. `recall` returns `""` and never
`agentcore_memory.format_records`' nothing-matched sentence, so the prompt chooses its own
placeholder in its own voice. `replan` deliberately queries on the **failure** — the
description plus the verify summary — rather than on the task, because a re-plan asking "what
do we know about this task" gets back exactly the records the attempt that just failed already
had (`nodes/plan.py`'s `REPLAN_PROMPT` is the one that reads it).

**IAM, in three parts — and the middle one is not the fence.**

| | Where | What |
|---|---|---|
| Grant | `api/src/handlers/provisioner.py::_execution_role_policy`'s `UseOwnMemory` | Exactly six data-plane actions — `bedrock-agentcore:CreateEvent`, `ListEvents`, `GetEvent`, `RetrieveMemoryRecords`, `ListMemoryRecords`, `GetMemoryRecord` — on the **literal** ARN `arn:aws:bedrock-agentcore:{region}:{account}:memory/{memory_id}`. The statement is omitted entirely when there is no `memoryId`, so a configuration whose `CreateMemory` failed gets no statement rather than an empty one. |
| Ceiling | `MayUseOwnMemory`, in `infra/stacks/agent_stack.py` | The same six, on `memory/*`. The permissions **boundary** is minted **once per deploy**, before any memory exists, so there is nothing narrower to name — and the trailing `*` also absorbs the ten-character suffix AgentCore appends to every memory id, so an ARN ending at the name would match nothing at all. |
| Deny | `MayNotEscalate`, in the same file | `CreateMemory`, `UpdateMemory`, `DeleteMemory`, `ListMemories`, `DeleteEvent`, `DeleteMemoryRecord` and `BatchDeleteMemoryRecords`, **by literal name** — the seven destructive actions the grant deliberately omits, denied so a later "helpful" widening of the grant fails a test instead of landing. |

The cross-repository fence is the **literal ARN on the grant**, not `memory/*` on the
boundary. `memory/*` says "some memory in this account"; only the grant says "this one".

And **no wildcard may enter the deny group.** An explicit Deny beats every Allow, in the
identity policy and in the boundary alike, so `bedrock-agentcore:*` — or even `List*`, which
shadows `ListEvents` and `ListMemoryRecords`, or `Create*`, which shadows `CreateEvent` —
turns every memory write from a container into an `AccessDenied`, with nothing in either
document naming the cause. That is why the group is twelve literal action names and not one
wildcard, and it is the same reason `ecr:*` was taken off it. The list will go stale: a
thirteenth destructive action AWS ships tomorrow is denied by the whitelist but not named
there, and that is the correct default — this is a named denial of what a future edit is most
likely to add, not an enumeration of the service.

**Network.** `infra/stacks/agent_stack.py` adds a `BEDROCK_AGENTCORE` interface endpoint, so
`create_event` and `retrieve_memory_records` — a task's own transcript — stop leaving the VPC
over the NAT gateway, and memory becomes the same in-boundary claim Bedrock and Secrets
Manager already are. It needs no change in the container: `private_dns_enabled` defaults on,
so the regional name the boto3 client already resolves now answers privately.

The **data plane** only. The control plane is `bedrock-agentcore-control`, which
`api/src/handlers/provisioner.py` calls for `CreateMemory` and `DeleteMemory` from Lambdas
this deployment keeps deliberately **outside** that VPC, so an endpoint for it here would be
an ENI in every AZ with no client. `bedrock-agentcore.gateway` is a third service and nothing
here speaks to a gateway either — which is why the diagram in `README.md` lists AgentCore once
among its VPC endpoints and not three times.

**Read-back and reporting.** `GET /configurations/{configId}/memory` reaches
`api/src/handlers/configurations.py::memory`: with a `q` it calls `retrieve_memory_records`,
without one it calls `list_memory_records`. No memory is a `200` carrying `"memoryId": None`
and `"items": []`, deliberately **not** a 404 — the configuration exists, only its memory does
not, and an empty `memoryId` would be a `ParamValidationError` inside botocore, i.e. a 500 for
a repository that is merely new. A `ClientError` out of either arm is deliberately **not**
swallowed into an empty list here: D11 exists to stop a *run* dying, and an operator asking
what a repository remembers must not be told "nothing" when the truth is "the call failed".
`memoryId` is in `_PUBLIC_FIELDS`, the console prints a `Memory` row on `#/register`
(`web/js/views/register.js` — the id, or "none: registered before memory, or provisioning did
not create one"), and `mcp/src/rca_mcp/format.py::repository` prints a `memory:` line in both
states, because a `memory:` line that is absent cannot be told from a field that was not in
the payload.

## What was probed against the pinned SDK, and what came back

Four things the documentation does not answer were measured here rather than assumed,
against `claude` **2.1.235** on `PATH`, the **2.1.236** CLI bundled inside
`claude-agent-sdk` **0.2.141**, and live Bedrock credentials. Each answer below carries
its reproduction, so it can be re-run after an SDK or CLI bump instead of trusted
forever. Re-run them if either version moves: three of the four changed the design.

### 1. The SDK never passes `--bare`, and the `/goal` half was probed on the `PATH` CLI

`--bare` disables the hooks system, and `/goal` is built on hooks. Every flag the SDK
appends was enumerated:

```bash
grep -o '"--[a-z-]*"' \
  .venv/lib/python3*/site-packages/claude_agent_sdk/_internal/transport/subprocess_cli.py \
  | sort -u
```

That gives 32 flags — `--add-dir --append-system-prompt --betas --continue --effort
--fallback-model --fork-session --include-hook-events --include-partial-messages
--input-format --json-schema --max-budget-usd --max-thinking-tokens --max-turns
--mcp-config --model --output-format --permission-mode --permission-prompt-tool
--plugin-dir --session-mirror --settings --strict-mcp-config --system-prompt
--system-prompt-file --task-budget --thinking --thinking-display --tools --verbose` —
and `--bare` is not among them. That half is a property of the SDK's own
`subprocess_cli.py`, so it holds for whichever CLI the SDK launches. The other half is
scoped narrower than it looks: `goal` appearing in the init message's `slash_commands`
was observed against **`claude` 2.1.235 on `PATH`**, and the container does not run that
binary. It runs the SDK's *bundled* CLI, `claude_agent_sdk/_bundled/claude`, which
announces itself in the runtime logs as "Using bundled Claude Code CLI" — a different
artefact from the one probed. So what was measured is that the SDK appends no `--bare`
and that `/goal` is registered on the CLI on `PATH`; the container's binary was not the
one probed, and nothing here says whether `/goal` is registered in it. The risk it guards
against is a *future* CLI or SDK default, and nothing here depends on the answer any more:
**no role invokes a slash command at all**. `CODE_GOAL_ENABLED` is still the switch and
still defaults on, but `cfg.goal_enabled` now selects a system prompt rather than a command
(`agent/src/coding_agent/nodes/code.py`'s `GOAL_SYSTEM_PROMPT`), and a session that made no
model call raises `RoleError` from `agent/src/coding_agent/subagents.py::run_role`
("produced no text at all, so the session did not run") instead of surfacing later as the
no-op guard in `nodes/code.py`. Held by `tests/test_agent_nodes.py`'s goal-on / goal-off
parametrisation (`test_the_coding_instructions_survive_the_goal_switch`) and
`tests/test_subagents.py::test_a_session_that_produced_nothing_is_a_failed_session`.

### 2. `/security-review` has a silent empty success, and neither pass is machine-readable

`/security-review` is a prompt template whose body opens with a shell pre-command,
``!`git log --no-decorate origin/HEAD...` ``. Everything below follows from that.

- **Where `refs/remotes/origin/HEAD` does not resolve, it aborts before any model call**
  and the CLI still reports `subtype: "success"`, `is_error: false`, `num_turns: 0`,
  `total_cost_usd: 0`, `terminal_reason: null` and **`result: ""`**, in about 100 ms, with
  the git error delivered out of band as a `<local-command-stderr>` user message. That is
  a no-op that reads as a clean pass — the exact class of confident-looking green this
  project exists to avoid. A plain local clone and a `git init` scratch repo both lack
  that ref, so `git_ops.has_origin_head` is checked before the pass runs and the reason is
  written to the artefact when it is missing.
- **Where the ref does resolve, it copes with an uncommitted-only diff.** It found a
  `shell=True` injection living only in the working tree while the `origin/HEAD...` commit
  range was empty. 3 turns, $0.21.
- **Observed output shape**, heuristic and never a contract — a `# Security Review`
  heading, then one block per finding:

  ```text
  ## Vuln 1: Command injection via unsanitised input: `app.py:42`
  * **Severity: HIGH**
  * **Category:** `command_injection`
  * **Confidence:** 9/10
  ```

  followed by Description, Exploit Scenario and Recommendation prose. It applies a
  **confidence ≥ 8** bar and states secrets on disk as a hard exclusion, so it is not a
  substitute for secret scanning.
- **`/code-review` returned `num_turns: 0` while producing 1234 characters of real
  findings and costing $0.157.** So `num_turns` is worthless as a did-it-run signal for
  these commands: the only reliable one is a **non-empty `result`**, and an empty one is
  recorded as "the pass did not run", never as "no findings". `/code-review` needs no
  `origin/HEAD` — it adapted to "no upstream, the change is uncommitted" — and it prints
  **absolute paths** (`/tmp/pb2/work/app.py`), so the clone directory is scrubbed out of
  anything that reaches a pull-request body.
- **A leading `/goal` consumed the whole coding prompt, and the session still reported
  success.** In the live walkthrough the coding role's input began with
  `/goal <condition>` on its own first line and the real coding prompt below it. The
  session came back with **0 turns, $0.00, in 2.2 seconds**, `subtype: "success"` and
  `error=False`, and wrote **no cost row at all** — the task item's `costs` array held
  only the `plan` row. The same repository and the same prompt with
  `CODE_GOAL_ENABLED=0` produced **9 turns and $0.17**. The CLI treats an input that
  begins with a slash command as a *command invocation*, so everything after it becomes
  that command's argument rather than a prompt the model answers. The fix is
  `nodes/code.py`'s `GOAL_CONDITION`, carried in the coding session's **system prompt** as
  `GOAL_SYSTEM_PROMPT` and passed `system_prompt=GOAL_SYSTEM_PROMPT if cfg.goal_enabled
  else None`, with **nothing prefixed to the prompt** — which is what makes the prompt
  survive. This is the same lesson as `/code-review`'s arriving through a different door:
  `num_turns`, `subtype` and `is_error` all reported a clean run, and the only thing that
  disagreed was the output — no cost row, because there was nothing to charge for.

The rule that follows, and it is in `nodes/review.py` as a docstring: parse both outputs
**heuristically, never as a contract**. Neither is documented as machine-readable, so a
findings count is an indication and a wording change upstream must degrade to a wrong
number rather than to an exception. Both passes are **advisory**: they never touch
`verified`, which has exactly one writer.

### 3. `TodoWrite` is not in the session's init tool list, and neither is `Agent`

Run with `--tools "Read,Write,Edit,Grep,Glob,Bash,Skill,Task,Agent,TodoWrite"`, the
`system:init` message reports exactly
`['Bash','Edit','Glob','Grep','Read','Skill','Task','Write']`. Two consequences:

- **Unknown names in `--tools` are silently tolerated** — no error — so a stale or
  misspelled name fails as an absent tool and never loudly.
- **`TodoWrite` and `Agent` are therefore not in `ALLOWED_TOOLS`.** That tuple is what the
  prompt's tool paragraph is generated from, so adding a name the session does not expose
  would make the prompt promise a tool the role does not have — the precise failure
  generating the paragraph exists to prevent. The loss is nothing: fan-out to the
  `implementer` subagent already delivers subtask breakdown through a supported primitive,
  and `TodoWrite`'s output was never going to be parsed as a contract anyway.

The `Task` → `Agent` rename in Claude Code v2.1.63 lands where it matters instead: code
that reads the message stream checks **both** names, via
`activity.SUBAGENT_TOOL_NAMES`, because current releases emit `"Agent"` in tool-use
blocks while the init tool list above still says `"Task"`.

### 4. `total_cost_usd` arrives populated on Bedrock

`0.02556625` on a one-turn call and `0.2059445` on a `/security-review`, with
`model_usage[...]["provider"] == "bedrock"`. So `graph._instrument` can tag each session's
cost with its node name and write it to the task item. Two caveats travel with the number
everywhere it appears, including beside `subagents.cost_row`:

- **It is a client-side estimate** from a price table bundled in the SDK, so it drifts from
  billing whenever pricing or the SDK version changes. Use it for operational budgeting,
  **not for invoicing**.
- **`usage` excludes subagents while `model_usage` does not.** That difference is
  invisible until fan-out is switched on, and then it is the whole difference.

`model_usage` is a plain dict keyed by model id whose `costUSD` is a **float**, and the
DynamoDB resource API refuses a float anywhere in an item, so `store._ddb_safe` over the
cost rows is mandatory rather than tidy.

### Two things that were not measured, said plainly

- **Fan-out has never been measured here.** The `implementer` subagent on the `code` role
  is a demonstration of a Claude Code capability, not a claim that it makes this agent
  produce better changes. `FANOUT_MAX_BUDGET_USD` is a runaway backstop chosen to bound a
  loop that misbehaves, not a figure observed from a run; and `FANOUT_TASK_BUDGET` rides a
  beta header this SDK cannot even name, so it may be silently inert on Bedrock and is a
  pacing hint at best.
- **`code` on Sonnet is unmeasured too.** Per-role routing puts `plan`/`replan`/`review` on
  Opus and `code` on Sonnet, and effort stays `high`: invariant 8's measurement — a coding
  role at `medium` reporting a task complete with zero edits — was made on Opus and does
  not transfer to Sonnet. What makes the experiment safe is the no-op guard in
  `nodes/code.py`: if the cheaper model under-performs to the point of changing nothing,
  the run raises instead of passing quietly.

## Only register repositories you trust

This system **executes untrusted code by design**: it clones an arbitrary repository,
points a model with unrestricted `Bash` at it, and then runs that repository's own
declared setup and test commands. No prompt engineering changes that. Everything under
this heading exists to bound the blast radius to the one repository the agent is
supposed to be working on — not to remove it.

### The GitHub token must be a fine-grained PAT, scoped to one repository

`contents: write` plus `pull requests: write`, nothing else, on exactly the repository
being registered. That is what makes a stolen token survivable: it then grants precisely
the access the agent already legitimately has, and a container that leaks its secret
still cannot reach a second repository.

`POST /configurations` refuses a classic PAT with a 400 `ClassicTokenRejected` and says
why in the message — a classic PAT cannot be scoped to one repository at all, so
accepting one would silently trade the whole control away. The check is
`api/src/handlers/configurations.py::_require_supported_token`, and
`PUT /configurations/{id}/github-token` runs it too, so a rotation cannot widen what
registration narrowed.

### Turn on branch protection on the default branch

The agent only ever pushes `agent/<taskId>` and opens a pull request, so protection is
never in its way — it is the backstop for the case where that goes wrong. With it on, the
worst a compromised container does with its own token is open a pull request nobody
merges.

## The boundary claim, and where it is qualified

**The customer's source code never leaves their AWS account, and nothing is retained
beyond what they choose to retain.** That is the reason this exists rather than a SaaS
coding agent: inference runs on Bedrock in the customer's own account, and Bedrock
retains neither prompts nor completions, does not train on them, and does not share
them with the model provider. Everything below is either what makes that claim
*enforced* rather than asserted, or a place where it is genuinely **qualified** — named
here rather than glossed, because a qualification an operator discovers later is a
broken promise.

### What makes it enforced rather than asserted

The harness is the Claude Code CLI: the single piece of software in this design capable
of talking to a non-AWS endpoint. Every AgentCore runtime therefore carries all ten of
its suppression variables, forwarded by `provisioner._fleet_environment()` as **fleet
policy** — so a runtime provisioned before the group existed converges onto it on the
next update rather than keeping whatever it was born with:

| Variable | Value | What it switches off |
|---|---|---|
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` | the umbrella: metrics, error reports, feature flags, surveys |
| `DISABLE_TELEMETRY` | `1` | usage telemetry |
| `DISABLE_ERROR_REPORTING` | `1` | crash and error reports |
| `DISABLE_AUTOUPDATER` | `1` | version checks and self-update downloads |
| `DISABLE_BUG_COMMAND` | `1` | `/bug`, which uploads a conversation |
| `DISABLE_FEEDBACK_COMMAND` | `1` | the feedback command |
| `ENABLE_CLAUDEAI_MCP_SERVERS` | `false` | vendor-hosted MCP servers (the one *enable*-shaped name, so its value is `false`) |
| `CLAUDE_CODE_DISABLE_ARTIFACT` | `1` | artifact upload |
| `CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL` | `1` | auto-installing plugins from the official marketplace |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `1` | Claude Code's own auto-memory — the **cross-tenant** one, see below. Not AgentCore Memory, which is a different mechanism |

On Bedrock the vendor telemetry and error-reporting endpoints are **never contacted
anyway**: that traffic exists only when the CLI talks to the Anthropic API directly. So
these do not close a hole that is open today — they make the posture **explicit and
auditable** in a `GetAgentRuntime` response instead of a default a reader has to know
about. Two of the ten are more than belt-and-braces:

- `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` is **cross-tenant**. Auto-memory loads
  *regardless* of `setting_sources`, and one container serves many tasks over its life,
  so without it one task's notes can surface in another's session. It is set on the
  runtime and again in the session options, which is what holds on a local run and on a
  runtime that has not converged yet.

  **This is not an argument against AgentCore Memory, which is a different mechanism —
  and the reason the distinction is worth a paragraph is that read alone the sentence
  above is a verdict on memory in general, while one shared, repository-scoped memory is
  exactly what this repository ships.** What separates them is not the sharing but who
  the sharing is keyed to, and whether anything mechanical bounds it. Auto-memory is
  keyed by **container**, arrives regardless of `setting_sources`, and carries whatever
  any session on that container happened to write — so on a fleet where one runtime
  serves many repositories' tasks in turn, its scope is an accident of scheduling.
  AgentCore Memory is keyed to **one configuration id** (D6 below), fenced by IAM to that
  one repository's own literal memory ARN by
  `api/src/handlers/provisioner.py::_execution_role_policy`, and reachable only through
  two tools with no id-shaped parameter in either schema — so a prompt-injected role has
  nowhere to name another repository's memory even if it knew the id. That is the
  controlled answer to the same want: memory that survives a task, bounded to the
  repository that earned it. See "AgentCore Memory" above for the whole design.
- **`WebFetch` is out of the toolset.** It is the one remaining vendor-bound call:
  before fetching a URL the CLI sends the **hostname** to the vendor for a safety check,
  and that check is explicitly **not** suppressed by the umbrella variable. Removing the
  tool is simpler than a settings key — `setting_sources=[]` means there is nowhere to
  put one — and it closes an exfiltration path at the same time. `WebSearch` needs no
  action because it does not exist on Bedrock, and is asserted anyway so the two fail
  the same way.

Alongside the variables: `setting_sources=[]` and `strict_mcp_config=True` together
mean **no MCP server, permission or setting arrives from the clone** — every server is
passed programmatically by `agent/src/coding_agent/subagents.py`, and a `.mcp.json` or
settings file in the repository being worked on is ignored. The image ships **no CLI
config file at all** (no `~/.claude.json`, no `/root/.claude/settings.json`), because
one would load regardless of `setting_sources` and is the one file that could widen a
session from inside the image.

None of that is self-enforcing, so two tests hold it: `tests/test_env_contract.py`
asserts every suppression name **with its expected value** and asserts it is in the
fleet group, and
`tests/test_subagents.py::test_no_role_can_reach_the_network` asserts `WebFetch` and
`WebSearch` are absent from every role's tools, from the session options and from the
generated tool paragraph, and from the fan-out `implementer` subagent. They exist
because a later "let the agent read the docs" change would undo the set in a single
line, and — since the endpoints are never contacted on Bedrock — nothing at run time
would say so.

### Qualification 1 — model invocation logging is a retention choice

Bedrock does not retain prompts or completions. **Model invocation logging**, if
enabled, writes them to the customer's own S3 bucket or CloudWatch log group. That is
still in-boundary, but it converts "not retained" into "retained by you", and the
repository's source then sits in a log with a different lifecycle and a different
access-control story from the tables and buckets this app creates.

**Nothing in this CDK app enables it.** It is an account-level setting this app neither
sets nor reads, so an operator who turns it on account-wide for audit reasons has
**silently changed this system's data posture** — no test and no green run will mention
it. If audit does require it, the conditions are: a dedicated bucket (not a shared log
bucket), SSE-KMS with a customer-managed key, a lifecycle expiry, and `BLOCK_ALL`
public access.

### Data residency

The default is a **`global.` inference profile**, and a `global.` profile may serve a
request from **any commercial AWS region**. That does not break zero retention and
shares nothing with the vendor — it is still Bedrock, still the customer's account,
still not retained — but it **does** break **data residency**, which is a different
claim and often the contractual one. Conflating the two is the dishonest move, so:
this deployment keeps zero retention everywhere and keeps residency only if you pin it.

A residency-constrained deployment must pin a **geo-scoped** profile (`us.` / `eu.` /
`apac.`) and deploy in a region where one exists for its model. The reason the default
is `global.` is concrete rather than lazy: `apac.anthropic.claude-opus-5` does not
exist, and bare `anthropic.claude-opus-5` rejects on-demand invocation, so from
`ap-southeast-1` the `global.` profile is the only thing that works for that model.

The pin goes in the `modelId` / `haikuModelId` **deploy-time context values**, which
reach the container through `provisioner._fleet_environment`. It is **not** an edit in
two deployment units: `config.MODEL_RE` validates a *pattern* — the `opus`/`sonnet`/
`haiku` aliases, any `us.`/`eu.`/`apac.`/`global.` profile id, or a full
`arn:aws:bedrock:...` ARN — instead of hard-coding three ids, so the container accepts
whatever the deployment pins without a code change.

A geo-scoped profile is also **tighter IAM**. A `global.` profile forces the execution
role's foundation-model resource to be `arn:aws:bedrock:*::foundation-model/anthropic.*`
(invariant 21); a geo-scoped one lets that ARN be region-scoped. Residency and least
privilege point the same way here, which is unusual and worth taking.

### Qualification 3 — the egress that genuinely leaves AWS

Three destinations, and they are the honest answer to "never leaves your account":

- **`github.com`** — clone, push and pull request. This is the one that carries source
  code out, and it carries it to a place the customer already keeps it.
- **PyPI and npm** — the `setupCommand` installs the repository's own dependencies
  before verification, so a package registry is reached on every run.
- **`docs.aws.amazon.com`** — the read-only AWS documentation MCP server. An AWS-owned
  domain that receives a **search query** and never repository content, and which has no
  path to the container's AWS credentials.

All three are unavoidable given what the agent is for. The VPC endpoints for Bedrock,
DynamoDB, Secrets Manager, ECR, S3 and CloudWatch Logs are in
`infra/stacks/agent_stack.py`, so those calls do not traverse the internet at all. And the
vendor destinations this design says it never contacts are **refused** there rather than
merely unused: the same file carries a Route 53 Resolver **DNS Firewall** deny list —
`BLOCKED_VENDOR_DOMAINS`, seven enumerated names (`api.anthropic.com`,
`downloads.claude.ai`, `mcp-proxy.anthropic.com`, `*.claudeusercontent.com`,
`*.datadoghq.com`, `storage.googleapis.com`, `code.claude.com`) — associated with the agent
VPC, with `BLOCK` rather than `ALERT` and `NXDOMAIN` as the block response, so the
container sees "no such host" rather than an empty answer. Resolver **query logging**
writes to `/aws/route53/rca-agent-dns`, and a metric filter on
`firewall_rule_action = "BLOCK"` drives an alarm that fires on a **single hit**. The alarm
is the point of the construct: it is what tells you a CLI upgrade introduced a new call,
which is otherwise a silent line in someone else's release notes.

What that does *not* do is worth stating plainly. The deny list stops **named**
destinations. It does not stop an arbitrary one: 443 to a host nobody enumerated is still
reachable through the NAT gateway, and the suppression set and the toolset are what argue
the container does not try. A TLS-inspecting egress allowlist is what would make the claim
hold against a host that was never named, and it is **not being built** — so this is where
the claim stops, permanently.

### Qualification 4 — the Codex audit does not transfer

The harness sits behind one module — `agent/src/coding_agent/subagents.py` — so swapping
the CLI is a single-module change rather than a rewrite. But **every finding above is
specific to *this* CLI's variables and endpoints**: the ten names, the `WebFetch`
hostname preflight, `setting_sources`, `strict_mcp_config`, auto-memory. A Codex-based
container needs its **own** egress audit before the same claim can be made for it.
Nothing here should be read as covering it.

## What remains exposed

Stated rather than implied, because a security section that claims completeness is worse
than one that does not. Four things are still true after everything above.

- **A container's execution-role credentials are reachable from inside the container.**
  `commands._base_env()` builds the repository command's environment from nothing — no
  `AWS_*`, no GitHub token — and
  `tests/test_commands.py::test_no_aws_variable_of_any_name_reaches_the_command` pins that
  for both runners, the verify command and the setup command. It defeats the
  environment-variable credential path. It does **not** defeat an attacker who hardcodes
  the container credentials endpoint. Within the permissions boundary, stolen credentials
  reach exactly four things: this one repository's secret, this one configuration's task
  items, Bedrock, and a read-only pull of the agent image from one ECR repository — the
  runtime cannot be created at all without that last one, which is why it is granted. The
  `dynamodb:LeadingKeys` fences on both table statements are what shrink that list to *this*
  configuration, and the VPC with its 443-only security group is what stops the credentials
  leaving. Four is the count as shipped; it becomes **five** on a deploy that passes
  `-c otelMetricsEnabled=true`, which adds `cloudwatch:PutMetricData` on `*` to the execution
  role and to the boundary. That fifth one is write-only and grants no read of any metric, and
  `*` is not laxity -- `PutMetricData` supports no resource-level permission, so no narrower
  grant exists. The exposure it buys an attacker is junk metrics and a small CloudWatch bill.
  The flag defaults to false, so an unmodified deploy of this tree grants four.
- **Prompt injection.** The controls are the `.agent/` quarantine — the clone's own
  `.agent/` is moved aside so repository content cannot pose as the roles' workspace —
  `setting_sources=[]` and `strict_mcp_config=True`, the `PreToolUse` hook that denies
  `git commit` on `Bash` for every role and every subagent, and `submit` being the sole
  publisher. None of them stop a malicious `npm test` or a `pip install` hook: that code
  runs because running it is the point, which is why the account is dedicated and the token
  is scoped to one repository.
- **A resume unpickles a file the container could have written.**
  `agent/src/coding_agent/checkpoint.py` calls `pickle.load` on the path
  `checkpoint.checkpoint_path` returns, `<workDir>/checkpoint/graph.pkl`, and unpickling is
  arbitrary code execution. The writers are this task's own roles and this task's own
  `setupCommand` and `verifyCommand`: `commands._base_env()` restricts what those two
  commands can *see*, not what they can write, and they run as the same user on the same
  mount. It is documented rather than fixed, because running that repository's commands is
  already arbitrary code execution by design — it is what the product does — and it happens
  before any resume reads the file, so a tampered checkpoint grants an attacker nothing they
  do not already have. The path is per task (`work_root / task_id`), so it reaches no other
  task. JSON is not the fix: the payload is LangGraph's own nested `defaultdict` structures
  and would not round-trip, which is why `checkpoint._load` rebuilds key by key instead of
  assigning.
- **There is no roadmap.** This list is the finished posture, not a staging post. A GitHub App
  issuing one-hour installation tokens, phase-separated execution roles, an SCP backstop on
  `iam:CreateRole`, Bedrock Guardrails and a TLS-inspecting egress allowlist were all
  considered and are **not being built** — so the exposures above are permanent for this
  system rather than pending. Read them as the terms of use: a dedicated account, a
  repository-scoped token, and branch protection are what bound them, and nothing further is
  coming to help.

### Static scanners: 29 findings, 13 retired and 16 suppressed

An artifact scan of this tree — Bandit plus Semgrep OSS, August 2026 — returned **29 HIGH
findings and not one real vulnerability**. This is where that triage lives, because the
alternative is deriving it again from the next report. Neither scanner is run by `make lint`,
which is ruff, oxlint and mypy; they belong to the artifact scan, so nothing local goes red when
a suppression is added and only a reader holding a report ever sees one. `ruff.toml` carries the
sibling decision for the same reason — `S105`/`S106` are deliberately not selected because every
one of them is a test-fixture token.

**13 of the 29 were retired rather than suppressed, and the mechanism is worth knowing** because
it is the cheapest fix available for this whole class. `B105`/`B106` fire on a *literal* — one
assigned to a name containing `token`, `secret`, `pass` or `pwd`, passed as such a keyword
argument, or filed under such a dict key. Behind a name they do not fire at all. So the fake PATs
live in `tests/conftest.py` as `FAKE_PAT`, `FAKE_FINE_GRAINED_PAT`,
`FAKE_FINE_GRAINED_REPLACEMENT_PAT` and `FAKE_UNLOGGED_PAT`, which is where `CLAUDE.md` already
said fake credentials belong, and the nine test modules that had their own copies now import
them. Two of the three moto clients passed `aws_secret_access_key="testing"` that the autouse
`conftest._aws_env` had already put in the environment, so the argument was deleted outright;
the third is built at *module import*, before any fixture runs, and splats
`conftest.FAKE_AWS_CREDENTIALS`. `tests/test_api_provisioner.py`'s secret ARN is an f-string
because a `JoinedStr` is not a literal, and `tests/test_plugin_manifest.py`'s loop variable is
`word` rather than `token` — it iterates the words of a `git push` line, and the name was the
only reason the scanner looked. **Nothing was deleted and no test lost coverage**: reaching for
deletion here would have cost 126 tests, because every one of those literals was a module-level
constant, and `tests/test_api_fakes.py` is imported by 13 other test modules.

The remaining **16 carry a suppression**, in five shapes, and the count is the whole argument:

| Finding | Count | Why no fix exists |
|---|---|---|
| `B301`/`B403` on `pickle` | 6 | `agent/src/coding_agent/checkpoint.py`'s accepted exposure, argued in the bullet above and again at `checkpoint._load`; in `tests/test_checkpoint.py` it reads back a file the test just wrote |
| `B602` and two Semgrep subprocess rules on `agent/src/coding_agent/commands.py` | 3 | `shell=True` in `commands._run` **is** the product: an untrusted repository's own declared `make test`, bounded by the dedicated account, the repository-scoped PAT and `_base_env` |
| Semgrep `dangerous-subprocess-use-audit` on `agent/src/coding_agent/telemetry.py` and two test files | 4 | list argv, no shell, every element a module constant or a path the test wrote |
| `B310` on the two `urlopen` calls | 2 | fixed in code — see below — but bandit flags the call site either way |
| `B104` on `tests/test_agent_telemetry.py` | 1 | the flagged line asserts `"0.0.0.0" not in settings` — it is the check, not a binding, and a constant would only move the literal |

The `B310` pair is the one that changed behaviour rather than gaining a comment.
`agent/src/coding_agent/github.py::_request` and `api/src/handlers/github_probe.py::_request`
now refuse a URL that is not under `_API_ROOT`, raising `GitHubError` and `ProbeUnavailable`
respectively. It was already true that no other scheme or host was reachable, but only because
every caller happened to build on the constant — and both functions set an `Authorization`
header carrying the PAT, so a URL naming another host hands the token to it, and `urlopen`
honours `file:`. The guard makes the suppression's reason structural instead of conventional.

**Every suppression names its rule and carries a reason**, beside the marker or in the comment
line directly above it. A bare `# nosec` blankets its whole line, including the real finding that
lands there next — which is the one way this triage could turn into the thing it was written to
prevent.
