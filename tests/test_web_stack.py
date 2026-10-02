"""`WebStack` synthesizes, and the four things about it that are silent when wrong.

This is the only one of the four stacks a test can build. `DataStack` and `AgentStack`
could be, but `AgentStack` carries a `DockerImageAsset` and `ApiStack` bundles `../api`
through Docker at *construction* time, so neither can be reached from this container --
`tests/test_grant_contract.py` is the static substitute for those. `WebStack` needs no
Docker and no credentials because the 17 handlers reach it as **ARN strings**, and this
file exists to keep it that way: every assertion below fails if someone "simplifies" the
hand-off into passing `Function` objects across the stack boundary.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import aws_cdk
import pytest
from aws_cdk.assertions import Template
from stacks import api_stack
from stacks.web_stack import WebStack

# Built from `api_stack.ROUTES` rather than spelled out, so this file covers all 17 routes
# and cannot drift from the route table: a route added there arrives here for free.
_FAKE_ARNS: dict[str, str] = {
    f"{method} {path}": f"arn:aws:lambda:ap-southeast-1:123456789012:function:rca-{module}-{function}"
    for method, path, module, function in api_stack.ROUTES
}

# The seven `make deploy` reads to write `web/config.js`. `web/config.example.js` is the
# other half of that contract (`region`, `userPoolId`, `clientId`, `cognitoDomain`, and a
# same-origin `apiBase` of "/api").
_EXPECTED_OUTPUTS = {
    "ConsoleUrl",
    "ConsoleBucket",
    "DistributionId",
    "UserPoolId",
    "UserPoolClientId",
    "CognitoDomain",
    "Region",
}

_ROUTE_KEY = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) /api/")

# What `AccountStack.web_acl_arn` hands over, spelled out here because the real value is a
# cross-region reference this stack cannot resolve on its own. An ARN and not an id: for a
# CloudFront-scoped ACL, `Distribution`'s `web_acl_id` property wants the ARN.
_FAKE_WEB_ACL_ARN = (
    "arn:aws:wafv2:us-east-1:123456789012:global/webacl/rca-console/"
    "0123abcd-4567-89ef-0123-456789abcdef"
)

# CloudFormation lets a token validity be expressed in any of these, so the assertions
# below read `TokenValidityUnits` instead of assuming the one CDK happens to emit.
_MINUTES_PER_UNIT = {"seconds": 1 / 60, "minutes": 1.0, "hours": 60.0, "days": 1440.0}


@pytest.fixture
def template(tmp_path: Path) -> Template:
    """`WebStack` synthesized into `tmp_path`.

    `outdir=` is not optional. `Template.from_stack` synthesizes into the app's outdir,
    and a bare `aws_cdk.App()` writes `cdk.out/` into the current working directory --
    which is the repository root under `make test`, and which fails
    `tests/test_layout_infra.py::test_the_cdk_entry_point_imports_without_building_an_app`
    in a file that has nothing to do with this one.
    """
    app = aws_cdk.App(outdir=str(tmp_path))
    stack = WebStack(
        app,
        "CodingAgentConsole",
        app_name="rca",
        handler_arns=_FAKE_ARNS,
        domain_prefix="rca-console-123456789012",
        web_acl_arn=_FAKE_WEB_ACL_ARN,
        env=aws_cdk.Environment(account="123456789012", region="ap-southeast-1"),
        # Set on both sides or on neither: `AccountStack` produces the ARN in us-east-1 and
        # CDK writes a custom resource on each side of the hand-off.
        cross_region_references=True,
    )
    return Template.from_stack(stack)


def _resources(template: Template, kind: str) -> list[dict[str, Any]]:
    return list(template.find_resources(kind).values())


def _properties(template: Template, kind: str) -> list[dict[str, Any]]:
    return [resource.get("Properties", {}) for resource in _resources(template, kind)]


def _token_minutes(properties: dict[str, Any], token: str) -> float:
    """`IdToken` -> the `IdTokenValidity` value converted to minutes."""
    unit = str(properties.get("TokenValidityUnits", {}).get(token, "minutes")).lower()
    return float(properties[f"{token}Validity"]) * _MINUTES_PER_UNIT[unit]


def test_web_stack_synthesizes_from_fake_arns_with_no_docker(template: Template) -> None:
    """The smoke test the whole design is measured on: three literal ARNs, no Docker
    daemon, no credentials, and a real synthesized template."""
    assert template.to_json()["Resources"], "the stack synthesized to nothing at all"


def test_every_account_global_cloudfront_name_carries_the_region(template: Template) -> None:
    """CloudFront is global, so these two names are unique per *account*, and CDK derives
    both from the stack name and construct path -- which are the same in every region.

    A regression test for a deploy that got as far as the console stack and then rolled the
    whole thing back. Deploying eu-west-1 into an account already running ap-southeast-1
    failed twice over: `Another response headers policy with the same name already exists
    within the aws account` (409), and the implicitly-created origin access control with
    `identifier 'SampleCodingAgentConsoleOrigin1S3OriginAccessControlD8807F2B' already
    exists`. The region in the name is what makes two fleets under one stack prefix possible.
    """
    (policy,) = _properties(template, "AWS::CloudFront::ResponseHeadersPolicy")
    (oac,) = _properties(template, "AWS::CloudFront::OriginAccessControl")
    for name, where in (
        (policy["ResponseHeadersPolicyConfig"]["Name"], "the response headers policy"),
        (oac["OriginAccessControlConfig"]["Name"], "the origin access control"),
    ):
        assert "ap-southeast-1" in name, (
            f"{where} is named {name!r}, which does not carry the region. CloudFront names "
            "are account-global, so a second region under the same stack prefix collides on "
            "a name nobody chose; see the comment in web_stack.py."
        )


def test_no_lambda_permission_references_the_web_api_id(template: Template) -> None:
    """The cyclic-reference guard, and the reason `handler_arns` is `Mapping[str, str]`.

    Passing `Function` objects instead of ARNs turns `FunctionName` into a cross-stack
    reference (`Ref`/`Fn::GetAtt`/`Fn::ImportValue`) whose matching `SourceArn` names the
    *web* API -- `ApiStack` then references `WebStack` and back, and `cdk synth` fails
    with a cyclic reference. The set equality covers the other direction too: dropping
    `same_environment=True` makes CDK add no permission at all, which is not a synth error
    and shows up only as every console route returning 500.
    """
    names = []
    for properties in _properties(template, "AWS::Lambda::Permission"):
        name = properties["FunctionName"]
        assert isinstance(name, str), (
            f"FunctionName is {name!r}, an intrinsic rather than a literal ARN: the "
            f"handlers must reach WebStack as ARN strings, or the two stacks reference "
            f"each other and cdk synth fails with a cyclic reference"
        )
        names.append(name)
    assert set(names) == set(_FAKE_ARNS.values()), (
        "every re-imported handler needs exactly one invoke permission naming its literal "
        "ARN; a missing one means `same_environment=True` was dropped from "
        "`Function.from_function_attributes`, which CDK does not complain about"
    )


def test_every_route_carries_the_literal_api_prefix_and_the_cognito_authorizer(
    template: Template,
) -> None:
    """CloudFront cannot *strip* a path prefix and an origin's `origin_path` only
    prepends, so the browser's `/api/cards` has to be this API's own route key. The
    prefix is what makes the console same-origin, which is what lets the CSP say
    `connect-src 'self'`."""
    routes = _properties(template, "AWS::ApiGatewayV2::Route")
    assert len(routes) == len(api_stack.ROUTES)
    assert {route["RouteKey"] for route in routes} == {
        f"{method} /api{path}" for method, path, _module, _function in api_stack.ROUTES
    }
    for route in routes:
        assert _ROUTE_KEY.match(route["RouteKey"]), route["RouteKey"]
        assert route["AuthorizationType"] == "JWT", (
            f"{route['RouteKey']} is unauthorized; there is no public route on the console API"
        )

    (authorizer,) = _properties(template, "AWS::ApiGatewayV2::Authorizer")
    assert authorizer["AuthorizerType"] == "JWT"
    # One audience, and it is the console app client. Without `user_pool_clients=` the
    # authorizer accepts a token minted for *any* client in the pool.
    assert len(authorizer["JwtConfiguration"]["Audience"]) == 1


def test_no_cors_is_configured_anywhere(template: Template) -> None:
    """Same-origin is the point *for the API*. CORS is configured on neither `HttpApi` in
    this repository, and the response-headers policy carries no `CorsConfig` either -- which
    is what keeps the API out of `connect-src`'s host list."""
    for api in _properties(template, "AWS::ApiGatewayV2::Api"):
        assert "CorsConfiguration" not in api, (
            "a CORS configuration here means someone stopped serving the API from the "
            "console's own origin; the CSP below is then a lie"
        )

    (policy,) = _properties(template, "AWS::CloudFront::ResponseHeadersPolicy")
    config = policy["ResponseHeadersPolicyConfig"]
    assert "CorsConfig" not in config
    csp = config["SecurityHeadersConfig"]["ContentSecurityPolicy"]["ContentSecurityPolicy"]
    assert "connect-src 'self'" in csp, csp
    assert "default-src 'none'" in csp, csp
    assert "execute-api" not in csp, (
        f"the CSP names an `execute-api` host, so the API is no longer same-origin and the "
        f"`/api/*` prefix has stopped earning its keep: {csp}"
    )


