"""The team-assistant stack and the function that builds the CDK app from context values."""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from team_assistant.config import NAMESPACES, AgentConfig, load_config
from team_assistant_infra.naming import (
    CDK_QUALIFIER,
    DEFAULT_ENV,
    GATEWAY_TARGET,
    PROJECT,
    ROLE_PATH,
    Names,
    names,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
# Where managed login sends the browser after login: the command-line client listens here.
CALLBACK_URL = "http://localhost:8976/callback"
# The web page on its development server (make web): after login and after logout.
# Cognito allows http only for localhost (developer guide, "App clients").
WEB_LOCAL_URL = "http://localhost:5173/"
# The hosted page's fixed security headers. connect-src allows any https
# host here; the exact gateway and login domain are added by a meta tag that make deploy writes
# into each HTML file, and the browser enforces both policies (the stricter wins). Keeping the
# environment's addresses out of this header avoids a loop: the distribution would need the
# gateway, the gateway the Cognito client, and the client the distribution's address.
WEB_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "connect-src 'self' https:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

# Tracing off ([tracing] enabled = false in config/agent.toml): nothing is exported. The image
# still starts the agent under ADOT, so the switch is the environment, not the image.
TRACING_OFF = {
    # The Runtime adds none of its own OpenTelemetry settings to the container (devguide,
    # observability-configure).
    "DISABLE_ADOT_OBSERVABILITY": "true",
    # The OpenTelemetry SDK hands out spans that record nothing (sdk/trace/__init__.py).
    "OTEL_SDK_DISABLED": "true",
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOGS_EXPORTER": "none",
    "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED": "false",
}

# Tracing on: what the agent's spans may hold and where they go. Read by Strands and by ADOT
# (aws-opentelemetry-distro 0.22.0, started by the image's CMD); each value was checked in their
# installed sources. OTEL_RESOURCE_ATTRIBUTES is left to the Runtime on purpose.
TRACING_ENVIRONMENT = {
    # Spans go to the agent's own log group, which `make destroy` deletes, not to the
    # account's shared aws/spans group.
    "UNIFIED_TRACES_DESTINATION_ENABLED": "true",
    # Strands: redact prompts, answers, the system prompt and tool input and output (an empty
    # list of unredacted attributes). Without the token, all of them are written into spans.
    "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_unredacted_attributes=",
    # ADOT would set this to true and record the content of Bedrock calls.
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
    # ADOT would export Strands' metrics with tool-use and cycle ids as dimensions: a new
    # metric per call, and CloudWatch metrics cannot be deleted.
    "OTEL_METRICS_EXPORTER": "none",
    # ADOT would export every Python log line a second time; stdout is already captured.
    "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED": "false",
    # ADOT replaces these attributes of exception events before export.
    "AWS_REDACT_SPAN_ATTRIBUTES": "exception.message,exception.stacktrace",
    # ADOT's own default list, plus the clients of AWS and HTTP calls: their spans carry the
    # error text of a failed call as the span status (it can quote request values) and the
    # address of the request. The model call and the tool call are still traced by Strands.
    "OTEL_PYTHON_DISABLED_INSTRUMENTATIONS": ",".join(
        [
            *"http,sqlalchemy,psycopg2,pymysql,sqlite3,aiopg,asyncpg,mysql_connector".split(","),
            *"urllib3,requests,system_metrics,google-genai,jinja2".split(","),
            *"botocore,aiobotocore,urllib,httpx,httpx2,aiohttp-client".split(","),
        ]
    ),
}


class TeamAssistantStack(cdk.Stack):
    """One environment of team-assistant."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        names: Names,
        config: AgentConfig | None = None,
        github_client_id: str = "placeholder",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.names = names
        self.config = config or load_config()
        # Tags on the stack are copied by CloudFormation to every resource that supports tags.
        cdk.Tags.of(self).add("project", PROJECT)
        cdk.Tags.of(self).add("env", names.env)
        # The page's hosting first: Cognito and the Runtime need its address.
        self.web_url = self._web()
        self.user_pool, self.user_pool_client = self._users()
        self.memory = self._memory()
        self.guardrail = self._guardrail()
        self.github_provider = self._github(github_client_id)
        # The gateway first: the Runtime names it as the only caller it accepts.
        self.gateway = self._gateway()
        self.runtime = self._runtime()
        self.exchange_provider = self._exchange_provider()
        self.gateway_target = self._gateway_target()

    def _exchange_provider(self) -> agentcore.CfnOAuth2CredentialProvider:
        """How the gateway gets the token it sends to the Runtime: it exchanges the caller's
        token at this environment's user pool (RFC 8693) for a new access token of the same
        user, bound to the agent.

        The provider acts as the login client: Cognito exchanges a token only for the client
        that issued it, and that client is public and has no secret. AgentCore refuses a
        provider without one for this authentication method, and Cognito ignores a secret sent
        in the request body for a public client (both). So the
        provider gets a generated value from a Secrets Manager secret that nobody needs to
        know or set; it is a placeholder, not a credential. The issued token has
        the same sub, client_id and aud as the user's own token, which is what the Runtime's
        authorizer checks, and it expires with the token it was derived from. The exchange
        narrows nothing; it is what puts the gateway into the request's identity chain.
        """
        placeholder = secretsmanager.Secret(
            self,
            "ExchangeSecret",
            secret_name=self.names.exchange_secret,
            description=(
                "Placeholder client secret of the token exchange provider of team-assistant "
                f"{self.names.env}; Cognito ignores it"
            ),
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template="{}", generate_string_key="client_secret"
            ),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.exchange_secret = placeholder
        Config = agentcore.CfnOAuth2CredentialProvider
        provider = Config(
            self,
            "ExchangeProvider",
            name=self.names.exchange_provider,
            credential_provider_vendor="CustomOauth2",
            oauth2_provider_config_input=Config.Oauth2ProviderConfigInputProperty(
                custom_oauth2_provider_config=Config.CustomOauth2ProviderConfigInputProperty(
                    oauth_discovery=Config.Oauth2DiscoveryProperty(
                        discovery_url=self.discovery_url
                    ),
                    client_id=self.user_pool_client.user_pool_client_id,
                    # Client id and secret travel in the request body, where Cognito ignores
                    # the secret; in a Basic header it refuses the request.
                    client_authentication_method="CLIENT_SECRET_POST",
                    client_secret_source="EXTERNAL",
                    client_secret_config=Config.SecretReferenceProperty(
                        secret_id=placeholder.secret_arn, json_key="client_secret"
                    ),
                    on_behalf_of_token_exchange_config=Config.OnBehalfOfTokenExchangeConfigProperty(
                        grant_type="TOKEN_EXCHANGE",
                        # No actor token: the issued token is the user's, without an act claim.
                        token_exchange_grant_type_config=Config.TokenExchangeGrantTypeConfigProperty(
                            actor_token_content="NONE"
                        ),
                    ),
                )
            ),
        )
        provider.apply_removal_policy(cdk.RemovalPolicy.DESTROY)
        return provider

    def _gateway_target(self) -> agentcore.CfnGatewayTarget:
        """Where the gateway sends a request it has accepted: this environment's Runtime.

        The gateway does not pass the caller's token on. It exchanges it (_exchange_provider)
        and sends the issued token, which names the same user; the Runtime verifies it and
        reads the user from it as before.
        """
        stack = cdk.Stack.of(self)

        def arn(resource: str) -> str:
            return stack.format_arn(
                service="bedrock-agentcore",
                resource=resource,
                arn_format=cdk.ArnFormat.NO_RESOURCE_NAME,
            )

        # What the exchange takes: a workload access token for the gateway's own identity
        # (named after the gateway) and the token from the exchange provider.
        self.gateway_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ExchangeUserTokens",
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetResourceOauth2Token",
                ],
                resources=[
                    arn("token-vault/default"),
                    self.exchange_provider.attr_credential_provider_arn,
                    arn("workload-identity-directory/default"),
                    arn(
                        "workload-identity-directory/default/workload-identity/"
                        f"{self.names.gateway}-*"
                    ),
                ],
            )
        )
        # AgentCore reads an EXTERNAL client secret with the caller's credentials.
        self.gateway_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ExchangeSecret",
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.exchange_secret.secret_arn],
            )
        )
        self._deny_user_id_access(self.gateway_role)
        runtime_arn = self.runtime.agent_runtime_arn
        # Invoking this one Runtime (and its endpoints) is all the gateway's role may do.
        self.gateway_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="InvokeThisRuntime",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[runtime_arn, f"{runtime_arn}/runtime-endpoint/*"],
            )
        )
        Config = agentcore.CfnGatewayTarget
        target = Config(
            self,
            "GatewayTarget",
            name=GATEWAY_TARGET,
            description=f"The team-assistant Runtime, env {self.names.env}",
            gateway_identifier=self.gateway.attr_gateway_identifier,
            target_configuration=Config.TargetConfigurationProperty(
                http=Config.HttpTargetConfigurationProperty(
                    agentcore_runtime=Config.RuntimeTargetConfigurationProperty(arn=runtime_arn)
                )
            ),
            credential_provider_configurations=[
                Config.CredentialProviderConfigurationProperty(
                    credential_provider_type="OAUTH",
                    credential_provider=Config.CredentialProviderProperty(
                        oauth_credential_provider=Config.OAuthCredentialProviderProperty(
                            provider_arn=self.exchange_provider.attr_credential_provider_arn,
                            grant_type="TOKEN_EXCHANGE",
                            # The user's token carries this scope only; the issued token gets
                            # the scopes common to both.
                            scopes=["openid"],
                            custom_parameters={
                                # Cognito refuses the type AgentCore sends by default
                                # ("...:jwt").
                                "subject_token_type": (
                                    "urn:ietf:params:oauth:token-type:access_token"
                                ),
                                "audience": self.names.audience,
                            },
                        )
                    ),
                )
            ],
        )
        target.apply_removal_policy(cdk.RemovalPolicy.DESTROY)
        # The role's policy exists before the target is created.
        target.node.add_dependency(self.gateway_role)
        return target

    def _gateway(self) -> agentcore.CfnGateway:
        """The address clients call: an AgentCore Gateway that checks the user's token (the
        same pool, client and audience as the Runtime) before anything reaches the agent.

        The low-level resource is used on purpose: the Gateway construct always sets the MCP
        protocol and has no target of the Runtime type (aws-cdk-lib 2.275.0). The target that
        points at the Runtime is added by _gateway_target.
        """
        stack = cdk.Stack.of(self)
        # Only this environment's gateway, in this account, can assume the role (a gateway id
        # is <name>-<10 characters>).
        role = iam.Role(
            self,
            "GatewayRole",
            path=ROLE_PATH,
            description=f"Service role of the team-assistant gateway, env {self.names.env}",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {
                        "aws:SourceArn": stack.format_arn(
                            service="bedrock-agentcore",
                            resource=f"gateway/{self.names.gateway}-*",
                            arn_format=cdk.ArnFormat.NO_RESOURCE_NAME,
                        )
                    },
                },
            ),
        )
        Config = agentcore.CfnGateway
        gateway = Config(
            self,
            "Gateway",
            name=self.names.gateway,
            description=f"Entry to the team-assistant agent, env {self.names.env}",
            role_arn=role.role_arn,
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=Config.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=Config.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=self.discovery_url,
                    allowed_clients=[self.user_pool_client.user_pool_client_id],
                    allowed_audience=[self.names.audience],
                )
            ),
        )
        gateway.apply_removal_policy(cdk.RemovalPolicy.DESTROY)
        self.gateway_role = role
        cdk.CfnOutput(self, "GatewayUrl", value=gateway.attr_gateway_url)
        return gateway

    def _web(self) -> str:
        """The web page's hosting: a private bucket that only this distribution reads
        (origin access control), HTTPS only, fixed security headers. make deploy uploads the
        files with aws s3 sync, not BucketDeployment (a Lambda function, a role and a CLI layer
        to copy a few files). Returns the page's address, https://<id>.cloudfront.net."""
        bucket = s3.Bucket(
            self,
            "PageBucket",
            bucket_name=self.names.web_bucket,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            # make destroy empties the bucket first; no autoDeleteObjects (a Lambda function).
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "PageHeaders",
            response_headers_policy_name=f"{PROJECT}-{self.names.env}-web",
            comment=f"Security headers of the team-assistant page, env {self.names.env}",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=WEB_CSP, override=True
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=cdk.Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                # The consent return carries a session id in its address: never sent on.
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True
                ),
            ),
        )
        distribution = cloudfront.Distribution(
            self,
            "PageDistribution",
            comment=f"team-assistant page, env {self.names.env}",
            default_root_object="index.html",
            # North America and Europe only: the cheapest class; the page is a demo.
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=headers,
            ),
        )
        url = f"https://{distribution.distribution_domain_name}"
        cdk.CfnOutput(self, "WebUrl", value=url)
        cdk.CfnOutput(self, "WebBucket", value=bucket.bucket_name)
        cdk.CfnOutput(self, "WebDistributionId", value=distribution.distribution_id)
        return url

    def _guardrail(self) -> bedrock.CfnGuardrail:
        """Checks text written by others (GitHub issues) for instructions to the assistant
        before the model reads it (guard.py). Only the PROMPT_ATTACK filter, at LOW."""
        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=self.names.guardrail,
            blocked_input_messaging="Blocked: the text may hold instructions to the assistant.",
            # Required by the resource; the agent checks no model output.
            blocked_outputs_messaging="Blocked.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK", input_strength="LOW", output_strength="NONE"
                    )
                ]
            ),
        )
        cdk.CfnOutput(self, "GuardrailId", value=guardrail.attr_guardrail_id)
        return guardrail

    def _runtime(self) -> agentcore.Runtime:
        """The agent itself: a container on AgentCore Runtime, callable only with a token of
        this environment's user pool (docs/adr/0003)."""
        runtime = agentcore.Runtime(
            self,
            "Runtime",
            runtime_name=self.names.runtime,
            description=f"team-assistant agent, env {self.names.env}",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_asset(
                str(REPO_ROOT),
                file="docker/agent.Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            # The Runtime checks signature, issuer and expiry of the bearer token, that its
            # client_id claim is this app client and that its aud claim is this environment's
            # audience, before the request reaches the agent.
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_cognito(
                self.user_pool, [self.user_pool_client], [self.names.audience]
            ),
            # Headers are not passed to the agent unless listed; the agent reads the user id
            # from this one.
            request_header_configuration=agentcore.RequestHeaderConfiguration(
                allowlisted_headers=["Authorization"]
            ),
            environment_variables={
                "MEMORY_ID": self.memory.attr_memory_id,
                # The credential provider the list_issues tool asks for tokens (github.py).
                "GITHUB_PROVIDER": self.names.github_provider,
                # The hosted page: its return page after a GitHub consent (config.return_url).
                "WEB_URL": self.web_url,
                # The guardrail that checks the GitHub tools' results (guard.py).
                "GUARDRAIL_ID": self.guardrail.attr_guardrail_id,
                **(TRACING_ENVIRONMENT if self.config.tracing else TRACING_OFF),
            },
            # A session that gets no request for 5 minutes is stopped (default: 15), so an
            # abandoned conversation stops holding memory sooner.
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=cdk.Duration.minutes(5)
            ),
        )
        # Only requests relayed by this environment's gateway are accepted: a call straight to
        # the Runtime has no gateway in its identity chain and is refused even with a valid
        # token. The gateway's request carries that chain only when the gateway itself gets
        # the token for the Runtime (the target's token exchange); with JWT_PASSTHROUGH the
        # Runtime refused the gateway too. The construct's
        # authorizer has no parameter for this (aws-cdk-lib 2.275.0).
        runtime.node.default_child.add_property_override(
            "AuthorizerConfiguration.CustomJWTAuthorizer.AllowedWorkloadConfiguration",
            {"HostingEnvironments": [{"Arn": self.gateway.attr_gateway_arn}]},
        )
        # The deploy permissions allow roles only under this path (docs/adr/0001).
        runtime.role.node.default_child.path = ROLE_PATH
        # The construct trusts runtime/<name>*, which also matches a Runtime whose name only
        # starts with this one. A Runtime id is <name>-<10 characters>.
        runtime.role.node.default_child.add_property_override(
            "AssumeRolePolicyDocument.Statement.0.Condition.ArnLike.aws:SourceArn",
            cdk.Stack.of(self).format_arn(
                service="bedrock-agentcore",
                resource=f"runtime/{self.names.runtime}-*",
                arn_format=cdk.ArnFormat.NO_RESOURCE_NAME,
            ),
        )
        self._grant_agent(runtime.role)
        cdk.CfnOutput(self, "RuntimeArn", value=runtime.agent_runtime_arn)
        return runtime

    def _grant_agent(self, role: iam.IRole) -> None:
        """What the agent's own code calls (docs/security.md lists every action of the role)."""
        partition, region, account = self.partition, self.region, self.account
        # A "global." inference profile routes to the model in any region, so the model is
        # allowed without a region and in this region (aws bedrock get-inference-profile).
        model = self.config.model_id.removeprefix("global.")
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="InvokeConfiguredModel",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:{partition}:bedrock:{region}:{account}:inference-profile/{self.config.model_id}",
                    f"arn:{partition}:bedrock:::foundation-model/{model}",
                    f"arn:{partition}:bedrock:{region}::foundation-model/{model}",
                ],
            )
        )
        # The calls the memory session manager makes (bedrock-agentcore 1.26.0,
        # memory/integrations/strands/session_manager.py), on this environment's memory only;
        # and ListSessions for the web page's conversation list (conversations.py).
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ConversationEvents",
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:DeleteEvent",
                    "bedrock-agentcore:ListSessions",
                ],
                resources=[self.memory.attr_memory_arn],
            )
        )
        # Long-term records (recall.py): the user's preferences are listed, facts and
        # summaries are searched by the model's memory tools. No IAM
        # condition on the namespace: the role is shared by every user, so it cannot tell
        # alice from bob; that separation is the actor id from the verified token
        # (identity.py).
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="LongTermRecords",
                actions=[
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                ],
                resources=[self.memory.attr_memory_arn],
            )
        )
        # AgentCore uses this to let X-Ray deliver the agent's spans to the agent's own log
        # group (devguide, observability-configure and runtime-permissions).
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="SpanDelivery",
                actions=["logs:PutResourcePolicy"],
                resources=[
                    cdk.Stack.of(self).format_arn(
                        service="logs",
                        resource="log-group",
                        resource_name=f"/aws/bedrock-agentcore/runtimes/{self.names.runtime}-*",
                        arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
                    )
                ],
            )
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="GuardToolResults",
                actions=["bedrock:ApplyGuardrail"],
                resources=[self.guardrail.attr_guardrail_arn],
            )
        )
        self._github_permissions(role)
        self._deny_user_id_access(role)
        # The Runtime construct allows the token actions on every workload identity of the
        # account, and the boundary (one for both roles) allows the gateway's identity. The
        # agent needs a token only for its own identity, so every other identity is denied.
        # AgentCore checks these actions against two resources, the identity directory and
        # the identity itself; the Deny leaves out both for the agent's own identity.
        stack = cdk.Stack.of(self)
        directory = "workload-identity-directory/default"
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="DenyOtherWorkloadIdentities",
                effect=iam.Effect.DENY,
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                ],
                not_resources=[
                    stack.format_arn(
                        service="bedrock-agentcore",
                        resource=resource,
                        arn_format=cdk.ArnFormat.NO_RESOURCE_NAME,
                    )
                    for resource in (
                        directory,
                        f"{directory}/workload-identity/{self.names.runtime}-*",
                    )
                ],
            )
        )

    def _deny_user_id_access(self, role: iam.IRole) -> None:
        """Deny the two actions that name a user by a plain id instead of a verified token.

        Every request here carries a user's token, so nothing needs them, and with them a
        caller could act for any user id it chooses (AgentCore developer guide,
        runtime-security-best-practices). An explicit Deny holds even if an Allow is added
        later; the Runtime construct allows GetWorkloadAccessTokenForUserId by default.
        """
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="DenyUserIdAccess",
                effect=iam.Effect.DENY,
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                resources=["*"],
            )
        )

    def _github_permissions(self, role: iam.IRole) -> None:
        """What the agent needs to get a user's GitHub token and to complete a user's consent.

        Resource types from the IAM service reference for bedrock-agentcore: the account's
        default token vault, this provider, the workload identity directory and this Runtime's
        identity. No condition on the user: one role serves every user; the user is named by
        the token of the request (identity.py). GetSecretValue: AgentCore reads an EXTERNAL
        client secret with the caller's credentials (seen when the provider was created).
        """
        stack = cdk.Stack.of(self)

        def arn(resource: str) -> str:
            return stack.format_arn(
                service="bedrock-agentcore",
                resource=resource,
                arn_format=cdk.ArnFormat.NO_RESOURCE_NAME,
            )

        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="GithubTokens",
                actions=[
                    "bedrock-agentcore:GetResourceOauth2Token",
                    "bedrock-agentcore:CompleteResourceTokenAuth",
                ],
                resources=[
                    arn("token-vault/default"),
                    self.github_provider.attr_credential_provider_arn,
                    arn("workload-identity-directory/default"),
                    arn(
                        "workload-identity-directory/default/workload-identity/"
                        f"{self.names.runtime}-*"
                    ),
                ],
            )
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="GithubClientSecret",
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.github_secret.secret_arn],
            )
        )

    def _memory(self) -> agentcore.CfnMemory:
        """Where conversations are kept, and the long-term records extracted from them.

        The low-level resource is used on purpose: the Memory construct always creates an IAM
        role, which would hold no permission here (no own key, no stream delivery). Only
        built-in strategies with overrides need a role, to invoke models in this account.
        Without an encryption key property the service encrypts with a key it owns (no key to
        pay for).
        """
        # namespace_templates: the API marks `namespaces` as deprecated.
        Strategy = agentcore.CfnMemory.MemoryStrategyProperty
        strategies = [
            Strategy(
                user_preference_memory_strategy=agentcore.CfnMemory.UserPreferenceMemoryStrategyProperty(
                    name="preferences", namespace_templates=[NAMESPACES["preferences"]]
                )
            ),
            Strategy(
                semantic_memory_strategy=agentcore.CfnMemory.SemanticMemoryStrategyProperty(
                    name="facts", namespace_templates=[NAMESPACES["facts"]]
                )
            ),
            Strategy(
                summary_memory_strategy=agentcore.CfnMemory.SummaryMemoryStrategyProperty(
                    name="summaries", namespace_templates=[NAMESPACES["summaries"]]
                )
            ),
        ]
        memory = agentcore.CfnMemory(
            self,
            "Memory",
            name=self.names.memory,
            description=f"Conversations and long-term records of team-assistant {self.names.env}",
            event_expiry_duration=self.config.event_expiry_days,
            memory_strategies=strategies,
        )
        memory.apply_removal_policy(cdk.RemovalPolicy.DESTROY)
        cdk.CfnOutput(self, "MemoryId", value=memory.attr_memory_id)
        return memory

    def _github(self, client_id: str) -> agentcore.CfnOAuth2CredentialProvider:
        """How the agent gets a user's GitHub token: a credential provider of AgentCore
        Identity for the environment's GitHub OAuth App.

        The app's client secret lives in a Secrets Manager secret that the provider references
        (clientSecretSource EXTERNAL), so the template never holds it. The stack creates the
        secret with a generated placeholder; make github writes the real value. The client id
        is not secret but identifies the operator's app, so it comes from local.mk, not from a
        committed file.
        """
        secret = secretsmanager.Secret(
            self,
            "GithubClientSecret",
            secret_name=self.names.github_secret,
            description=f"Client secret of the GitHub OAuth App of team-assistant {self.names.env}",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template="{}", generate_string_key="client_secret"
            ),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        Config = agentcore.CfnOAuth2CredentialProvider
        provider = Config(
            self,
            "GithubProvider",
            name=self.names.github_provider,
            credential_provider_vendor="GithubOauth2",
            oauth2_provider_config_input=Config.Oauth2ProviderConfigInputProperty(
                github_oauth2_provider_config=Config.GithubOauth2ProviderConfigInputProperty(
                    client_id=client_id,
                    client_secret_source="EXTERNAL",
                    client_secret_config=Config.SecretReferenceProperty(
                        secret_id=secret.secret_arn, json_key="client_secret"
                    ),
                )
            ),
        )
        provider.apply_removal_policy(cdk.RemovalPolicy.DESTROY)
        self.github_secret = secret
        # The GitHub OAuth App must list this URL as a redirect URI (docs/runbook.md).
        cdk.CfnOutput(self, "GithubCallbackUrl", value=provider.attr_callback_url)
        return provider

    def _users(self) -> tuple[cognito.UserPool, cognito.UserPoolClient]:
        """Who may talk to the agent: a Cognito user pool that issues the JWT (docs/adr/0002)."""
        names = self.names
        pool = cognito.UserPool(
            self,
            "Users",
            user_pool_name=names.user_pool,
            # Essentials: the lowest plan that binds an access token to a resource (the aud
            # claim, RFC 8707).
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            # Only the operator creates users (make user); nobody can register.
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True),
            # No e-mail or phone: no messages are sent, so no SES or SNS setup and no cost.
            account_recovery=cognito.AccountRecovery.NONE,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        domain = pool.add_domain(
            "LoginDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=names.domain_prefix),
            # Resource binding works only with managed login, not with the classic hosted UI.
            managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
        )
        client = pool.add_client(
            "CliClient",
            user_pool_client_name=f"{names.user_pool}-cli",
            # A public client (a command-line program cannot keep a secret): authorization code
            # with PKCE only.
            generate_secret=False,
            auth_flows=cognito.AuthFlow(),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID],
                callback_urls=[CALLBACK_URL, WEB_LOCAL_URL, f"{self.web_url}/"],
                logout_urls=[WEB_LOCAL_URL, f"{self.web_url}/"],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            access_token_validity=cdk.Duration.minutes(60),
            id_token_validity=cdk.Duration.minutes(60),
            refresh_token_validity=cdk.Duration.days(1),
            prevent_user_existence_errors=True,
        )
        # Without this property Cognito also allows the password (SRP) and custom flows of its
        # API (CreateUserPoolClient, ExplicitAuthFlows). Managed login is the only way in here;
        # the refresh flow keeps a login alive.
        # The second flow lets this client exchange a token it issued for a new one of the same
        # user (RFC 8693, Cognito GetDerivedToken). The gateway does that for every request
        # (_exchange_provider); Cognito exchanges a token only for the client that issued it
        # (another client of the pool is refused).
        client.node.default_child.explicit_auth_flows = [
            "ALLOW_REFRESH_TOKEN_AUTH",
            "ALLOW_TOKEN_EXCHANGE_IMPERSONATION",
        ]
        # Managed login pages of an app client created by code answer 403 until the client has
        # a style; this one is Cognito's default style, with no settings or assets.
        cognito.CfnManagedLoginBranding(
            self,
            "LoginStyle",
            user_pool_id=pool.user_pool_id,
            client_id=client.user_pool_client_id,
            use_cognito_provided_values=True,
        )
        # The resource a login asks its token to be bound to (auth.py sends resource=<this>);
        # the token then carries it as aud. Registered as a resource server, without scopes,
        # because the Cognito guide starts from one; the URL has no network meaning.
        # The name is given because it defaults to the identifier, and a name may not hold
        # ":" or "/" (cognito-idp ResourceServerNameType).
        pool.add_resource_server(
            "Audience",
            identifier=names.audience,
            user_pool_resource_server_name=f"{names.user_pool}-audience",
        )
        region = cdk.Stack.of(self).region
        cdk.CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        cdk.CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        # Where a verifier of tokens (the Runtime, the gateway) finds the pool's signing keys.
        self.discovery_url = (
            f"https://cognito-idp.{region}.amazonaws.com/{pool.user_pool_id}"
            "/.well-known/openid-configuration"
        )
        cdk.CfnOutput(self, "DiscoveryUrl", value=self.discovery_url)
        cdk.CfnOutput(self, "Audience", value=names.audience)
        cdk.CfnOutput(
            self,
            "LoginDomain",
            value=f"https://{domain.domain_name}.auth.{region}.amazoncognito.com",
        )
        return pool, client


