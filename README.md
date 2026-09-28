# Remote coding agent on Amazon Bedrock AgentCore

Submit a coding task from Claude Code or the web console, follow its progress,
and get a GitHub pull request with verification evidence. The agent runs remotely
on Amazon Bedrock AgentCore Runtime, so your terminal does not need to stay open.

![Real console with synthetic fixture data: a task progresses from Backlog through Planning, Coding and Verifying to Done, then shows verification details.](assets/console-preview.gif)

**UI preview, not a live AWS run.** This recording uses the actual console with synthetic
API responses and shortened stages. It demonstrates the interface, not agent output or
execution speed. [Static preview](assets/console-preview.png) ·
[Capture details and live-demo checklist](assets/README.md#console-preview)

[Quick start](#quick-start) · [Architecture](#architecture) ·
[Costs and cleanup](#costs-and-cleanup) · [Contributing](#contributing)

## Why try it?

- Delegate a bounded bug fix, test addition, or small feature while you keep working.
- Follow the same task in Claude Code or a live-polled board with run activity and test evidence.
- Run Claude Code with Bedrock model access and a separate Runtime and AgentCore Memory per repository.

**A run that did not verify green opens a draft pull request and says so.**
`verified` means a real verification command ran and a review judged it green.

**This is a sample for trusted repositories in a dedicated AWS account.** It executes
repository setup and test commands. It does not make untrusted code safe or replace
human review. A runtime or publishing failure may produce no PR.

## Quick start

### Before you start

| You need | Why |
|---|---|
| Docker running | the ~1.3 GB ARM64 agent image is built on *this* machine. No build service stands in. |
| Python ≥ 3.12, `uv` on `PATH`, Node ≥ 20, AWS CLI and credentials | Node is for the pinned CDK CLI; select your intended profile and region |
| AgentCore availability and active Bedrock inference profiles in your region | Preflight checks the configured services and models |
| Claude Code with plugin support (optional) | Required for terminal submission; the console can submit tasks without it |
| `cdk bootstrap`, once per account and region | `make preflight` prints the exact command and never runs it for you |
| A **fine-grained** GitHub PAT for one repository: `contents: write`, `pull requests: write` | it bounds what a compromise reaches. A classic PAT is refused with `400 ClassicTokenRejected`. |
| An AWS account with nothing else in it | this runs an arbitrary repository's own setup and test commands |

### Deploy it

1. **Deploy the fleet.**

   ```bash
   git clone https://github.com/aws-samples/sample-remote-coding-agent
   cd sample-remote-coding-agent
   make setup
   make preflight
   make deploy EMAIL=you@example.com
   ```

   **Read the account and region from `make preflight` before deploying.** Preflight is read-only;
   deploy starts spending without confirmation. Allow **15–25 min**, mostly the local image build.
   On completion it prints the console URL, API URL, and region. Check known issues if it stalls.

2. **Install the optional Claude Code plugin, then restart Claude Code.**

   ```
   /plugin marketplace add aws-samples/sample-remote-coding-agent
   /plugin install sample-rca@sample-remote-coding-agent
   ```

   **Leave all three config fields empty** — a value in `RCA_API_URL`, `AWS_REGION` or
   `RCA_STACK_NAME` **outranks** what the deploy just wrote. The restart is what loads the
   plugin's MCP server; until then `/sample-rca:remote` has no tools.

### Use it

3. **Sign in to the console**, at the URL the deploy printed. Cognito emails a temporary password
   to the `EMAIL=` address; **check spam**, and the hosted UI makes you change it on first sign-in.

4. **Register a repository in the console.** A first sign-in lands on the register form; later it
   is **+ Add repository**. Two fields — the repository URL and the PAT — so leave **Advanced**
   folded. **2-5 min** to `READY`; a task started sooner gets `409 ConfigurationNotReady`.

   **Register a throwaway repository, not one you care about**: the agent pushes branches and
   opens pull requests against whatever you register. Start with a small repository whose tests
   already pass. For an empty repository, run the scaffold locally and push it before dispatch:

   ```
   /sample-rca:scaffold
   ```

5. **Run a task**, or drag a card from **Backlog** to **Planning** — the same path.

   ```
   /sample-rca:remote Add a regression test for one uncovered edge case in an existing
   utility function. Keep the change small, use the existing test runner, and run its tests.
   Explain the chosen case and verification command in the pull request.
   ```

   Or paste the prompt into the console, choose **Add card**, then start it from Backlog.
   The call returns **in seconds**, once queued; a run can take **tens of minutes**.
   These are planning estimates, not guarantees. Declare setup/test entry points so verification
   can run: [command detection details](CLAUDE.md#why-the-makefile-targets-are-named-that-way).

6. **Watch it**, or the board: Backlog → Planning → Coding → Verifying → Done, live-polled.

   ```
   /sample-rca:remote status
   ```

7. **Read the result** — a pull request, **draft if it did not verify green**, with the reason in
   the body. `SUCCEEDED` means a verification command really ran *and* a review judged it green.

   Click the card's status for the run timeline, activity, and verification command/result.
   **Reviewing** and **Submitting** appear within Verifying, not as separate board columns.
   Done means the run ended; “Draft, not verified” and “Failed verification” are not green.
   Follow **Open pull request** and inspect the diff and evidence before merging.

## Architecture

![AWS architecture: Claude Code or the console submits through API Gateway and Lambda; SQS dispatches AgentCore Runtime, which uses private AWS endpoints and GitHub over HTTPS.](assets/architecture.png)

[Editable draw.io source](assets/architecture.drawio) · [Asset sources](assets/README.md#architecture)

The local MCP server signs requests to an IAM-authorized HTTP API. The browser uses
CloudFront/S3 and a separate Cognito-authorized HTTP API. Both reach shared Lambda
handlers and the SQS dispatcher. Each repository owns a Runtime; tasks run in isolated
sessions with a clone and checkpoint under `/mnt/workspace`.

The agent runs **checkout → plan → code → verify → review → submit**, with bounded
re-planning or re-verification. It writes status/activity to DynamoDB; the console polls
through the API. Each repository's AgentCore Memory is created at registration and deleted
at deregistration. Later tasks can retrieve earlier lessons; a repository without memory runs without it.

VPC endpoints connect runtime traffic to **Bedrock, AgentCore, Secrets Manager,
DynamoDB, ECR API/Docker, S3, and CloudWatch Logs**. Lambda runs outside the agent VPC.
GitHub receives source code through clone/push/PR traffic. Package installation reaches
PyPI/npm, and the documentation tool reaches AWS documentation. These are intended destinations,
**not an enforced domain allowlist**: NAT permits outbound TCP 443, and the DNS firewall
blocks named vendor domains only. [Boundary details](CLAUDE.md#the-boundary-claim-and-where-it-is-qualified)

## Costs and cleanup

Deployment incurs charges even between tasks: NAT gateways, interface VPC endpoints,
WAF, images, logs, and stored data can all cost money. Tasks add AgentCore Runtime,
Bedrock, memory, and supporting-service usage. No measured whole-stack monthly or per-task
cost estimate is published yet.

The default **$200 monthly Bedrock budget sends email notifications**, not a hard spending
cap, and does not cover every service. Monitor your actual account charges.
[Budget configuration](infra/stacks/account_stack.py)

Deregister every repository in the console **first** — settings → **Deregister**, 1-3 min each —
and only then destroy the stacks. The other order orphans the runtime, the IAM role and the
secret, which no stack owns and which keep costing money. Follow the
[exact five-stack teardown commands](CLAUDE.md#tearing-a-fleet-down) for your prefix and region.
Stack deletion retains tables, the Cognito pool, access logs, bootstrap ECR images, and runtime
log groups. Review the [retained-resource cleanup table](CLAUDE.md#what-deliberately-survives-a-destroy)
before deleting them; some are the only history of your runs.

## Troubleshooting

- **A tool recommends `/sample-rca:setup`.** That command is gone; run `make deploy EMAIL=you@example.com` from a checkout.
- **The console URL answers `<Error><Code>AccessDenied</Code></Error>`.** The bucket has no
  `index.html`. Re-run the deploy above; it is not an OAC or IAM problem.
- **The temporary password is lost or expired.** Redeployment does not resend it. Use Cognito's
  resend action with the pool ID from the console stack; see [operator notes](CLAUDE.md#what-the-walkthrough-touches-and-where-it-lives).
- **Every tool reports success but the console lists no repository.** A leftover `RCA_API_URL` is
  aiming the tools at another deployment; every tool's answer ends with an `endpoint used:` line.
- **Another `/remote` command answers.** Use `/sample-rca:remote`; other plugin namespaces are unrelated.

Known onboarding issues: [VPC quota preflight](https://github.com/aws-samples/sample-remote-coding-agent/issues/2),
[Cognito domain collisions](https://github.com/aws-samples/sample-remote-coding-agent/issues/3),
[deploy completion](https://github.com/aws-samples/sample-remote-coding-agent/issues/5),
[wrong-account 403 handling](https://github.com/aws-samples/sample-remote-coding-agent/issues/7), and
[account selection](https://github.com/aws-samples/sample-remote-coding-agent/issues/8).
Check their current status before deploying.

## Contributing

Run `make setup`, `make test`, and `make lint` from a checkout before proposing a change.
Local tests use fake AWS services; they do not prove a deployed fleet works.
See [contribution guidelines](CONTRIBUTING.md), [code of conduct](CODE_OF_CONDUCT.md),
and [maintainer reference](CLAUDE.md). Keep diagrams and previews in sync with UI changes.

## Security

Use a fine-grained PAT for one trusted repository and protect its default branch.
The account boundary does not stop repository code from making outbound HTTPS requests.
[Remaining exposure](CLAUDE.md#what-remains-exposed)

Model-data handling, stored logs/memory, and regional residency are separate questions.
Bedrock invocation logging is a retention choice; default `global.` inference profiles
can serve outside your deployment region. Review the
[logging qualification](CLAUDE.md#qualification-1--model-invocation-logging-is-a-retention-choice)
and [data residency](CLAUDE.md#data-residency) before sending sensitive code.

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This sample is licensed under the [MIT-0 License](LICENSE). Third-party visual assets retain
their respective rights; see [asset attribution](assets/README.md#architecture).
