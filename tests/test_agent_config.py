"""The routing table and the filesystem roots: a misconfiguration must be loud.

`no_aws(monkeypatch)` is called in every test here and is not optional. This container
genuinely has `WORK_ROOT`, `VENV_ROOT`, `AGENT_SKILL_PATHS=/app/skills` and
`ANTHROPIC_MODEL` set in the image, so without clearing them `load_config()` returns the
host's values and every default assertion in this file passes or fails for the wrong
reason.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from agent_helpers import no_aws
from coding_agent import config
from conftest import REPO_ROOT


@pytest.mark.parametrize(
    "value",
    [
        "opus",
        "sonnet",
        "haiku",
        "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        # `apac.` is the case a `[a-z]{2}\.` bound silently rejects, and `global.` the
        # case a `{2,4}` bound does. Both are legitimate inference-profile prefixes
        # (invariant 21), so both are asserted explicitly.
        "apac.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "global.anthropic.claude-opus-4-5-20251101-v1:0",
        "arn:aws:bedrock:us-west-2:123456789012:inference-profile/global.anthropic.claude-opus-4-5-20251101-v1:0",
    ],
)
def test_the_model_pattern_accepts_the_real_shapes(value: str) -> None:
    """Every shape a Bedrock model can legitimately arrive in is accepted."""
    assert config.validate_model(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "gpt-4",
        # A trailing space is what a copy-paste out of the console produces.
        "opus ",
        "../../etc/passwd",
        "anthropic.claude; rm -rf /",
        "OPUS",
    ],
)
def test_the_model_pattern_rejects_junk(value: str) -> None:
    """A model id that is not one raises, and the message says what a usable one is."""
    with pytest.raises(ValueError, match="is not a usable model") as excinfo:
        config.validate_model(value)

    message = str(excinfo.value)
    assert "opus/sonnet/haiku" in message
    assert "arn:aws:bedrock:" in message


def test_the_defaults_are_the_documented_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing in the environment, the roots are the two documented directories."""
    no_aws(monkeypatch)
    cfg = config.load_config()

    assert cfg.work_root == Path("/mnt/workspace/work")
    # OFF the 1 GB session-storage mount, deliberately: a virtualenv for this repository
    # is ~692 MB of perfectly reconstructible bytes, and the obvious-looking tidy-up that
    # moves it onto the mount fills the session and kills the task.
    assert cfg.venv_root == Path("/tmp/venv")
    assert cfg.model == "sonnet"


def test_the_environment_overrides_each_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both roots are read from the environment, because a Dockerfile ENV cannot be unset."""
    no_aws(monkeypatch)
    monkeypatch.setenv("WORK_ROOT", "/mnt/other/work")
    monkeypatch.setenv("VENV_ROOT", "/scratch/venv")

    cfg = config.load_config()
    assert cfg.work_root == Path("/mnt/other/work")
    assert cfg.venv_root == Path("/scratch/venv")


def test_the_skill_paths_fall_back_to_the_in_tree_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset, the plugin root is this repository's own `agent/skills`; set, it is honoured."""
    no_aws(monkeypatch)

    # A local run gets the same skills the image bakes in with no environment at all.
    assert config.skill_paths() == (REPO_ROOT / "agent" / "skills",)

    # Colon-separated like `PATH`, in order, and an empty segment is dropped rather than
    # turned into `Path("")` -- which is `Path(".")` and would hand a role the cwd.
    monkeypatch.setenv("AGENT_SKILL_PATHS", "/app/skills::/opt/extra-skills:")
    assert config.skill_paths() == (Path("/app/skills"), Path("/opt/extra-skills"))
    assert config.load_config().skill_paths == (Path("/app/skills"), Path("/opt/extra-skills"))


