# README assets

## Console preview

`console-preview.gif` is a **synthetic UI preview**, not a recording of an AWS agent run.
It captures the unmodified console from this branch using browser-intercepted API responses.
The opening title and README caption say “Sample run — simulated”; there is no bottom overlay.
The example repository, task, verification evidence, and PR URL are fixtures. No AWS or GitHub
requests are made. There is no measured task duration. Typing plays at 10 frames per second,
stages pause for four seconds, and the ending holds for five seconds before replaying.

The preview starts on an empty board and types a title followed by its description into the
actual composer (the first line becomes the title). It clicks **Add card**, then **Start run**,
and follows Planning, Coding, Verifying, Reviewing, Submitting, and Done. The final frames show
the run details with an illustrative test result. A “Task complete” screen separates the ending
from the new-task introduction on replay; it does not depict moving a finished card to Backlog.
The [unverified draft example](console-draft.png) is separate from this successful sequence.
Reviewing and Submitting stay in the Verifying column. It does not claim to show a real PR diff.

Recreate it from the repository root with Chrome and ffmpeg installed:

```bash
uv run --with playwright==1.58.0 assets/capture-preview.py
```

The capture script asserts typed content, the Add card request, the created title,
rendered stages, the board-column mapping, and the result link.
It blocks external browser requests and fails on JavaScript errors or unexpected API calls.
The fixture is **Add CSV export** in `example/expense-tracker`: add a Download CSV button
to Expenses, export date/category/amount, and test CSV escaping and empty exports.
Its illustrative verification command is `npm test`. [Static preview](console-preview.png).
UI source baseline: `72e8b3f` (2026-09-28). Refresh when the UI or status vocabulary changes.

### Live recording still required

Before calling this an end-to-end demo, use an approved disposable AWS account and repository:

1. Record the source commit, initial repository commit, exact prompt, model configuration,
   verification command, and actual elapsed time. Do not expose credentials or private code.
2. Create the task in Backlog and start it. Capture the same card progressing automatically
   through Planning, Coding, Verifying, and Done. Do not move it to fake execution progress.
3. Open run details and show Reviewing/Submitting, activity, verification evidence, and the
   actual GitHub PR diff. Capture a draft outcome separately, without relabeling it as success.
4. Trim waiting time to roughly 30–60 seconds, disclose speed changes and actual runtime,
   and provide captions or a text walkthrough. Keep a static fallback and readable text.
5. Verify GitHub playback and light/dark rendering. Review every frame for sensitive content.
   Replace the synthetic preview only once these checks pass. Independently verify clean-clone
   deployment and deregistration-first cleanup; a UI recording does not prove those steps.

## Architecture

`architecture.drawio` is the editable source. `architecture.png` is its white-background
export, suitable for GitHub light and dark themes. The VPC spans two Availability Zones: the
runtime sits in the private subnets behind a security group that allows only outbound HTTPS,
reaches the services in the **AWS services** group (container id `endpoints`) through the VPC
endpoints box, and reaches GitHub through the public subnet's NAT gateway and the internet
gateway. Connectors describe the main request path, not every IAM or lifecycle operation.
Lambda is outside the runtime's VPC. GitHub is outside the AWS account.
All connectors are straight. The account uses the native AWS Account group border;
the Claude Code artwork's black background is removed for transparent placement.

AWS service icons use draw.io's AWS4 library. Runtime and Claude Code/GitHub reference assets
come from slide 10 of the owner-supplied **Coding Agents on Amazon Bedrock AgentCore** deck.
The generic AgentCore symbol identifies Memory; it is not a distinct Memory service icon.
AWS, Claude, and GitHub artwork and marks retain their owners' rights and identify the services;
the repository's MIT-0 license does not relicense these marks. The full deck is not redistributed.

Export after editing in draw.io Desktop:

```bash
drawio -x -f png --width 2000 -o assets/architecture.png assets/architecture.drawio
```

On macOS, use `/Applications/draw.io.app/Contents/MacOS/draw.io` if `drawio` is not on PATH.
Inspect the PNG for missing icons, clipped labels, and overlapping edges before committing.
Documentation tests check the editable graph's endpoint coverage and connector references.