def test_the_csp_lets_the_browser_reach_the_cognito_token_endpoint(template: Template) -> None:
    """`connect-src` names the Cognito hosted-UI origin, not just `'self'`.

    This is the one that was missing, and the console was completely unusable without it:
    `web/js/auth.js` finishes sign-in by POSTing to
    `<prefix>.auth.<region>.amazoncognito.com/oauth2/token`, which is cross-origin by
    construction -- the hosted UI is Cognito's domain and cannot be served from this
    distribution. Under `connect-src 'self'` the browser refused that request before it left,
    and `fetch` reports a CSP block as `TypeError: Failed to fetch`, naming no policy and no
    host.

    Nothing server-side can see this. The distribution served every asset, `/api/*` proxied
    to the API and answered `401 www-authenticate: Bearer` as designed, the Cognito domain
    was `ACTIVE` and returned a correct `access-control-allow-origin` for this exact origin,
    and sign-in itself worked -- so every `curl` probe passed, because `curl` does not
    enforce CSP. Only a browser could tell, which is why this needs a template assertion
    rather than a live check.

    The origin is asserted in full rather than by substring: a `connect-src` entry with the
    wrong region or a stale prefix is the same outage with a more convincing header.
    """
    (policy,) = _properties(template, "AWS::CloudFront::ResponseHeadersPolicy")
    csp = policy["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]["ContentSecurityPolicy"][
        "ContentSecurityPolicy"
    ]
    assert isinstance(csp, str), (
        f"the CSP is not a literal string in the template but {type(csp).__name__}, so a "
        f"token reached it -- spell it from synth-time values: {csp}"
    )

    # The fixture's own prefix and region, so this fails if either stops reaching the policy.
    expected = "https://rca-console-123456789012.auth.ap-southeast-1.amazoncognito.com"
    assert f"connect-src 'self' {expected};" in csp, (
        f"`connect-src` does not name the Cognito hosted-UI origin `{expected}`, so the "
        f"browser will refuse the PKCE token exchange in `web/js/auth.js` and the console "
        f"will fail immediately after sign-in with `Failed to fetch`. Got: {csp}"
    )


