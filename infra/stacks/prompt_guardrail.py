"""The prompt guardrail `runs.screen_prompt` applies to every task prompt (invariant 43).

A module rather than a stack, and applied **inside** `ApiStack`, its only consumer. It used
to live in `DataStack`, and that was measured to be a deploy that breaks on its second
change: `ApiStack` imports the guardrail's version, CloudFormation refuses to delete an
export another stack imports, and a policy change publishes a new version -- so the producer
rolled back with "Cannot delete export ... as it is in use by" the consumer. In one stack the
new version is created, the Lambdas move to it, and the old one is removed in cleanup.

A module and not a method on `ApiStack` because `ApiStack` bundles `../api` through Docker
at construction, so no test can synthesize it; `add_prompt_guardrail` applied to a bare
stack is what `tests/test_prompt_guardrail_stack.py` checks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_bedrock as bedrock

# The cross-region guardrail profile each source region may use, from "Supported Regions for
# cross-Region guardrail inference" in the Bedrock user guide. A Standard-tier guardrail --
# the tier whose filters understand code -- cannot be created without one.
#
# A table and not a rule on the region's prefix, because the prefix is wrong in exactly the
# places that matter: `eu-west-2` is `uk.`, `ap-southeast-2` is `au.`, `ca-central-1` is
# `ca.`, and `il-central-1`/`me-central-1` sit in `eu.`/`apac.`. Where a region is a source
# for two profiles the narrower geography is chosen, because residency is the reason this
# deployment pins geographies at all. A region missing here fails the synth with the fix,
# the way `agent_stack.AGENTCORE_VPC_ZONE_IDS` does.
GUARDRAIL_PROFILES: dict[str, str] = {
    **dict.fromkeys(("us-east-1", "us-east-2", "us-west-1", "us-west-2"), "us.guardrail.v1:0"),
    **dict.fromkeys(
        (
            "eu-central-1",
            "eu-west-1",
            "eu-west-3",
            "eu-north-1",
            "eu-south-1",
            "eu-south-2",
            "il-central-1",
        ),
        "eu.guardrail.v1:0",
    ),
    "eu-west-2": "uk.guardrail.v1:0",
    "ap-southeast-2": "au.guardrail.v1:0",
    "ca-central-1": "ca.guardrail.v1:0",
    **dict.fromkeys(
        (
            "ap-south-1",
            "ap-northeast-1",
            "ap-northeast-2",
            "ap-southeast-1",
            "ap-southeast-3",
            "ap-southeast-4",
            "ap-southeast-5",
            "ap-southeast-7",
            "ap-east-2",
            "me-central-1",
        ),
        "apac.guardrail.v1:0",
    ),
}

# The text a refused prompt is answered with. `ApplyGuardrail` returns it as the output, and
# `runs.screen_prompt` never shows it -- it keys on `action` -- so it is a marker for anyone
# reading a raw response, not a message an operator sees.
GUARDRAIL_BLOCKED_MESSAGE = "RCA_PROMPT_GUARDRAIL_BLOCKED"

# What a pasted credential looks like in a task prompt. The two AWS key types are built-in
# PII entities; these are the GitHub and private-key shapes, which are not. A refused prompt
# is never stored, so a secret caught here reaches neither the Tasks table nor the model.
GUARDRAIL_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("GITHUB_TOKEN", r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    ("GITHUB_FINE_GRAINED_TOKEN", r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"),
    ("PRIVATE_KEY", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


@dataclass(frozen=True)
class PromptGuardrail:
    """What `ApiStack` needs from the guardrail: the ARN the handlers name, the published
    version they pin, and the profile the `guardrail:apply` grant has to cover."""

    guardrail: bedrock.CfnGuardrail
    version: str
    profile_id: str

    @property
    def arn(self) -> str:
        return self.guardrail.attr_guardrail_arn


def guardrail_profile_for(region: str | None, override: str = "") -> str:
    """The guardrail profile id for `region`, or `override` when one was passed."""
    if override:
        return override
    profile = GUARDRAIL_PROFILES.get(region or "")
    if profile is None:
        raise ValueError(
            f"no cross-region guardrail profile is known for region {region!r}, and the "
            "prompt guardrail's Standard tier needs one. Pass -c guardrailProfile=<id> if "
            "Bedrock lists one for this region, or -c guardrailEnabled=false to deploy "
            "without prompt screening."
        )
    return profile


def guardrail_properties(profile_arn: str) -> dict[str, Any]:
    """The prompt guardrail's policies: only the ones about a coding task (invariant 43).

    Input only, everywhere. This guardrail screens the prompt an operator submits and is
    never attached to a model call, so an output setting would be configuration nothing
    exercises. Hate, insults, sexual content, violence, other PII, word lists, denied
    topics and grounding are left off: on a coding prompt they are false positives
    (`kill -9`, an author's email address) rather than protection.
    """
    return {
        "blocked_input_messaging": GUARDRAIL_BLOCKED_MESSAGE,
        "blocked_outputs_messaging": GUARDRAIL_BLOCKED_MESSAGE,
        "content_policy_config": bedrock.CfnGuardrail.ContentPolicyConfigProperty(
            # Standard is the tier whose classifiers read code -- comments, identifiers,
            # string literals -- rather than treating a prompt as prose.
            content_filters_tier_config=bedrock.CfnGuardrail.ContentFiltersTierConfigProperty(
                tier_name="STANDARD"
            ),
            # LOW on both, and that is measured rather than lenient. Against the live
            # Standard tier, LOW refused every attack in the probe set (two injections, a
            # keylogger, ransomware, a credential stealer) and none of ten ordinary tasks.
            # One step up started refusing legitimate work: PROMPT_ATTACK at MEDIUM refused
            # "Ignore all previous lint warnings in legacy/ ...", and MISCONDUCT at MEDIUM
            # refused "kill the child process with SIGKILL", "fix the SQL injection", "escape
            # HTML to prevent XSS" and "add a fuzz test ... hostile inputs". A coding agent's
            # security tasks are exactly the ones a stricter setting refuses.
            filters_config=[
                # Jailbreak, prompt injection and prompt leakage. The output strength must be
                # NONE for this filter type; the service rejects anything else.
                bedrock.CfnGuardrail.ContentFilterConfigProperty(
                    type="PROMPT_ATTACK",
                    input_strength="LOW",
                    output_strength="NONE",
                    input_action="BLOCK",
                    output_enabled=False,
                ),
                # Malware, credential theft, intrusion tooling.
                bedrock.CfnGuardrail.ContentFilterConfigProperty(
                    type="MISCONDUCT",
                    input_strength="LOW",
                    output_strength="NONE",
                    input_action="BLOCK",
                    output_enabled=False,
                ),
            ],
        ),
        "sensitive_information_policy_config": (
            bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type=kind, action="BLOCK", input_action="BLOCK", output_enabled=False
                    )
                    for kind in ("AWS_ACCESS_KEY", "AWS_SECRET_KEY")
                ],
                regexes_config=[
                    bedrock.CfnGuardrail.RegexConfigProperty(
                        name=name,
                        pattern=pattern,
                        action="BLOCK",
                        input_action="BLOCK",
                        output_enabled=False,
                    )
                    for name, pattern in GUARDRAIL_SECRET_PATTERNS
                ],
            )
        ),
        "cross_region_config": bedrock.CfnGuardrail.GuardrailCrossRegionConfigProperty(
            guardrail_profile_arn=profile_arn
        ),
    }


def add_prompt_guardrail(stack: cdk.Stack, *, app_name: str, profile_id: str) -> PromptGuardrail:
    """The guardrail and the one published version the handlers pin, created in `stack`.

    A pinned version rather than `DRAFT`, because `DRAFT` is whatever the guardrail was last
    edited to, console included, and the template should say what screens prompts. The
    version's construct id carries a digest of the policies, so changing a policy publishes a
    new version on the next deploy instead of leaving the old one pinned -- measured: a policy
    change created version 2 and retired version 1 in the same update.
    """
    properties = guardrail_properties(
        f"arn:aws:bedrock:{stack.region}:{stack.account}:guardrail-profile/{profile_id}"
    )
    guardrail = bedrock.CfnGuardrail(
        stack,
        "PromptGuardrail",
        name=f"{app_name}-prompt-guardrail",
        description="Screens coding-agent task prompts before a run is queued.",
        **properties,
    )
    # Over the inputs rather than the synthesized properties: the profile ARN carries the
    # account and region, which are not policy, and jsii's struct `repr` is a stable
    # rendering of every field.
    digest = hashlib.sha256(repr(guardrail_properties(profile_id)).encode()).hexdigest()[:8]
    version = bedrock.CfnGuardrailVersion(
        stack,
        f"PromptGuardrailVersion{digest}",
        guardrail_identifier=guardrail.attr_guardrail_id,
        description=f"policies {digest}",
    )
    return PromptGuardrail(guardrail=guardrail, version=version.attr_version, profile_id=profile_id)