def _context(app: cdk.App, key: str) -> str:
    value = app.node.try_get_context(key)
    if not value:
        raise ValueError(f"missing required context: {key} (pass with -c {key}=<value>)")
    return str(value)


def build_app(app: cdk.App, *, require_env: bool = False) -> cdk.App:
    """Add the stack to ``app``. Context: env (default dev unless required), account, region."""
    env_name = app.node.try_get_context("env")
    if not env_name:
        if require_env:
            raise ValueError("missing required context: env (pass with -c env=<name>)")
        env_name = DEFAULT_ENV
    n = names(
        str(env_name),
        _context(app, "account"),
        _context(app, "region"),
        _context(app, "login_suffix"),
    )
    TeamAssistantStack(
        app,
        n.stack,
        names=n,
        # make deploy requires it (scripts/ops.sh); synth and tests may use a placeholder.
        github_client_id=str(app.node.try_get_context("github_client_id") or "placeholder"),
        env=cdk.Environment(account=n.account, region=n.region),
        # This project's own CDK bootstrap, not the account's default one (docs/adr/0001).
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=CDK_QUALIFIER),
        # Every IAM role of the stack gets this environment's permissions boundary.
        permissions_boundary=cdk.PermissionsBoundary.from_name(n.boundary_policy),
    )
    return app