def test_the_policy_permits_the_github_app_manifest_post(template: Template) -> None:
    """`form-action` names github.com, because the app-manifest flow is a real form submit.

    The sibling of the `connect-src` test above, and the **same outage a second time**. This
    directive was `'none'` on the reasoning that every outbound call in this console is a
    `fetch` -- true until `web/js/views/connect.js` POSTed a `<form>` to GitHub, which the
    app-manifest flow cannot avoid: GitHub has to receive the manifest under the operator's
    own github.com session and answer with its own confirmation page, which is a navigation
    and not a response JavaScript may read.

    Under `'none'` the browser refuses that submit and the console sits on "Preparing the
    GitHub App…" with nothing to go on -- measured on the live deployment, where every
    server-side signal was green: `POST /github-app` ran, returned a manifest and logged a
    clean request line, and nothing was ever sent to GitHub. `curl` does not enforce CSP, so
    no probe outside a browser can see it, which is why the assertion belongs on the template.

    The origin is derived from `github_app._MANIFEST_FORM_URL` rather than written out, so
    changing where the manifest is posted fails here instead of shipping a console that
    silently cannot post it.
    """
    from urllib.parse import urlsplit

    from handlers import github_app

    target = urlsplit(github_app._MANIFEST_FORM_URL)
    origin = f"{target.scheme}://{target.netloc}"

    (policy,) = _properties(template, "AWS::CloudFront::ResponseHeadersPolicy")
    csp = policy["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]["ContentSecurityPolicy"][
        "ContentSecurityPolicy"
    ]
    assert f"form-action {origin}" in csp, (
        f"`form-action` does not permit `{origin}`, the origin "
        f"`github_app._MANIFEST_FORM_URL` posts the GitHub App manifest to. The browser will "
        f"refuse the submit, the connect page will hang on 'Preparing the GitHub App…', and "
        f"every server-side signal will look correct. Got: {csp}"
    )
    assert "form-action 'none'" not in csp


