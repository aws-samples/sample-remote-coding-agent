# CLAUDE.md

Rules for changing this repository. Operator-facing reference lives in `README.md`,
`docs/operations.md` and `docs/security.md`.

## Verification

```bash
make setup     # idempotent; run it first in a fresh container
make test
make lint
```

`make test` is the whole gate, and it is fast: run it after each change.

- `make setup` comes before `make test`. A fresh container's `.venv` is an empty symlink, so
  without it `make test` fails with `No module named pytest`, which is a missing environment
  rather than a failing suite.
- To run one file serially use `-n0` (`python -m pytest -q -n0 tests/test_graph.py`), never
  `-p no:xdist`: `addopts` passes `-n`/`--dist`, and unregistering the plugin aborts before
  collection. `--dist loadfile` is load-bearing, because several files `importlib.reload`
  handler modules and the reload order is a within-file contract.
- There is no `mcp/__init__.py` and no `infra/stacks/__init__.py`. Both directories are
  namespace packages on purpose: a regular `mcp` package at the root would shadow the MCP SDK,
  and `stacks` already resolves through `pythonpath`.
- Every dependency declaration carries an upper bound at the next major version (invariant 29).

## Conventions

`from __future__ import annotations` at the top of every module. Modern typing
(`str | None`, `list[tuple[str, int]]`). Comments explain *why*, not what, and are updated with
the behaviour they describe. British spelling.

## Layout

| Path | What it is |
|---|---|
| `Makefile` | `setup`, `test`, `lint`, `preflight`, `deploy`: the single owner of every operator command |
| `pytest.ini` | `pythonpath` across all four packages, `asyncio_mode = auto`, `-n auto --dist loadfile` |
| `ruff.toml`, `mypy.ini` | lint and type-check configuration |
| `requirements-dev.txt` | the test toolchain shared by all four packages |
| `agent/` | the AgentCore Runtime container: `Dockerfile`, `pyproject.toml`, `src/coding_agent/`, `skills/` |
| `api/` | the control-plane Lambda bundle: `requirements.txt`, `src/handlers/` |
| `infra/` | the CDK app: `app.py`, `cdk.json`, `requirements.txt`, `stacks/` |
| `mcp/` | the stdio MCP server Claude Code loads: `pyproject.toml`, `src/rca_mcp/` |
| `plugins/sample-rca/` | the Claude Code plugin: MCP server pin, `remote` and `scaffold` skills |
| `web/` | the console |
| `scripts/scaffold.sh` | turns an empty repository into one the agent can run tasks against |
| `tests/` | `conftest.py` (moto fixtures, fake credentials) plus one file per area |

AgentCore Identity, AgentCore runtimes and memory, and `ApplyGuardrail` are not in moto:
`tests/test_api_fakes.py` fakes them, validating every call against the real botocore service
model.

### Single owners

Each of these has exactly one owner and a test behind it.

- `runs.queue_run` is the only definition of "queue a run"; `tasks.create` and `cards.move`
  both go through it. `cards.create_claimed` is the one way `POST /tasks` creates a card.
- `nodes/submit.py` is the only publisher: nothing else writes to a git remote or the GitHub API.
- `nodes/review.py` is the only place `verifyResult` is composed.
- `observability.session_id` owns session ids.
- `_common` is the only place a boto3 client is built.
- `github_app` owns the GitHub credential: the credential provider, the workload identity,
  the keying rule (`userId` is the `configId`) and every control-plane Identity call.
  `store.github_token` is the container's half.
- `git_ops` is every git invocation in the container.
- `agentcore_memory` owns the memory keying rule and every memory data-plane call in the
  container (not to be confused with `memory`, the filesystem `.agent/memory/` digest).
- `subagents.build_options` is the one option surface for all four roles.
- `cards.STORED_COLUMNS`/`BOARD_COLUMNS` define the board, mirrored in `web/js/transitions.js`
  and `web/js/status.js`.
