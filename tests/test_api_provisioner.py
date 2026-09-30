"""The provisioner: the four phases, and the two failures that cost the most to find.

AgentCore is exercised through `FakeAgentCore` and never moto -- moto's
`get_agent_runtime` drops `filesystemConfigurations`, so a carry-forward test written
against it would pass while the mount was being stripped. moto owns DynamoDB, SQS, IAM
and Secrets Manager here, which are the parts whose real behaviour the code depends on
(a conditional write that fails, a `NoSuchEntity` on a second teardown pass).

Two properties get the most attention, because both were silent in production:

* `UpdateAgentRuntime` is a full replace, so every field is either carried forward from
  the live runtime or converged to fleet policy (invariant 24), and the idempotence
  guard has to cover the converged fields rather than the image alone.
* IAM is eventually consistent, so a create that fails on an unassumable role is retried
  as a **new** SQS message (invariant 15), so the receive count resets rather than burning
  the redrive budget on a healthy configuration -- inside a wall-clock budget, and only
  for a failure the `_TRANSIENT_CREATE_FAILURES` allowlist names. An *unrecognised*
  message out of `CreateAgentRuntime` gets exactly one retry first -- the allowlist's
  floor, which is what a gap in the enumeration costs -- and is then terminal with the
  vendor's own words in `statusReason`, because keyword-matching the vendor's prose
  reported three separate permanent failures as an execution role that was never the
  problem.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from botocore.exceptions import ClientError
from test_api_fakes import FakeAgentCore, client_error, install, runtime
from test_api_fakes import load_handlers as _load

_CONFIG_ID = "cfg_abc123"
_RUNTIME_ID = "rca_cfg_abc123-ABC123"
# What `FakeAgentCore` answers `CreateMemory` with for `_CONFIG_ID`: `<name>-<10
# service-generated characters>`, which is botocore's own `memoryId` pattern -- so a test
# that seeds this value exercises the same shape a real registration stores.
_MEMORY_ID = "rca_mem_cfg_abc123-ABCDEF1234"
_REGION = "ap-southeast-1"
_ACCOUNT = "123456789012"
_IMAGE = f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/rca:latest"
_OLD_IMAGE = f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/rca:2f9c1aa"
# An f-string rather than one literal: this names a secret, it is not one, and the name is what
# a scanner reads.
_PROVIDER_NAME = "rca_cfg_abc123"
_PROVIDER_ARN = (
    f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:"
    f"token-vault/default/apikeycredentialprovider/{_PROVIDER_NAME}"
)
_PLAN_MODEL = "global.anthropic.claude-opus-4-5-20251101-v1:0"
_CODE_MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
_HAIKU_MODEL = "apac.anthropic.claude-haiku-4-5-20251001-v1:0"

# The verbatim `Error.Message` from two real eu-west-1 registrations, and the string that
# broke provisioning entirely: the six original propagation shapes were built around
# "assume"/"assumed", AgentCore says "assumption", so nothing matched and every
# `POST /configurations` failed on its first attempt. Pinned in full, character for
# character, because a paraphrase of it is exactly what let this through.
_LIVE_ROLE_VALIDATION_MESSAGE = (
    "Role validation failed for 'arn:aws:iam::111122223333:role/rca/rca-cfg_15af1931a731'. "
    "Please verify that the role exists and its trust policy allows assumption by this service"
)

# A phrasing the allowlist deliberately does not name, standing in for whatever the vendor
# invents next. The floor is the only thing between a message like this and a provisioning
# outage, so it is written here as an *invention* rather than as an observed shape.
_UNENUMERATED_MESSAGE = "Some phrasing AgentCore has not used yet"


def _fleet(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    """The fleet-policy environment, set before `_load` because the module binds it at
    import -- which is also how a real deploy changes it."""
    monkeypatch.setenv("AGENT_IMAGE_URI", _IMAGE)
    monkeypatch.setenv("PLAN_MODEL_ID", _PLAN_MODEL)
    monkeypatch.setenv("CODE_MODEL_ID", _CODE_MODEL)
    monkeypatch.setenv("HAIKU_MODEL_ID", _HAIKU_MODEL)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def _seed(common: Any, **overrides: Any) -> dict[str, Any]:
    item = {
        "configId": _CONFIG_ID,
        "repository": "owner/repo",
        "repositoryUrl": "https://github.com/owner/repo",
        "status": "PENDING",
        "statusReason": "",
        "credentialProviderName": _PROVIDER_NAME,
        "credentialProviderArn": _PROVIDER_ARN,
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:00.000Z",
    }
    item.update(overrides)
    common.config_table.put_item(Item=item)
    return item


def _spy_sqs(monkeypatch: pytest.MonkeyPatch, common: Any) -> list[dict[str, Any]]:
    """Record every `send_message` and forward it with no delay.

    moto honours `DelaySeconds`, so a re-enqueued `CHECK` or a backed-off `CREATE` would
    be invisible to `receive_message` and a test asserting "a new message was sent" would
    read as "nothing was sent". The recorded kwargs carry the delay, which is the part
    worth asserting anyway.
    """
    sent: list[dict[str, Any]] = []
    real = common.sqs.send_message

    def record(**kwargs: Any) -> Any:
        sent.append(kwargs)
        return real(**{**kwargs, "DelaySeconds": 0})

    monkeypatch.setattr(common.sqs, "send_message", record)
    return sent


def _bodies(sent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [json.loads(message["MessageBody"]) for message in sent]


def _run(
    provisioner: Any, phase: str, config_id: str = _CONFIG_ID, *, attempt: int = 0, **body: Any
) -> None:
    """One SQS record through the real entry point, so the message shape is under test too.
    `**body` merges extra members -- `retryDeadline` is how the wall-clock budget is
    exercised without freezing a clock."""
    provisioner.lambda_handler(
        {
            "Records": [
                {
                    "messageId": "msg-1",
                    "body": json.dumps(
                        {"phase": phase, "configId": config_id, "attempt": attempt, **body}
                    ),
                }
            ]
        },
        None,
    )


def _status(common: Any, config_id: str = _CONFIG_ID) -> dict[str, Any] | None:
    return common.config_table.get_item(Key={"configId": config_id}).get("Item")


# -- CREATE -----------------------------------------------------------------------


def test_create_never_creates_an_endpoint(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant 14: `create_agent_runtime` provisions `DEFAULT` itself. A second
    endpoint call conflicts while the runtime is still `CREATING`, and polling an
    endpoint that was never created reports `ResourceNotFound` forever while the runtime
    sits `READY`."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE")

    # The order is the invariant: the memory exists before the role's inline policy is
    # written, because that policy names the memory's exact ARN.
    assert control.operations() == ["CreateMemory", "CreateAgentRuntime"]
    assert not any("Endpoint" in operation for operation in control.operations())

    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.CREATING
    assert stored["agentRuntimeId"]
    assert stored["memoryId"]
    assert stored["containerUri"] == _IMAGE
    assert stored["executionRoleArn"].startswith("arn:aws:iam::123456789012:role/rca/")
    # The runtime is not `READY` yet, so the phase hands off to `CHECK` rather than
    # blocking a Lambda on a poll loop.
    assert _bodies(sent) == [{"phase": "CHECK", "configId": _CONFIG_ID, "attempt": 0}]
    assert sent[0]["DelaySeconds"] == provisioner.CHECK_DELAY_SECONDS


def test_create_sends_the_mount_the_lifetime_and_the_work_root(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three settings a runtime cannot be given later without a full update, and
    whose absence is invisible: no mount means a resume starts from an empty directory,
    the 2h default lifetime kills a long task mid-review, and a `WORK_ROOT` outside the
    mount puts the checkout somewhere that does not survive."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE")
    call = control.last("CreateAgentRuntime")

    assert call["filesystemConfigurations"] == [{"sessionStorage": {"mountPath": "/mnt/workspace"}}]
    assert call["lifecycleConfiguration"]["maxLifetime"] == 28800
    assert call["environmentVariables"]["WORK_ROOT"] == "/mnt/workspace/work"
    assert call["environmentVariables"]["WORK_ROOT"].startswith(provisioner.MOUNT_PATH + "/"), (
        "the work root has to be *under* the mount, or it does not survive a resume"
    )
    assert call["agentRuntimeArtifact"] == {"containerConfiguration": {"containerUri": _IMAGE}}
    assert call["protocolConfiguration"] == {"serverProtocol": "HTTP"}
    assert call["networkConfiguration"] == {"networkMode": "PUBLIC"}
    # The container reads its own configuration and its own secret; it is told which.
    assert call["environmentVariables"]["CONFIG_ID"] == _CONFIG_ID
    assert call["environmentVariables"]["CREDENTIAL_PROVIDER_NAME"] == _PROVIDER_NAME
    assert call["environmentVariables"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == _CODE_MODEL
    assert call["environmentVariables"]["CLAUDE_CODE_USE_BEDROCK"] == "1"


def test_a_memory_is_created_before_the_role_so_its_policy_can_name_it(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one AgentCore Memory a registered repository owns, and the ordering that makes it
    usable: `_ensure_role` writes `rca-execution` on every provision and that policy names
    the memory's **exact** ARN, so the memory has to exist first. The last assertion is what
    makes the order load-bearing rather than cosmetic -- with `_ensure_memory` after
    `_ensure_role` the `UseOwnMemory` Sid is simply absent and nothing else notices.

    The two strategies are the split the container's keying rule depends on: `{actorId}` is
    the configuration id and `{sessionId}` is the task id, so the episodic namespace is ONE
    namespace shared by every session of this repository and the summary namespace is one per
    task."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE")
    call = control.last("CreateMemory")

    assert call["name"] == "rca_mem_cfg_abc123"
    # A retry of `_create` inside the service's idempotency window must not mint a second
    # memory for one configuration.
    assert call["clientToken"] == _CONFIG_ID
    assert call["eventExpiryDuration"] == 90
    assert call["tags"]["rca:repository"] == "owner/repo"
    episodic, session = call["memoryStrategies"]
    assert episodic["semanticMemoryStrategy"]["name"] == "repository_episodic"
    assert episodic["semanticMemoryStrategy"]["namespaces"] == ["/rca/{actorId}/episodic"]
    assert session["summaryMemoryStrategy"]["name"] == "session_summary"
    assert session["summaryMemoryStrategy"]["namespaces"] == ["/rca/{actorId}/session/{sessionId}"]

    (memory_id,) = control.memories
    stored = _status(common)
    assert stored is not None and stored["memoryId"] == memory_id
    written = common.iam.get_role_policy(
        RoleName=f"rca-{_CONFIG_ID}", PolicyName=provisioner._POLICY_NAME
    )
    document = written["PolicyDocument"]
    document = json.loads(document) if isinstance(document, str) else document
    (use_memory,) = [s for s in document["Statement"] if s["Sid"] == "UseOwnMemory"]
    assert use_memory["Resource"] == (
        f"arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:memory/{memory_id}"
    ), "the inline policy names the one memory this configuration owns, id and all"


def test_a_memory_that_cannot_be_created_still_leaves_a_working_repository(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliverable D11's control-plane half. `_create`'s contract is that every arm returns,
    and `_ensure_memory` swallowing its own `ClientError` is what keeps a memory failure out
    of the classifier: a `ValidationException` reaching `_create`'s `except ClientError`
    would fall past the transient allowlist -- gated on `CreateAgentRuntime` -- into the
    terminal arm and call the configuration `FAILED` because a *memory* could not be made.
    The container degrades to a no-op when `memoryId` is absent, so the repository is still
    registered, still provisioned and simply has no memory."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    control = FakeAgentCore(
        "bedrock-agentcore-control",
        errors={
            "CreateMemory": [
                client_error(
                    "ValidationException", "memory strategy is not valid", operation="CreateMemory"
                )
            ]
        },
    )
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE")

    assert "CreateAgentRuntime" in control.operations(), "the runtime is not the memory's hostage"
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.CREATING
    assert stored["statusReason"] == "", "a memory failure is not a configuration failure"
    assert stored["memoryId"] == ""
    assert _bodies(sent) == [{"phase": "CHECK", "configId": _CONFIG_ID, "attempt": 0}]

    written = common.iam.get_role_policy(
        RoleName=f"rca-{_CONFIG_ID}", PolicyName=provisioner._POLICY_NAME
    )
    document = written["PolicyDocument"]
    document = json.loads(document) if isinstance(document, str) else document
    assert not [s for s in document["Statement"] if s["Sid"] == "UseOwnMemory"], (
        "no memory means no `UseOwnMemory` statement -- an Allow on `memory/` with nothing "
        "after the slash is not a grant IAM can evaluate"
    )


def test_a_retried_create_reuses_the_memory_it_already_made(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same shape as `test_the_role_and_its_policy_are_rewritten_on_a_second_create`, for
    the resource a second create must **not** re-mint. A `CREATE` retried inside the
    transient budget (invariant 15) re-enters `_create` with the same configuration item,
    which by then carries a `memoryId`; a helper that always called `CreateMemory` would get
    `ConflictException`, return `""`, and the single `_set_config` write at the end of
    `_create` would overwrite a live memory id with an empty string -- silently un-granting
    `UseOwnMemory` on the next provision."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common, memoryId=_MEMORY_ID)
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE")

    assert "CreateMemory" not in control.operations()
    stored = _status(common)
    assert stored is not None and stored["memoryId"] == _MEMORY_ID


def test_the_runtime_name_carries_no_hyphen(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`AgentRuntimeName` is `[a-zA-Z][a-zA-Z0-9_]{0,47}` -- no hyphens, 48 characters.
    botocore does not enforce a `pattern` client-side, so passing an id through unchanged
    is a server-side `ValidationException` that reads like a service problem. Asserted on
    the sent value for that reason: the fake cannot catch it either."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    hyphenated = "cfg-legacy-01"
    _seed(common, configId=hyphenated)
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE", hyphenated)
    name = control.last("CreateAgentRuntime")["agentRuntimeName"]

    assert "-" not in name
    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", name), name
    assert provisioner._runtime_name("cfg_" + "a" * 60) == ("rca_cfg_" + "a" * 60)[:48]
    # The role name is a *different* namespace, where a hyphen is legal and is what the
    # `iam:CreateRole` grant's `rca-*` fence expects.
    assert provisioner._role_name(hyphenated) == "rca-cfg-legacy-01"


def test_the_memory_name_carries_no_hyphen(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`CreateMemory`'s `name` is `[a-zA-Z][a-zA-Z0-9_]{0,47}` too, so a legacy `cfg-`-shaped
    id passed through unchanged is the same server-side `ValidationException` the runtime name
    above guards against -- and one that costs the repository its memory rather than its
    runtime, which is quieter."""
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    name = provisioner._memory_name("cfg-abc-123")

    assert "-" not in name
    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", name), name
    assert len(provisioner._memory_name("cfg_" + "a" * 60)) <= 48