def test_the_distribution_sends_api_to_the_http_api_and_everything_else_to_s3(
    template: Template,
) -> None:
    (distribution,) = _properties(template, "AWS::CloudFront::Distribution")
    config = distribution["DistributionConfig"]
    origins = {origin["Id"]: origin for origin in config["Origins"]}

    (api_behavior,) = config["CacheBehaviors"]
    assert api_behavior["PathPattern"] == "/api/*"
    # Asserted as "a policy is attached", not as a GUID: the managed policy ids live in
    # aws-cdk-lib's bundled jsii archive, and a GUID copied into a test is a value nobody
    # reading it can check. What matters is that `/api/*` does not share the document's
    # caching, and that an origin-request policy forwards the `Authorization` header the
    # JWT authorizer reads.
    assert api_behavior.get("OriginRequestPolicyId")
    assert api_behavior.get("CachePolicyId") != config["DefaultCacheBehavior"].get("CachePolicyId")

    api_origin = origins[api_behavior["TargetOriginId"]]
    assert "execute-api" in json.dumps(api_origin["DomainName"])
    assert api_origin.get("OriginPath", "") in ("", "/"), (
        "`origin_path` only *prepends*; the /api prefix is already in the route keys, so "
        "setting it here would ask the API for /api/api/cards"
    )

    default_origin = origins[config["DefaultCacheBehavior"]["TargetOriginId"]]
    assert default_origin.get("OriginAccessControlId"), (
        "the default behaviour must serve the console bucket through origin access "
        "control; the bucket is BLOCK_ALL and the distribution is its only reader"
    )
    assert not api_origin.get("OriginAccessControlId")
    assert _resources(template, "AWS::CloudFront::OriginAccessControl")


def test_no_custom_error_response_rewrites_an_api_error(template: Template) -> None:
    """The `403`/`404` -> `200 /index.html` rewrite a static site usually carries is not
    here, and its absence is the assertion rather than a gap.

    `CustomErrorResponses` is a property of the distribution, not of a cache behaviour, so
    one list covers `/api/*` too -- and the console is a hash router, so it never asks
    CloudFront for a path S3 does not have (`tests/test_web_console.py` pins that premise).
    """
    (distribution,) = _properties(template, "AWS::CloudFront::Distribution")
    config = distribution["DistributionConfig"]
    assert not config.get("CustomErrorResponses"), (
        f"the distribution carries {config.get('CustomErrorResponses')!r}. There is one such "
        f"list per distribution and it covers the /api/* behaviour, so an "
        f'`ApiError(404, "NotFound", ...)` from api/src/handlers/tasks.py reaches '
        f"`web/js/api.js::request` as 200 text/html: `response.ok` accepts it, `payloadOf` "
        f"cannot parse HTML and returns null, and the view renders a deleted task from "
        f"nothing instead of showing the sentence the server sent. The rewrite also cannot "
        f"serve /index.html when /index.html is the missing object, which is how an empty "
        f"console bucket answers with S3's <Error><Code>AccessDenied</Code></Error> XML."
    )
    assert config["DefaultRootObject"] == "index.html", (
        f"DefaultRootObject is {config.get('DefaultRootObject')!r}. With no error responses "
        f"this is the whole of the hash router's server-side routing: `/` has to resolve to "
        f"the document, and every other path the console ever asks for is a real object or "
        f"/api/*."
    )


