"""The one definition of "queue a run", and the owner of the task vocabulary.

`tasks.create` and `cards.move` both call `queue_run` and nothing else, so the two
entry points cannot drift about what a run is -- which item attributes exist, what a
dispatch message looks like, or whether a configuration was allowed to run at all.

`screen_prompt` is the one caller of the prompt guardrail (invariant 43), and `queue_run`
will not queue a prompt it has not passed: a `str` is screened on the way in, and the only
other thing it accepts is the `ScreenedPrompt` that `screen_prompt` alone constructs.
"""

from __future__ import annotations

import json
import secrets as secrets_module
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError

from . import _common, github_app, observability
from ._common import LOG, ApiError

QUEUED, DISPATCHED = "QUEUED", "DISPATCHED"

# Pinned duplication of `agent/src/coding_agent/state.py`'s `TaskStatus` and
# `TERMINAL_STATUSES`, in that file's own order. The two deployment units must not import
# each other -- `api/` is a Lambda bundle and the agent is a container image -- so the
# vocabulary is written twice on purpose, and
# `tests/test_web_status_vocabulary.py::test_the_api_status_tuple_is_the_console_vocabulary`
# is what holds the two copies equal (order included) rather than review.
# `tests/test_api_agentcore_figures.py::test_the_watchdog_and_the_task_vocabulary_cannot_drift`
# holds the other half: `watchdog.RESUMABLE_STATUSES` is exactly the non-terminal names here.
# If the agent lands a name this tuple has never heard of, the board degrades to the stored
# column (see `cards._STATUS_COLUMN`) and the watchdog treats the status as neither resumable
# nor terminal and leaves it alone; neither raises.
TASK_STATUSES = (
    "QUEUED",
    "DISPATCHED",
    "CHECKING_OUT",
    "PLANNING",
    "CODING",
    "VERIFYING",
    "REVIEWING",
    "SUBMITTING",
    "SUCCEEDED",
    "SUCCEEDED_UNVERIFIED",
    "FAILED_VERIFICATION",
    "FAILED",
    "CANCELLED",
)
# The container's own `TERMINAL_STATUSES`: what the watchdog must never resume and what the
# console renders as finished. `FAILED_VERIFICATION` and `CANCELLED` were missing, and a task
# in either was in *neither* the watchdog's live half nor its finished half -- never listed,
# so its AgentCore session billed until `MAX_LIFETIME_SECONDS`, and `done` never went true.
TERMINAL_STATUSES = frozenset(
    {"SUCCEEDED", "SUCCEEDED_UNVERIFIED", "FAILED_VERIFICATION", "FAILED", "CANCELLED"}
)

# The other half of the same agreement -- the graph's node names, appended to the task item's `steps` as each node finishes. `cards`
# reads them to place a failed run in the column it died in.
STEPS = ("checkout", "plan", "code", "verify", "review", "submit")


def is_terminal(status: str | None) -> bool:
    return status in TERMINAL_STATUSES


@dataclass(frozen=True)
class ScreenedPrompt:
    """A prompt the guardrail has passed. Constructed by `screen_prompt` and nowhere else
    (`tests/test_api_runs.py` holds that), so holding one is the proof it was screened."""

    text: str


# The assessment members that can block, and the key that names *what* tripped in each.
# `match` is deliberately never read: it is the prompt's own text, and a refusal that echoed
# it would put a pasted secret into the response, the console and the log.
_ASSESSMENT_FINDINGS: tuple[tuple[str, str, str, str], ...] = (
    ("contentPolicy", "filters", "content", "type"),
    ("topicPolicy", "topics", "topic", "name"),
    ("wordPolicy", "managedWordLists", "word", "type"),
    ("sensitiveInformationPolicy", "piiEntities", "sensitiveInformation", "type"),
    ("sensitiveInformationPolicy", "regexes", "sensitiveInformation", "name"),
)