@pytest.mark.parametrize(
    ("code", "message", "transient"),
    [
        (
            "ValidationException",
            "Role arn:aws:iam::123456789012:role/rca/rca-cfg_abc123 cannot be assumed",
            True,
        ),
        # Deliberately flipped, and the one behaviour change in the allowlist that is not a
        # bug fix. An `AccessDeniedException` that names only the `sts:AssumeRole` *action*
        # is word for word what an SCP or a permissions-boundary denial of that same action
        # looks like -- see the two rows below it -- so it is not classifiable and must not
        # borrow the propagation budget. It now reaches `FAILED` with AWS's own words on the
        # first attempt, which is a strictly better report than six retries followed by a
        # claim about assumability that the code never established.
        ("AccessDeniedException", "is not authorized to perform sts:AssumeRole", False),
        # A shape variant of the genuine article: the same failed assume, phrased the other
        # way round. Retryable, and the reason the allowlist pins a small closed SET of
        # shapes rather than one string.
        (
            "ValidationException",
            "AgentCore was unable to assume the provided execution role",
            True,
        ),
        # A throttle, the second allowlist entry, and the one whose code and operation are
        # the whole test: a throttle carries no cause worth matching.
        ("ThrottlingException", "Rate exceeded", True),
        # The live `AccessDenied` whose CloudTrail `errorMessage` was null. Unclassifiable,
        # so terminal -- and it used to `raise` rather than report anything at all.
        ("AccessDenied", "", False),
        # The same code, a different cause. Retrying this ten times ends in a report
        # about a role problem that does not exist, while the real fault -- a bad image
        # tag -- goes unmentioned.
        ("ValidationException", "The container image could not be pulled from ECR", False),
        ("ValidationException", "agentRuntimeName failed to satisfy constraint", False),
        ("ResourceNotFoundException", "role not found", False),
        # A missing grant, which is permanent. This exact message cost 26 minutes and ended
        # in "the execution role was still not assumable": it contains "not authorized", so
        # it matched the propagation arm, while the real cause -- no
        # `bedrock-agentcore:CreateAgentRuntimeEndpoint` in the provisioner's policy -- was
        # sitting in the same sentence. Waiting never adds a statement to a policy.
        (
            "AccessDeniedException",
            "User: arn:aws:sts::123456789012:assumed-role/Provisioner/Provisioner is not "
            "authorized to perform: bedrock-agentcore:CreateAgentRuntimeEndpoint on "
            "resource: arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/* because "
            "no identity-based policy allows the "
            "bedrock-agentcore:CreateAgentRuntimeEndpoint action",
            False,
        ),
        # The other two shapes IAM uses for a permanent refusal.
        (
            "AccessDeniedException",
            "is not authorized to perform: sts:AssumeRole with an explicit deny in a "
            "service control policy",
            False,
        ),
        (
            "AccessDeniedException",
            "is not authorized to perform: iam:CreateRole because no permissions boundary "
            "allows the action",
            False,
        ),
        # The verbatim live eu-west-1 message, and the third time this report lied about the
        # cause. It contains the bare word "role", which is how it matched the propagation
        # arm: a permanent missing grant -- no `iam:CreateServiceLinkedRole` on the
        # provisioner's own policy -- was retried ten times over 26 minutes and then
        # reported as "the execution role was still not assumable", about a role that was
        # never the problem. Propagation is a role that cannot be *assumed*, and this
        # message never says so.
        (
            "ValidationException",
            "Failed creating service linked role. Please verify that the calling role has "
            "sufficient permissions to create a service linked role.",
            False,
        ),
        # AWS spells it both ways and it arrives under either code, so the predicate may not
        # be pinned to one spelling.
        (
            "AccessDeniedException",
            "Failed creating service-linked role for bedrock-agentcore.amazonaws.com",
            False,
        ),
        # The live message, and the row whose absence stopped provisioning altogether: the
        # service says "assumption", not "assume"/"assumed", so none of the six shapes above
        # matched it. Covered by the table as well as by the dedicated pin below, because a
        # shape this table does not exercise is a shape nobody notices losing.
        ("ValidationException", _LIVE_ROLE_VALIDATION_MESSAGE, True),
    ],
)
def test_only_an_allowlisted_failure_is_retried(
    aws: Any, monkeypatch: pytest.MonkeyPatch, code: str, message: str, transient: bool
) -> None:
    """The allowlist is the whole mechanism, and its default is *no label*. Every row that
    is `False` here establishes nothing, so `_create` reports it with the vendor's own words
    rather than treating it as retryable by omission because nobody had met it yet -- after
    spending the floor's single retry, which is a bound on that report arriving late, not a
    licence to retry to the cap. This predicate is called directly, so the floor changes no
    assertion here."""
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    err = client_error(code, message, operation="CreateAgentRuntime")
    assert (provisioner._transient_create_failure(err) is not None) is transient