def test_a_lowered_effort_is_never_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Lowering a role below its default is allowed, but always says so in the log."""
    no_aws(monkeypatch)

    monkeypatch.setenv("ROLE_CODE_EFFORT", "medium")
    with caplog.at_level(logging.WARNING):
        cfg = config.load_config()
    # The override applies -- an operator may need it.
    assert cfg.roles["code"].effort == "medium"
    # ...but invariant 8's measurement is named, because a quietly weakened role produces
    # a plausible-looking run that changed nothing.
    assert "214 insertions" in caplog.text
    assert "invariant 8" in caplog.text
    monkeypatch.delenv("ROLE_CODE_EFFORT")

    caplog.clear()
    monkeypatch.setenv("ROLE_PLAN_TURNS", "25")
    with caplog.at_level(logging.WARNING):
        cfg = config.load_config()
    assert cfg.roles["plan"].turns == 25
    assert "25 turns" in caplog.text
    assert "no usable spec" in caplog.text
    monkeypatch.delenv("ROLE_PLAN_TURNS")

    caplog.clear()
    monkeypatch.setenv("ROLE_REVIEW_EFFORT", "turbo")
    with caplog.at_level(logging.WARNING):
        cfg = config.load_config()
    # An unrecognised value is ignored rather than applied, and the log is the only place
    # a typo in a runtime's environment variables can surface.
    assert cfg.roles["review"].effort == "low"
    assert "not an effort level" in caplog.text
    assert "ROLE_REVIEW_EFFORT" in caplog.text


def test_an_unusable_model_in_the_environment_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad model id kills `load_config()` rather than reaching Bedrock as a 400."""
    no_aws(monkeypatch)

    monkeypatch.setenv("ROLE_PLAN_MODEL", "gpt-4")
    with pytest.raises(ValueError, match="is not a usable model"):
        config.load_config()
    monkeypatch.delenv("ROLE_PLAN_MODEL")

    monkeypatch.setenv("ANTHROPIC_MODEL", "nonsense")
    with pytest.raises(ValueError, match="is not a usable model"):
        config.load_config()


def test_the_fallback_model_is_validated_and_defaults_to_sonnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that is momentarily unavailable must degrade the run, not kill it."""
    no_aws(monkeypatch)

    assert config.load_config().fallback_model == "sonnet"

    monkeypatch.setenv("ANTHROPIC_FALLBACK_MODEL", "global.anthropic.claude-opus-4-5-20251101-v1:0")
    assert config.load_config().fallback_model == "global.anthropic.claude-opus-4-5-20251101-v1:0"

    # Through `validate_model`, so a typo fails at load time rather than at the one moment
    # nobody is watching -- the moment the primary model is unavailable and the fallback is
    # the whole run.
    monkeypatch.setenv("ANTHROPIC_FALLBACK_MODEL", "gpt-4")
    with pytest.raises(ValueError, match="is not a usable model"):
        config.load_config()


def test_the_goal_switch_costs_no_background_model_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The switch on with no haiku model set is not worth a word, and that is the change.

    This used to warn: `/goal` was a slash command whose condition the CLI re-evaluated
    after every turn, a separate small-fast-model call, which made
    `ANTHROPIC_DEFAULT_HAIKU_MODEL` load-bearing for cost and not only for routing.
    `nodes/code.py` now carries the condition in the coding role's session system prompt,
    so there is no per-turn evaluator and no background call to pay for. The assertion is
    about an absence, which is why the reason is written down here.
    """
    no_aws(monkeypatch)

    with caplog.at_level(logging.WARNING):
        cfg = config.load_config()
    assert cfg.goal_enabled is True
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in caplog.text

    # And switching it off says nothing either: there was never a cost to stop paying.
    caplog.clear()
    monkeypatch.setenv("CODE_GOAL_ENABLED", "0")
    with caplog.at_level(logging.WARNING):
        cfg = config.load_config()
    assert cfg.goal_enabled is False
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", True),
        ("1", True),
        ("true", True),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
    ],
)
def test_the_goal_switch_reads_the_documented_off_values(
    value: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default on; `0`, `false` and `no` off, case-insensitively."""
    no_aws(monkeypatch)
    if value:
        monkeypatch.setenv("CODE_GOAL_ENABLED", value)

    assert config.load_config().goal_enabled is expected


def test_the_forwarded_model_variables_are_named_in_one_place() -> None:
    """One owner on the agent side for what `provisioner._fleet_environment` forwards.

    The three alias variables are not decoration: `config.ROLES` routes by alias, so an
    unset `ANTHROPIC_DEFAULT_OPUS_MODEL` means `model="opus"` resolves to whatever the CLI
    would pick rather than to the Bedrock id this fleet chose.
    """
    assert config.MODEL_ENV_VARS == (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    )
