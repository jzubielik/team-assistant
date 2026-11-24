"""The synthesised stack: named by naming.py, tagged, and empty until resources are added."""

from __future__ import annotations

import re

import aws_cdk as cdk

# Imported for its side effect: jsii registers ArtifactManifest as a data type only when this
# submodule is loaded, and reading `artifact.manifest` fails with "Unknown interface" when it
# is not. aws-cdk-lib loads its submodules lazily, so without this line the load depends on
# what the synthesis happened to touch first.
import aws_cdk.cloud_assembly_schema  # noqa: F401
import pytest
from team_assistant_infra.stack import build_app

CONTEXT = {"account": "123456789012", "region": "eu-west-1", "login_suffix": "0a1b2c3d"}


def synth(**context):
    app = cdk.App(context={**CONTEXT, **context})
    return build_app(app).synth()


def test_stack_is_named_by_env():
    assert [s.stack_name for s in synth(env="qa-2").stacks] == ["TeamAssistant-qa-2"]


def test_env_defaults_to_dev_outside_the_cli():
    assert [s.stack_name for s in synth().stacks] == ["TeamAssistant-dev"]


def test_stack_has_the_project_and_env_tags():
    stack = synth(env="dev").get_stack_by_name("TeamAssistant-dev")
    assert stack.tags == {"project": "team-assistant", "env": "dev"}


def test_stack_targets_the_given_account_and_region():
    stack = synth(env="dev").get_stack_by_name("TeamAssistant-dev")
    assert stack.environment.account == CONTEXT["account"]
    assert stack.environment.region == CONTEXT["region"]


def resources(template: dict, kind: str) -> list[dict]:
    return [r for r in template["Resources"].values() if r["Type"] == kind]


TEMPLATE = (
    synth(env="dev", github_client_id="Iv1.example").get_stack_by_name("TeamAssistant-dev").template
)


def test_stack_has_only_the_expected_resource_types():
    types = {r["Type"] for r in TEMPLATE["Resources"].values()}
    assert types - {"AWS::CDK::Metadata"} == {
        "AWS::Cognito::UserPool",
        "AWS::Cognito::UserPoolClient",
        "AWS::Cognito::UserPoolDomain",
        "AWS::Cognito::ManagedLoginBranding",
        "AWS::Cognito::UserPoolResourceServer",
        "AWS::BedrockAgentCore::Memory",
        "AWS::BedrockAgentCore::Runtime",
        "AWS::BedrockAgentCore::Gateway",
        "AWS::BedrockAgentCore::GatewayTarget",
        "AWS::BedrockAgentCore::PolicyEngine",
        "AWS::BedrockAgentCore::Policy",
        "AWS::IAM::Role",
        "AWS::IAM::Policy",
        "AWS::SecretsManager::Secret",
        "AWS::BedrockAgentCore::OAuth2CredentialProvider",
        "AWS::Bedrock::Guardrail",
        # The function that adds the Memory namespaces to a token.
        "AWS::Lambda::Function",
        "AWS::Lambda::Permission",
        # The web page: no Lambda function, no custom resource.
        "AWS::S3::Bucket",
        "AWS::S3::BucketPolicy",
        "AWS::CloudFront::Distribution",
        "AWS::CloudFront::OriginAccessControl",
        "AWS::CloudFront::ResponseHeadersPolicy",
    }


def test_github_secret_holds_only_a_generated_placeholder():
    secret = TEMPLATE["Resources"][_secret_id("github")]
    properties = secret["Properties"]
    assert properties["Name"] == "team-assistant/dev/github"
    # No value in the template: make github writes the real one (docs/runbook.md).
    assert "SecretString" not in properties
    assert properties["GenerateSecretString"]["GenerateStringKey"] == "client_secret"
    assert secret["DeletionPolicy"] == "Delete"


def test_github_provider_points_at_the_secret_and_takes_the_client_id_from_context():
    provider = _provider("github")
    properties = provider["Properties"]
    assert properties["Name"] == "team-assistant-dev-github"
    assert properties["CredentialProviderVendor"] == "GithubOauth2"
    github = properties["Oauth2ProviderConfigInput"]["GithubOauth2ProviderConfig"]
    assert github["ClientId"] == "Iv1.example"
    assert github["ClientSecretSource"] == "EXTERNAL"
    assert "ClientSecret" not in github
    secret_id = _secret_id("github")
    reference = {"SecretId": {"Ref": secret_id}, "JsonKey": "client_secret"}
    assert github["ClientSecretConfig"] == reference
    assert provider["DeletionPolicy"] == "Delete"