- `tests/invoke_payload.py` defines the AgentCore invocation payload (invariant 13b).
- `runs.screen_prompt` is the only caller of `ApplyGuardrail` (invariant 43).

## Changing `mcp/`

The plugin installs the MCP server from a pinned sha (invariant 34), and
`tests/test_plugin_manifest.py` requires that pin to be at or after every commit that touched
`mcp/`. So an `mcp/` change lands as **two commits pushed together**: the change, then a commit
moving the pin in `plugins/sample-rca/.mcp.json` to it. Merge with a fast-forward or a merge
commit, never squash or rebase, so the pinned sha stays reachable.

## Invariants

Each rule carries a comment at the code it governs and a regression test.

1. **Never run the graph on the invocation thread.** Use `add_async_task` plus a thread and
   return in milliseconds; a blocked ping thread is reaped at 15 minutes.
2. **ARM64 everywhere:** the Dockerfile, `Platform.LINUX_ARM64`, `Architecture.ARM_64`.
3. **The Claude Code CLI must be on `PATH`;** keep the build-time `claude --version`.
4. **The GitHub token never enters the container environment, the `origin` URL or a command
   line.** All network git goes through `_run_authenticated`/`_askpass`. The token is fetched
   from the token vault when needed (`store.github_token`) and never cached.
4b. **The commit identity is configurable.** `provisioner._git_identity` prefers
   `-c gitAuthorName=`/`-c gitAuthorEmail=` and otherwise uses the GitHub App's bot account,
   `<botUserId>+<slug>[bot]@users.noreply.github.com` (the bot *user* id, not the app id).
   Both keys are always sent, `""` included, because they are fleet policy (invariant 24).
5. **Nothing under `.agent/` counts as work.** Keep the two-way split in `code` and `submit`'s
   independent guard.
6. **The veto is one-way, and `passed` is not `verified`.** `verify` holds the exit code,
   `review` holds the verdict, and only `review` composes `verifyResult`.
6b. **Nothing advisory may touch `verified`.** A `/goal` condition and `/security-review` are
   model judgements; `verified` has exactly one writer.
6c. **A review that edits the tree costs exactly one command re-run,** detected by
   `source_diff_digest` either side of the review session.
7. **Read `final_text`, not `text`,** which is the whole transcript.
8. **The `code` role's effort stays `high`.** The no-op guard in `nodes/code.py` raises when a
   role changes nothing.
9. **`permission_mode="acceptEdits"`, never `"plan"`.**
10. **`setting_sources=[]` and `strict_mcp_config=True`,** with
    `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`: nothing in the clone or the user's settings may widen
    a session.
10b. **No MCP tool may call an AWS API,** except the in-process `memory` server's two fixed
    tools, whose schemas have no parameter that can name a memory, actor, session or namespace.
10c. **A role-side `git commit` is denied by a `PreToolUse` hook,** in subagents too.
11. **Never hand a subagent a path outside its `cwd`:** pass `owner/repo`, not `repositoryUrl`.
12. **`RECURSION_LIMIT` stays above the worst-case node count,** derived from `MAX_ITERATIONS`
    at six node visits per iteration.
13. **Every observable transition is written to DynamoDB.** A new node needs a `_NODE_STATUS`
    entry, a `TaskStatus` literal, a `watchdog.RESUMABLE_STATUSES` line and a
    `web/js/status.js` line. Activity writes skip `updatedAt`, the watchdog's liveness clock.
13b. **The invocation payload is a contract across two deployment units,** defined once in
    `tests/invoke_payload.py`. The wire name is `prompt`; `graph._initial_state` maps it to
    `description`.
14. **No custom AgentCore endpoint:** `ENDPOINT_NAME = "DEFAULT"`.
15. **Provisioning retries are an allowlist** (`provisioner._TRANSIENT_CREATE_FAILURES`):
    error code, operation and, for IAM propagation, pinned message shapes, within a 90-second
    wall-clock budget. An unrecognised `CreateAgentRuntime` failure is retried exactly once,
    then reported in the service's own words.
