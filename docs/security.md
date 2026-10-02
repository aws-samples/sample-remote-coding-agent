# Security and data boundary

**Deploy this into a dedicated AWS account.** The agent runs code it did not write: a
repository's own `setupCommand` and test command. The account boundary is the outermost fence,
and the only one no bug inside this repository can widen.

## Only register repositories you trust

This system clones a repository, points a model with unrestricted `Bash` at it, and runs the
repository's own setup and test commands. Everything below bounds the blast radius to the
repository the agent is working on; nothing removes it.

## GitHub access: a GitHub App held by AgentCore Identity

No GitHub token is stored by this system. One GitHub App is created per deployment. AgentCore
Identity's OAuth2 credential provider holds the app's client secret in AgentCore's token
vault, and each repository's container fetches a short-lived user-to-server token from the
vault when it needs one. There is no credential field on any route and nothing to rotate.

The operator takes three actions, each one click:

1. **Connect.** `POST /github-app` creates the credential provider and returns a GitHub App
   manifest carrying its callback URL. The console posts it to GitHub, GitHub creates the
   app, and `POST /github-app/conversions` stores the app's credentials in the provider.
2. **Install** the app on the repositories to be registered, and only those.
3. **Authorise** each repository. Opening the repository's page in the console starts this;
   GitHub returns straight away once the account has approved the app.

Design points:

- The provider's callback URL is assigned when it is created and preserved by updates, which
  is what makes the one-click manifest possible.
- The manifest sets `request_oauth_on_install` to `false`, so installing does not start an
  authorisation before the provider holds credentials.
- The authorisation return URL must be registered on the workload identity, so a caller
  cannot redirect an authorisation elsewhere; the console sends its own origin and
  `github_app.require_https` validates it.
- GitHub has no client-credentials grant, so `USER_FEDERATION` is the only flow and a person
  authorises once per repository. The vault's `userId` is the configuration id, so
  deregistering a repository makes its authorisation unreachable.
- A token can write only to repositories the app is installed on. `permissions.push` on
  `GET /repos/{owner}/{repo}` describes the user, not the token, so `_push_verdict` checks
  push access by creating a throwaway ref.

### The blast radius is the app's installation list

The container's two credential actions accept no narrower resource than
`workload-identity-directory/*` (`GetWorkloadAccessTokenForUserId`) and `*`
(`GetResourceOauth2Token`), and offer no condition key for the user id. So:

- A compromised container could request any token any operator has authorised in the
  account. In practice it knows one configuration id, its own (invariant 31), and the
  dedicated account limits "any" to this fleet.
- A token can write to every repository the app is installed on. **Install the app only on
  the repositories you register:** choose "Only select repositories" on GitHub's install page.

In exchange, nothing here stores a credential, tokens last eight hours and are refreshed by
the vault, and each repository needs one operator action.

AgentCore Identity performs its Secrets Manager work with the caller's permissions. The grants
are on AgentCore's own `secret:bedrock-agentcore-identity!*` prefix, never on a secret this
repository creates:

- `github:provider-admin` creates, updates and deletes the provider's client secret.
- `github:credential` reads it for the code exchange that completes an authorisation.
- Each execution role's `RefreshGitHubCredential` reads it to refresh an expired token, on
  the **one literal ARN** `github_app.provider_secret_arn()` returns, never the prefix.

`MayNotEscalate` in `infra/stacks/agent_stack.py` denies a container every Identity
control-plane action and every Secrets Manager write, rotate, delete and enumerate action, by
literal name.

### Turn on branch protection

The agent only pushes `agent/<taskId>` and opens a pull request, so branch protection on the
default branch never gets in its way, and it limits what a compromised container can do to
opening a pull request nobody merges.

## The data boundary

**Source code stays in the customer's AWS account, and nothing is retained beyond what they
choose to retain.** Inference runs on Bedrock in the customer's account; Bedrock does not
retain prompts or completions, train on them, or share them with the model provider.

### How it is enforced

The Claude Code CLI is the one component that could talk to a non-AWS endpoint. Every runtime
carries its suppression variables as fleet policy (`provisioner._fleet_environment`):

