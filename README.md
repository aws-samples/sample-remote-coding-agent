# agentcore-coding-agent

Dispatch a coding task from Claude Code and get a pull request back, with the whole
pipeline — `plan → code → verify → review` — running on Bedrock AgentCore Runtime inside a
single AWS account: remote agent capability without giving up Bedrock's zero data retention
or the in-account boundary.

**A run that did not verify green opens a draft pull request and says so.** That is the
honesty rule the whole design serves: `verified` means a real verification command ran and a
review judged it green, and nothing advisory may set it.

Only register repositories you trust — this executes their code by design, and `CLAUDE.md`
carries the guardrails that bound it rather than pretending to remove it.

## Architecture

```
┌─ your machine ──────────────────┐
│  git clone · make deploy        │
│  Claude Code                    │
│    /sample-rca:remote  …        │
│         │ MCP (stdio)           │
│    rca-mcp ─── SigV4 ───────────┼───┐
└─────────────────────────────────┘   │
                                      │
┌─ your AWS account (five stacks) ─────┼─────────────────────────────────┐
│                                      ▼                                 │
│   HTTP API (IAM) ─────────► Lambda ──────► DynamoDB · Secrets Manager   │
│                               │                                        │
│   CloudFront ─► S3 (console)  └──► SQS ──► dispatcher                   │
│        │  └─► HTTP API (Cognito) ────┘         │ InvokeAgentRuntime     │
│        ▲                                       ▼                       │
│     browser                    ┌── AgentCore Runtime (one per repo) ──┐ │
│                                │  VPC · private subnet · 8 h session  │ │
│                                │    Claude Code CLI (Bedrock only)    │ │
│                                │  plan → code → verify → review → PR  │ │
│                                │    /mnt/workspace  ← the clone       │ │
│                                └──────┬──────────────────┬────────────┘ │
│                          VPC endpoints│                  │ NAT :443     │
│         Bedrock · AgentCore · DynamoDB · Secrets         │              │
│              Manager · ECR · S3 · Logs                   │              │
└──────────────────────────────────────────────────────────┼──────────────┘
                                                           ▼
                                        github.com · PyPI · npm
                                     ── the only egress that exists ──
```

Prompts and code stay in-account: Bedrock retains neither prompts nor completions and does
not train on them. The only traffic that leaves is `github.com`, the package registries the
repository's own `setupCommand` reaches, and `docs.aws.amazon.com` — AWS-owned, read-only,
receiving a search query and never repository content. Zero retention is not residency: the
default `global.` profile may serve from any commercial region, so pin a geo-scoped (`us.` /
`eu.` / `apac.`) one — `CLAUDE.md`, "Data residency".

Each registered repository gets its **own** AgentCore Memory, created when it is registered and
deleted when it is deregistered. Every session of that repository shares one episodic namespace, so
a lesson one task records is available to the next; a repository with no memory runs without one.

## From nothing to nothing

### Before you start

| You need | Why |
|---|---|
| Docker running | the ~1.3 GB ARM64 agent image is built on *this* machine. No build service stands in. |
| `uv` on `PATH`, Node ≥ 20, AWS credentials that resolve | Node is for the pinned CDK CLI |
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

   **`make preflight` is read-only and prints the account and region — read them before you run
   the last line, which starts spending and asks for no confirmation.** **15-25 min** on the
   first run, nearly all of it the image build, so a long silence is not a hang. It ends by
   printing the console URL, the API URL and the region.

2. **Install the plugin, then restart Claude Code.**

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
   opens pull requests against whatever you register. An empty one is enough, and this fills it
   with a project whose `setup` and `test` the agent can detect.

   ```
   /sample-rca:scaffold
   ```

5. **Run a task**, or drag a card from **Backlog** to **Planning** — the same path.

   ```
   /sample-rca:remote Build a 2048 game as a Vue 3 single-page app. Keep the board and merge
   logic in a plain module with Vitest tests covering spawn, merge, win and game over. Add
   scripts.setup, scripts.test and scripts.dev to package.json, and a README whose first
   section is the exact commands to install it, run it in a browser, and test it.
   ```

   The call returns **in seconds**, once queued; the run itself takes **tens of minutes**. Two
   clauses there earn their length. `scripts.setup` and `scripts.test` are what detection finds,
   so the tests really run and the pull request comes back **not** a draft. And **ask for the
   README explicitly**: a run told only to build the game builds it and leaves you guessing how
   to start it — the commands are obvious to the agent and invisible to you.

6. **Watch it**, or the board: Backlog → Planning → Coding → Verifying → Done, live-polled.

   ```
   /sample-rca:remote status
   ```

7. **Read the result** — a pull request, **draft if it did not verify green**, with the reason in
   the body. `SUCCEEDED` means a verification command really ran *and* a review judged it green.

### Tear it down

Deregister every repository in the console **first** — settings → **Deregister**, 1-3 min each —
and only then destroy the stacks. The other order orphans the runtime, the IAM role and the
secret, which no stack owns and which keep costing money. The commands, the five stack names and
what survives on purpose: `CLAUDE.md`, "Tearing a fleet down".

### If something goes wrong

- **A tool tells you to run `/sample-rca:setup`.** That command is gone. Deploy from a checkout:

  ```bash
  make deploy EMAIL=you@example.com
  ```
- **The console URL answers `<Error><Code>AccessDenied</Code></Error>`.** The bucket has no
  `index.html`. Re-run the deploy above; it is not an OAC or IAM problem.
- **The temporary password is lost or expired.** A second deploy does **not** resend. Send one
  yourself, with the pool id from the `SampleCodingAgentConsole` stack's `UserPoolId` output:

  ```bash
  aws cognito-idp admin-create-user --region <region> --user-pool-id <pool> \
    --username you@example.com --message-action RESEND
  ```
- **Every tool reports success but the console lists no repository.** A leftover `RCA_API_URL` is
  aiming the tools at another deployment; every tool's answer ends with an `endpoint used:` line.
- **A command resolves but nothing you recognise happens.** Only `/sample-rca:*` is this system.
  Unnamespaced `/remote` works only in a checkout of this repository, and any other namespace
  belongs to a different plugin that happens to be installed.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
