# Operations reference

Reference for deploying, running and removing a fleet. `README.md` has the walkthrough.

## Deploying

```bash
make setup                         # create .venv and install all four packages
make preflight                     # prerequisite checks only; read-only, safe at any time
make deploy EMAIL=you@example.com  # the whole deploy, first console operator included
make deploy EMAIL=you@example.com STACK_PREFIX=Foo  # a second, separate fleet
```

**`make preflight`** checks: a Docker daemon (the ~1.3 GB ARM64 agent image is built on this
machine), `uv` on `PATH`, Node `^20.19 || >=22.12`, AWS credentials and the region they resolve
to, CDK bootstrap in that account and region, that Bedrock AgentCore answers there, and that
every inference profile the deploy forwards is `ACTIVE` there. Each failure prints a
`preflight: FAILED --` line and a `fix:`. A missing bootstrap is printed as the exact command,
never run for you.

**`make deploy`** runs `preflight`, then deploys five stacks: data, runtime, API, console, and
an account stack in `us-east-1` with the console's WAF web ACL and the monthly Bedrock budget
(notifications go to `EMAIL`). It takes 15-25 minutes, mostly building and pushing the image.
It then uploads the console, creates `EMAIL` as the first console operator, prints the console
URL, the API URL and the region, writes `~/.rca/endpoint.json` for the MCP server, and offers
to register the current repository when `origin` is on GitHub. Both targets are idempotent, so
a re-run is the health check.

A console URL that answers `<Error><Code>AccessDenied</Code></Error>` means the bucket has no
`index.html`; re-run `make deploy EMAIL=...`.

**The first operator's password** is generated and emailed by Cognito, never printed. A second
deploy does not resend it; to resend, run
`aws cognito-idp admin-create-user --message-action RESEND` with the console stack's
`UserPoolId` output.

**`STACK_PREFIX`** renames all five stacks, so `STACK_PREFIX=Foo` is a separate fleet in the
same account. A second fleet in the same region also needs `-c consoleDomainPrefix=<unique>`,
and the plugin follows it through its `stack_name` setting (`RCA_STACK_NAME`). Two fleets in
one account share the `/rca/` IAM path and the one AgentCore Identity credential provider, so
disconnecting GitHub in either console revokes both.

**Model profiles** default to `global.`. `-c planModelId=`, `-c codeModelId=` and
`-c haikuModelId=` pin others; see `docs/security.md` for residency.

## Claude Code plugin

The plugin is `sample-rca`. Its tools are `mcp__plugin_sample-rca_remote-coding-agent__*`;
two installed plugins with the same name collide on tool names, which is why the name is
qualified.

The MCP server finds the deployment in this order: `RCA_API_URL` (the plugin's `api_url`
setting), then `~/.rca/endpoint.json`, then the `ApiUrl` output of the `SampleCodingAgentApi`
stack (or `RCA_STACK_NAME`). `RCA_REGION` (the plugin's `aws_region` setting) pins the region;
the ambient `AWS_REGION` does not. Leave the settings empty for a single fleet. Every tool's
answer ends with an `endpoint used:` line naming the URL, the region and the source.

`/sample-rca:remote` dispatches and follows tasks; `/sample-rca:scaffold` turns an empty
repository into one the agent can run tasks against. In a checkout of this repository the
un-namespaced `/remote` command does the same as `/sample-rca:remote`.

## Repositories

- **Register** in the console (**+ Add repository**) with the full
  `https://github.com/owner/repo` URL, or with the plugin's `register_repository`.
- **Provisioning** takes about 8-12 minutes. A task started before the repository is `READY`
  is refused with `409 ConfigurationNotReady`.
- **Authorise** by opening the repository's page in the console; until then a task is refused
  with `409 GitHubNotAuthorized`.
- **After a deploy that changes the agent image,** press **Update runtime** on each
  repository's settings page to move it to the new image.
- **Deregister** on the repository's settings page. It returns at once and removes the
  runtime, the memory, the `/rca/` role and its policy, then the item: 1-3 minutes.
  Deregistration is console-only; the plugin can send only `GET` and `POST`.

The board polls for changes, and a task's page links to its log group,
`/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`.

## Why the Makefile targets are named that way

The agent uses only entry points a repository declares: a Makefile `setup:` or `bootstrap:`
target to prepare and a `test:` target to verify (or `package.json` `scripts.setup` and
`scripts.test`). It guesses nothing else, so renaming either target turns verification off
for this repository. `setup` is idempotent because it runs before verification on every run.

## Tearing a fleet down

Order matters: AgentCore runtimes, per-repository IAM roles, memories and the Identity
credential provider are created by API calls, not by CloudFormation, so destroying the stacks
first leaves them behind.

1. **Deregister every repository** in the console (settings page → **Deregister**).
2. **Disconnect GitHub** on the console's **GitHub App** page. This deletes the credential
   provider and every stored authorisation. The GitHub App itself belongs to your GitHub
   account; delete it on github.com if you no longer need it.
3. **Destroy the five stacks by name**, with the same pinned CDK CLI `make deploy` uses:

   ```bash
   cd infra && PATH="$PWD/../.venv/bin:$PATH" npx --yes aws-cdk@2.1138.0 destroy \
     -c account=<account> -c region=<region> \
     SampleCodingAgentConsole SampleCodingAgentApi SampleCodingAgentRuntime \
     SampleCodingAgentData SampleCodingAgentAccountEuWest1
   ```

   The `PATH` prefix makes `cdk.json`'s `python3 app.py` find the CDK libraries, and the
   context values are required because `infra/app.py` reads the account and region from
   context. The account stack is suffixed with the consuming region (`eu-west-1` →
   `…AccountEuWest1`); `STACK_PREFIX=Foo` renames all five. Run `… ls` with the same
   arguments first to see the names. It takes 10-20 minutes, mostly the CloudFront
   distribution.

### What survives a destroy

| Resource | Why it is kept | Delete it with |
|---|---|---|
| The Configurations, Tasks and Cards tables | `RemovalPolicy.RETAIN`, point-in-time recovery on | find the names with `aws cloudformation list-stack-resources --stack-name SampleCodingAgentData` before the destroy, then `aws dynamodb delete-table` |
| The Cognito user pool | it is the operator list | `aws cognito-idp delete-user-pool-domain`, then `aws cognito-idp delete-user-pool` |
| The access-log bucket | CloudFront and S3 access logs | `aws s3 rb s3://<name> --force` |
| ECR images (~1.3 GB each) | they live in the CDK bootstrap repository, shared by every CDK app in the account | `aws ecr batch-delete-image`, not `delete-repository` |
| AgentCore runtime log groups | created by the service; they hold run transcripts | `aws logs delete-log-group --log-group-name /aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT` |

The DNS query log group `/aws/route53/rca-agent-dns` is deleted with the stack; export it
first if you need it.