def test_runtime_accepts_only_tokens_of_this_pools_client_bound_to_this_agent():
    (runtime,) = resources(TEMPLATE, "AWS::BedrockAgentCore::Runtime")
    properties = runtime["Properties"]
    assert properties["AgentRuntimeName"] == "team_assistant_dev_runtime"
    (pool_id,) = [
        k for k, r in TEMPLATE["Resources"].items() if r["Type"] == "AWS::Cognito::UserPool"
    ]
    client_id = _client_id("cli")
    authorizer = properties["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert authorizer["AllowedClients"] == [{"Ref": client_id}]
    # Both are checked: a token of this client that was not bound to this agent is refused.
    assert authorizer["AllowedAudience"] == ["https://team-assistant-dev.agent"]
    # Only requests relayed by this environment's gateway: a direct call is refused even with
    # a valid token.
    gateway_id = _gateway_id("team-assistant-dev-gateway")
    assert authorizer["AllowedWorkloadConfiguration"] == {
        "HostingEnvironments": [{"Arn": {"Fn::GetAtt": [gateway_id, "GatewayArn"]}}]
    }
    assert {"Ref": pool_id} in authorizer["DiscoveryUrl"]["Fn::Join"][1]
    assert str(authorizer["DiscoveryUrl"]).endswith("/.well-known/openid-configuration']]}")
    assert properties["RequestHeaderConfiguration"] == {"RequestHeaderAllowlist": ["Authorization"]}
    variables = properties["EnvironmentVariables"]
    assert variables["MEMORY_ID"] == {"Fn::GetAtt": ["Memory", "MemoryId"]}
    assert variables["GITHUB_PROVIDER"] == "team-assistant-dev-github"
    assert properties["LifecycleConfiguration"] == {"IdleRuntimeSessionTimeout": 300}
    assert properties["ProtocolConfiguration"] == "HTTP"


def template_with(config) -> dict:
    from team_assistant_infra.naming import names
    from team_assistant_infra.stack import TeamAssistantStack

    app = cdk.App()
    n = names("dev", CONTEXT["account"], CONTEXT["region"], CONTEXT["login_suffix"])
    TeamAssistantStack(app, n.stack, names=n, config=config)
    return app.synth().get_stack_by_name(n.stack).template


def test_tracing_is_off_by_default_and_the_agent_then_exports_nothing():
    (runtime,) = resources(TEMPLATE, "AWS::BedrockAgentCore::Runtime")
    variables = dict(runtime["Properties"]["EnvironmentVariables"])
    del variables["MEMORY_ID"], variables["GITHUB_PROVIDER"], variables["WEB_URL"]
    del variables["MEMORY_GATEWAY_URL"]
    del variables["GUARDRAIL_ID"]
    assert variables == {
        # The Runtime adds none of its OpenTelemetry settings to the container.
        "DISABLE_ADOT_OBSERVABILITY": "true",
        # The OpenTelemetry SDK hands out spans that record nothing.
        "OTEL_SDK_DISABLED": "true",
        "OTEL_TRACES_EXPORTER": "none",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED": "false",
    }


def test_runtime_tracing_settings_keep_content_out_and_spans_deletable():
    from team_assistant.config import AgentConfig

    traced = template_with(AgentConfig("m", 100, 30, tracing=True))
    (runtime,) = resources(traced, "AWS::BedrockAgentCore::Runtime")
    variables = dict(runtime["Properties"]["EnvironmentVariables"])
    del variables["MEMORY_ID"], variables["GITHUB_PROVIDER"], variables["WEB_URL"]
    del variables["MEMORY_GATEWAY_URL"]
    del variables["GUARDRAIL_ID"]
    disabled = variables.pop("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS").split(",")
    assert variables == {
        # Spans go to the agent's own log group, which make destroy deletes.
        "UNIFIED_TRACES_DESTINATION_ENABLED": "true",
        # Strands redacts prompts, answers, the system prompt and tool input and output.
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_unredacted_attributes=",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
        # No custom metrics: they cannot be deleted and would get an id as a dimension.
        "OTEL_METRICS_EXPORTER": "none",
        # stdout already reaches the log group; a second copy would be counted twice.
        "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED": "false",
        "AWS_REDACT_SPAN_ATTRIBUTES": "exception.message,exception.stacktrace",
    }
    # No span per AWS call or HTTP request: those carry the error text of the call as their
    # status and the address as an attribute.
    assert {"botocore", "aiobotocore", "urllib", "urllib3", "requests", "httpx"} <= set(disabled)
    assert len(disabled) == len(set(disabled)) and "" not in disabled
    # The span of the incoming request stays: it links the agent's spans to the Runtime's.
    assert "starlette" not in disabled
    assert "OTEL_RESOURCE_ATTRIBUTES" not in runtime["Properties"]["EnvironmentVariables"]


def test_runtime_role_lets_agentcore_deliver_spans_to_the_agents_log_group():
    statements = [
        s
        for r in resources(TEMPLATE, "AWS::IAM::Policy")
        for s in r["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Sid") == "SpanDelivery"
    ]
    (statement,) = statements
    assert statement["Action"] == "logs:PutResourcePolicy"
    assert ":log-group:/aws/bedrock-agentcore/runtimes/team_assistant_dev_runtime-*" in str(
        statement["Resource"]
    )


def test_runtime_image_is_built_from_the_agent_dockerfile_for_arm64():
    import json
    from pathlib import Path

    assembly = synth(env="dev")
    manifest = json.loads((Path(assembly.directory) / "TeamAssistant-dev.assets.json").read_text())
    (image,) = manifest["dockerImages"].values()
    assert image["source"]["dockerFile"] == "docker/agent.Dockerfile"
    assert image["source"]["platform"] == "linux/arm64"


def test_both_roles_are_under_the_project_path_with_the_boundary_and_a_narrow_trust():
    # One role per AgentCore resource: only that resource of this account can assume it.
    sources = []
    for role in resources(TEMPLATE, "AWS::IAM::Role"):
        properties = role["Properties"]
        assert properties["Path"] == "/team-assistant/"
        assert "TeamAssistantCdkPermissionsBoundary-dev" in str(properties["PermissionsBoundary"])
        (trust,) = properties["AssumeRolePolicyDocument"]["Statement"]
        assert trust["Action"] == "sts:AssumeRole"
        if trust["Principal"] == {"Service": "lambda.amazonaws.com"}:
            continue  # test_token_claims_function_has_a_role_without_permissions
        assert trust["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
        assert trust["Condition"]["StringEquals"] == {"aws:SourceAccount": CONTEXT["account"]}
        assert set(trust["Condition"]) == {"StringEquals", "ArnLike"}
        sources.append(str(trust["Condition"]["ArnLike"]))
    gateway, memory_gateway, runtime = sorted(sources, key=lambda source: source.split(":")[-1])
    assert ":runtime/team_assistant_dev_runtime-*" in runtime
    assert ":gateway/team-assistant-dev-gateway-*" in gateway
    assert ":gateway/team-assistant-dev-memory-*" in memory_gateway
    assert len(sources) == 3


def test_gateway_accepts_only_tokens_of_this_pools_client_bound_to_this_agent():
    gateway = _gateway("team-assistant-dev-gateway")
    properties = gateway["Properties"]
    assert properties["AuthorizerType"] == "CUSTOM_JWT"
    (pool_id,) = _ids("AWS::Cognito::UserPool")
    client_id = _client_id("cli")
    (authorizer,) = properties["AuthorizerConfiguration"].values()
    assert set(properties["AuthorizerConfiguration"]) == {"CustomJWTAuthorizer"}
    assert authorizer["AllowedClients"] == [{"Ref": client_id}]
    assert authorizer["AllowedAudience"] == ["https://team-assistant-dev.agent"]
    assert {"Ref": pool_id} in authorizer["DiscoveryUrl"]["Fn::Join"][1]
    assert set(authorizer) == {"AllowedClients", "AllowedAudience", "DiscoveryUrl"}
    # No protocol type: the only value is MCP, and the gateway relays plain HTTP to the Runtime.
    assert "ProtocolType" not in properties and "ProtocolConfiguration" not in properties
    (role_id,) = [k for k in _ids("AWS::IAM::Role") if k.startswith("GatewayRole")]
    assert properties["RoleArn"] == {"Fn::GetAtt": [role_id, "Arn"]}
    # Only the Memory gateway has access rules: they refuse whatever no rule permits.
    assert "PolicyEngineConfiguration" not in properties
    assert properties["Tags"] == {"project": "team-assistant", "env": "dev"}
    assert gateway["DeletionPolicy"] == "Delete"
    url = TEMPLATE["Outputs"]["GatewayUrl"]["Value"]
    assert url["Fn::GetAtt"][1] == "GatewayUrl"


def test_the_role_names_only_the_configured_model_and_this_memory():
    statements = {
        s.get("Sid"): s
        for policy in resources(TEMPLATE, "AWS::IAM::Policy")
        for s in policy["Properties"]["PolicyDocument"]["Statement"]
    }
    model = statements["InvokeConfiguredModel"]
    arns = [str(r) for r in model["Resource"]]
    assert len(arns) == 3
    assert all("claude-haiku-4-5-20251001-v1:0" in arn for arn in arns)
    assert any("inference-profile/global.anthropic" in arn for arn in arns)
    assert not any(arn.endswith("/*']]}") for arn in arns)


def test_runtime_role_has_no_memory_action_and_the_agent_knows_the_memory_gateway():
    (role_id,) = [k for k in _ids("AWS::IAM::Role") if k.startswith("RuntimeExecutionRole")]
    actions = {
        action
        for policy in resources(TEMPLATE, "AWS::IAM::Policy")
        if {"Ref": role_id} in policy["Properties"]["Roles"]
        for s in policy["Properties"]["PolicyDocument"]["Statement"]
        if s["Effect"] == "Allow"
        for action in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    }
    memory = {"CreateEvent", "GetEvent", "ListEvents", "DeleteEvent", "ListSessions"}
    memory |= {"ListMemoryRecords", "RetrieveMemoryRecords", "GetMemoryRecord"}
    assert not actions & {f"bedrock-agentcore:{name}" for name in memory}
    (runtime,) = resources(TEMPLATE, "AWS::BedrockAgentCore::Runtime")
    variables = runtime["Properties"]["EnvironmentVariables"]
    gateway_id = _gateway_id("team-assistant-dev-memory")
    assert variables["MEMORY_GATEWAY_URL"] == {"Fn::GetAtt": [gateway_id, "GatewayUrl"]}


def test_the_guardrail_checks_inputs_for_prompt_attacks_only():
    (guardrail,) = resources(TEMPLATE, "AWS::Bedrock::Guardrail")
    properties = guardrail["Properties"]
    assert properties["Name"] == "team-assistant-dev-guardrail"
    assert properties["ContentPolicyConfig"] == {
        "FiltersConfig": [
            {"Type": "PROMPT_ATTACK", "InputStrength": "LOW", "OutputStrength": "NONE"}
        ]
    }
    (runtime,) = resources(TEMPLATE, "AWS::BedrockAgentCore::Runtime")
    variables = runtime["Properties"]["EnvironmentVariables"]
    assert variables["GUARDRAIL_ID"] == {"Fn::GetAtt": ["Guardrail", "GuardrailId"]}


def test_runtime_role_can_apply_this_guardrail_only():
    statements = {
        s.get("Sid"): s
        for policy in resources(TEMPLATE, "AWS::IAM::Policy")
        for s in policy["Properties"]["PolicyDocument"]["Statement"]
    }
    guard = statements["GuardToolResults"]
    assert guard["Action"] == "bedrock:ApplyGuardrail"
    assert guard["Resource"] == {"Fn::GetAtt": ["Guardrail", "GuardrailArn"]}
    assert guard["Effect"] == "Allow"


def test_runtime_role_can_get_and_complete_github_tokens_of_this_provider_only():
    statements = {
        s.get("Sid"): s
        for policy in resources(TEMPLATE, "AWS::IAM::Policy")
        for s in policy["Properties"]["PolicyDocument"]["Statement"]
    }
    tokens = statements["GithubTokens"]
    assert sorted(tokens["Action"]) == [
        "bedrock-agentcore:CompleteResourceTokenAuth",
        "bedrock-agentcore:GetResourceOauth2Token",
    ]
    text = str(tokens["Resource"])
    assert "token-vault/default" in text
    assert "GithubProvider" in text and "CredentialProviderArn" in text
    assert "ExchangeProvider" not in text
    assert "workload-identity/team_assistant_dev_runtime-*" in text
    assert "*" not in [r for r in tokens["Resource"] if isinstance(r, str)]
    secret = statements["GithubClientSecret"]
    assert secret["Action"] == "secretsmanager:GetSecretValue"
    assert secret["Resource"] == {"Ref": _secret_id("github")}


def _denies(role_prefix: str) -> dict[str, dict]:
    (role_id,) = [k for k in _ids("AWS::IAM::Role") if k.startswith(role_prefix)]
    return {
        s["Sid"]: s
        for r in resources(TEMPLATE, "AWS::IAM::Policy")
        if r["Properties"]["Roles"] == [{"Ref": role_id}]
        for s in r["Properties"]["PolicyDocument"]["Statement"]
        if s["Effect"] == "Deny"
    }


@pytest.mark.parametrize("role_prefix", ["RuntimeExecutionRole", "GatewayRole"])
def test_both_roles_are_denied_the_actions_that_name_a_user_by_a_plain_id(role_prefix):
    deny = _denies(role_prefix)["DenyUserIdAccess"]
    assert sorted(deny["Action"]) == [
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:InvokeAgentRuntimeForUser",
    ]
    assert deny["Resource"] == "*" and "Condition" not in deny


def test_runtime_role_gets_workload_tokens_for_its_own_identity_only():
    # The construct's Allow names every workload identity of the account.
    denies = _denies("RuntimeExecutionRole")
    assert set(denies) == {"DenyUserIdAccess", "DenyOtherWorkloadIdentities"}
    deny = denies["DenyOtherWorkloadIdentities"]
    assert sorted(deny["Action"]) == [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
    ]
    assert "Resource" not in deny and "Condition" not in deny
    excepted = sorted(str(r).split("bedrock-agentcore:")[1] for r in deny["NotResource"])
    assert [e.split(":", 2)[2].rstrip("'\"]}") for e in excepted] == [
        "workload-identity-directory/default",
        "workload-identity-directory/default/workload-identity/team_assistant_dev_runtime-*",
    ]
    assert set(_denies("GatewayRole")) == {"DenyUserIdAccess"}


def test_memory_strategies_and_namespaces():
    (memory,) = resources(TEMPLATE, "AWS::BedrockAgentCore::Memory")
    properties = memory["Properties"]
    assert properties["Name"] == "team_assistant_dev_memory"
    assert properties["EventExpiryDuration"] == 30
    # Each strategy is a one-key object: the key is the strategy type.
    found = [(kind, body) for s in properties["MemoryStrategies"] for kind, body in s.items()]
    assert [(kind, body["Name"], body["NamespaceTemplates"]) for kind, body in found] == [
        ("UserPreferenceMemoryStrategy", "preferences", ["/preferences/{actorId}/"]),
        ("SemanticMemoryStrategy", "facts", ["/facts/{actorId}/"]),
        ("SummaryMemoryStrategy", "summaries", ["/summaries/{actorId}/{sessionId}/"]),
    ]
    # namespaceTemplates, not the deprecated namespaces.
    assert not [body for _, body in found if "Namespaces" in body]
    # Service-owned encryption key and no role: nothing with a fee, no IAM change.
    assert "EncryptionKeyArn" not in properties
    assert "MemoryExecutionRoleArn" not in properties
    assert properties["Tags"] == {"project": "team-assistant", "env": "dev"}
    assert memory["DeletionPolicy"] == "Delete"


def test_memory_expiry_comes_from_the_agent_config():
    from team_assistant_infra.naming import names
    from team_assistant_infra.stack import TeamAssistantStack

    from team_assistant.config import AgentConfig

    app = cdk.App()
    n = names("dev", CONTEXT["account"], CONTEXT["region"], CONTEXT["login_suffix"])
    TeamAssistantStack(app, n.stack, names=n, config=AgentConfig("m", 100, 45))
    template = app.synth().get_stack_by_name(n.stack).template
    (memory,) = resources(template, "AWS::BedrockAgentCore::Memory")
    assert memory["Properties"]["EventExpiryDuration"] == 45


def test_user_pool_is_essentials_and_closed_to_sign_up():
    (pool,) = resources(TEMPLATE, "AWS::Cognito::UserPool")
    properties = pool["Properties"]
    assert properties["UserPoolName"] == "team-assistant-dev-users"
    # Essentials: the lowest plan that binds a token to a resource (the aud claim).
    assert properties["UserPoolTier"] == "ESSENTIALS"
    assert properties["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    assert pool["DeletionPolicy"] == "Delete"
    # Sign-in by username only; no e-mail or SMS configuration.
    assert "AliasAttributes" not in properties and "UsernameAttributes" not in properties
    assert "EmailConfiguration" not in properties and "SmsConfiguration" not in properties
    assert properties.get("AutoVerifiedAttributes", []) == []


def test_the_pool_calls_the_token_claims_function_for_access_tokens():
    (pool,) = resources(TEMPLATE, "AWS::Cognito::UserPool")
    (function_id,) = [
        key for key, r in TEMPLATE["Resources"].items() if r["Type"] == "AWS::Lambda::Function"
    ]
    trigger = pool["Properties"]["LambdaConfig"]["PreTokenGenerationConfig"]
    # V2_0: the version that changes access tokens.
    assert trigger == {"LambdaArn": {"Fn::GetAtt": [function_id, "Arn"]}, "LambdaVersion": "V2_0"}
    (permission,) = resources(TEMPLATE, "AWS::Lambda::Permission")
    assert permission["Properties"]["Principal"] == "cognito-idp.amazonaws.com"
    assert permission["Properties"]["Action"] == "lambda:InvokeFunction"
    assert "Users" in str(permission["Properties"]["SourceArn"])


def _token_claims_function() -> dict:
    (function,) = resources(TEMPLATE, "AWS::Lambda::Function")
    return function["Properties"]


def test_token_claims_function_adds_the_users_namespaces_to_the_access_token():
    from team_assistant.config import claim_namespaces

    properties = _token_claims_function()
    assert properties["FunctionName"] == "team-assistant-dev-token-claims"
    assert properties["Runtime"] == "python3.13"
    scope: dict = {}
    exec(properties["Code"]["ZipFile"], scope)  # noqa: S102 - the stack's own inline code
    event = {
        "triggerSource": "TokenGeneration_TokenExchange",
        "request": {"userAttributes": {"sub": "u-1"}, "scopes": ["openid"]},
        "response": {"claimsAndScopeOverrideDetails": None},
    }
    result = scope[properties["Handler"].split(".")[1]](event, None)
    assert result["request"] == {"userAttributes": {"sub": "u-1"}, "scopes": ["openid"]}
    assert result["response"] == {
        "claimsAndScopeOverrideDetails": {
            "accessTokenGeneration": {"claimsToAddOrOverride": claim_namespaces("u-1")}
        }
    }


def test_token_claims_function_leaves_an_event_without_a_user_id_as_it_is():
    properties = _token_claims_function()
    scope: dict = {}
    exec(properties["Code"]["ZipFile"], scope)  # noqa: S102 - the stack's own inline code
    for request in ({}, {"userAttributes": {}}, {"userAttributes": None}):
        event = {"request": request, "response": {"claimsAndScopeOverrideDetails": None}}
        assert scope["handler"](dict(event), None) == event


def test_token_claims_function_has_a_role_without_permissions():
    role_id = _token_claims_function()["Role"]["Fn::GetAtt"][0]
    role = TEMPLATE["Resources"][role_id]["Properties"]
    assert "ManagedPolicyArns" not in role and "Policies" not in role
    attached = [
        policy
        for policy in resources(TEMPLATE, "AWS::IAM::Policy")
        if {"Ref": role_id} in policy["Properties"]["Roles"]
    ]
    assert attached == []


def test_app_client_is_public_and_uses_the_authorization_code_flow_only():
    properties = TEMPLATE["Resources"][_client_id("cli")]["Properties"]
    assert properties.get("GenerateSecret", False) is False
    assert properties["AllowedOAuthFlows"] == ["code"]
    assert properties["AllowedOAuthFlowsUserPoolClient"] is True
    assert properties["AllowedOAuthScopes"] == ["openid"]
    callbacks = properties["CallbackURLs"]
    assert callbacks[:2] == ["http://localhost:8976/callback", "http://localhost:5173/"]
    # The hosted page: https://<distribution domain>/
    assert "PageDistribution" in str(callbacks[2]) and str(callbacks[2]).startswith("{'Fn::Join'")
    assert properties["LogoutURLs"][0] == "http://localhost:5173/"
    assert "PageDistribution" in str(properties["LogoutURLs"][1])
    assert properties["SupportedIdentityProviders"] == ["COGNITO"]
    assert "client_credentials" not in str(properties)
    # Direct password flows are off: only managed login can log a user in.
    # The token exchange flow: the gateway exchanges a user's token as this client, because
    # Cognito exchanges a token only for the client that issued it.
    assert properties["ExplicitAuthFlows"] == [
        "ALLOW_REFRESH_TOKEN_AUTH",
        "ALLOW_TOKEN_EXCHANGE_IMPERSONATION",
    ]
    assert (properties["AccessTokenValidity"], properties["TokenValidityUnits"]["AccessToken"]) == (
        60,
        "minutes",
    )
    assert (
        properties["RefreshTokenValidity"],
        properties["TokenValidityUnits"]["RefreshToken"],
    ) == (
        1440,
        "minutes",
    )


def test_domain_is_the_prefix_domain_with_managed_login():
    (domain,) = resources(TEMPLATE, "AWS::Cognito::UserPoolDomain")
    assert domain["Properties"]["Domain"] == "ta-dev-0a1b2c3d"
    assert domain["Properties"]["ManagedLoginVersion"] == 2
    assert "CustomDomainConfig" not in domain["Properties"]


def _ids(kind: str) -> list[str]:
    return [k for k, r in TEMPLATE["Resources"].items() if r["Type"] == kind]


def _client_id(suffix: str) -> str:
    """The logical id of the app client whose name ends in ``-<suffix>``."""
    (found,) = [
        k
        for k in _ids("AWS::Cognito::UserPoolClient")
        if TEMPLATE["Resources"][k]["Properties"]["ClientName"].endswith(f"-{suffix}")
    ]
    return found


def _secret_id(suffix: str) -> str:
    """The logical id of the secret whose name ends in ``/<suffix>``."""
    (found,) = [
        k
        for k in _ids("AWS::SecretsManager::Secret")
        if TEMPLATE["Resources"][k]["Properties"]["Name"].endswith(f"/{suffix}")
    ]
    return found


def _provider(suffix: str) -> dict:
    """The credential provider whose name ends in ``-<suffix>``."""
    (found,) = [
        r
        for r in resources(TEMPLATE, "AWS::BedrockAgentCore::OAuth2CredentialProvider")
        if r["Properties"]["Name"].endswith(f"-{suffix}")
    ]
    return found


def test_exchange_provider_lets_the_gateway_exchange_a_users_token_at_this_pool():
    # Cognito exchanges a token only for the app client that issued it, so the
    # provider acts as the login client, which has no secret.
    provider = _provider("exchange")
    properties = provider["Properties"]
    assert properties["Name"] == "team-assistant-dev-exchange"
    assert properties["CredentialProviderVendor"] == "CustomOauth2"
    (pool_id,) = _ids("AWS::Cognito::UserPool")
    custom = properties["Oauth2ProviderConfigInput"]["CustomOauth2ProviderConfig"]
    assert {"Ref": pool_id} in custom["OauthDiscovery"]["DiscoveryUrl"]["Fn::Join"][1]
    assert custom["ClientId"] == {"Ref": _client_id("cli")}
    # AgentCore wants a client secret for this method; the login client has none, and Cognito
    # ignores one sent in the request body for such a client. So
    # the secret is a generated value that no one ever has to know, never in the template.
    assert custom["ClientAuthenticationMethod"] == "CLIENT_SECRET_POST"
    assert "ClientSecret" not in custom
    assert custom["ClientSecretSource"] == "EXTERNAL"
    assert custom["ClientSecretConfig"] == {
        "SecretId": {"Ref": _secret_id("exchange")},
        "JsonKey": "client_secret",
    }
    placeholder = TEMPLATE["Resources"][_secret_id("exchange")]
    assert placeholder["Properties"]["Name"] == "team-assistant/dev/exchange"
    assert "SecretString" not in placeholder["Properties"]
    assert placeholder["Properties"]["GenerateSecretString"]["GenerateStringKey"] == "client_secret"
    assert placeholder["DeletionPolicy"] == "Delete"
    assert custom["OnBehalfOfTokenExchangeConfig"] == {
        "GrantType": "TOKEN_EXCHANGE",
        "TokenExchangeGrantTypeConfig": {"ActorTokenContent": "NONE"},
    }
    assert provider["DeletionPolicy"] == "Delete"
    assert len(_ids("AWS::Cognito::UserPoolClient")) == 1


def test_app_client_has_the_default_managed_login_style():
    # Without a branding resource the login pages of a client created by code answer 403.
    (branding,) = resources(TEMPLATE, "AWS::Cognito::ManagedLoginBranding")
    (pool_id,) = _ids("AWS::Cognito::UserPool")
    client_id = _client_id("cli")
    assert branding["Properties"] == {
        "UserPoolId": {"Ref": pool_id},
        "ClientId": {"Ref": client_id},
        "UseCognitoProvidedValues": True,
    }


def test_audience_is_a_resource_server_without_scopes():
    (server,) = resources(TEMPLATE, "AWS::Cognito::UserPoolResourceServer")
    (pool_id,) = _ids("AWS::Cognito::UserPool")
    properties = server["Properties"]
    assert properties["Identifier"] == "https://team-assistant-dev.agent"
    # The name has its own rule (cognito-idp ResourceServerNameType): no ":" or "/", so it
    # cannot default to the identifier, which is a URL.
    assert properties["Name"] == "team-assistant-dev-users-audience"
    assert re.fullmatch(r"[\w\s+=,.@-]+", properties["Name"])
    assert properties["UserPoolId"] == {"Ref": pool_id}
    assert "Scopes" not in properties
    assert TEMPLATE["Outputs"]["Audience"]["Value"] == "https://team-assistant-dev.agent"


def test_gateway_target_is_the_runtime_and_exchanges_the_users_token():
    target = _target("agent")
    gateway_id = _gateway_id("team-assistant-dev-gateway")
    (runtime_id,) = _ids("AWS::BedrockAgentCore::Runtime")
    properties = target["Properties"]
    assert properties["Name"] == "agent"
    assert properties["GatewayIdentifier"] == {"Fn::GetAtt": [gateway_id, "GatewayIdentifier"]}
    runtime_arn = {"Fn::GetAtt": [runtime_id, "AgentRuntimeArn"]}
    assert properties["TargetConfiguration"] == {"Http": {"AgentcoreRuntime": {"Arn": runtime_arn}}}
    # The gateway exchanges the caller's token at the pool for one of the same user, bound to
    # the agent; only a request made this way carries the gateway in its identity chain.
    (configuration,) = properties["CredentialProviderConfigurations"]
    assert configuration["CredentialProviderType"] == "OAUTH"
    oauth = configuration["CredentialProvider"]["OauthCredentialProvider"]
    exchange_id = next(
        k
        for k, r in TEMPLATE["Resources"].items()
        if r["Type"].endswith("OAuth2CredentialProvider")
        and r["Properties"]["Name"].endswith("-exchange")
    )
    assert oauth == {
        "ProviderArn": {"Fn::GetAtt": [exchange_id, "CredentialProviderArn"]},
        "GrantType": "TOKEN_EXCHANGE",
        "Scopes": ["openid"],
        "CustomParameters": {
            # Cognito refuses the type AgentCore sends by default ("...:jwt").
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": "https://team-assistant-dev.agent",
        },
    }
    assert target["DeletionPolicy"] == "Delete"


def test_gateway_role_can_invoke_this_runtime_and_exchange_tokens_and_nothing_else():
    (role_id,) = [k for k in _ids("AWS::IAM::Role") if k.startswith("GatewayRole")]
    (runtime_id,) = _ids("AWS::BedrockAgentCore::Runtime")
    (policy_id,) = [
        k
        for k, r in TEMPLATE["Resources"].items()
        if r["Type"] == "AWS::IAM::Policy" and r["Properties"]["Roles"] == [{"Ref": role_id}]
    ]
    statements = {
        s["Sid"]: s
        for s in TEMPLATE["Resources"][policy_id]["Properties"]["PolicyDocument"]["Statement"]
    }
    assert set(statements) == {
        "InvokeThisRuntime",
        "ExchangeUserTokens",
        "ExchangeSecret",
        "DenyUserIdAccess",
    }
    assert statements["ExchangeSecret"]["Action"] == "secretsmanager:GetSecretValue"
    assert statements["ExchangeSecret"]["Resource"] == {"Ref": _secret_id("exchange")}
    invoke = statements["InvokeThisRuntime"]
    assert invoke["Action"] == "bedrock-agentcore:InvokeAgentRuntime"
    runtime_arn = {"Fn::GetAtt": [runtime_id, "AgentRuntimeArn"]}
    assert invoke["Resource"] == [
        runtime_arn,
        {"Fn::Join": ["", [runtime_arn, "/runtime-endpoint/*"]]},
    ]
    exchange = statements["ExchangeUserTokens"]
    assert sorted(exchange["Action"]) == [
        "bedrock-agentcore:GetResourceOauth2Token",
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
    ]
    text = str(exchange["Resource"])
    assert "token-vault/default" in text and "workload-identity-directory/default" in text
    assert "CredentialProviderArn" in text and "GithubProvider" not in text
    assert "workload-identity/team-assistant-dev-gateway-*" in text
    assert "*" not in [r for r in exchange["Resource"] if isinstance(r, str)]
    # The role can act before the target is created.
    assert policy_id in _target("agent")["DependsOn"]


def _gateway_id(name: str) -> str:
    (found,) = [
        k
        for k in _ids("AWS::BedrockAgentCore::Gateway")
        if TEMPLATE["Resources"][k]["Properties"]["Name"] == name
    ]
    return found


def _gateway(name: str) -> dict:
    return TEMPLATE["Resources"][_gateway_id(name)]


def _target(name: str) -> dict:
    (target,) = [
        t
        for t in resources(TEMPLATE, "AWS::BedrockAgentCore::GatewayTarget")
        if t["Properties"]["Name"] == name
    ]
    return target


def _flat(value) -> str:
    """A template value as text, a reference written as <logical id.attribute>."""
    if isinstance(value, str):
        return value
    if "Fn::Join" in value:
        separator, parts = value["Fn::Join"]
        return separator.join(_flat(part) for part in parts)
    if "Fn::GetAtt" in value:
        return "<{}.{}>".format(*value["Fn::GetAtt"])
    return "<{}>".format(value["Ref"])


def test_memory_gateway_takes_the_same_tokens_and_enforces_the_access_rules():
    properties = _gateway("team-assistant-dev-memory")["Properties"]
    agent = _gateway("team-assistant-dev-gateway")["Properties"]
    assert properties["AuthorizerType"] == "CUSTOM_JWT"
    assert properties["AuthorizerConfiguration"] == agent["AuthorizerConfiguration"]
    # No protocol type: a connector target is plain HTTP.
    assert "ProtocolType" not in properties
    (engine_id,) = _ids("AWS::BedrockAgentCore::PolicyEngine")
    assert properties["PolicyEngineConfiguration"] == {
        "Arn": {"Fn::GetAtt": [engine_id, "PolicyEngineArn"]},
        "Mode": "ENFORCE",
    }
    engine = TEMPLATE["Resources"][engine_id]
    assert engine["Properties"]["Name"] == "team_assistant_dev_memory"
    assert engine["DeletionPolicy"] == "Delete"
    (role_id,) = [k for k in _ids("AWS::IAM::Role") if k.startswith("MemoryGatewayRole")]
    assert properties["RoleArn"] == {"Fn::GetAtt": [role_id, "Arn"]}
    assert _gateway("team-assistant-dev-memory")["DeletionPolicy"] == "Delete"
    url = TEMPLATE["Outputs"]["MemoryGatewayUrl"]["Value"]
    gateway_id = _gateway_id("team-assistant-dev-memory")
    assert url == {"Fn::GetAtt": [gateway_id, "GatewayUrl"]}


def test_memory_gateway_target_is_this_memory_called_with_the_gateways_role():
    target = _target("memory")
    properties = target["Properties"]
    (memory_id,) = _ids("AWS::BedrockAgentCore::Memory")
    assert properties["TargetConfiguration"] == {
        "Http": {
            "Connector": {
                "Source": {"ConnectorId": "agentcore-memory"},
                "Parameters": {"memoryId": {"Fn::GetAtt": [memory_id, "MemoryId"]}},
            }
        }
    }
    assert properties["CredentialProviderConfigurations"] == [
        {"CredentialProviderType": "GATEWAY_IAM_ROLE"}
    ]
    assert target["DeletionPolicy"] == "Delete"


EVENT_ACTIONS = [
    "memory___POST:/memories/{memoryId}/actor/{actorId}/sessions/{sessionId}",
    "memory___POST:/memories/{memoryId}/actor/{actorId}/sessions",
    "memory___POST:/memories/{memoryId}/events",
]


def test_the_access_rules_let_a_user_reach_only_their_own_memory():
    gateway_id = _gateway_id("team-assistant-dev-memory")
    (engine_id,) = _ids("AWS::BedrockAgentCore::PolicyEngine")
    (target_id,) = [
        k
        for k in _ids("AWS::BedrockAgentCore::GatewayTarget")
        if TEMPLATE["Resources"][k]["Properties"]["Name"] == "memory"
    ]
    policies = {
        p["Properties"]["Name"]: p for p in resources(TEMPLATE, "AWS::BedrockAgentCore::Policy")
    }
    # Named with the environment.
    assert all(name.endswith("_dev") for name in policies)
    policies = {name.removesuffix("_dev"): policy for name, policy in policies.items()}
    assert set(policies) == {"own_events", "earlier_layout", "own_preferences", "own_records"}
    head = 'permit(principal is AgentCore::OAuthUser, action {}, resource == AgentCore::Gateway::"<'
    head += gateway_id + '.GatewayArn>") when {{ '
    guard = "context has input && context.input has "
    expected = {
        "own_events": head.format(
            "in [" + ", ".join(f'AgentCore::Action::"{a}"' for a in EVENT_ACTIONS) + "]"
        )
        + 'principal.hasTag("sub") && '
        + guard
        + 'actorId && context.input.actorId == principal.getTag("sub") };',
        # A read only, of the actor the SDK's session manager also looks under.
        "earlier_layout": head.format(f'== AgentCore::Action::"{EVENT_ACTIONS[0]}"')
        + guard
        + 'actorId && context.input.actorId like "session_*" };',
        "own_preferences": head.format(
            '== AgentCore::Action::"memory___POST:/memories/{memoryId}/memoryRecords"'
        )
        + 'principal.hasTag("memory_preferences") && '
        + guard
        + "namespace && !(context.input has namespacePath) && "
        + 'context.input.namespace == principal.getTag("memory_preferences") };',
        "own_records": head.format(
            '== AgentCore::Action::"memory___POST:/memories/{memoryId}/retrieve"'
        )
        + 'principal.hasTag("memory_facts") && principal.hasTag("memory_summaries") && '
        + guard
        + "namespacePath && !(context.input has namespace) && "
        + '(context.input.namespacePath == principal.getTag("memory_facts") || '
        + 'context.input.namespacePath == principal.getTag("memory_summaries")) };',
    }
    for name, policy in policies.items():
        properties = policy["Properties"]
        assert _flat(properties["Definition"]["Cedar"]["Statement"]) == expected[name], name
        assert properties["PolicyEngineId"] == {"Fn::GetAtt": [engine_id, "PolicyEngineId"]}
        # A rule is checked against the target's operations, so the target comes first.
        assert target_id in policy["DependsOn"]
        assert policy["DeletionPolicy"] == "Delete"


def test_memory_gateway_role_can_call_this_memory_and_ask_the_rules_and_nothing_else():
    (role_id,) = [k for k in _ids("AWS::IAM::Role") if k.startswith("MemoryGatewayRole")]
    (memory_id,) = _ids("AWS::BedrockAgentCore::Memory")
    (engine_id,) = _ids("AWS::BedrockAgentCore::PolicyEngine")
    (policy_id,) = [
        k
        for k, r in TEMPLATE["Resources"].items()
        if r["Type"] == "AWS::IAM::Policy" and r["Properties"]["Roles"] == [{"Ref": role_id}]
    ]
    statements = {
        s["Sid"]: s
        for s in TEMPLATE["Resources"][policy_id]["Properties"]["PolicyDocument"]["Statement"]
    }
    assert set(statements) == {"MemoryOfTheTokensUser", "ReadAccessRules", "AskAccessRules"}
    memory = statements["MemoryOfTheTokensUser"]
    assert sorted(memory["Action"]) == [
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:ListMemoryRecords",
        "bedrock-agentcore:ListSessions",
        "bedrock-agentcore:RetrieveMemoryRecords",
    ]
    assert memory["Resource"] == {"Fn::GetAtt": [memory_id, "MemoryArn"]}
    engine_arn = {"Fn::GetAtt": [engine_id, "PolicyEngineArn"]}
    assert statements["ReadAccessRules"]["Action"] == "bedrock-agentcore:GetPolicyEngine"
    assert statements["ReadAccessRules"]["Resource"] == engine_arn
    ask = statements["AskAccessRules"]
    assert sorted(ask["Action"]) == [
        "bedrock-agentcore:AuthorizeAction",
        "bedrock-agentcore:PartiallyAuthorizeActions",
    ]
    assert ask["Resource"][0] == engine_arn
    assert _flat(ask["Resource"][1]).endswith(":gateway/team-assistant-dev-memory-*")
    assert len(ask["Resource"]) == 2
    # The role can act before the gateway and its target are created.
    assert policy_id in _target("memory")["DependsOn"]
    assert policy_id in _gateway("team-assistant-dev-memory")["DependsOn"]


def test_outputs():
    outputs = TEMPLATE["Outputs"]
    assert set(outputs) == {
        "UserPoolId",
        "UserPoolClientId",
        "DiscoveryUrl",
        "LoginDomain",
        "Audience",
        "MemoryId",
        "GuardrailId",
        "RuntimeArn",
        "GatewayUrl",
        "MemoryGatewayUrl",
        "MemoryGatewayArn",
        "MemoryRulesId",
        "GithubCallbackUrl",
        "WebUrl",
        "WebBucket",
        "WebDistributionId",
    }
    assert "/.well-known/openid-configuration" in str(outputs["DiscoveryUrl"]["Value"])
    assert ".auth.eu-west-1.amazoncognito.com" in str(outputs["LoginDomain"]["Value"])


def test_stack_uses_this_projects_bootstrap():
    artifact = synth(env="dev").get_stack_by_name("TeamAssistant-dev")
    version = artifact.template["Parameters"]["BootstrapVersion"]["Default"]
    assert version == "/cdk-bootstrap/teamasst/version"
    assert "cdk-teamasst-deploy-role" in artifact.assume_role_arn
    assert "hnb659fds" not in str(artifact.manifest)


@pytest.mark.parametrize("missing", ["account", "region"])
def test_account_and_region_are_required(missing):
    context = {k: v for k, v in CONTEXT.items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        build_app(cdk.App(context=context))


def test_the_cli_must_pass_env():
    with pytest.raises(ValueError, match="env"):
        build_app(cdk.App(context=CONTEXT), require_env=True)


def test_invalid_env_is_refused():
    with pytest.raises(ValueError, match="ENV"):
        build_app(cdk.App(context={**CONTEXT, "env": "Dev"}))


def test_the_page_bucket_is_private_encrypted_and_named_without_the_account():
    (bucket,) = resources(TEMPLATE, "AWS::S3::Bucket")
    props = bucket["Properties"]
    assert props["BucketName"] == "ta-dev-web-0a1b2c3d"
    assert props["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert "WebsiteConfiguration" not in props
    rule = props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    assert rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    assert bucket["DeletionPolicy"] == "Delete"


def test_only_this_distribution_reads_the_bucket_over_https():
    (policy,) = resources(TEMPLATE, "AWS::S3::BucketPolicy")
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    reads = [s for s in statements if s["Effect"] == "Allow"]
    assert [s["Principal"] for s in reads] == [{"Service": "cloudfront.amazonaws.com"}]
    assert "PageDistribution" in str(reads[0]["Condition"]["StringEquals"]["AWS:SourceArn"])
    assert any(s["Effect"] == "Deny" and "aws:SecureTransport" in str(s) for s in statements)


def test_the_distribution_serves_https_with_the_security_headers():
    (dist,) = resources(TEMPLATE, "AWS::CloudFront::Distribution")
    config = dist["Properties"]["DistributionConfig"]
    assert config["DefaultRootObject"] == "index.html"
    behavior = config["DefaultCacheBehavior"]
    assert behavior["ViewerProtocolPolicy"] == "redirect-to-https"
    assert "OriginAccessControlId" in config["Origins"][0]
    (headers,) = resources(TEMPLATE, "AWS::CloudFront::ResponseHeadersPolicy")
    security = headers["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]
    csp = security["ContentSecurityPolicy"]["ContentSecurityPolicy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp and "unsafe" not in csp
    assert security["ReferrerPolicy"]["ReferrerPolicy"] == "no-referrer"
    assert security["FrameOptions"]["FrameOption"] == "DENY"
    assert security["StrictTransportSecurity"]["AccessControlMaxAgeSec"] == 365 * 24 * 3600


def test_the_runtime_and_the_outputs_know_the_page():
    (runtime,) = resources(TEMPLATE, "AWS::BedrockAgentCore::Runtime")
    web_url = runtime["Properties"]["EnvironmentVariables"]["WEB_URL"]
    assert "PageDistribution" in str(web_url) and "https://" in str(web_url)
    assert {"WebUrl", "WebBucket", "WebDistributionId"} <= set(TEMPLATE["Outputs"])