16. **`iam:TagRole` must not carry the `iam:PermissionsBoundary` condition.**
17. **`iam:CreateRole` is fenced three ways:** path, boundary, and `PassRole` limited to
    `bedrock-agentcore.amazonaws.com`.
18. **Session ids are deterministic and at least 33 characters,** owned by
    `observability.session_id`.
19. **Dispatch and resume are claim-based** conditional updates; resume also pins
    `resumeCount` and carries `attribute_exists(taskId)`.
20. **`api/requirements.txt` bundles `botocore>=1.43.0`** for `bedrock-agentcore`.
21. **`global.` inference profiles need a non-region-scoped model ARN.**
22. **Tables are `RETAIN`;** `DELETE /configurations/{id}` is the teardown path. A `FAILED`
    configuration is retired and replaced by `POST /configurations`; a `DELETING` one is not.
22b. **A teardown checks the runtime exists before deleting it** (`_runtime_exists`), because
    `DeleteAgentRuntime` answers `AccessDeniedException` for a runtime that is already gone.
23. **`_set_config` is conditional on `attribute_exists`, and `_create`/`_check` return early
    for `DELETING`,** so a late `CHECK` cannot resurrect a torn-down configuration.
24. **`UpdateAgentRuntime` is a full replace.** Read the live runtime back, carry forward
    per-configuration values, converge fleet policy, and compare per key.
25. **The work mount is writable but not owned by the container user.** Exempt the exact path
    in `safe.directory` through the `GIT_CONFIG_*` group in `os.environ`; never use a glob.
    Probe the root with a real `git init`.
26. **A Dockerfile `ENV` cannot be unset by a runtime's `environmentVariables`,** so
    `WORK_ROOT` is forwarded by the provisioner, not baked in.
27. **The watchdog's quiet threshold clears `VERIFY_TIMEOUT`** (`SETUP_TIMEOUT` 900 ≤
    `VERIFY_TIMEOUT` 1800).
28. **A cancelled task carries `cancelledAt`,** written before `StopRuntimeSession`, and the
    watchdog resolves it; the attribute stays in `_SCAN_PROJECTION`.
29. **Every dependency declaration has an upper bound at the next major.**
30. **`network_mode` is VPC and is converged as fleet policy** on every update.
30b. **The agent VPC's AZs are chosen by AZ ID** (`agent_stack.AGENTCORE_VPC_ZONE_IDS`),
    resolved at synth by `infra/app.py::_agent_zones`; `-c agentAvailabilityZones=a,b`
    overrides it.
31. **The container writes no configuration item and reads only its own:** `dynamodb:LeadingKeys`
    on both table statements, `GetItem` only on Configurations.
32. **A repository's own commands run with an environment built from nothing:** `PATH`,
    `HOME`, `LANG`, `CI` and `git_config_env()` only.
32b. **The container exports no metrics,** `CLAUDE_CODE_ENABLE_TELEMETRY` is pinned `"0"`, and
    no handler names a CloudWatch action. A fleet variable is removed through
    `provisioner.RETIRED_FLEET_KEYS`, not by deleting it.
33. **No role may reach the network and no vendor endpoint may be reached:** `WebFetch` and
    `WebSearch` stay out of `ALLOWED_TOOLS`, the CLI suppression variables stay in the fleet
    environment, and the DNS Firewall deny list stays in the VPC.
34. **Nothing is installed from a moving reference:** the plugin pins a sha, the Dockerfile
    pins the CLI version, and the documentation MCP server is installed at build time at a
    pinned version. See "Changing `mcp/`".
35. **This repository's `verifyCommand` is `make test` and `setupCommand` is `make setup`.**
    Setup detection accepts only a declared entry point (`Makefile` `setup:`/`bootstrap:`,
    `package.json` `scripts.setup`).
36. **A replacement registration retires the `FAILED` item first and inherits no
    authorisation;** its new `configId` needs authorising again.
