"""`infra/` is a CDK app: `stacks` is a namespace package, `app.py` is inert on import, and
its five stack names all hang off one prefix."""

from __future__ import annotations

import json
import re

import app
import stacks
from conftest import REPO_ROOT


def test_stacks_stays_a_namespace_package() -> None:
    assert stacks.__file__ is None
    assert next(iter(stacks.__path__)).endswith("infra/stacks")
    assert not (REPO_ROOT / "infra/stacks/__init__.py").exists(), (
        "`stacks` must stay a namespace package: `pythonpath` already contains "
        "`infra`, and an __init__.py here makes `stacks` a regular package whose "
        "import then depends on which directory pytest was started from"
    )


def test_the_cdk_entry_point_imports_without_building_an_app() -> None:
    assert app.APP_NAME == "rca"
    assert callable(app.main)
    assert not (REPO_ROOT / "cdk.out").exists(), (
        "importing infra/app.py must not construct an aws_cdk.App(); that writes "
        "cdk.out/ as an import side effect"
    )


def test_the_five_stack_names_share_the_default_prefix() -> None:
    assert app.DEFAULT_STACK_PREFIX == "SampleCodingAgent"
    assert app.stack_names() == (
        app.ACCOUNT_STACK_NAME,
        app.DATA_STACK_NAME,
        app.AGENT_STACK_NAME,
        app.API_STACK_NAME,
        app.WEB_STACK_NAME,
    ), (
        "the five module constants are the *defaults*, i.e. `stack_names()` with no "
        "argument; a constant that does not come out of `stack_names()` is a name "
        "`-c stackPrefix=SampleCodingAgent` cannot reproduce"
    )
    assert app.API_STACK_NAME == "SampleCodingAgentApi", (
        "this is the default the MCP client pins: `mcp/src/rca_mcp/client.py` looks "
        "`ApiUrl` up on `DEFAULT_STACK_NAME`, so changing the default prefix or the "
        "`Api` suffix breaks plugin discovery, not just a name"
    )


def test_the_stack_prefix_renames_all_five_stacks() -> None:
    """The no-region call, whose unsuffixed `FooAccount` is the *pre-split, shared* account
    stack name -- the one a deploy from before the per-region split left behind, and the one
    `make deploy` now reports as orphaned."""
    names = app.stack_names("Foo")
    assert names == ("FooAccount", "FooData", "FooRuntime", "FooApi", "FooConsole")
    assert len(set(names)) == 5
    assert not [name for name in names if "SampleCodingAgent" in name], (
        "`-c stackPrefix=` has to move all five names together: one left as a literal "
        "deploys part of a second fleet on top of the first one's stacks"
    )


def test_the_account_stack_is_one_per_consuming_region() -> None:
    shared = app.stack_names("Foo")
    apse1 = app.stack_names("Foo", "ap-southeast-1")
    use1 = app.stack_names("Foo", "us-east-1")
    reason = (
        "the account stack has to be one instance per *(fleet, consuming region)* pair: "
        "while every region shared one `FooAccount`, a synth only knew the one region it "
        "was deploying, so it submitted a template that deleted the other region's CDK "
        "`ExportsWriter` custom resource -- protected only by CDK's in-use tag, so "
        "CloudFormation reported `DELETE_FAILED (skipped)` after ~6 minutes of retry on "
        "every deploy in both directions, and left the stack in "
        "`UPDATE_COMPLETE_CLEANUP_IN_PROGRESS`, where the next deploy was refused outright"
    )
    assert apse1.account == "FooAccountApSoutheast1", reason
    assert use1.account == "FooAccountUsEast1", reason
    assert apse1.account != use1.account, reason
    assert (apse1.data, apse1.agent, apse1.api, apse1.web) == (
        shared.data,
        shared.agent,
        shared.api,
        shared.web,
    ), (
        "the consuming region reaches the `account` entry and nothing else: the other four "
        "stacks already deploy into that region, so suffixing them renames live stacks"
    )


def test_the_region_suffix_is_a_legal_stack_name_token() -> None:
    assert app.region_suffix(None) == ""
    assert app.region_suffix("") == ""
    assert app.region_suffix("us-east-1") == "UsEast1"
    assert app.region_suffix("AP-SOUTHEAST-1") == "ApSoutheast1", (
        "the suffix is a stack-name token, so it is normalized rather than pasted in: "
        "`AP-SOUTHEAST-1` and `ap-southeast-1` are the same region and must name the same "
        "stack, or one shell's `AWS_REGION` builds a second copy of the account stack"
    )
    for region in ("us-east-1", "ap-southeast-1", "eu-west-1", "ap-northeast-1"):
        name = app.stack_names(region=region).account
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name), (
            f"`{name}` is not a legal CloudFormation stack name; a suffix that produces one "
            "fails at `CreateStack` in front of an operator, not at synth"
        )
        assert len(name) <= 128, f"`{name}` is over CloudFormation's 128-character limit"


def test_the_api_stack_name_is_untouched_by_the_region() -> None:
    assert app.stack_names("Foo", "eu-west-1").api == "FooApi"
    assert app.stack_names(region="us-east-1").api == app.API_STACK_NAME, (
        "`mcp/src/rca_mcp/client.py` pins `DEFAULT_STACK_NAME` and looks `ApiUrl` up on a "
        "stack of that name, so a region reaching the `api` entry breaks plugin discovery "
        "with an error about a missing stack rather than about a rename"
    )


def test_cdk_json_points_at_app_py() -> None:
    cdk_json = json.loads((REPO_ROOT / "infra/cdk.json").read_text())
    assert cdk_json["app"] == "python3 app.py"