def test_the_allowlist_is_keyed_on_the_operation_too(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CreateAgentRuntime` is the only call in this module issued moments after
    `CreateRole`, so it is the only IAM-propagation window that exists. The same message
    arriving from `UpdateAgentRuntime` means something else and must not inherit the
    budget."""
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    message = "Role arn:aws:iam::123456789012:role/rca/rca-cfg_abc123 cannot be assumed"
    assert (
        provisioner._transient_create_failure(
            client_error("ValidationException", message, operation="CreateAgentRuntime")
        )
        is not None
    )
    assert (
        provisioner._transient_create_failure(
            client_error("ValidationException", message, operation="UpdateAgentRuntime")
        )
        is None
    )


def test_the_live_role_validation_message_is_classified_as_propagation(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression pin, and the only test in this file that names a vendor string in full.
    `_LIVE_ROLE_VALIDATION_MESSAGE` is what AgentCore returned for two real registrations,
    and it matched none of the six `assume`/`assumed` shapes, so every `POST /configurations`
    was declared permanently failed on its first attempt -- provisioning was simply gone.
    A paraphrase is what let that through, so the string is asserted character for
    character."""
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    err = client_error(
        "ValidationException", _LIVE_ROLE_VALIDATION_MESSAGE, operation="CreateAgentRuntime"
    )

    assert provisioner._transient_create_failure(err) is not None, (
        "the live message is IAM propagation and heals with time; not matching it is the "
        "outage this test exists to keep from returning"
    )
    # Not a collision with the *permanent* neighbour: the sentence contains "by this
    # service", which is not "service-linked role", and a match there would mean the shape
    # landed by accident and reported the wrong cause.
    assert provisioner._is_missing_service_linked_role(err) is False
    # And the shape did not widen the operation gate: only `CreateAgentRuntime` runs moments
    # after `CreateRole`, so only it has a propagation window to wait out.
    assert (
        provisioner._transient_create_failure(
            client_error(
                "ValidationException",
                _LIVE_ROLE_VALIDATION_MESSAGE,
                operation="UpdateAgentRuntime",
            )
        )
        is None
    )


def test_a_genuine_unassumable_role_still_retries_as_a_new_message(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 15. A raised exception returns the message to the queue and burns one of
    three receives, so a configuration whose role takes four attempts to propagate would
    DLQ while perfectly healthy. A *new* message resets the receive count."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    control = FakeAgentCore(
        "bedrock-agentcore-control",
        errors={
            "CreateAgentRuntime": [
                client_error(
                    "ValidationException",
                    "Role arn:aws:iam::123456789012:role/rca/rca-cfg_abc123 cannot be assumed by"
                    " bedrock-agentcore.amazonaws.com",
                    operation="CreateAgentRuntime",
                )
            ]
        },
    )
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    # Returns normally: the current message is deleted, and the retry is the new one. No
    # `retryDeadline` in the body, so this receive starts the budget.
    _run(provisioner, "CREATE", attempt=2)

    (body,) = _bodies(sent)
    assert body["phase"] == "CREATE"
    assert body["configId"] == _CONFIG_ID
    assert body["attempt"] == 3
    assert sent[0]["DelaySeconds"] == provisioner._RETRY_DELAY_SECONDS, (
        "a fixed step, not a doubling one"
    )
    # The budget travels with the message, so it is 90 seconds of real time across however
    # many receives that takes rather than 90 seconds each time round.
    fresh = provisioner._now_epoch() + provisioner._TRANSIENT_BUDGET_SECONDS
    assert abs(body["retryDeadline"] - fresh) <= 2, body
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == "PENDING", "no status write: the configuration is not failed yet"
    # The role was minted before the runtime, and the retry reuses it.
    assert provisioner._role_name(_CONFIG_ID) in json.dumps(
        common.iam.list_roles(PathPrefix=provisioner.ROLE_PATH), default=str
    )


def test_an_image_failure_is_not_retried_as_propagation(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminal because of the *positive* allowlist and nothing else: "could not be pulled"
    is not "could not assume", so there is no `ecr`/`image`/`container` exclusion list
    behind this and none to forget to extend. The report is AWS's own sentence, which names
    the bad image tag the wrapper used to talk over.

    Driven at `attempt=1` because the allowlist's floor spends one retry on any unrecognised
    `CreateAgentRuntime` failure first: the verdict is identical, it arrives one attempt
    later. Terminal once that single retry is spent."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    control = FakeAgentCore(
        "bedrock-agentcore-control",
        errors={
            "CreateAgentRuntime": [
                client_error(
                    "ValidationException",
                    "The container image in ECR could not be pulled by the execution role",
                    operation="CreateAgentRuntime",
                )
            ]
        },
    )
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE", attempt=1)

    assert sent == [], "permanent: a bad image tag does not heal while a Lambda waits"
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.FAILED
    assert "could not be pulled" in stored["statusReason"], "AWS's own words, not a paraphrase"
    assert "assumable" not in stored["statusReason"]


def test_a_missing_endpoint_grant_is_terminal_and_quotes_aws(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first of the three live failures, and the one whose true cause was in the same
    sentence as the wrong conclusion: `... because no identity-based policy allows the
    bedrock-agentcore:CreateAgentRuntimeEndpoint action`. It contained "not authorized", so
    the old keyword match retried it ten times over 26 minutes and then reported an
    execution role that was never the problem. Waiting never adds a statement to a policy.

    Driven at `attempt=1`: the allowlist's floor spends one retry on an unrecognised
    `CreateAgentRuntime` failure before the verdict, so this is terminal once that single
    retry is spent rather than on the very first attempt. The report is unchanged."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    control = FakeAgentCore(
        "bedrock-agentcore-control",
        errors={
            "CreateAgentRuntime": [
                client_error(
                    "AccessDeniedException",
                    "User: arn:aws:sts::123456789012:assumed-role/Provisioner/Provisioner is not "
                    "authorized to perform: bedrock-agentcore:CreateAgentRuntimeEndpoint on "
                    "resource: arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/* because "
                    "no identity-based policy allows the "
                    "bedrock-agentcore:CreateAgentRuntimeEndpoint action",
                    operation="CreateAgentRuntime",
                )
            ]
        },
    )
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE", attempt=1)

    assert sent == [], "terminal once the floor's one retry is spent: there is nothing to wait for"
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.FAILED
    reason = stored["statusReason"]
    # The vendor's own sentence, plus the code and the operation that produced it, so the
    # operator reads what AWS said rather than what this module guessed.
    assert "no identity-based policy allows" in reason
    assert "CreateAgentRuntimeEndpoint" in reason
    assert "AccessDeniedException" in reason
    assert "CreateAgentRuntime" in reason
    assert "assumable" not in reason, "the code established no such thing"


def test_an_access_denied_with_no_message_still_writes_a_status_reason(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third live failure: an `AccessDenied` whose CloudTrail `errorMessage` was null,
    so `message` is `""`. This arm used to fall through to `raise`, which returned the
    message to the queue and eventually DLQ'd it with the item still `PENDING` and **no
    `statusReason` at all** -- the operator read the one field that matters and found
    nothing. The absence is now reported as an absence.

    Driven at `attempt=1`, because the allowlist's floor spends one retry on an unrecognised
    `CreateAgentRuntime` failure first; the reported absence is the same one attempt later."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    control = FakeAgentCore(
        "bedrock-agentcore-control",
        errors={
            "CreateAgentRuntime": [client_error("AccessDenied", "", operation="CreateAgentRuntime")]
        },
    )
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    # Neither raised nor retried, the floor already spent.
    _run(provisioner, "CREATE", attempt=1)

    assert sent == []
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.FAILED
    reason = stored["statusReason"]
    assert reason, "the whole point: PENDING with no reason at all is what this replaces"
    assert "AccessDenied" in reason
    assert "CreateAgentRuntime" in reason
    assert "assumable" not in reason, "an unclassifiable failure may not assert a cause"


def test_a_missing_service_linked_role_is_terminal_and_reported_as_itself(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live eu-west-1 failure, and both things that were wrong about it. A
    `networkMode: VPC` runtime (invariant 30) needs a service-linked role AgentCore creates
    during `CreateAgentRuntime`, and a provisioner without `iam:CreateServiceLinkedRole`
    cannot make it, which is permanent. It was retried to the cap anyway, because the
    message contains the bare word "role", and then reported as an execution-role problem.
    So: no retry on the *first* attempt, and a `statusReason` naming the real cause where
    the API and the console already read it."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    control = FakeAgentCore(
        "bedrock-agentcore-control",
        errors={
            "CreateAgentRuntime": [
                client_error(
                    "ValidationException",
                    "Failed creating service linked role. Please verify that the calling role "
                    "has sufficient permissions to create a service linked role.",
                    operation="CreateAgentRuntime",
                )
            ]
        },
    )
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    # Neither raised nor retried: a raise DLQs the message and leaves the configuration in
    # `PENDING` with no reason at all, which reads to an operator as a stuck provision.
    _run(provisioner, "CREATE")

    assert sent == [], "permanent: waiting never adds a statement to a policy"
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.FAILED
    assert "service-linked role" in stored["statusReason"]
    assert "iam:CreateServiceLinkedRole" in stored["statusReason"], (
        "the report has to name the grant that is missing, which is the only actionable thing in it"
    )
    assert "assumable" not in stored["statusReason"], (
        "the execution role was never the problem: this is the third incident where the "
        "wrapper contradicted a message that carried the true cause the whole time"
    )


def _unrecognised_control() -> FakeAgentCore:
    """A `CreateAgentRuntime` failing with a phrasing the allowlist does not name. `errors`
    lists pop, so each invocation under test needs its own."""
    return FakeAgentCore(
        "bedrock-agentcore-control",
        errors={
            "CreateAgentRuntime": [
                client_error(
                    "ValidationException", _UNENUMERATED_MESSAGE, operation="CreateAgentRuntime"
                )
            ]
        },
    )


def test_an_unrecognised_create_failure_is_retried_exactly_once(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor on the allowlist, both halves. The live `assumption` message was not on the
    list and provisioning stopped entirely, so the question this pins is "what does the NEXT
    unenumerated phrasing cost?": one retry step, never an immediate permanent failure. And
    exactly one -- an unclassified failure does not get the whole budget, or the allowlist's
    default would be retry again, which is the defect it replaced."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    install(monkeypatch, common, control=_unrecognised_control())
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE")

    # One retry, as a new message (invariant 15), carrying the same wall-clock budget the
    # allowlisted failures get -- the floor borrows `_retry_create` rather than inventing a
    # second retry path that could outlive the deadline.
    (body,) = _bodies(sent)
    assert body["phase"] == "CREATE"
    assert body["configId"] == _CONFIG_ID
    assert body["attempt"] == 1
    assert sent[0]["DelaySeconds"] == provisioner._RETRY_DELAY_SECONDS
    fresh = provisioner._now_epoch() + provisioner._TRANSIENT_BUDGET_SECONDS
    assert abs(body["retryDeadline"] - fresh) <= 2, body
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == "PENDING", "the floor writes no verdict: nothing is decided yet"

    # The second half. A fresh fake because `errors` lists pop and each `_run` is a separate
    # invocation; the same list cleared rather than a second spy, because a second `_spy_sqs`
    # would wrap the first and record into both.
    install(monkeypatch, common, control=_unrecognised_control())
    sent.clear()

    _run(provisioner, "CREATE", attempt=1)

    assert sent == [], "exactly once: the floor is a floor, not a budget"
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.FAILED
    reason = stored["statusReason"]
    # Byte for byte the report this failure got before the floor existed, one attempt later.
    assert "ValidationException" in reason
    assert "CreateAgentRuntime" in reason
    assert _UNENUMERATED_MESSAGE in reason, "AWS's own words, not a paraphrase"
    assert "assumable" not in reason, "nothing was classified, so nothing may be claimed"


def test_the_floors_exhausted_deadline_report_claims_no_cause(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one path where the floor's own label reaches a `statusReason`: a replayed body
    whose `retryDeadline` is already spent, so `_retry_create` reports instead of enqueueing.
    The label is composed into the report, so it is the one string that could reintroduce the
    original lie -- a cause the code never established -- and it may not."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    install(monkeypatch, common, control=_unrecognised_control())
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE", retryDeadline=provisioner._now_epoch() - 1)

    assert sent == [], "the budget is spent, so the floor has nothing left to spend"
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.FAILED
    reason = stored["statusReason"]
    assert reason
    assert "assumable" not in reason, (
        "the allowlist recognised nothing here, so the report may say only that -- the word "
        "belongs to the entry that PROVED it"
    )
    assert _UNENUMERATED_MESSAGE in reason, "AWS's own words sit beside the label"


def test_the_floor_does_not_cover_a_permanent_iam_failure(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_ensure_role` runs inside the same `try` as `create_agent_runtime`, so an
    `iam:CreateRole` denial arrives at the same `except`. It is permanent -- waiting never
    adds a statement to a policy -- and must not borrow the floor's retry, which is why the
    floor is gated on the operation as well as on the attempt. Nothing covered that gate
    before, so removing it would otherwise be invisible."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)

    def denied(**_kwargs: Any) -> Any:
        raise client_error(
            "AccessDeniedException",
            "User: arn:aws:sts::123456789012:assumed-role/Provisioner/Provisioner is not "
            "authorized to perform: iam:CreateRole on resource: "
            "arn:aws:iam::123456789012:role/rca/rca-cfg_abc123",
            operation="CreateRole",
        )

    monkeypatch.setattr(common.iam, "create_role", denied)
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE")

    assert sent == [], "terminal on the first attempt: the floor speaks for CreateAgentRuntime only"
    # The property is that the *runtime* was never asked for -- the role does not exist. Not
    # an empty `calls` list: `_ensure_memory` runs before `_ensure_role`, so a `CreateMemory`
    # precedes the denial by design and asserting emptiness would fail for a reason that has
    # nothing to do with the floor.
    assert "CreateAgentRuntime" not in control.operations()
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.FAILED
    reason = stored["statusReason"]
    assert "iam:CreateRole" in reason
    assert "CreateRole" in reason
    assert "assumable" not in reason


def _unassumable_control() -> FakeAgentCore:
    """A `CreateAgentRuntime` that fails with the one message the allowlist pins as
    genuinely transient."""
    return FakeAgentCore(
        "bedrock-agentcore-control",
        errors={
            "CreateAgentRuntime": [
                client_error(
                    "ValidationException",
                    "the role cannot be assumed",
                    operation="CreateAgentRuntime",
                )
            ]
        },
    )


def test_the_transient_budget_is_wall_clock_and_names_what_it_established(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget is a deadline the message carries, so exhaustion is a `retryDeadline`
    already in the past rather than an attempt count. The report may say "assumable" here,
    and only here, because the allowlist *established* it -- and AWS's own sentence sits
    beside the label so the two can be compared instead of the wrapper being trusted."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    install(monkeypatch, common, control=_unassumable_control())
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE", retryDeadline=provisioner._now_epoch() - 1)

    assert sent == [], "the retry chain stops rather than looping forever"
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.FAILED
    reason = stored["statusReason"]
    assert str(provisioner._TRANSIENT_BUDGET_SECONDS) in reason, "a duration an operator can read"
    assert "assumable" in reason
    assert "cannot be assumed" in reason, "AWS's own words beside the label the allowlist proved"


def test_a_replayed_message_cannot_buy_an_unbounded_budget(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `retryDeadline` arriving in a message body is untrusted input, so it is clamped
    from above. A replayed or hand-edited body must not be able to buy a retry loop that
    outlives the incident."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    install(monkeypatch, common, control=_unassumable_control())
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE", retryDeadline=provisioner._now_epoch() + 10**6)

    (body,) = _bodies(sent)
    assert (
        body["retryDeadline"]
        <= provisioner._now_epoch() + provisioner._MAX_TRANSIENT_BUDGET_SECONDS
    )


def test_the_role_is_created_under_one_path_behind_a_boundary(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 17: the path, the boundary and a `PassRole` limited to
    `bedrock-agentcore.amazonaws.com` are the three fences on this Lambda's own
    `iam:CreateRole` grant. Asserted on the kwargs rather than through moto, which does
    not enforce a boundary."""
    boundary = "arn:aws:iam::123456789012:policy/rca-execution-boundary"
    _fleet(monkeypatch, EXECUTION_ROLE_BOUNDARY_ARN=boundary)
    _common, provisioner = _load("provisioner")
    kwargs = provisioner._role_kwargs(
        _CONFIG_ID, {"configId": _CONFIG_ID, "repository": "owner/repo"}
    )

    assert kwargs["Path"] == "/rca/"
    assert kwargs["PermissionsBoundary"] == boundary
    assert kwargs["RoleName"] == f"rca-{_CONFIG_ID}"
    trust = json.loads(kwargs["AssumeRolePolicyDocument"])
    (statement,) = trust["Statement"]
    assert statement["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    assert statement["Action"] == "sts:AssumeRole"
    # Invariant 16: `Tags` on `CreateRole` triggers a separate `TagRole` authorization.
    assert {"Key": "rca:configId", "Value": _CONFIG_ID} in kwargs["Tags"]


def test_the_trust_policy_fences_the_source_account_when_it_is_known(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The account comes off the configuration's own `credentialProviderArn` -- deliberately
    not from
    `sts:GetCallerIdentity`, which would widen this Lambda's grant surface for a value
    every provisioned configuration already carries."""
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    assert provisioner._account_and_region({"credentialProviderArn": _PROVIDER_ARN}) == (
        "123456789012",
        "ap-southeast-1",
    )
    condition = provisioner._trust_policy("123456789012")["Statement"][0]["Condition"]
    assert condition == {"StringEquals": {"aws:SourceAccount": "123456789012"}}

    # A half-written configuration still has to produce a policy IAM will accept, and a
    # literal `*` in `StringEquals` would deny every assume rather than allowing any.
    account, region = provisioner._account_and_region({})
    assert (account, region) == ("*", "ap-southeast-1")
    assert "Condition" not in provisioner._trust_policy("*")["Statement"][0]


def test_the_execution_role_policy_is_fenced_to_one_configuration(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 31 / Tier 1 #7 and #8. Without `dynamodb:LeadingKeys` a compromised
    container reads every other repository's rows, and writing another configuration's
    `verifyCommand` is remote code execution in that container."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    policy = provisioner._execution_role_policy(
        {"configId": _CONFIG_ID, "credentialProviderArn": _PROVIDER_ARN}
    )
    statements = {statement["Sid"]: statement for statement in policy["Statement"]}

    for sid in ("ReadOwnConfiguration", "OwnTasks"):
        condition = statements[sid]["Condition"]["ForAllValues:StringEquals"]
        assert condition["dynamodb:LeadingKeys"] == [_CONFIG_ID]
    # `GetItem` only on Configurations: the container never writes a configuration item,
    # and `verifyCommand` is a command this system runs.
    assert statements["ReadOwnConfiguration"]["Action"] == ["dynamodb:GetItem"]
    assert common.config_table.name in statements["ReadOwnConfiguration"]["Resource"]
    assert set(statements["OwnTasks"]["Action"]) == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
    }
    # The literal secret ARN, which is why rotation writes a new *version*.
    assert statements["UseOwnCredential"]["Resource"] == _PROVIDER_ARN
    assert statements["WriteRuntimeLogs"]["Resource"].startswith(
        "arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/runtimes"
    )

    # The runtime pulls `AGENT_IMAGE_URI` with this role, so the pull is scoped to the one
    # repository that URI names -- derived from it, never guessed.
    assert (
        statements["PullAgentImage"]["Resource"]
        == "arn:aws:ecr:ap-southeast-1:123456789012:repository/rca"
    )

    granted = json.dumps(policy)
    # The configuration this policy was built from carries no `memoryId`, so there is no
    # memory statement -- stated rather than left as a check that passes for a reason nobody
    # wrote down. The six data-plane memory actions a configuration *with* a `memoryId`
    # carries are asserted in tests/test_api_grant_contract.py, which builds from a fixture
    # that has one.
    assert "UseOwnMemory" not in statements
    for forbidden in ('"iam:', '"s3:'):
        assert forbidden not in granted, f"{forbidden} is not on the container's list"
    # The one AgentCore action this configuration does carry, on its own provider ARN. Named
    # here rather than covered by a prefix check, so a second AgentCore grant appearing in
    # this policy fails a test.
    assert statements["UseOwnCredential"]["Action"] == ["bedrock-agentcore:GetResourceApiKey"]
    assert statements["UseOwnCredential"]["Resource"] == _PROVIDER_ARN
    # `AuthenticateToEcr` is the *only* statement allowed a wildcard resource, because
    # `ecr:GetAuthorizationToken` supports no resource types and a scoped ARN there matches
    # no authorization at all. Every other statement names what it may touch, so the next
    # wildcard to land here fails this test rather than passing unnoticed.
    assert statements["AuthenticateToEcr"]["Resource"] == "*"
    for sid, statement in statements.items():
        if sid != "AuthenticateToEcr":
            assert statement["Resource"] != "*", f"{sid} may not carry a wildcard resource"
    assert all(statement["Effect"] == "Allow" for statement in policy["Statement"])


def test_the_bedrock_grant_is_not_region_scoped(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant 21: a `global.` inference profile resolves to a foundation model in
    whichever region serves the request, so a region in the model ARN denies the call
    from every other one -- and the failure surfaces as an `AccessDeniedException` from
    the CLI inside the container, nowhere near this policy."""
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    policy = provisioner._execution_role_policy(
        {"configId": _CONFIG_ID, "credentialProviderArn": _PROVIDER_ARN}
    )
    (invoke,) = [s for s in policy["Statement"] if s["Sid"] == "InvokeClaude"]
    assert "arn:aws:bedrock:*::foundation-model/anthropic.*" in invoke["Resource"]
    assert any(resource.endswith("inference-profile/*") for resource in invoke["Resource"])
    assert not any("ap-southeast-1" in resource for resource in invoke["Resource"])


def test_the_role_and_its_policy_are_rewritten_on_a_second_create(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create whose `CreateAgentRuntime` failed leaves the role behind, so `_ensure_role`
    has to tolerate `EntityAlreadyExists` -- and rewrites the inline policy so a
    fleet-wide policy change lands on a re-provision."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    config = _seed(common)
    first = provisioner._ensure_role(_CONFIG_ID, config)
    second = provisioner._ensure_role(_CONFIG_ID, config)

    assert first == second
    assert second == f"arn:aws:iam::123456789012:role/rca/rca-{_CONFIG_ID}"
    written = common.iam.get_role_policy(RoleName=f"rca-{_CONFIG_ID}", PolicyName="rca-execution")
    # botocore decodes an IAM policy document for some operations and not others.
    document = written["PolicyDocument"]
    document = json.loads(document) if isinstance(document, str) else document
    assert {statement["Sid"] for statement in document["Statement"]} >= {
        "InvokeClaude",
        "UseOwnCredential",
        "OwnTasks",
    }


# -- CHECK ------------------------------------------------------------------------


def test_check_returns_early_for_a_deleting_config(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `CHECK` already in flight when `DELETE /configurations/{id}` lands must not walk
    the item back to `READY` while the teardown is draining."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common, status=provisioner.DELETING, agentRuntimeId=_RUNTIME_ID)
    control = FakeAgentCore("bedrock-agentcore-control", runtimes={_RUNTIME_ID: runtime()})
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CHECK")

    assert control.calls == [], "the phase stops before it asks the service anything"
    assert sent == []
    stored = _status(common)
    assert stored is not None and stored["status"] == provisioner.DELETING


def test_a_late_check_cannot_resurrect_a_deleted_config(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 23, both halves. `update_item` *creates* an absent item, so a `CHECK`
    landing after a successful teardown would re-create a configuration whose runtime,
    role and secret are all gone -- and flip it to `READY`."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    control = FakeAgentCore("bedrock-agentcore-control", runtimes={_RUNTIME_ID: runtime()})
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CHECK")

    assert _status(common) is None
    assert control.calls == []
    assert sent == []

    # The conditional write itself, in case a future phase reaches it without the guard.
    provisioner._set_config(_CONFIG_ID, status=provisioner.READY, statusReason="")
    assert _status(common) is None


def test_check_walks_a_creating_runtime_to_ready(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common, status=provisioner.CREATING, agentRuntimeId=_RUNTIME_ID)
    live = runtime(agentRuntimeId=_RUNTIME_ID, status="CREATING")
    control = FakeAgentCore("bedrock-agentcore-control", runtimes={_RUNTIME_ID: live})
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CHECK", attempt=4)
    assert _bodies(sent) == [{"phase": "CHECK", "configId": _CONFIG_ID, "attempt": 5}]
    assert sent[0]["DelaySeconds"] == provisioner.CHECK_DELAY_SECONDS
    stored = _status(common)
    assert stored is not None and stored["status"] == provisioner.CREATING

    control.runtimes[_RUNTIME_ID] = runtime(
        agentRuntimeId=_RUNTIME_ID, status="READY", agentRuntimeVersion="3"
    )
    _run(provisioner, "CHECK", attempt=5)
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.READY
    assert stored["agentRuntimeVersion"] == "3"
    assert stored["statusReason"] == ""
    assert len(sent) == 1, "a READY runtime queues nothing further"


def test_check_stops_polling_at_the_cap_and_reports_a_service_failure(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common, status=provisioner.CREATING, agentRuntimeId=_RUNTIME_ID)
    control = FakeAgentCore(
        "bedrock-agentcore-control",
        runtimes={_RUNTIME_ID: runtime(agentRuntimeId=_RUNTIME_ID, status="CREATING")},
    )
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CHECK", attempt=provisioner.MAX_CHECK_ATTEMPTS)
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.FAILED
    assert str(provisioner.MAX_CHECK_ATTEMPTS) in stored["statusReason"]
    assert sent == []

    control.runtimes[_RUNTIME_ID] = runtime(
        agentRuntimeId=_RUNTIME_ID,
        status="CREATE_FAILED",
        failureReason="the container image could not be pulled",
    )
    _seed(common, status=provisioner.CREATING, agentRuntimeId=_RUNTIME_ID)
    _run(provisioner, "CHECK")
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.FAILED
    # The service's own reason, forwarded rather than replaced by "provisioning failed".
    assert stored["statusReason"] == "the container image could not be pulled"


def test_check_without_a_stored_runtime_id_fails_instead_of_polling_forever(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common, status=provisioner.CREATING)
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "CHECK")

    stored = _status(common)
    assert stored is not None and stored["status"] == provisioner.FAILED
    assert control.calls == []
    assert sent == []


# -- UPDATE -----------------------------------------------------------------------


def _ready(common: Any, control: FakeAgentCore, **live_overrides: Any) -> dict[str, Any]:
    """A `READY` configuration whose runtime is in the fake's store."""
    _seed(common, status="READY", agentRuntimeId=_RUNTIME_ID, containerUri=_OLD_IMAGE)
    live = runtime(agentRuntimeId=_RUNTIME_ID, **live_overrides)
    control.runtimes[_RUNTIME_ID] = live
    return live


def test_update_carries_forward_what_it_cannot_know(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 24. `UpdateAgentRuntime` is a full replace, so every member omitted is
    a member *stripped* from a live runtime -- which then starts and fails with no
    signal. The members this deployment does not own are read back off the live runtime
    and sent again."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    live = _ready(
        common,
        control,
        roleArn="arn:aws:iam::123456789012:role/rca/rca-cfg_abc123",
        description="hand-edited in the console, months ago",
        protocolConfiguration={"serverProtocol": "HTTP"},
        lifecycleConfiguration={"maxLifetime": 7200, "idleRuntimeSessionTimeout": 1800},
        environmentVariables={
            "SOMETHING_THIS_CODE_HAS_NEVER_HEARD_OF": "keep me",
            "CONFIG_ID": _CONFIG_ID,
            "WORK_ROOT": "/tmp/work",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "a model from two deploys ago",
        },
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": _OLD_IMAGE}},
    )
    _spy_sqs(monkeypatch, common)

    _run(provisioner, "UPDATE")
    call = control.last("UpdateAgentRuntime")

    # Carried forward: this code has no opinion about any of these.
    assert call["roleArn"] == live["roleArn"]
    assert call["description"] == live["description"]
    assert call["protocolConfiguration"] == live["protocolConfiguration"]
    assert call["lifecycleConfiguration"]["idleRuntimeSessionTimeout"] == 1800
    assert call["environmentVariables"]["SOMETHING_THIS_CODE_HAS_NEVER_HEARD_OF"] == "keep me"
    # Converged: these are fleet policy, and the live values are stale.
    assert call["lifecycleConfiguration"]["maxLifetime"] == 28800
    assert call["environmentVariables"]["WORK_ROOT"] == "/mnt/workspace/work"
    assert call["environmentVariables"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == _CODE_MODEL
    # `UpdateAgentRuntime` names the runtime by id and rejects a name -- the fake's
    # parameter validation is the real service model, so a name here is a hard error.
    assert call["agentRuntimeId"] == _RUNTIME_ID
    assert "agentRuntimeName" not in call


def test_update_converges_fleet_policy(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime that predates session storage and the 8-hour lifetime -- which is what
    both runtimes on the deploy this was measured against looked like -- has to *gain*
    them from an update, not keep what it has."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    live = _ready(
        common,
        control,
        lifecycleConfiguration={"maxLifetime": 7200},
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": _OLD_IMAGE}},
    )
    live.pop("filesystemConfigurations")
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "UPDATE")
    call = control.last("UpdateAgentRuntime")

    assert call["filesystemConfigurations"] == [{"sessionStorage": {"mountPath": "/mnt/workspace"}}]
    assert call["lifecycleConfiguration"] == {"maxLifetime": 28800}
    assert call["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == _IMAGE
    assert call["networkConfiguration"] == {"networkMode": "PUBLIC"}

    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.UPDATING
    assert stored["containerUri"] == _IMAGE
    assert stored["agentRuntimeVersion"] == "2"
    # An update leaves the runtime `UPDATING`, so the same `CHECK` phase watches it land.
    assert _bodies(sent) == [{"phase": "CHECK", "configId": _CONFIG_ID, "attempt": 0}]


def test_a_configured_vpc_is_fleet_policy_not_carry_forward(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 30: getting this backwards silently returns a runtime to public egress,
    which is invisible until something exfiltrates. Asserted on `_desired_runtime` rather
    than through a call, because what is under test is the classification."""
    _fleet(
        monkeypatch,
        AGENT_SUBNET_IDS="subnet-0aaa, subnet-0bbb",
        AGENT_SECURITY_GROUP_IDS="sg-0ccc",
    )
    _common, provisioner = _load("provisioner")
    assert provisioner._fleet_network(None) == {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": ["subnet-0aaa", "subnet-0bbb"],
            "securityGroups": ["sg-0ccc"],
        },
    }
    config = {"configId": _CONFIG_ID, "credentialProviderArn": _PROVIDER_ARN}
    live = runtime(agentRuntimeId=_RUNTIME_ID, networkConfiguration={"networkMode": "PUBLIC"})
    desired = provisioner._desired_runtime(live, config)
    assert desired["networkConfiguration"]["networkMode"] == "VPC"
    assert provisioner._matches_intent(live, desired) is False

    # The other half of "converged rather than carried forward": a runtime born in a VPC
    # that has since been re-laid-out loses its stale subnets. `PUBLIC` -> `VPC` alone
    # does not test that -- a carry-forward implementation would pass it for any runtime
    # that already had a `networkConfiguration` of the right shape.
    stale = runtime(
        agentRuntimeId=_RUNTIME_ID,
        networkConfiguration={
            "networkMode": "VPC",
            "networkModeConfig": {"subnets": ["subnet-stale"], "securityGroups": ["sg-stale"]},
        },
    )
    stale_desired = provisioner._desired_runtime(stale, config)
    assert stale_desired["networkConfiguration"]["networkModeConfig"]["subnets"] == [
        "subnet-0aaa",
        "subnet-0bbb",
    ]
    assert provisioner._matches_intent(stale, stale_desired) is False


def test_an_unset_subnet_list_never_moves_a_live_runtime_back_to_public_egress(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one silent path back to public egress, and the reason `_fleet_network` is a
    ratchet rather than a plain convergence: one deploy whose `AGENT_SUBNET_IDS` arrived
    empty -- a `select_subnets` that returned nothing, a hand-rolled deploy, stack drift
    -- would otherwise move every live runtime from VPC to `PUBLIC` on its next UPDATE.
    This test is the only thing between that and a deploy nobody would look twice at."""
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    config = {"configId": _CONFIG_ID, "credentialProviderArn": _PROVIDER_ARN}
    vpc = {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": ["subnet-0aaa", "subnet-0bbb"],
            "securityGroups": ["sg-0ccc"],
        },
    }
    live = runtime(agentRuntimeId=_RUNTIME_ID, networkConfiguration=vpc)
    live["environmentVariables"] = provisioner._desired_runtime(live, config)[
        "environmentVariables"
    ]

    desired = provisioner._desired_runtime(live, config)

    assert desired["networkConfiguration"] == vpc
    # Kept rather than converged, so the field is not drift either: an otherwise converged
    # runtime issues no update on account of it, and the update it does not issue is the
    # one that would have downgraded it.
    assert "networkConfiguration" not in {
        key for key in provisioner._INTENT_KEYS if live.get(key) != desired.get(key)
    }
    assert provisioner._matches_intent(live, desired) is True


def test_no_live_vpc_and_no_configured_subnets_is_public(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ratchet only ratchets up: a local or pre-VPC deployment still provisions
    `PUBLIC`, and a live runtime's own configuration is not a way to smuggle a VPC into a
    deployment that has none."""
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")

    assert provisioner._fleet_network(None) == {"networkMode": "PUBLIC"}
    assert provisioner._fleet_network({"networkConfiguration": {"networkMode": "PUBLIC"}}) == {
        "networkMode": "PUBLIC"
    }


def test_the_idempotence_guard_covers_the_converged_fields_not_the_image_alone(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured failure: an image-only guard short-circuits before convergence, so a
    runtime already on the current image never gains the mount or the 8-hour lifetime, no
    matter how many runtime updates are issued."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    live = _ready(
        common,
        control,
        lifecycleConfiguration={"maxLifetime": 7200},
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": _IMAGE}},
    )
    live.pop("filesystemConfigurations")
    config = _status(common)
    assert config is not None
    # Everything else converged, so the drift is exactly the two fields a runtime
    # provisioned before session storage and the 8-hour lifetime is missing.
    live["environmentVariables"] = provisioner._desired_runtime(live, config)[
        "environmentVariables"
    ]
    # `platformVersion` is an intent key too, and this assertion compares `_INTENT_KEYS`
    # raw rather than through `_matches_intent`. This deployment is V2 only, so a converged
    # runtime reports V2 -- spelling it keeps the drift set exactly the two fields this test
    # is about rather than three.
    live["platformVersion"] = "V2"
    desired = provisioner._desired_runtime(live, config)

    assert provisioner._container_uri(live) == provisioner._container_uri(desired), (
        "the images match, so only a guard wider than the image can see this drift"
    )
    assert {key for key in provisioner._INTENT_KEYS if live.get(key) != desired.get(key)} == {
        "lifecycleConfiguration",
        "filesystemConfigurations",
    }
    assert provisioner._matches_intent(live, desired) is False

    _spy_sqs(monkeypatch, common)
    _run(provisioner, "UPDATE")
    assert control.operations() == ["GetAgentRuntime", "UpdateAgentRuntime"]
    issued = control.last("UpdateAgentRuntime")
    assert issued["filesystemConfigurations"]
    assert issued["lifecycleConfiguration"]["maxLifetime"] == 28800


def test_each_converged_field_on_its_own_is_enough_to_issue_an_update(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: from a runtime that matches intent, drifting any single
    *fleet* field is seen. `roleArn`, `description` and `protocolConfiguration` are
    absent from this list on purpose -- they are carried forward, so they cannot drift."""
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    config = {
        "configId": _CONFIG_ID,
        "credentialProviderArn": _PROVIDER_ARN,
        "repository": "owner/repo",
    }

    converged = runtime(agentRuntimeId=_RUNTIME_ID)
    converged["environmentVariables"] = provisioner._desired_runtime(converged, config)[
        "environmentVariables"
    ]
    assert (
        provisioner._matches_intent(converged, provisioner._desired_runtime(converged, config))
        is True
    )

    for drifted in (
        {"lifecycleConfiguration": {"maxLifetime": 7200, "idleRuntimeSessionTimeout": 900}},
        {"filesystemConfigurations": []},
        # A stale PUBLIC shape, not a VPC one: with no `AGENT_SUBNET_IDS` set the ratchet
        # in `_fleet_network` deliberately keeps a live VPC configuration rather than
        # converging it down (invariant 30), so a live VPC config is no longer drift here.
        # The VPC direction is `test_a_configured_vpc_is_fleet_policy_not_carry_forward`.
        {"networkConfiguration": {"networkMode": "PUBLIC", "networkModeConfig": {"subnets": []}}},
        {"environmentVariables": {**converged["environmentVariables"], "WORK_ROOT": "/tmp/work"}},
        {"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": _OLD_IMAGE}}},
    ):
        stale = {**converged, **drifted}
        assert (
            provisioner._matches_intent(stale, provisioner._desired_runtime(stale, config)) is False
        ), drifted


def test_a_redelivery_of_a_converged_update_issues_no_call(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQS delivers at least once, and `POST /runtime-update` is a button an operator can
    press twice. Neither may roll a converged runtime, or every redelivery costs a new
    runtime version and parks the configuration in `UPDATING`."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    # Converged by construction: the runtime the fake stores is exactly what `CREATE`
    # asked for, which is what a runtime provisioned by this code looks like.
    _run(provisioner, "CREATE")
    runtime_id = _status(common)["agentRuntimeId"]
    control.runtimes[runtime_id]["status"] = "READY"
    _run(provisioner, "CHECK")
    assert _status(common)["status"] == provisioner.READY

    before = len(sent)
    _run(provisioner, "UPDATE")
    _run(provisioner, "UPDATE")

    assert control.calls_to("UpdateAgentRuntime") == []
    assert control.operations().count("GetAgentRuntime") == 3, "read back, compared, and left alone"
    assert len(sent) == before, "no CHECK queued for an update that never happened"
    stored = _status(common)
    assert stored is not None
    assert stored["status"] == provisioner.READY, "a converged fleet does not sit in UPDATING"


def test_update_of_a_configuration_with_no_runtime_yet_is_a_no_op(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common, status="READY")
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    _run(provisioner, "UPDATE")

    assert control.calls == []
    assert sent == []
    assert _status(common)["status"] == "READY"


def test_an_invalid_mount_path_is_refused_before_the_call(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MountPath`'s pattern is `/mnt/[a-zA-Z0-9._-]+/?` and botocore does not enforce a
    `pattern` client-side, so `/mnt/a/b` reaches the service as a bare
    `ValidationException`. The guard is on this module's own constant."""
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    monkeypatch.setattr(provisioner, "MOUNT_PATH", "/mnt/workspace/nested")
    with pytest.raises(ValueError, match="mount path"):
        provisioner._desired_runtime(
            None, {"configId": _CONFIG_ID, "credentialProviderArn": _PROVIDER_ARN}
        )


# -- DELETE -----------------------------------------------------------------------


def test_delete_tears_down_in_dependency_order_and_removes_the_item_last(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 22: this is the teardown path, not `cdk destroy` -- the tables are
    `RETAIN`, so nothing else removes a configuration's runtime, role or credential."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common)
    control = FakeAgentCore("bedrock-agentcore-control")
    control.credential_providers[_PROVIDER_NAME] = {
        "name": _PROVIDER_NAME,
        "apiKey": "github_pat_x",
    }
    install(monkeypatch, common, control=control)
    _spy_sqs(monkeypatch, common)

    _run(provisioner, "CREATE")
    # What `DELETE /configurations/{configId}` does before it queues this phase. An
    # update rather than a re-`put_item`, because the runtime id the create just stored
    # is what teardown needs.
    provisioned = _status(common)
    assert provisioned is not None and provisioned["agentRuntimeId"]
    common.config_table.put_item(Item={**provisioned, "status": provisioner.DELETING})
    _run(provisioner, "DELETE")

    assert "DeleteAgentRuntime" in control.operations()
    assert "DeleteMemory" in control.operations()
    # The memory goes after the runtime, so nothing is still reading it when it disappears --
    # and, like the runtime, before the item that records its id.
    operations = control.operations()
    assert operations.index("DeleteMemory") > operations.index("DeleteAgentRuntime"), (
        f"the memory is torn down after the runtime that uses it, got {operations}"
    )
    assert control.last("DeleteMemory")["memoryId"] == provisioned["memoryId"]
    assert control.runtimes == {}
    assert control.memories == {}
    assert common.iam.list_roles(PathPrefix=provisioner.ROLE_PATH)["Roles"] == []
    # The credential goes with its provider, and so does the service-linked Secrets Manager
    # secret Identity keeps underneath -- which is why nothing here names a secret and why
    # `api/` holds no `secretsmanager:DeleteSecret` grant.
    assert control.credential_providers == {}
    assert _status(common) is None, "the item goes last, so a crash mid-teardown is resumable"


def test_teardown_is_replayable_when_the_runtime_is_already_gone(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `DELETING` configuration whose runtime, role and credential are already gone is the
    normal state after a crash mid-teardown. The second pass must finish, not fail.

    The fake answers `GetAgentRuntime` with `ResourceNotFoundException` and
    `DeleteAgentRuntime` with **`AccessDeniedException`**, which is what the live service
    does for a runtime that no longer exists -- measured, and with administrator credentials,
    so it is the service's answer for a missing resource and not a statement about the
    caller. It is also indistinguishable from a real missing grant, which is why `_is_gone`
    must never accept that code: doing so would turn an absent permission into a teardown
    that looks successful and leaves a live runtime billing.

    So the delete is guarded by `_runtime_exists`, and the assertion is that
    `DeleteAgentRuntime` is **never called** on this path. Without the guard the item is
    never deleted, the configuration stays `DELETING` forever, and the watchdog's
    configuration sweep re-drives the same `DELETE` into the same failure.
    """
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common, status=provisioner.DELETING, agentRuntimeId=_RUNTIME_ID, memoryId=_MEMORY_ID)
    control = FakeAgentCore(
        "bedrock-agentcore-control",
        errors={
            "GetAgentRuntime": [
                client_error(
                    "ResourceNotFoundException",
                    "runtime not found",
                    status=404,
                    operation="GetAgentRuntime",
                )
            ],
            "DeleteAgentRuntime": [
                client_error(
                    "AccessDeniedException",
                    "is not authorized to perform: bedrock-agentcore:DeleteAgentRuntime",
                    status=403,
                    operation="DeleteAgentRuntime",
                )
            ],
            # `ResourceNotFoundException` is the only code `DeleteMemory` uses for "already
            # gone", so it is the only one a replay may swallow there.
            "DeleteMemory": [
                client_error(
                    "ResourceNotFoundException",
                    "memory not found",
                    status=404,
                    operation="DeleteMemory",
                )
            ],
        },
    )
    install(monkeypatch, common, control=control)

    _run(provisioner, "DELETE")
    assert _status(common) is None, (
        "the item must be deleted even though the runtime was already gone -- otherwise the "
        "configuration is stuck DELETING with nothing able to finish it"
    )
    assert "DeleteAgentRuntime" not in control.operations(), (
        "the existence probe must stop the delete: on a missing runtime the service answers "
        "AccessDenied, which _is_gone rightly refuses to swallow"
    )

    # And a redelivery of the same message, with the item now gone.
    _run(provisioner, "DELETE")
    assert control.operations() == [
        "GetAgentRuntime",
        "DeleteMemory",
        "DeleteApiKeyCredentialProvider",
    ], "the second pass asked nothing"


def test_a_permission_failure_on_the_runtime_delete_still_raises(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same decision, and the reason `_is_gone` was not widened.

    Here the runtime *does* exist and the delete is genuinely denied. That must raise and
    leave the item in place, so the message is redelivered and an operator eventually reads a
    DLQ -- rather than completing quietly and abandoning a runtime that keeps billing.
    """
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    _seed(common, status=provisioner.DELETING, agentRuntimeId=_RUNTIME_ID)
    control = FakeAgentCore(
        "bedrock-agentcore-control",
        runtimes={_RUNTIME_ID: runtime(agentRuntimeId=_RUNTIME_ID, status="READY")},
        errors={
            "DeleteAgentRuntime": [
                client_error(
                    "AccessDeniedException",
                    "is not authorized to perform: bedrock-agentcore:DeleteAgentRuntime",
                    status=403,
                    operation="DeleteAgentRuntime",
                )
            ]
        },
    )
    install(monkeypatch, common, control=control)

    with pytest.raises(ClientError):
        _run(provisioner, "DELETE")
    assert _status(common) is not None, "the item stays, so the teardown is retried"


# -- the entry point --------------------------------------------------------------


def test_enqueue_owns_the_message_shape_and_refuses_a_phase_it_does_not_own(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    sent = _spy_sqs(monkeypatch, common)

    provisioner.enqueue(_CONFIG_ID, "CREATE", attempt=2, delay=45)
    assert _bodies(sent) == [{"phase": "CREATE", "configId": _CONFIG_ID, "attempt": 2}]
    assert sent[0]["QueueUrl"] == common.PROVISION_QUEUE_URL
    assert sent[0]["DelaySeconds"] == 45

    # SQS caps a per-message delay at 900s; a backoff change must not become an
    # `InvalidParameterValue` nobody expects.
    provisioner.enqueue(_CONFIG_ID, "CHECK", delay=4000)
    assert sent[1]["DelaySeconds"] == 900

    with pytest.raises(ValueError, match="not a provisioning phase"):
        provisioner.enqueue(_CONFIG_ID, "PROVISION")
    assert len(sent) == 2

    # `retryDeadline` is added only when a caller passes a deadline, and it adds nothing
    # else. `tests/test_api_configurations.py` asserts provision bodies with whole-dict
    # equality, so an unconditional member would break assertions in a file this change
    # does not own -- and the two bodies above are the same shape for the same reason.
    provisioner.enqueue(_CONFIG_ID, "CREATE", attempt=1, deadline=1799999999)
    assert _bodies(sent)[2] == {
        "phase": "CREATE",
        "configId": _CONFIG_ID,
        "attempt": 1,
        "retryDeadline": 1799999999,
    }
    assert "retryDeadline" not in _bodies(sent)[0]


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "[1, 2]",
        "null",
        json.dumps({"phase": "MIGRATE", "configId": _CONFIG_ID}),
        json.dumps({"phase": "CREATE"}),
        "",
    ],
)
def test_a_message_this_function_cannot_read_is_dropped_not_redelivered(
    aws: Any, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """Redelivering a message that cannot parse cannot make it parse, so it is dropped
    rather than sent round the redrive loop three times. Anything else raises, because
    SQS redelivery is the retry and the DLQ is the report."""
    _fleet(monkeypatch)
    common, provisioner = _load("provisioner")
    control = FakeAgentCore("bedrock-agentcore-control")
    install(monkeypatch, common, control=control)
    sent = _spy_sqs(monkeypatch, common)

    provisioner.lambda_handler({"Records": [{"messageId": "m", "body": body}]}, None)

    assert control.calls == []
    assert sent == []


def test_the_entry_point_is_not_an_http_route(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no API Gateway integration here, so `lambda_handler` is undecorated: a
    `@handler` would turn an unhandled provisioning failure into a `500` response body
    that SQS would treat as a *success* and delete the message."""
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    assert not hasattr(provisioner.lambda_handler, "__api_handler__")
    assert not hasattr(provisioner, "ROUTES")
    assert provisioner.PHASES == ("CREATE", "CHECK", "DELETE", "UPDATE")


# -- platform version (AgentCore Runtime V2) --------------------------------------------


def test_this_deployment_is_v2_only_and_says_so_without_an_environment_variable(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PLATFORM_VERSION` is a constant, not a knob, and `V2` reaches both calls.

    Asserted as a constant deliberately: a platform version is not a per-deployment taste.
    V1 and V2 differ in how a container is *started*, so offering both would mean the
    120-second health requirement holds on one path and not the other, and every failure
    report would have to say which path produced it. There is also no environment variable
    to set -- if one reappears, this test is where that shows up.
    """
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    assert provisioner.PLATFORM_VERSION == "V2"
    config = {
        "configId": _CONFIG_ID,
        "credentialProviderArn": _PROVIDER_ARN,
        "repository": "owner/repo",
    }
    assert provisioner._desired_runtime(None, config)["platformVersion"] == "V2"

    # And it cannot be talked out of it from the environment.
    monkeypatch.setenv("AGENT_PLATFORM_VERSION", "V1")
    _common, reloaded = _load("provisioner")
    assert reloaded.PLATFORM_VERSION == "V2"


def test_the_platform_version_is_fleet_policy_and_is_sent_on_create_and_update(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`V2` reaches an update **even when the live runtime reports V1**.

    Sent always rather than only when it differs, because the service's omission semantics
    are asymmetric: omitted on create means `V1`, but omitted on *update* means "keep the
    current platform version". A conditional send would therefore be one-way -- a fleet that
    reached V2 could never be brought back, and clearing the deploy-time key would pin
    whatever each runtime happened to have rather than restoring the default.
    """
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    config = {
        "configId": _CONFIG_ID,
        "credentialProviderArn": _PROVIDER_ARN,
        "repository": "owner/repo",
    }

    assert provisioner._desired_runtime(None, config)["platformVersion"] == "V2"

    live = runtime(agentRuntimeId=_RUNTIME_ID)
    live["platformVersion"] = "V1"
    assert provisioner._desired_runtime(live, config)["platformVersion"] == "V2"
    assert provisioner._matches_intent(live, provisioner._desired_runtime(live, config)) is False, (
        "a V1 runtime under a V2 fleet is drift, or the fleet never converges"
    )


def test_a_runtime_that_reports_no_platform_version_is_drift_and_converges_once(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured, not assumed: every runtime in the live fleet answered `GetAgentRuntime`
    with `platformVersion` **absent**, because they predate the member.

    Absence is the service's default, which is V1, so in a V2-only deployment such a runtime
    is drift and one update moves it -- the same convergence any other fleet field gets
    (invariant 24). The reason `_matches_intent` normalises absence to `V1` rather than
    treating it as "unknown" is that it makes the convergence happen **exactly once**: after
    the update the runtime reports V2 and matches, instead of being updated on every
    reconcile forever, which under V2 would be a snapshot rebuild each time.
    """
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    config = {
        "configId": _CONFIG_ID,
        "credentialProviderArn": _PROVIDER_ARN,
        "repository": "owner/repo",
    }

    pre_v2 = runtime(agentRuntimeId=_RUNTIME_ID)
    pre_v2.pop("platformVersion")
    pre_v2["environmentVariables"] = provisioner._desired_runtime(pre_v2, config)[
        "environmentVariables"
    ]
    assert "platformVersion" not in pre_v2
    assert (
        provisioner._matches_intent(pre_v2, provisioner._desired_runtime(pre_v2, config)) is False
    )

    # What the update writes, and that it then settles.
    converged = {**pre_v2, "platformVersion": "V2"}
    assert (
        provisioner._matches_intent(converged, provisioner._desired_runtime(converged, config))
        is True
    )


def test_a_vendor_only_network_member_is_not_drift(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`requireServiceS3Endpoint` comes back on every VPC runtime and can never be sent.

    This is the shape read off a live runtime. `_fleet_network` does not emit that member --
    it is a `ValidationException` on `CreateAgentRuntime`, and on `UpdateAgentRuntime` for
    any runtime created after the May 2026 rollout -- so whole-object equality on
    `networkConfiguration` could never match and every reconcile issued an update. Under V2
    each of those is a snapshot rebuild, so this is the difference between a converged fleet
    and one that rebuilds itself on every redelivery.
    """
    subnets = "subnet-0aaa,subnet-0bbb"
    _fleet(monkeypatch, AGENT_SUBNET_IDS=subnets, AGENT_SECURITY_GROUP_IDS="sg-0ccc")
    _common, provisioner = _load("provisioner")
    config = {
        "configId": _CONFIG_ID,
        "credentialProviderArn": _PROVIDER_ARN,
        "repository": "owner/repo",
    }

    live = runtime(agentRuntimeId=_RUNTIME_ID)
    live["networkConfiguration"] = {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": ["subnet-0aaa", "subnet-0bbb"],
            "securityGroups": ["sg-0ccc"],
            "requireServiceS3Endpoint": False,
        },
    }
    live["environmentVariables"] = provisioner._desired_runtime(live, config)[
        "environmentVariables"
    ]
    desired = provisioner._desired_runtime(live, config)
    assert "requireServiceS3Endpoint" not in desired["networkConfiguration"]["networkModeConfig"]
    assert provisioner._matches_intent(live, desired) is True, (
        "a member only the service may set must not read as drift"
    )


def test_a_stale_member_this_code_wants_removed_is_still_drift(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the previous test, and the reason it enumerates one member rather
    than ignoring everything `desired` omits.

    A blanket "keys `desired` does not state are not a difference" rule would also stop
    seeing a member this code wants *gone*. `networkModeConfig` on a `PUBLIC` runtime is
    exactly that, and it stays visible.
    """
    _fleet(monkeypatch)
    _common, provisioner = _load("provisioner")
    config = {
        "configId": _CONFIG_ID,
        "credentialProviderArn": _PROVIDER_ARN,
        "repository": "owner/repo",
    }

    live = runtime(agentRuntimeId=_RUNTIME_ID)
    live["networkConfiguration"] = {
        "networkMode": "PUBLIC",
        "networkModeConfig": {"subnets": []},
    }
    live["environmentVariables"] = provisioner._desired_runtime(live, config)[
        "environmentVariables"
    ]
    assert provisioner._matches_intent(live, provisioner._desired_runtime(live, config)) is False


def test_a_retired_fleet_variable_is_removed_from_a_live_runtime(
    aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a key from `_fleet_environment` does not remove it from a runtime.

    `_desired_runtime` merges `{**create-time, **live, **fleet}` so that a key this version of
    the code has never heard of survives an update (invariant 24). The mirror of that rule is
    that a key this code has *stopped* sending is carried forward for the rest of the runtime's
    life -- it stops being converged rather than being removed.

    Measured rather than reasoned about: removing the five `OTEL_*` exporter variables with the
    metrics collector (invariant 32b) deployed cleanly, a `runtime-update` converged
    `CLAUDE_CODE_ENABLE_TELEMETRY` from `1` to `0`, and every one of the five `OTEL_*` names was
    still on the live runtime afterwards. Nothing failed, and invariant 32b's own claim that no
    `OTEL_*` variable is set anywhere was false for every runtime that predated the removal.

    `RETIRED_ENVIRONMENT_KEYS` is the tombstone list that fixes it, and this is what holds it: a
    retired name is removed, while an unknown name beside it is still carried forward, because
    the second half is what stops the fix becoming a blanket "drop what the fleet does not
    send" rule -- which would silently discard per-configuration keys too.
    """
    _common, provisioner = _load("provisioner")

    live = {
        "agentRuntimeId": "rca_cfg_abc-XYZ",
        "environmentVariables": {
            # The retired group, as a runtime provisioned before the removal still carries it.
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "none",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
            "OTEL_METRIC_EXPORT_INTERVAL": "10000",
            # Still fleet policy, and converged by being sent rather than by being dropped.
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            # A key this code has never heard of: the carry-forward rule must still hold.
            "SOME_FUTURE_KNOB": "carried",
        },
    }
    desired = provisioner._desired_runtime(live, {"configId": "cfg_abc", "repository": "o/r"})
    environment = desired["environmentVariables"]

    for name in provisioner.RETIRED_ENVIRONMENT_KEYS:
        assert name not in environment, (
            f"{name} is in RETIRED_ENVIRONMENT_KEYS and must be removed from the runtime, not merely "
            f"absent from the fleet group -- `UpdateAgentRuntime` is a full replace, so a key "
            f"left in this dict is a key the runtime keeps"
        )
    assert environment["CLAUDE_CODE_ENABLE_TELEMETRY"] == "0", (
        "the exporter switch is still fleet policy and converges to 0 by being sent; retiring "
        "it instead would leave a runtime with no statement about telemetry at all"
    )
    assert environment["SOME_FUTURE_KNOB"] == "carried", (
        "invariant 24's carry-forward rule still holds for everything not retired: a blanket "
        "'drop what the fleet does not send' would also discard per-configuration keys"
    )
