"""The prompt guardrail's infra half (invariant 43): what `add_prompt_guardrail` creates,
the region table its profile comes from, and the environment `ApiStack` hands every handler.

`ApiStack` bundles `../api` through Docker at construction, so no test can synthesize it.
`add_prompt_guardrail` is a function for exactly that reason, and it is applied here to a
bare stack -- the same code path `ApiStack.__init__` runs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import aws_cdk
import pytest
from aws_cdk.assertions import Template
from stacks import api_stack, prompt_guardrail
from stacks.data_stack import DataStack

_ACCOUNT = "123456789012"


def _app(tmp_path: Path) -> aws_cdk.App:
    """`outdir=` for the reason `tests/test_web_stack.py::template` gives: a bare app writes
    `cdk.out/` into the repository root and fails the layout test."""
    return aws_cdk.App(outdir=str(tmp_path))


def _stack(
    tmp_path: Path, *, region: str = "ap-southeast-1"
) -> tuple[aws_cdk.Stack, prompt_guardrail.PromptGuardrail]:
    stack = aws_cdk.Stack(
        _app(tmp_path),
        "SampleCodingAgentApi",
        env=aws_cdk.Environment(account=_ACCOUNT, region=region),
    )
    built = prompt_guardrail.add_prompt_guardrail(
        stack, app_name="rca", profile_id=prompt_guardrail.guardrail_profile_for(region)
    )
    return stack, built


def _guardrail(template: Template) -> dict[str, Any]:
    (resource,) = template.find_resources("AWS::Bedrock::Guardrail").values()
    return resource["Properties"]


def test_the_guardrail_screens_only_what_a_coding_prompt_needs(tmp_path: Path) -> None:
    properties = _guardrail(Template.from_stack(_stack(tmp_path)[0]))

    content = properties["ContentPolicyConfig"]
    # Standard is the tier whose classifiers read code; Classic treats a prompt as prose.
    assert content["ContentFiltersTierConfig"] == {"TierName": "STANDARD"}
    filters = {f["Type"]: f for f in content["FiltersConfig"]}
    assert set(filters) == {"PROMPT_ATTACK", "MISCONDUCT"}, (
        "hate, insults, sexual and violence are false positives on code (`kill -9`)"
    )
    # Measured against the live service (see `guardrail_properties`): MEDIUM on either one
    # refuses ordinary security tasks, so raising one is a regression, not hardening.
    assert filters["PROMPT_ATTACK"]["InputStrength"] == "LOW"
    assert filters["MISCONDUCT"]["InputStrength"] == "LOW"
    assert all(f["InputAction"] == "BLOCK" for f in filters.values())

    sensitive = properties["SensitiveInformationPolicyConfig"]
    assert {e["Type"] for e in sensitive["PiiEntitiesConfig"]} == {
        "AWS_ACCESS_KEY",
        "AWS_SECRET_KEY",
    }, "names and emails are everywhere in code; only credentials are refused"
    assert {r["Name"] for r in sensitive["RegexesConfig"]} == {
        name for name, _ in prompt_guardrail.GUARDRAIL_SECRET_PATTERNS
    }

    # Every policy is input-only: nothing attaches this guardrail to a model's output.
    for entry in (*filters.values(), *sensitive["PiiEntitiesConfig"], *sensitive["RegexesConfig"]):
        assert entry["OutputEnabled"] is False

    for absent in ("TopicPolicyConfig", "WordPolicyConfig", "ContextualGroundingPolicyConfig"):
        assert absent not in properties


def test_the_guardrail_runs_through_the_regions_own_profile(tmp_path: Path) -> None:
    properties = _guardrail(Template.from_stack(_stack(tmp_path, region="eu-west-2")[0]))
    assert properties["CrossRegionConfig"] == {
        "GuardrailProfileArn": (
            f"arn:aws:bedrock:eu-west-2:{_ACCOUNT}:guardrail-profile/uk.guardrail.v1:0"
        )
    }


def test_the_handlers_pin_a_published_version_in_the_same_stack(tmp_path: Path) -> None:
    """Same stack is the point: a version imported across stacks cannot change, and a
    policy change always changes it (see `stacks/prompt_guardrail.py`)."""
    app = _app(tmp_path)
    env = aws_cdk.Environment(account=_ACCOUNT, region="ap-southeast-1")
    data = DataStack(app, "SampleCodingAgentData", app_name="rca", env=env)
    stack = aws_cdk.Stack(app, "SampleCodingAgentApi", env=env)
    built = prompt_guardrail.add_prompt_guardrail(
        stack, app_name="rca", profile_id="apac.guardrail.v1:0"
    )

    environment = stack.resolve(api_stack._common_environment(data, built))
    (version_id,) = Template.from_stack(stack).find_resources("AWS::Bedrock::GuardrailVersion")
    assert environment["GUARDRAIL_VERSION"] == {"Fn::GetAtt": [version_id, "Version"]}, (
        "a local GetAtt on the published version: never DRAFT, never an ImportValue"
    )
    assert environment["GUARDRAIL_ID"]["Fn::GetAtt"][1] == "GuardrailArn"
    assert Template.from_stack(data).find_resources("AWS::Bedrock::Guardrail") == {}


def test_a_policy_change_publishes_a_new_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def version_ids(path: Path) -> set[str]:
        return set(
            Template.from_stack(_stack(path)[0]).find_resources("AWS::Bedrock::GuardrailVersion")
        )

    before = version_ids(tmp_path / "a")
    original = prompt_guardrail.guardrail_properties
    monkeypatch.setattr(
        prompt_guardrail,
        "guardrail_properties",
        lambda arn: {**original(arn), "blocked_input_messaging": "changed"},
    )
    assert version_ids(tmp_path / "b").isdisjoint(before)


def test_a_deploy_with_the_guardrail_off_sends_both_keys_empty(tmp_path: Path) -> None:
    """ "Screens nothing" is a value in the template, not an absent key a handler would have
    to guess about."""
    data = DataStack(
        _app(tmp_path),
        "SampleCodingAgentData",
        app_name="rca",
        env=aws_cdk.Environment(account=_ACCOUNT, region="ap-southeast-1"),
    )
    environment = api_stack._common_environment(data, None)
    assert environment["GUARDRAIL_ID"] == "" and environment["GUARDRAIL_VERSION"] == ""


@pytest.mark.parametrize(
    ("region", "profile"),
    [
        ("us-east-1", "us.guardrail.v1:0"),
        ("eu-west-1", "eu.guardrail.v1:0"),
        # The three a prefix rule gets wrong, which is why this is a table.
        ("eu-west-2", "uk.guardrail.v1:0"),
        ("ap-southeast-2", "au.guardrail.v1:0"),
        ("ca-central-1", "ca.guardrail.v1:0"),
        ("ap-southeast-1", "apac.guardrail.v1:0"),
        ("me-central-1", "apac.guardrail.v1:0"),
    ],
)
def test_each_region_maps_to_the_profile_bedrock_lists_for_it(region: str, profile: str) -> None:
    assert prompt_guardrail.guardrail_profile_for(region) == profile


def test_a_region_with_no_known_profile_fails_the_synth_with_the_fix() -> None:
    with pytest.raises(ValueError, match="guardrailEnabled=false") as raised:
        prompt_guardrail.guardrail_profile_for("sa-east-1")
    assert "guardrailProfile=" in str(raised.value)
    assert prompt_guardrail.guardrail_profile_for("sa-east-1", "us.guardrail.v1:0") == (
        "us.guardrail.v1:0"
    ), "an explicit profile outranks the table"


@pytest.mark.parametrize(
    ("name", "caught", "passed"),
    [
        ("GITHUB_TOKEN", "ghp_" + "a" * 36, "the ghp_ prefix alone"),
        ("GITHUB_FINE_GRAINED_TOKEN", "github_pat_" + "A1_" * 10, "a github_pat_ mention"),
        ("PRIVATE_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN PUBLIC KEY-----"),
    ],
)
def test_each_secret_pattern_catches_its_shape_and_not_a_mention(
    name: str, caught: str, passed: str
) -> None:
    """Python's `re` stands in for the guardrail's regex engine; the patterns use nothing
    the two dialects disagree on, and the live probe refused both shapes it was given."""
    pattern = dict(prompt_guardrail.GUARDRAIL_SECRET_PATTERNS)[name]
    assert re.search(pattern, f"use {caught} please")
    assert not re.search(pattern, f"use {passed} please")