def test_the_distribution_carries_the_web_acl_arn(template: Template) -> None:
    """The ARN, asserted exactly rather than as "something is set". The property is spelled
    `WebACLId`, and for a CloudFront-scoped ACL it takes the full ARN: a bare id here
    deploys cleanly, attaches nothing, and leaves a distribution that reads as protected in
    the template and is not. The keyword is also required, so there is no code path that
    ships this console with no WAF."""
    (distribution,) = _properties(template, "AWS::CloudFront::Distribution")
    assert distribution["DistributionConfig"]["WebACLId"] == _FAKE_WEB_ACL_ARN


def test_the_callback_url_is_the_distribution_domain(template: Template) -> None:
    """The construction-order guard: only api -> distribution -> client can produce this.

    A client built before the distribution has no domain name to point at, and the
    trailing slash is required rather than tidy -- `web/js/auth.js::redirectUri()` returns
    `${location.origin}/` and the Cognito hosted UI matches the callback list *exactly*.
    """
    (client,) = _properties(template, "AWS::Cognito::UserPoolClient")
    (callback,) = client["CallbackURLs"]
    assert isinstance(callback, dict) and "Fn::Join" in callback, (
        f"the callback URL is {callback!r}, a literal: it must be built from the "
        f"distribution's own domain name"
    )
    _separator, parts = callback["Fn::Join"]
    attributes = [
        part["Fn::GetAtt"] for part in parts if isinstance(part, dict) and "Fn::GetAtt" in part
    ]
    assert any(attribute[1] == "DomainName" for attribute in attributes), parts
    assert str(parts[0]).startswith("https://"), parts
    assert str(parts[-1]).endswith("/"), parts


def test_the_console_user_pool_is_locked_down(template: Template) -> None:
    """Nobody signs themselves up, and the refresh window is 30 days rather than the source
    repository's 3650. A JWT authorizer cannot consult Cognito, so the ID token's one-hour
    lifetime *is* the revocation lag; a longer refresh window buys no session length because
    `auth.js` refreshes two minutes before expiry anyway.

    **MFA is deliberately OFF**, which is a step down from Tier 1 #13 taken for the
    walkthrough: `Mfa.REQUIRED` put a TOTP enrolment between a new operator and the board.
    This asserts the demo posture rather than the hardened one, so it is the test to change
    back -- together with `mfa`/`mfa_second_factor` in `infra/stacks/web_stack.py` -- when
    this stops being a demo. SMS must never be what comes back: it is redirectable at a
    phone shop.

    What is still asserted is the control that bounds *who can be an operator at all*, and it
    is the one that must not follow MFA out: with `AllowAdminCreateUserOnly` false, a public
    CloudFront URL in front of an API that can register repositories becomes joinable by
    anyone, which is a different order of exposure from single-factor sign-in.
    """
    (pool,) = _properties(template, "AWS::Cognito::UserPool")
    assert pool.get("MfaConfiguration", "OFF") == "OFF", (
        "MFA is on. That is the hardened posture and a better one -- if this is deliberate, "
        "update this test and the comment in `web_stack.py` together, so the code and the "
        "reason stay in the same place."
    )
    assert "SMS_MFA" not in pool.get("EnabledMfas", []), (
        "SMS is a second factor an attacker can have redirected at a phone shop"
    )
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True, (
        "a console that can mint its own accounts is a console anyone can join"
    )

    # The relaxed policy is asserted so a silent tightening cannot lock an operator out of a
    # walkthrough, and a silent loosening below 8 characters cannot pass either.
    policy = pool["Policies"]["PasswordPolicy"]
    assert policy["MinimumLength"] == 8, policy
    assert policy["RequireLowercase"] is True, policy
    for absent in ("RequireUppercase", "RequireNumbers", "RequireSymbols"):
        assert policy[absent] is False, (
            f"{absent} is back on. The emailed temporary password is changed immediately, so "
            f"the ceremony bought little; if this is deliberate, say so here. Got {policy}"
        )

    (client,) = _properties(template, "AWS::Cognito::UserPoolClient")
    assert _token_minutes(client, "IdToken") == 60
    assert _token_minutes(client, "RefreshToken") == 30 * 24 * 60


def test_the_outputs_are_the_ones_make_deploy_reads(template: Template) -> None:
    """`web/config.example.js` is the other half of this contract: `make deploy` reads
    these outputs and writes `web/config.js` from them, so renaming one here breaks a
    deploy rather than a build -- and the console then boots into its "no config.js"
    panel with nothing to say why."""
    assert set(template.to_json()["Outputs"]) == _EXPECTED_OUTPUTS
