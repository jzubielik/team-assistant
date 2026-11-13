"""Every physical name of team-assistant, in one place.

Convention: ``team-assistant-<env>-<component>``, adapted where the service forbids hyphens or
limits the length. Each constraint was read from aws-cdk-lib 2.275.0 (construct validation)
or, where CDK does not validate, from the AWS API model in botocore 1.44.2:

- Stack TeamAssistant-<env>: ^[A-Za-z][A-Za-z0-9-]*$, <= 128 (core/lib/stack.js).
- Runtime team_assistant_<env with - as _>_runtime: ^[a-zA-Z][a-zA-Z0-9_]{0,47}$
  (aws-bedrockagentcore/lib/runtime/runtime.js).
- Memory team_assistant_<env with - as _>_memory: ^[a-zA-Z][a-zA-Z0-9_]{0,47}$
  (aws-bedrockagentcore/lib/memory/memory.js).
- User pool team-assistant-<env>-users: [\\w\\s+=,.@-]+, 1..128 (cognito-idp UserPoolNameType).
- Prefix domain ta-<env>-<login suffix>: ^[a-z0-9](?:[a-z0-9\\-]{0,61}[a-z0-9])?$, 1..63
  (cognito-idp DomainType). A prefix must be unique in the region across all of AWS. The
  suffix (eight random hexadecimal characters, LOGIN_SUFFIX_<env> in local.mk) makes it so
  without the account id, which the browser would otherwise show in the login address.
- GitHub credential provider team-assistant-<env>-github: [a-zA-Z0-9\\-_]+, 1..128
  (bedrock-agentcore-control CredentialProviderName).
- Exchange credential provider team-assistant-<env>-exchange and its placeholder secret
  team-assistant/<env>/exchange: the same rules as the GitHub provider and secret.
- GitHub client secret team-assistant/<env>/github: letters, digits and /_+=.@-, <= 512, not
  ending in "-" and six characters (CloudFormation reference, AWS::SecretsManager::Secret).
- Gateway team-assistant-<env>-gateway: ^([0-9a-zA-Z][-]?){1,48}$, so no two hyphens in a row
  (CloudFormation resource schema, AWS::BedrockAgentCore::Gateway, Name); 38 characters at the
  longest environment name.
- Web page bucket ta-<env>-web-<login suffix>: 3..63 lowercase letters, digits and hyphens,
  unique across all of AWS (S3 bucket naming rules); the login suffix keeps it unique without
  the account id. 23 characters at the longest environment name.
- Token audience https://team-assistant-<env>.agent: [\\x21\\x23-\\x5B\\x5D-\\x7E]+, 1..256
  (cognito-idp ResourceServerIdentifierType). A URL because Cognito binds a token only to a
  URL-formatted resource; nothing is served at it.

CDK bootstrap of this project (docs/adr/0001), shared by every environment of an account:

- Qualifier teamasst: [A-Za-z0-9_-]{1,10} in the CLI's bootstrap template; lowercase
  alphanumerics here because it is part of the assets bucket name.
- Toolkit stack TeamAssistantToolkit, policies stack team-assistant-cdk-policies.
- Managed policies TeamAssistantCdkExecution, TeamAssistantCdkExecutionRoles,
  TeamAssistantCdkExecutionGateway, TeamAssistantCdkExecutionWeb (several: a managed policy
  holds at most 6144 characters)
  and TeamAssistantCdkPermissionsBoundary-<env>: [\\w+=,.@-]+, <= 128 (iam CreatePolicy
  policyNameType).
- Boundary stack team-assistant-<env>-boundary; role path /team-assistant/.

Environment names are lowercase alphanumerics with single inner hyphens, at most 15
characters: 15 keeps the Runtime and Memory names (the tightest limit, 48) at 38 characters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PROJECT = "team-assistant"
DEFAULT_ENV = "dev"

CDK_QUALIFIER = "teamasst"
TOOLKIT_STACK = "TeamAssistantToolkit"
POLICIES_STACK = "team-assistant-cdk-policies"
EXECUTION_POLICIES = (
    "TeamAssistantCdkExecution",
    "TeamAssistantCdkExecutionRoles",
    "TeamAssistantCdkExecutionGateway",
    "TeamAssistantCdkExecutionWeb",
)
BOUNDARY_POLICY_PREFIX = "TeamAssistantCdkPermissionsBoundary-"
BOOTSTRAP_PARAMETER = f"/cdk-bootstrap/{CDK_QUALIFIER}/version"
ROLE_PATH = "/team-assistant/"
# The gateway's one target, the Runtime. The name is unique only inside a gateway, so it needs
# no environment; it is a part of the address clients call (<gateway>/<target>/invocations).
GATEWAY_TARGET = "agent"

_ENV = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_ENV_MAX = 15
_ACCOUNT = re.compile(r"[0-9]{12}")
# Made once per environment by the operator: openssl rand -hex 4.
_LOGIN_SUFFIX = re.compile(r"[0-9a-f]{8}")


def validate_env(env: str) -> str:
    """Return ``env`` when it is a valid environment name; raise ValueError otherwise."""
    if not _ENV.fullmatch(env) or len(env) > _ENV_MAX:
        raise ValueError(
            f"invalid ENV '{env}': lowercase letters and digits with single inner hyphens, "
            f"starting with a letter, at most {_ENV_MAX} characters (e.g. dev, qa-2)"
        )
    return env


@dataclass(frozen=True)
class Names:
    env: str
    account: str
    region: str
    stack: str
    runtime: str
    memory: str
    user_pool: str
    domain_prefix: str
    boundary_policy: str
    boundary_stack: str
    github_provider: str
    github_secret: str
    exchange_provider: str
    exchange_secret: str
    audience: str
    gateway: str
    web_bucket: str


def names(env: str, account: str, region: str, login_suffix: str) -> Names:
    validate_env(env)
    if not _ACCOUNT.fullmatch(account):
        raise ValueError("invalid account: expected 12 digits")
    if not _LOGIN_SUFFIX.fullmatch(login_suffix):
        raise ValueError(
            "invalid login suffix: expected 8 lowercase hexadecimal characters "
            "(LOGIN_SUFFIX_<env> in local.mk; make one with: openssl rand -hex 4)"
        )
    snake = env.replace("-", "_")
    return Names(
        env=env,
        account=account,
        region=region,
        stack=f"TeamAssistant-{env}",
        runtime=f"team_assistant_{snake}_runtime",
        memory=f"team_assistant_{snake}_memory",
        user_pool=f"{PROJECT}-{env}-users",
        domain_prefix=f"ta-{env}-{login_suffix}",
        boundary_policy=f"{BOUNDARY_POLICY_PREFIX}{env}",
        boundary_stack=f"{PROJECT}-{env}-boundary",
        github_provider=f"{PROJECT}-{env}-github",
        github_secret=f"{PROJECT}/{env}/github",
        exchange_provider=f"{PROJECT}-{env}-exchange",
        exchange_secret=f"{PROJECT}/{env}/exchange",
        audience=f"https://{PROJECT}-{env}.agent",
        gateway=f"{PROJECT}-{env}-gateway",
        web_bucket=f"ta-{env}-web-{login_suffix}",
    )
