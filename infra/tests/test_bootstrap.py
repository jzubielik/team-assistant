"""The two IAM templates of the CDK bootstrap: what the execution role and app roles may do."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from team_assistant_infra import naming

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap"
ROLE_ARN = "arn:${AWS::Partition}:iam::${AWS::AccountId}:role" + naming.ROLE_PATH + "*"
BOUNDARY_ARN = (
    "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/" + naming.BOUNDARY_POLICY_PREFIX + "*"
)
# Actions that give a role permissions; each must require the boundary.
# names() and CDK context for a stack synthesized in these tests.
NAMES_ARGS = ("dev", "123456789012", "eu-west-1", "0a1b2c3d")
CONTEXT = {"account": "123456789012", "region": "eu-west-1", "login_suffix": "0a1b2c3d"}
GRANTING = {"iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy"}
# Never allowed to the execution role.
FORBIDDEN = {
    "iam:UpdateAssumeRolePolicy",
    "iam:DeleteRolePermissionsBoundary",
    "iam:PutRolePermissionsBoundary",
    "iam:CreateUser",
    "iam:CreateAccessKey",
    "iam:CreatePolicy",
    "iam:CreatePolicyVersion",
}


class _CfnLoader(yaml.SafeLoader):
    """CloudFormation short tags: ``!Sub x`` loads as ``{"Sub": x}``."""


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node)}
    return {suffix: loader.construct_mapping(node)}


_CfnLoader.add_multi_constructor("!", _tag)


def load(name: str) -> dict:
    return yaml.load((BOOTSTRAP / name).read_text(), Loader=_CfnLoader)  # noqa: S506


def as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def text(value) -> str:
    return value["Sub"] if isinstance(value, dict) else value


def policies(template: dict) -> dict[str, dict]:
    return {
        lid: r["Properties"]
        for lid, r in template["Resources"].items()
        if r["Type"] == "AWS::IAM::ManagedPolicy"
    }


def statements(properties: dict, effect: str) -> list[dict]:
    return [s for s in properties["PolicyDocument"]["Statement"] if s["Effect"] == effect]


EXECUTION = load("execution-policies.yaml")
BOUNDARY = load("boundary.yaml")
EXECUTION_POLICIES = list(policies(EXECUTION).values())
ALLOWS = [s for policy in EXECUTION_POLICIES for s in statements(policy, "Allow")]
DENIES = [s for policy in EXECUTION_POLICIES for s in statements(policy, "Deny")]


def test_templates_hold_only_managed_policies():
    for template, count in ((EXECUTION, 6), (BOUNDARY, 1)):
        assert {r["Type"] for r in template["Resources"].values()} == {"AWS::IAM::ManagedPolicy"}
        assert len(template["Resources"]) == count


def test_policy_names_match_naming():
    names = [policy["ManagedPolicyName"] for policy in EXECUTION_POLICIES]
    assert names == list(naming.EXECUTION_POLICIES)
    (boundary,) = policies(BOUNDARY).values()
    assert text(boundary["ManagedPolicyName"]) == naming.BOUNDARY_POLICY_PREFIX + "${Env}"
    assert naming.names(*NAMES_ARGS).boundary_policy == (naming.BOUNDARY_POLICY_PREFIX + "dev")


def test_no_allow_uses_a_wildcard_action():
    for statement in ALLOWS:
        for action in as_list(statement["Action"]):
            assert "*" not in action, statement["Sid"]
        assert "NotAction" not in statement and "NotResource" not in statement


def test_no_allow_pairs_an_iam_action_with_every_resource():
    for statement in ALLOWS:
        resources = [text(r) for r in as_list(statement["Resource"])]
        if statement["Sid"] == "RuntimeIdentityServiceRole":
            # AgentCore's own service-linked role: one fixed name, one service.
            assert statement["Action"] == "iam:CreateServiceLinkedRole"
            assert len(resources) == 1 and "*" not in resources[0]
        elif any(a.startswith("iam:") for a in as_list(statement["Action"])):
            assert resources == [ROLE_ARN], statement["Sid"]


# Allows on every resource, each with the reason it cannot name one (service reference of the
# service: the action supports no resource type).
EVERY_RESOURCE = {
    "UserPoolCreate": "the pool does not exist yet; the request must carry the project tag",
    "UserPoolDomainRead": "DescribeUserPoolDomain supports no resource type; read-only",
    "MemoryCreate": "the memory does not exist yet; the request must carry the project tag",
    "GatewayCreate": "CreateGateway supports no resource type; the request must carry the tag",
    "GithubSecretPlaceholder": "GetRandomPassword supports no resource type; returns random text",
    "GuardrailCreate": "CreateGuardrail has no guardrail resource; the request must carry the tag",
    "PolicyEngineCreate": "CreatePolicyEngine supports no resource type; needs the tag",
    "PolicyAdmin": "ManageAdminPolicy supports no resource type",
}
# Creates authorized on <type>/* because the id does not exist yet; each requires the tag.
TAGGED_CREATES = {"RuntimeCreate", "RuntimeTagOnCreate"}
PROJECT_TAG = {"StringEquals": {"aws:ResourceTag/project": "team-assistant"}}


def test_only_listed_statements_apply_to_every_resource():
    wide = {s["Sid"] for s in ALLOWS if "*" in [text(r) for r in as_list(s["Resource"])]}
    assert wide == set(EVERY_RESOURCE)


def test_user_pools_are_limited_to_this_projects_tag():
    by_sid = {s["Sid"]: s for s in ALLOWS}
    create = by_sid["UserPoolCreate"]
    assert create["Action"] == "cognito-idp:CreateUserPool"
    assert create["Condition"] == {"StringEquals": {"aws:RequestTag/project": "team-assistant"}}
    pool = by_sid["UserPool"]
    assert text(pool["Resource"]).endswith(":userpool/*")
    assert pool["Condition"] == PROJECT_TAG
    # The login style and the resource server (the token audience) are sub-resources of the
    # pool; the actions are the handler permissions in their CloudFormation resource schemas.
    assert {
        "cognito-idp:CreateManagedLoginBranding",
        "cognito-idp:DescribeManagedLoginBrandingByClient",
        "cognito-idp:DescribeManagedLoginBranding",
        "cognito-idp:UpdateManagedLoginBranding",
        "cognito-idp:DeleteManagedLoginBranding",
        "cognito-idp:CreateResourceServer",
        "cognito-idp:DescribeResourceServer",
        "cognito-idp:UpdateResourceServer",
        "cognito-idp:DeleteResourceServer",
    } <= set(pool["Action"])
    # Users are created by the operator (make user), never through CloudFormation.
    assert not [
        a for s in ALLOWS for a in as_list(s["Action"]) if a.startswith("cognito-idp:Admin")
    ]


def test_memories_are_limited_to_this_projects_tag_and_their_content_is_out_of_reach():
    by_sid = {s["Sid"]: s for s in ALLOWS}
    create = by_sid["MemoryCreate"]
    assert create["Action"] == "bedrock-agentcore:CreateMemory"
    assert create["Condition"] == {"StringEquals": {"aws:RequestTag/project": "team-assistant"}}
    # By name, without a tag condition: tags stop authorizing while a memory is being deleted.
    assert text(by_sid["Memory"]["Resource"]).endswith(":memory/team_assistant_*")
    assert "Condition" not in by_sid["Memory"]
    assert naming.names(*NAMES_ARGS).memory.startswith("team_assistant_")
    actions = {a for s in ALLOWS for a in as_list(s["Action"])}
    content = {
        "CreateEvent",
        "GetEvent",
        "ListEvents",
        "DeleteEvent",
        "ListSessions",
        "RetrieveMemoryRecords",
    }
    assert not {f"bedrock-agentcore:{a}" for a in content} & actions


def test_forbidden_actions_are_never_allowed():
    allowed = {a for s in ALLOWS for a in as_list(s["Action"])}
    assert allowed & FORBIDDEN == set()


def test_every_action_that_grants_permissions_requires_the_boundary():
    for statement in ALLOWS:
        if GRANTING & set(as_list(statement["Action"])):
            condition = statement["Condition"]["StringLike"]["iam:PermissionsBoundary"]
            assert text(condition) == BOUNDARY_ARN, statement["Sid"]


def test_roles_without_the_boundary_are_denied_on_every_resource():
    (deny,) = [s for s in DENIES if s["Sid"] == "DenyRolesWithoutBoundary"]
    assert GRANTING <= set(deny["Action"])
    assert deny["Resource"] == "*"
    assert text(deny["Condition"]["StringNotLike"]["iam:PermissionsBoundary"]) == BOUNDARY_ARN


def test_roles_can_be_passed_only_to_agentcore_and_lambda():
    (pass_role,) = [s for s in ALLOWS if "iam:PassRole" in as_list(s["Action"])]
    assert text(pass_role["Resource"]) == ROLE_ARN
    assert pass_role["Condition"] == {
        "StringEquals": {
            "iam:PassedToService": ["bedrock-agentcore.amazonaws.com", "lambda.amazonaws.com"]
        }
    }


def test_access_rules_and_the_token_claims_function_are_limited_to_this_projects_names():
    by_sid = {s["Sid"]: s for s in ALLOWS}
    create = by_sid["PolicyEngineCreate"]
    assert create["Condition"] == {"StringEquals": {"aws:RequestTag/project": "team-assistant"}}
    assert create["Action"] == "bedrock-agentcore:CreatePolicyEngine"
    # Tags only on policy engines, not on every resource the create statement names.
    tag = by_sid["PolicyEngineTagOnCreate"]
    assert tag["Action"] == "bedrock-agentcore:TagResource"
    assert text(tag["Resource"]).endswith(":policy-engine/*")
    assert tag["Condition"] == create["Condition"]
    engine = [text(r) for r in as_list(by_sid["PolicyEngine"]["Resource"])]
    assert [r.split(":")[-1] for r in engine] == [
        "policy-engine/team_assistant_*",
        "policy-engine/team_assistant_*/policy/*",
    ]
    scoped = by_sid["PolicyForGateway"]
    assert text(scoped["Resource"]).endswith(":gateway/team-assistant-*-memory-*")
    assert {"bedrock-agentcore:InvokeGateway", "bedrock-agentcore:ListGatewayTargets"} < set(
        scoped["Action"]
    )
    function = by_sid["TokenClaimsFunction"]
    assert text(function["Resource"]).endswith(":function:team-assistant-*-token-claims")
    assert all(a.startswith("lambda:") for a in function["Action"])
    n = naming.names(*NAMES_ARGS)
    assert n.policy_engine.startswith("team_assistant_")
    assert n.token_claims.startswith("team-assistant-") and n.token_claims.endswith("-token-claims")


def test_boundary_lets_the_memory_gateway_ask_this_environments_access_rules():
    by_sid = {s["Sid"]: s for s in BOUNDARY_ALLOWS}
    rules = [text(r).split(":")[-1] for r in as_list(by_sid["MemoryAccessRules"]["Resource"])]
    assert rules == ["policy-engine/${MemoryName}-*", "gateway/team-assistant-${Env}-memory-*"]
    n = naming.names(*NAMES_ARGS)
    # The boundary names the engine by the Memory's name parameter.
    assert n.policy_engine == n.memory
    assert n.memory_gateway == "team-assistant-dev-memory"


def test_bootstrap_version_parameter_matches_the_qualifier():
    (version,) = [s for s in ALLOWS if s["Sid"] == "BootstrapVersion"]
    assert text(version["Resource"]).endswith(f":parameter{naming.BOOTSTRAP_PARAMETER}")


def test_execution_policy_cannot_touch_its_own_policies_or_the_toolkit_roles():
    by_sid = {s["Sid"]: s for s in DENIES}
    assert text(by_sid["DenyPolicyChanges"]["Resource"]).endswith(":policy/TeamAssistantCdk*")
    assert by_sid["DenyToolkitRoles"]["Action"] == "iam:*"
    assert text(by_sid["DenyToolkitRoles"]["Resource"]).endswith(":role/cdk-*")


def test_boundary_denies_identity_changes_without_a_condition():
    (boundary,) = policies(BOUNDARY).values()
    (deny,) = [s for s in statements(boundary, "Deny")]
    assert "Condition" not in deny and deny["Resource"] == "*"
    assert {"iam:*", "sts:AssumeRole", "organizations:*"} <= set(deny["Action"])


def test_runtime_creates_require_the_project_tag_and_other_actions_name_this_project():
    by_sid = {s["Sid"]: s for s in ALLOWS}
    for sid in TAGGED_CREATES:
        assert text(by_sid[sid]["Resource"]).endswith(":runtime/*")
        tags = by_sid[sid]["Condition"]["StringEquals"]
        assert tags["aws:RequestTag/project"] == "team-assistant"
    for sid in ("Runtime", "RuntimeWorkloadIdentity"):
        for resource in as_list(by_sid[sid]["Resource"]):
            value = text(resource)
            assert "team_assistant_" in value or value.endswith(
                "workload-identity-directory/default"
            )
    slr = by_sid["RuntimeIdentityServiceRole"]
    assert slr["Action"] == "iam:CreateServiceLinkedRole"
    assert "aws-service-role/runtime-identity.bedrock-agentcore.amazonaws.com/" in text(
        slr["Resource"]
    )
    # Invoking the agent is for users with a token, never for a deployment.
    actions = {a for s in ALLOWS for a in as_list(s["Action"])}
    assert not {a for a in actions if "InvokeAgentRuntime" in a}


def test_gateways_are_created_with_the_project_tag_and_managed_by_name():
    by_sid = {s["Sid"]: s for s in ALLOWS}
    create = by_sid["GatewayCreate"]
    assert create["Action"] == "bedrock-agentcore:CreateGateway"
    assert create["Condition"] == {"StringEquals": {"aws:RequestTag/project": "team-assistant"}}
    # The gateway and its targets, by name (a gateway id is <name>-<10 characters>); the
    # actions are the handler permissions in the two CloudFormation resource schemas that a
    # target of the Runtime type with a passed-through token needs.
    gateway = by_sid["Gateway"]
    assert text(gateway["Resource"]).endswith(":gateway/team-assistant-*")
    assert set(gateway["Action"]) == {
        "bedrock-agentcore:GetGateway",
        "bedrock-agentcore:UpdateGateway",
        "bedrock-agentcore:DeleteGateway",
        "bedrock-agentcore:SynchronizeGatewayTargets",
        "bedrock-agentcore:CreateGatewayTarget",
        "bedrock-agentcore:GetGatewayTarget",
        "bedrock-agentcore:UpdateGatewayTarget",
        "bedrock-agentcore:DeleteGatewayTarget",
        "bedrock-agentcore:TagResource",
        "bedrock-agentcore:UntagResource",
        "bedrock-agentcore:ListTagsForResource",
    }
    # Tags sent with the create are authorized on gateway/* (the id does not exist yet), so
    # that statement requires the project tag in the request.
    tag = by_sid["GatewayTagOnCreate"]
    assert tag["Action"] == "bedrock-agentcore:TagResource"
    assert text(tag["Resource"]).endswith(":gateway/*")
    assert tag["Condition"] == {"StringEquals": {"aws:RequestTag/project": "team-assistant"}}
    identity = by_sid["GatewayWorkloadIdentity"]
    assert set(identity["Action"]) == {
        "bedrock-agentcore:CreateWorkloadIdentity",
        "bedrock-agentcore:DeleteWorkloadIdentity",
    }
    for resource in as_list(identity["Resource"]):
        value = text(resource)
        assert value.endswith("workload-identity-directory/default") or value.endswith(
            "workload-identity/team-assistant-*"
        )
    assert naming.names(*NAMES_ARGS).gateway.startswith("team-assistant-")
    # The target's schema also lists actions of other target types (OAuth providers, API
    # Gateway, VPC, models): none is granted.
    actions = {a for s in ALLOWS for a in as_list(s["Action"])}
    assert not {a for a in actions if a.startswith(("vpc-lattice:", "apigateway:", "ec2:"))}
    assert "bedrock-agentcore:GetWorkloadAccessToken" not in actions


def test_distributions_are_created_with_the_project_tag_and_changed_only_with_it():
    # A distribution's ARN holds only a generated id, so the tag is the only way to tell this
    # project's distribution from another one of the account.
    by_sid = {s["Sid"]: s for s in ALLOWS}
    changing = {
        "cloudfront:CreateDistribution": "WebDistributionCreate",
        "cloudfront:CreateDistributionWithTags": "WebDistributionCreate",
        "cloudfront:UpdateDistribution": "WebDistribution",
        "cloudfront:DeleteDistribution": "WebDistribution",
        "cloudfront:UntagResource": "WebDistribution",
    }
    for action, sid in changing.items():
        holders = {s["Sid"] for s in ALLOWS if action in as_list(s["Action"])}
        assert holders == {sid}, action
    tagging = {s["Sid"] for s in ALLOWS if "cloudfront:TagResource" in as_list(s["Action"])}
    assert tagging == {"WebDistributionCreate", "WebDistribution"}
    create = by_sid["WebDistributionCreate"]["Condition"]
    assert create == {"StringEquals": {"aws:RequestTag/project": "team-assistant"}}
    assert by_sid["WebDistribution"]["Condition"] == PROJECT_TAG
    # Reads have no condition (a deleted distribution has no tag) and change nothing.
    read = by_sid["WebDistributionRead"]
    assert "Condition" not in read
    assert all(a.split(":")[1].startswith(("Get", "List")) for a in read["Action"])
    for sid in ("WebDistributionCreate", "WebDistribution", "WebDistributionRead"):
        assert text(by_sid[sid]["Resource"]).endswith(":distribution/*")


BOUNDARY_ALLOWS = statements(next(iter(policies(BOUNDARY).values())), "Allow")
# Boundary statements that cannot name this environment's resources, with the reason.
BOUNDARY_WIDE = {
    "DescribeLogGroups": "lists names; the action cannot be limited to one group",
    "ImagePull": "the bootstrap's image repository is shared by every environment",
    "ImagePullToken": "the action has no resource",
    "Model": "the role's own policy names the configured model",
    "Traces": "the actions have no resource",
    "Metrics": "limited by namespace",
    "GuardToolResults": "a guardrail ARN holds a generated id; limited by the project and env tags",
}


def test_boundary_allows_name_this_environment_unless_listed():
    for statement in BOUNDARY_ALLOWS:
        if statement["Sid"] in BOUNDARY_WIDE:
            continue
        for resource in as_list(statement["Resource"]):
            assert (
                "${RuntimeName}-" in text(resource)
                or "${MemoryName}-" in text(resource)
                or "team-assistant-${Env}-" in text(resource)
                or "team-assistant/${Env}/" in text(resource)
                or text(resource).endswith("workload-identity-directory/default")
                or text(resource).endswith("token-vault/default")
            ), statement["Sid"]
    assert {s["Sid"] for s in BOUNDARY_ALLOWS} >= set(BOUNDARY_WIDE)


def test_boundary_lets_the_gateway_exchange_tokens_with_its_own_identity_and_provider():
    # The boundary names resources too: an action allowed only on the Runtime's identity would
    # be denied to the gateway's role at run time.
    by_sid = {s["Sid"]: s for s in BOUNDARY_ALLOWS}
    identity = (
        "workload-identity-directory/default/workload-identity/team-assistant-${Env}-gateway-*"
    )
    provider = "token-vault/default/oauth2credentialprovider/team-assistant-${Env}-exchange"
    tokens = [text(r) for r in as_list(by_sid["WorkloadIdentityTokens"]["Resource"])]
    assert any(r.endswith(identity) for r in tokens)
    vault = [text(r) for r in as_list(by_sid["GithubTokens"]["Resource"])]
    assert any(r.endswith(identity) for r in vault)
    assert any(r.endswith(provider) for r in vault)
    secrets = [text(r) for r in as_list(by_sid["GithubClientSecret"]["Resource"])]
    assert any(r.endswith(":secret:team-assistant/${Env}/exchange-*") for r in secrets)
    n = naming.names(*NAMES_ARGS)
    assert n.gateway == "team-assistant-dev-gateway"
    assert n.exchange_provider == "team-assistant-dev-exchange"


def test_boundary_covers_every_action_of_the_stacks_role():
    """An action the role has but the boundary lacks would be denied at run time."""
    import aws_cdk as cdk
    from team_assistant_infra.stack import build_app

    app = build_app(cdk.App(context=CONTEXT))
    template = app.synth().get_stack_by_name("TeamAssistant-dev").template
    granted = {
        action
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::IAM::Policy"
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
        if statement["Effect"] == "Allow"
        for action in as_list(statement["Action"])
    }
    allowed = {a for s in BOUNDARY_ALLOWS for a in as_list(s["Action"])}
    assert granted - allowed == set()
    assert allowed - granted == set()


@pytest.mark.parametrize("env", ["dev", "ab1-cb1-cb1-cdz"])
def test_boundary_parameters_accept_the_names_from_naming(env):
    import re

    n = naming.names(env, *NAMES_ARGS[1:])
    parameters = BOUNDARY["Parameters"]
    assert re.fullmatch(parameters["Env"]["AllowedPattern"].strip("^$"), env)
    assert len(env) <= parameters["Env"]["MaxLength"]
    assert re.fullmatch(parameters["RuntimeName"]["AllowedPattern"].strip("^$"), n.runtime)
    assert re.fullmatch(parameters["MemoryName"]["AllowedPattern"].strip("^$"), n.memory)


def test_managed_policy_stays_under_the_iam_size_limit():
    import json

    # IAM limit: 6144 characters per managed policy, whitespace not counted.
    for policy in [*EXECUTION_POLICIES, *policies(BOUNDARY).values()]:
        size = len(json.dumps(policy["PolicyDocument"], separators=(",", ":")))
        assert size < 6144, (policy["ManagedPolicyName"], size)