| Variable | Value | What it switches off |
|---|---|---|
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` | metrics, error reports, feature flags, surveys |
| `DISABLE_TELEMETRY` | `1` | usage telemetry |
| `DISABLE_ERROR_REPORTING` | `1` | crash and error reports |
| `DISABLE_AUTOUPDATER` | `1` | version checks and self-update downloads |
| `DISABLE_BUG_COMMAND` | `1` | `/bug`, which uploads a conversation |
| `DISABLE_FEEDBACK_COMMAND` | `1` | the feedback command |
| `ENABLE_CLAUDEAI_MCP_SERVERS` | `false` | vendor-hosted MCP servers |
| `CLAUDE_CODE_DISABLE_ARTIFACT` | `1` | artifact upload |
| `CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL` | `1` | plugin auto-install from the official marketplace |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `1` | Claude Code's own auto-memory, which is keyed to the container rather than the repository |

On Bedrock the vendor telemetry endpoints are not contacted in any case; the variables make
the posture explicit and auditable in a `GetAgentRuntime` response.

- **`WebFetch` and `WebSearch` are not in the toolset.** `WebFetch` sends the hostname of a URL
  to the vendor for a safety check, which no variable suppresses.
- **Nothing in the clone configures a session.** `setting_sources=[]` and
  `strict_mcp_config=True` mean every MCP server is passed by
  `agent/src/coding_agent/subagents.py`; the image ships no CLI config file.
- **AgentCore Memory is a different mechanism from auto-memory:** it is keyed to one
  configuration id rather than to a container, granted on that memory's literal ARN, and
  reached through two tools with no id-shaped parameter.

`tests/test_env_contract.py` and `tests/test_subagents.py::test_no_role_can_reach_the_network`
hold these.

### Model invocation logging is a retention choice

Bedrock model invocation logging, if enabled in the account, writes prompts and completions to
the customer's own S3 bucket or log group. That stays in the account but changes "not
retained" to "retained by you". This app neither enables nor reads that setting. If audit
requires it, use a dedicated bucket, SSE-KMS with a customer-managed key, a lifecycle expiry
and `BLOCK_ALL` public access.

### Data residency

The default inference profiles are `global.`, which may serve a request from any commercial
AWS region: still Bedrock, still the customer's account, still not retained, but not
region-resident. For residency, pin a geo-scoped profile (`us.`, `eu.`, `apac.`) with the
`-c planModelId=`, `-c codeModelId=` and `-c haikuModelId=` deploy-time values, in a region
where one exists for the model. `config.MODEL_RE` accepts any profile id, so no code change is
needed. A geo-scoped profile also lets the execution role's model ARN be region-scoped.

### Egress that leaves AWS

- **`github.com`:** clone, push and pull request.
- **PyPI and npm:** the repository's own `setupCommand` installs its dependencies.
- **`docs.aws.amazon.com`:** the read-only AWS documentation MCP server, which receives search
  queries only.

Bedrock, AgentCore, DynamoDB, ECR (API and Docker), S3 and CloudWatch Logs are reached through
VPC endpoints (`infra/stacks/agent_stack.py`). A Route 53 Resolver DNS Firewall associated with
the agent VPC blocks `BLOCKED_VENDOR_DOMAINS` with `NXDOMAIN`; query logging writes to
`/aws/route53/rca-agent-dns`, and an alarm fires on a single blocked query. The deny list
covers named destinations; other HTTPS hosts remain reachable through the NAT gateway.

The audit above is specific to the Claude Code CLI. A different harness behind
`agent/src/coding_agent/subagents.py` needs its own egress review.

## Prompt guardrail

A Bedrock guardrail screens every task prompt before a run is queued and every card's text
before it is stored (invariant 43): input-only `PROMPT_ATTACK` and `MISCONDUCT` at `LOW`, the
AWS access-key and secret-key PII types, and secret-shaped regexes. A refusal names the policy
types, never the matched text. It screens the operator's prompt only, not repository content
read during a run, model output or the diff. `-c guardrailEnabled=false` turns it off.

## What remains exposed

- **A container's execution-role credentials are reachable from inside it.** The repository's
  commands get an environment with no `AWS_*` variables (`commands._base_env`), but a command
  can reach the credentials endpoint directly. Within the permissions boundary those
  credentials reach this configuration's task items, this repository's memory, Bedrock, a
  read-only pull of the agent image, and the GitHub credential below. The VPC's 443-only
  security group keeps them from leaving except through allowed egress.
- **The GitHub credential is bounded by the app's installation list,** not by repository; see
  above.
- **Prompt injection through repository content.** The controls are the `.agent/` quarantine,
  `setting_sources=[]` and `strict_mcp_config=True`, the `PreToolUse` hook denying
  `git commit`, and `submit` as the only publisher. A malicious `npm test` or install hook
  still runs, because running the repository's commands is the product; the dedicated account,
  the installation list and branch protection bound it.
- **A resume unpickles a file the task's own commands could write**
  (`<workDir>/checkpoint/graph.pkl`). Those commands already execute arbitrary code by design,
  and the path is per task.

## Static analysis suppressions

`make lint` runs ruff, oxlint and mypy. Bandit and Semgrep findings are suppressed only where no
fix exists, each with its rule and reason beside the marker:

| Finding | Why |
|---|---|
| `B301`/`B403` on `pickle` | the checkpoint exposure above |
| `B602` and two Semgrep subprocess rules in `agent/src/coding_agent/commands.py` | `shell=True` runs the repository's own declared command, which is the product |
| Semgrep `dangerous-subprocess-use-audit` in two test files | list argv, no shell, constant or test-written arguments |
| `B310` on two `urlopen` calls | both refuse any URL outside `_API_ROOT` |

Fake credentials live behind named constants in `tests/conftest.py`, so `B105`/`B106` need no
suppression.
