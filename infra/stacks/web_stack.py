"""The console stack: an S3 origin behind CloudFront, Cognito, and a second HTTP API.

This is the one stack in this app a test can actually build (`tests/test_web_stack.py`),
and that is a property to protect rather than an accident. Nothing here bundles anything
through Docker, and the 17 control-plane handlers arrive as **ARN strings** in
`handler_arns` rather than as `Function` objects.

That is load-bearing in both directions, and silent when wrong either way:

* Passing the `Function` objects would make `HttpLambdaIntegration` write a Lambda
  permission whose `SourceArn` names the *web* API's id. `ApiStack` would then reference
  `WebStack` while `WebStack` already references `ApiStack`, and `cdk synth` fails with a
  cyclic reference -- which is also exactly what would make this stack untestable.
* Re-importing each handler with `same_environment=True` is what makes the permission's
  `FunctionName` a literal ARN, and a literal is one-directional. **Omitting** it makes
  CDK skip adding the permission at all: there is no synth error, and every console route
  500s at run time instead.

The construction order inside `__init__` is fixed at api -> distribution -> client ->
authorizer -> routes; the comment there says why, and rearranging it reintroduces the
cycle this stack exists to avoid.

Route keys carry a literal `/api/` prefix, because CloudFront cannot *strip* a path
prefix and an origin's `origin_path` only prepends. Same-origin is the whole point of the
prefix: no CORS is configured on either `HttpApi` anywhere in this repository, which is
what keeps `'self'` the only entry `connect-src` needs *for the API*. It is not the only
entry `connect-src` needs, and assuming it was is what broke the console -- see
`_content_security_policy` below.

Deliberately **not** in this stack, so the absence reads as a decision rather than an
oversight:

* the WAF web ACL itself and the Bedrock spend budget alarm, which live in `AccountStack`
  because a CloudFront-scoped web ACL is only creatable in `us-east-1` and Budgets is
  global; the ACL's ARN arrives here as `web_acl_arn` and is attached to the distribution
  below. The Route 53 Resolver DNS Firewall deny list and its query logging are still
  outstanding -- PLAN assigns those to the compliance pass, and half a pass in the place a
  reader reads as a whole one is worse than none;
* an `aws_s3_deployment.BucketDeployment` for the console, which cannot work: `make
  deploy` writes `web/config.js` from *this stack's own outputs* and only then
  `aws s3 sync`s `web/`, and a stack resource cannot contain a file that does not exist
  until the stack does;
* the `403`/`404` -> `200 /index.html` custom error responses a static site usually
  carries. `web/js/app.js` is a hash router, so no deep link ever reaches CloudFront and
  `default_root_object` is the whole of the routing; the rewrite would meanwhile turn the
  `/api/*` behaviour's error envelopes into `200 text/html`, because
  `CustomErrorResponses` belongs to the distribution and not to a behaviour. The comment
  at the distribution below carries the argument. Two tests hold the premise up rather
  than the conclusion: `tests/test_web_stack.py` refuses a `CustomErrorResponses` on the
  synthesized template, and `tests/test_web_console.py` refuses `pushState` anywhere in
  `web/js/` -- adopt history routing and that is the one that fails, which is where the
  reader is told to put a CloudFront Function back before the paths start 404ing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from constructs import Construct


# The document's own policy. `connect-src` carries exactly two entries and both are
# load-bearing:
#
# * `'self'` covers the API, and covers it *only* because `/api/*` is served from this same
#   distribution -- that is the reason the route keys carry the prefix, and a cross-origin
#   API would force a `https://*.execute-api...` entry here and CORS there.
# * the Cognito hosted-UI origin covers the PKCE token exchange in `web/js/auth.js`, which
#   POSTs to `<prefix>.auth.<region>.amazoncognito.com/oauth2/token`. That origin cannot be
#   made same-origin: the hosted UI is Cognito's own domain, and the exchange is a `fetch`
#   and therefore governed by `connect-src` rather than by `form-action`.
#
# `connect-src 'self'` alone is insufficient: the browser refuses a `fetch` that CSP
# blocks before it leaves -- `TypeError: Failed to fetch` with no policy name -- and `curl`
# does not enforce CSP, so the missing Cognito entry is invisible to any server-side probe.
#
# `form-action` follows the same pattern. `'none'` looks correct because every outbound
# call in this console is a `fetch` -- until `web/js/views/connect.js` submits a real
# `<form>` to GitHub, which is the one thing the app-manifest flow cannot avoid: GitHub
# must receive the manifest under the operator's own session and answer with a confirmation
# page, a navigation and not a response JavaScript may read. With `'none'` the browser
# refuses that submit silently, every server-side signal stays green, and the console
# blocks at "Preparing the GitHub App…" indefinitely. A CSP directive narrowed to current
# behaviour is a tripwire for the next feature, which only a browser can trip.
#
# `tests/test_web_stack.py::test_the_policy_permits_the_github_app_manifest_post` ties this
# entry to `github_app._MANIFEST_FORM_URL`, so the two cannot drift.
def _content_security_policy(cognito_origin: str) -> str:
    return (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        f"font-src 'self'; connect-src 'self' {cognito_origin}; frame-ancestors 'none'; "
        "base-uri 'none'; form-action https://github.com"
    )


def _construct_id(route_key: str) -> str:
    """A stable construct id for a route key: `GET /cards/{cardId}` -> `GETCardsCardId`.

    Derived from the key rather than from an enumeration index so that adding a route to
    `api_stack.ROUTES` does not renumber -- and therefore replace -- every construct
    after it. The raw key cannot be used: a construct id may not contain `/`, and `{}`
    would survive into logical ids.
    """
    return "".join(
        part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", route_key) if part
    )


class WebStack(cdk.Stack):
    """The console's front door. Reads nothing from the other stacks but strings.

    Three of those strings come from `ApiStack` in the same region and one --
    `web_acl_arn` -- from `AccountStack` in `us-east-1`, which is what
    `cross_region_references=True` on both stacks exists to carry.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        app_name: str,
        handler_arns: Mapping[str, str],
        domain_prefix: str,
        web_acl_arn: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # The order below is api -> distribution -> client -> authorizer -> routes, and it
        # is not a style choice. The app client's callback URL needs the distribution's
        # domain name, the distribution's `/api/*` behaviour needs the API's id, and the
        # JWT authorizer needs the client. Any other order needs a forward reference to
        # something not built yet -- which, across the ApiStack boundary, is precisely the
        # cyclic reference the ARN-string hand-off exists to avoid.

        # -- the buckets ----------------------------------------------------------
        log_bucket = s3.Bucket(
            self,
            "LogBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            # SSE-S3 and not SSE-KMS: CloudFront's log delivery cannot write into a
            # KMS-encrypted bucket, and the failure is a bucket that simply stays empty.
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # CloudFront's log delivery writes objects with an ACL, so ownership cannot be
            # BUCKET_OWNER_ENFORCED (the CDK default) here.
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            # RETAIN, unlike the console bucket: if a token is ever misused, these logs
            # are the only thing that says which sessions preceded it.
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        console_bucket = s3.Bucket(
            self,
            # Not `ConsoleBucket` and not `Console`: a `CfnOutput` id and a
            # `cloudfront.Distribution` id are construct ids in the same namespace as
            # every other child of this stack, and this stack already uses both of those
            # names (the output `make deploy` reads, and the distribution). Either
            # collision is `There is already a Construct with name ...` at synth time, so
            # the bucket takes the third name and the output keeps the readable one,
            # because the output is the half a human types.
            "ConsoleAssets",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # Disposable on purpose: the console is rebuilt from `web/` by every deploy,
            # so there is nothing here a `cdk destroy` could lose. Invariant 22 is about
            # the DynamoDB tables, which live in `DataStack` and are RETAIN.
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            server_access_logs_bucket=log_bucket,
            server_access_logs_prefix="s3-access/",
        )

        # -- the handlers, re-imported by ARN -------------------------------------
        # `same_environment=True` is the whole trick: it tells CDK the function is in this
        # account and region, so `HttpLambdaIntegration`'s `addPermission` writes a
        # permission whose `FunctionName` is the literal ARN string above. Drop the
        # keyword and CDK silently adds no permission -- every route then 500s with
        # nothing wrong at synth time. Pass a `Function` object instead of the ARN and the
        # permission becomes a cross-stack reference back into `ApiStack`, and `cdk synth`
        # fails with a cyclic reference. `tests/test_web_stack.py` pins both directions.
        handlers = {
            key: lambda_.Function.from_function_attributes(
                self,
                f"Handler{_construct_id(key)}",
                function_arn=arn,
                same_environment=True,
            )
            for key, arn in sorted(handler_arns.items())
        }

        # -- step 1: the API, with no authorizer and no routes yet ----------------
        # No `cors_preflight`, here or in `ApiStack`: the console is same-origin behind
        # CloudFront and the MCP client SigV4-signs, so CORS is configured nowhere in this
        # repository and `connect-src 'self'` above stays true.
        api = apigwv2.HttpApi(
            self,
            "WebApi",
            api_name=f"{app_name}-console",
            create_default_stage=True,
        )

        # -- step 2: the distribution, which needs the API's id -------------------
        api_domain = f"{api.api_id}.execute-api.{self.region}.{self.url_suffix}"
        # Both CloudFront names below carry `self.region`, and neither is decoration.
        # CloudFront is a *global* service, so a response headers policy name and an origin
        # access control name are unique per ACCOUNT, not per region -- while CDK derives
        # both from the stack name and the construct path, which are identical in every
        # region. So a second fleet deployed to a second region under the same stack prefix
        # collides on names it never chose:
        #
        #   AWS::CloudFront::ResponseHeadersPolicy  Another response headers policy with the
        #     same name already exists within the aws account (409)
        #   AWS::CloudFront::OriginAccessControl    Resource of type ... with identifier
        #     'SampleCodingAgentConsoleOrigin1S3OriginAccessControlD8807F2B' already exists
        #
        # Observed deploying eu-west-1 into an account already running ap-southeast-1. The
        # OAC is the subtler half: `S3BucketOrigin.with_origin_access_control(bucket)` creates
        # one implicitly, so there is no name to fix until the construct is passed explicitly.
        # `-c stackPrefix=` is the other way out -- a different prefix changes both derived
        # names -- but that forces a whole second fleet including another ~1.3 GB image push,
        # for a name collision. This is the same anti-pattern as `rca-execution-boundary` and
        # the Bedrock budget: an account-global name that does not carry the region.
        cloudfront_name = f"{self.stack_name}-{self.region}"
        # Spelled from `domain_prefix` and `self.region` rather than read off the
        # `pool.add_domain(...)` return value below, because the construct order above puts
        # the distribution before the pool: `domain.base_url()` does not exist yet here.
        # Both inputs are plain synth-time strings, so the policy stays a literal in the
        # template instead of becoming an `Fn::Join` a reader cannot grep.
        cognito_origin = f"https://{domain_prefix}.auth.{self.region}.amazoncognito.com"
        headers_policy = cloudfront.ResponseHeadersPolicy(
            self,
            "ConsoleHeaders",
            response_headers_policy_name=cloudfront_name,
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=_content_security_policy(cognito_origin),
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=cdk.Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
            ),
        )
        distribution = cloudfront.Distribution(
            self,
            "Console",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                # Origin access control, not a public bucket and not the legacy OAI: the
                # bucket stays BLOCK_ALL and the distribution is the only reader.
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    console_bucket,
                    # Explicit so the name can carry the region -- see `cloudfront_name`
                    # above. Passing the construct is the only way to name it; the default
                    # is generated from the stack name and path and so is region-blind.
                    origin_access_control=cloudfront.S3OriginAccessControl(
                        self,
                        "ConsoleOac",
                        origin_access_control_name=cloudfront_name,
                    ),
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                # The security headers belong to the document, not to a JSON response, so
                # the policy is attached here rather than to the `/api/*` behaviour.
                response_headers_policy=headers_policy,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    # No `origin_path`: it only *prepends*, and the web API's own route
                    # keys already carry `/api`.
                    origin=origins.HttpOrigin(api_domain),
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    # Required, and not cosmetic: this is what forwards the
                    # `Authorization` header the Cognito JWT authorizer reads, while still
                    # letting the origin see its own host (a forwarded `Host` header makes
                    # API Gateway reject the request outright).
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                )
            },
            # No `error_responses`, deliberately. `web/js/app.js` is a pure hash router --
            # a fragment is never sent to a server -- so the only paths CloudFront ever
            # sees are the real objects, `/api/*`, and `/`, which `default_root_object`
            # above already covers. There is no deep link to rewrite, and the rewrite is
            # not free: `CustomErrorResponses` is a property of the *distribution* and not
            # of a cache behaviour, so a 403/404 -> 200 `/index.html` mapping also rewrites
            # the `/api/*` behaviour's real error envelopes into `200 text/html` --
            # `web/js/api.js::request` then reads `response.ok`, `payloadOf` cannot parse
            # HTML, and a served 404 arrives in a view as a successful `null`. And it does
            # not even do the job it was added for: the page it would serve is
            # `/index.html`, so when that object is the missing one the rewrite cannot fire
            # and CloudFront returns the origin's own body instead -- which is how an empty
            # console bucket surfaces as S3's `<Error><Code>AccessDenied</Code></Error>`
            # XML rather than as anything a reader can act on.
            enable_logging=True,
            log_bucket=log_bucket,
            log_file_prefix="cloudfront/",
            # The property is spelled `web_acl_id` and takes the full **ARN** for a
            # CloudFront-scoped ACL; an id here deploys and protects nothing. The ACL is
            # created in `AccountStack` because `us-east-1` is the only region in which
            # CloudFront scope is legal, and the keyword is required rather than optional so
            # there is no code path that ships this console with no WAF and says nothing.
            web_acl_id=web_acl_arn,
        )
        # The trailing slash is required, not tidiness: `web/js/auth.js::redirectUri()`
        # returns `${location.origin}/`, and the Cognito hosted UI matches a redirect URI
        # against the callback list *exactly*. Without it every sign-in is refused with
        # `redirect_mismatch`.
        console_url = f"https://{distribution.distribution_domain_name}/"

        # -- the user pool (Tier 1 #13 / Tier 1b) --------------------------------
        pool = cognito.UserPool(
            self,
            "ConsoleUsers",
            # An operator is created by an administrator. There is no sign-up page, and a
            # console that can mint its own accounts is a console anyone can join.
            self_sign_up_enabled=False,
            # MFA is OFF, and this is a deliberate step DOWN from Tier 1 #13, taken for a
            # demo and wrong for anything real. `Mfa.REQUIRED` blocks the hosted UI from
            # showing the board until a TOTP is enrolled. This console is a public
            # CloudFront URL in front of an API that can register repositories -- re-enable
            # MFA, with `mfa_second_factor(otp=True, sms=False)`, before this is used for
            # anything but a walkthrough. SMS is never the second factor to add back: it is
            # redirectable at a phone shop and drags an SNS role into this stack.
            #
            # `self_sign_up_enabled=False` stays, and it is what still bounds who can be an
            # operator at all: accounts are minted only by `admin-create-user`. Losing both
            # would make the console joinable rather than merely single-factor.
            mfa=cognito.Mfa.OFF,
            # A relaxed password policy, for the same reason and with the same caveat: the
            # Cognito default demanded upper, lower, digit and symbol, which is a lot of
            # ceremony for a temporary password that is emailed and changed immediately. The
            # 8-character floor and the lowercase requirement are kept so the pool still
            # refuses something trivially guessable.
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=False,
                require_digits=False,
                require_symbols=False,
            ),
            # `feature_plan=PLUS` plus `standard_threat_protection_mode` is the
            # non-deprecated spelling of compromised-credential detection;
            # `advanced_security_mode` is deprecated in aws-cdk-lib 2.266 and warns.
            feature_plan=cognito.FeaturePlan.PLUS,
            standard_threat_protection_mode=cognito.StandardThreatProtectionMode.FULL_FUNCTION,
            sign_in_aliases=cognito.SignInAliases(email=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            # RETAIN: the pool *is* the operator list, and `cdk destroy` must not be the
            # thing that deletes everyone's account and MFA registration.
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        # A prefix domain rather than a custom one: a custom domain needs a certificate in
        # us-east-1 and a DNS zone this deployment does not assume it owns. The prefix has
        # to be a concrete lowercase string, so `infra/app.py` derives it from the account
        # id and never from a CDK token.
        domain = pool.add_domain(
            "HostedUi",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )

        # -- step 3: the app client, which needs the distribution's domain -------
        client = pool.add_client(
            "Console",
            # A browser cannot keep a secret, so there is none and PKCE takes its place
            # (`web/js/auth.js` does S256).
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[console_url],
                logout_urls=[console_url],
            ),
            id_token_validity=cdk.Duration.hours(1),
            access_token_validity=cdk.Duration.hours(1),
            # 30 days, deliberately short (Tier 1b). A JWT authorizer
            # cannot consult Cognito, so the ID token's one-hour lifetime *is* the
            # revocation lag; and `auth.js` refreshes silently two minutes before expiry,
            # so a longer refresh window buys no session length -- only a longer window in
            # which a stolen refresh token still works.
            refresh_token_validity=cdk.Duration.days(30),
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
        )

        # -- step 4: the authorizer, which needs the client ----------------------
        # `pool` is the second *positional* argument, and `user_pool_clients` pins the
        # audience: without it the authorizer accepts a token minted for any client in the
        # pool.
        authorizer = apigwv2_authorizers.HttpUserPoolAuthorizer(
            "ConsoleAuthorizer",
            pool,
            user_pool_clients=[client],
        )

        # -- step 5: the routes, which need the authorizer -----------------------
        # The `/api` prefix is literal because CloudFront cannot strip a path prefix and
        # `origin_path` only prepends -- so the browser's `/api/cards` has to be this
        # API's own route key. Every route is authorized; there is no public route here.
        for key, handler in handlers.items():
            method, path = key.split(" ", 1)
            api.add_routes(
                path=f"/api{path}",
                methods=[apigwv2.HttpMethod(method)],
                integration=apigwv2_integrations.HttpLambdaIntegration(
                    f"Integration{_construct_id(key)}", handler
                ),
                authorizer=authorizer,
            )

        # -- the outputs, which are a contract ------------------------------------
        # `make deploy` reads exactly these seven and writes `web/config.js` from them;
        # `web/config.example.js` is the other half of the contract and
        # `tests/test_web_stack.py` pins the key set. Renaming one here breaks a deploy,
        # not a build.
        cdk.CfnOutput(self, "ConsoleUrl", value=console_url)
        cdk.CfnOutput(self, "ConsoleBucket", value=console_bucket.bucket_name)
        cdk.CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        cdk.CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        cdk.CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        cdk.CfnOutput(self, "CognitoDomain", value=domain.base_url())
        cdk.CfnOutput(self, "Region", value=self.region)