37. **A VPC-mode runtime needs `iam:CreateServiceLinkedRole`,** fenced by `StringLike` on
    `iam:AWSServiceName` to `bedrock-agentcore.amazonaws.com` and its subdomains, in its own
    statement. A permanent authorisation failure is terminal and reported as itself.
38. **Every run has a card.** `tasks.create` creates it through `cards.create_claimed`; there
    is no opt-out. A failed queue releases the card to `BACKLOG`. The column is derived from
    the run on every read (`cards.derived_view`).
39. **A stalled provisioning chain is re-driven only with messages safe to send twice:**
    `watchdog.CONFIG_DRIVERS` sends `CHECK` or `DELETE`, never `CREATE`; a stalled `PENDING`
    is resolved to `FAILED`. The threshold is derived from `MAX_CHECK_ATTEMPTS ×
    CHECK_DELAY_SECONDS`.
39b. **Only the provisioner opts out of Lambda's recursive loop detection**
    (`api_stack.SELF_CHAINING_ENTRY_POINTS`), because its `CHECK` chain re-triggers it through
    its own queue; `MAX_CHECK_ATTEMPTS` bounds the chain instead.
40. **No route takes a GitHub credential.** A body carrying one is refused with
    `400 TokenNotAccepted`, and no MCP tool schema has a credential-shaped property.
41. **Authorisation is checked before a run is queued** (`runs.queue_run`), so an unauthorised
    repository is refused with `409 GitHubNotAuthorized` before a run spends anything.
42. **Nothing is stored about whether a repository is authorised;** `github_status` asks the
    vault every time.
43. **Every prompt is screened by the guardrail before a run is queued, and every card's text
    before it is stored.** A refusal is `422 PromptBlockedByGuardrail` naming the policy types,
    never the matched text; an outage is `503 GuardrailUnavailable`, never a pass. The
    guardrail lives in `ApiStack` (`prompt_guardrail.add_prompt_guardrail`), and its
    `ApplyGuardrail` grant covers the guardrail profile in every region.

## AgentCore Memory

One memory per registered repository, created by `provisioner._ensure_memory` before the
execution role and deleted with the registration.

- **D6, the keying rule:**
  `actorId` is the **configuration id**;
  `sessionId` is the **task id**.
  The `semanticMemoryStrategy` `repository_episodic` writes to
  `/rca/{actorId}/episodic`, shared by all of a repository's tasks; the
  `summaryMemoryStrategy` `session_summary` writes to `/rca/{actorId}/session/{sessionId}`.
  Both templates are spelled in `provisioner.py` and `agentcore_memory.py`, and a test keeps
  them identical.
- **D11, degradation:** every memory call in the container catches `ClientError` and
  `BotoCoreError`, logs a warning and returns an empty result. Memory never fails a run. The
  read-back route `GET /configurations/{configId}/memory` does not swallow errors.
- The memory id reaches the container through the configuration item, resolved once in
  `graph.run_task`, never through the environment.
- `memory_search` and `memory_record` (`MEMORY_SERVER_NAME` = `memory`, allowlisted as
  `mcp__memory__*`) are the only tools;
  no parameter may name a memory. `graph._record_memory` writes one event per node visit,
  capped at **500** characters, and events expire after **90** days.
- IAM: the execution role's `UseOwnMemory` grant allows `CreateEvent`, `ListEvents`,
  `GetEvent`, `RetrieveMemoryRecords`, `ListMemoryRecords` and `GetMemoryRecord` on the
  memory's literal ARN; the permissions boundary allows the same six on `memory/*`;
  `MayNotEscalate` denies `CreateMemory`, `UpdateMemory`, `DeleteMemory`, `ListMemories`,
  `DeleteEvent`, `DeleteMemoryRecord` and `BatchDeleteMemoryRecords` by literal name. No
  wildcard may enter the deny group, because an explicit Deny beats every Allow.
- The `BEDROCK_AGENTCORE` VPC endpoint serves the data plane only, keeping memory and Identity
  calls in the VPC. The control plane, `bedrock-agentcore-control`, is called by Lambdas outside
  the VPC and has no endpoint.
