# 0001. Dedicated least-privilege CDK bootstrap

Status: accepted · 2026-11-13

## Context

CDK deploys through a "bootstrap": a stack with an assets bucket, an image repository and five
IAM roles. `cdk bootstrap` with defaults creates the stack `CDKToolkit` (qualifier
`hnb659fds`). Its CloudFormation execution role gets `AdministratorAccess`, so whoever can
deploy through it can create anything in the account, IAM users and admins included. Its
deploy, publishing and lookup roles trust every principal of the account. With an empty
`FileAssetsBucketKmsKeyId` parameter the stock template also creates a customer KMS key, which
has a fixed monthly fee.

**Threat model.** The attacker controls what CloudFormation deploys through the bootstrap: a
mistaken or malicious template change, or a compromised session that can assume the deploy
role. Such a deploy must be able to create only this project's resources, and no role with
more rights than the agent needs at run time.

## Decision

- **Own bootstrap.** Qualifier `teamasst`, toolkit stack `TeamAssistantToolkit`; both in
  `infra/team_assistant_infra/naming.py`. The stack uses
  `DefaultStackSynthesizer(qualifier=...)`. An existing default bootstrap is never touched.
- **Execution policy instead of AdministratorAccess.** `infra/bootstrap/execution-policies.yaml`
  (stack `team-assistant-cdk-policies`, deployed by `make bootstrap` with the operator's
  credentials) holds `TeamAssistantCdkExecution`, `TeamAssistantCdkExecutionRoles`,
  `TeamAssistantCdkExecutionGateway`, `TeamAssistantCdkExecutionWeb`,
  `TeamAssistantCdkExecutionGuardrail` and `TeamAssistantCdkExecutionMemoryGateway` (six,
  because a
  managed policy holds at most 6144 characters), the only policies of the execution role. Each
  statement covers one resource type of the stack, scoped to this project's names; a change
  that adds a resource type adds its actions.
- **Roles only with a boundary.** The execution role may create roles only under the path
  `/team-assistant/` and only with the permissions boundary
  `TeamAssistantCdkPermissionsBoundary-<env>`; a Deny on every resource backs the condition.
  It may pass such a role only to `bedrock-agentcore.amazonaws.com`. It cannot change who may
  assume a role, remove a boundary, create users, access keys or policies, or change its own
  policies or the toolkit roles.
- **Per-environment boundary.** `infra/bootstrap/boundary.yaml`, stack
  `team-assistant-<env>-boundary`, deployed by `make deploy` before the stack and removed by
  `make destroy` after it. It denies IAM, Organizations, account, Identity Center and
  `sts:AssumeRole*`, and allows nothing until the stack has a role.
- **Deploy roles trust only the deployer.** `scripts/bootstrap_template.py` copies the installed
  CLI's bootstrap template and adds `ArnLike aws:PrincipalArn` to the trust of the deploy,
  file-publishing, image-publishing and lookup roles. The pattern is derived from the caller of
  `make bootstrap` (for IAM Identity Center: every role of the same permission set).
  Cross-account trust statements are dropped.
- **No customer KMS key.** `make bootstrap` passes `--bootstrap-kms-key-id AWS_MANAGED_KEY`; a
  test fails when the option is removed.
- **Account check.** Every AWS command compares the profile's account with `ACCOUNT_<env>` from
  `local.mk` and stops on a mismatch or when the setting is missing. Messages and banners never
  print the account id.

## Consequences

- A resource type the policy does not cover fails with `AccessDenied` in a stack event of
  `make deploy`. The fix is a scoped statement in `execution-policies.yaml`, then
  `make bootstrap` again.
- `infra/tests/test_bootstrap.py` fails on an Allow with a wildcard action, on an IAM action
  outside `/team-assistant/`, and on a role-granting action without the boundary condition.
- Residual risks:
  - The two IAM templates are deployed with the operator's own credentials; a change to them is
    reviewed in the diff of the commit, not by a tool.
  - The execution role can create a `/team-assistant/` role that trusts any principal. That
    role is still limited by a boundary, and its trust cannot be changed afterwards.
  - Every principal of the operator's permission set can assume the deploy roles.
  - `cdk synth` (in `make deploy`, `make diff`) runs this repository's code with the
    operator's credentials.
  - Nothing stops someone from creating the default bootstrap next to this one.