def _blocking_findings(assessments: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """What the guardrail blocked on, as `{policy, type}` pairs -- never the matched text."""
    found: list[dict[str, str]] = []
    for assessment in assessments:
        for member, entries, policy, label in _ASSESSMENT_FINDINGS:
            for entry in (assessment.get(member) or {}).get(entries) or []:
                if entry.get("action") in ("BLOCKED", "ANONYMIZED"):
                    finding = {"policy": policy, "type": str(entry.get(label, ""))}
                    if finding not in found:
                        found.append(finding)
    return found


def screen_prompt(prompt: str) -> ScreenedPrompt:
    """Pass `prompt` through the deployment's guardrail, or refuse it (invariant 43).

    **Fails closed.** A guardrail that cannot be reached is a `503`, not a pass: this is the
    one check between an operator's text and a model with unrestricted `Bash`, and a check
    that waves prompts through whenever the service is slow is one an attacker only has to
    wait out. The refusal names the policies that tripped and never the text that tripped
    them, because the sensitive-information policy exists to catch a pasted secret.
    """
    if not _common.GUARDRAIL_ID:
        # `-c guardrailEnabled=false`: a deploy-time decision, recorded in the template,
        # rather than something a missing variable decides at run time.
        return ScreenedPrompt(prompt)
    try:
        answer = _common.bedrock_runtime().apply_guardrail(
            guardrailIdentifier=_common.GUARDRAIL_ID,
            guardrailVersion=_common.GUARDRAIL_VERSION,
            source="INPUT",
            content=[{"text": {"text": prompt}}],
            outputScope="INTERVENTIONS",
        )
    except (ClientError, BotoCoreError) as err:
        # The service's own code goes into the message, because the 503 covers two very
        # different things: `ThrottlingException` clears on a retry, while
        # `AccessDeniedException` or `ValidationException` (a deleted or mistyped version,
        # measured) is a broken deploy that no retry fixes.
        reason = (
            err.response.get("Error", {}).get("Code", "ClientError")
            if isinstance(err, ClientError)
            else type(err).__name__
        )
        LOG.error("guardrail unavailable, refusing to queue: %s", reason)
        raise ApiError(
            503,
            "GuardrailUnavailable",
            f"the prompt guardrail did not answer ({reason}), so the prompt was not screened "
            "and no run was queued. A throttle clears on a retry; an access or validation "
            "error means the deployment's guardrail settings need fixing.",
            reason=reason,
        ) from err
    if answer.get("action") != "GUARDRAIL_INTERVENED":
        return ScreenedPrompt(prompt)
    findings = _blocking_findings(answer.get("assessments") or [])
    LOG.warning("prompt refused by the guardrail: %s", findings)
    # The types are spelled into the message as well as `findings`, because the MCP client
    # renders `error` and `message` only: from Claude Code, the message is all there is.
    tripped = ", ".join(finding["type"] for finding in findings) or "unspecified"
    raise ApiError(
        422,
        "PromptBlockedByGuardrail",
        f"the prompt guardrail refused this prompt ({tripped}), so no run was queued. "
        "Rephrase the task, and never paste a credential into one.",
        findings=findings,
    )


def new_task_id() -> str:
    """`tsk_<epoch-ms>_<6 hex>`: time-sortable, so a `query` with
    `ScanIndexForward=False` is newest-first with no GSI at all (PLAN's `### Data`
    removed the `byConfig` index), and still opaque enough not to be guessable."""
    return f"tsk_{int(time.time() * 1000)}_{secrets_module.token_hex(3)}"


def queue_run(
    config: Mapping[str, Any],
    *,
    prompt: str | ScreenedPrompt,
    card: Mapping[str, Any] | None = None,
    source: str,
) -> dict[str, Any]:
    """Write the task item and enqueue its dispatch. Returns the stored item.

    A `str` prompt is screened here; a `ScreenedPrompt` already was. `tasks.create` screens
    before it creates the card, so a refused prompt never lands on the board, and this stays
    both the one place a run is queued and the one place an unscreened prompt is refused."""
    status = config.get("status")
    if status != "READY":
        # A run queued against a `CREATING` configuration dies in the dispatcher with a
        # `ResourceNotFound` nobody can read, so it is refused where the operator is.
        raise ApiError(
            409,
            "ConfigurationNotReady",
            "the configuration is not READY, so a run cannot be dispatched against it",
            configId=config.get("configId"),
            status=status,
        )

    # The second gate, and it fails the same way the first does rather than 20 minutes
    # later. A repository can be perfectly `READY` and still have no GitHub credential: the
    # authorisation is a separate operator action (`github_app.py`), and without it the run
    # clones, plans, codes, verifies and then dies at `submit` having spent a full task's
    # tokens. Checked here rather than in `tasks.create`, because `runs.queue_run` is the one
    # definition of "queue a run" and `cards.move` must refuse for the same reason.
    if not github_app.resolve_token(str(config["configId"]))["accessToken"]:
        raise ApiError(
            409,
            "GitHubNotAuthorized",
            "this repository has no GitHub credential yet. Authorise it in the console "
            "(Repository settings -> Authorise GitHub); if it is already authorised, the "
            "deployment's GitHub App is not installed on this repository.",
            configId=config.get("configId"),
            repository=config.get("repository"),
        )

    # Third, after the two refusals that cost nothing to compute and before anything is
    # written, so a refused prompt leaves no task item and no dispatch message.
    screened = prompt if isinstance(prompt, ScreenedPrompt) else screen_prompt(prompt)

    task_id = new_task_id()
    stamp = _common.now_iso()
    task: dict[str, Any] = {
        "configId": config["configId"],
        "taskId": task_id,
        "prompt": screened.text,
        "status": QUEUED,
        "sessionId": observability.session_id(task_id),
        # Copied rather than resolved at dispatch time, so a `DELETE` of the
        # configuration cannot orphan a dispatch that is already in flight.
        "agentRuntimeArn": config.get("agentRuntimeArn", ""),
        "repository": config.get("repository", ""),
        "resumeCount": 0,
        "activity": [],
        "steps": [],
        "source": source,
        "createdAt": stamp,
        "updatedAt": stamp,
    }
    if card is not None:
        task["cardId"] = card["cardId"]

    _common.tasks_table.put_item(Item=task, ConditionExpression=Attr("taskId").not_exists())
    enqueue_dispatch(task)
    return task


def enqueue_dispatch(task: Mapping[str, Any], *, resume: bool = False) -> None:
    """The single owner of the dispatch message shape. The watchdog sends the same
    message with `resume=True`, so the dispatcher has one body to parse."""
    task_id, config_id = task["taskId"], task["configId"]
    _common.sqs.send_message(
        QueueUrl=_common.TASK_QUEUE_URL,
        MessageBody=json.dumps({"taskId": task_id, "configId": config_id, "resume": resume}),
    )
    LOG.info("queued dispatch task=%s config=%s resume=%s", task_id, config_id, resume)
