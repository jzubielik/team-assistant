# 0002. Cognito user pool as the JWT issuer

Status: accepted · 2026-11-13

## Context

AgentCore Runtime can check a JWT on every request before the agent sees it. It needs an
issuer that publishes an OpenID Connect discovery document. The agent keeps a conversation
history per user, so every user needs a stable id that cannot be chosen by the caller. The
project allows no resource with a fixed monthly fee and no agent component from outside AWS.

## Decision

- **Amazon Cognito user pool**, one per environment, in the stack `TeamAssistant-<env>`.
- **Feature plan Essentials with managed login** (version 2). An access token can be bound to
  a resource (the `aud` claim, "resource indicators", RFC 8707) only on Essentials or Plus and
  only with managed login.
- **A resource server** whose identifier is the audience, `https://team-assistant-<env>.agent`,
  without scopes. The Cognito guide to resource binding starts from a resource server; the URL
  has no network meaning. `scripts/auth.py` and the page send it as `resource=` in the
  authorize request only; a refreshed token keeps its `aud`. The resource server needs a name
  of its own: it defaults to the identifier, and a name may not hold ":" or "/".
- **A managed login style for the app client** (`AWS::Cognito::ManagedLoginBranding` with
  Cognito's default values). Without one, the login pages of an app client created by code
  answer 403.
- **Operator-created users only.** Self sign-up is off. `make user ENV=<env> USER=<name>`
  creates a user with a permanent password typed at a hidden prompt. No message is sent: the
  pool has no e-mail or SMS configuration, so nothing depends on SES or SNS.
- **Sign-in by username.** No e-mail or phone attribute is collected.
- **One public app client**, without a secret, because neither a command-line program nor a
  browser page can keep one. OAuth flow: authorization code only (the client adds PKCE), scope
  `openid`. Access and id tokens live 60 minutes, the refresh token one day.
  `ExplicitAuthFlows` holds the refresh flow and the token exchange flow
  (`ALLOW_TOKEN_EXCHANGE_IMPERSONATION`, used by the gateway, docs/adr/0004): left unset,
  Cognito also allows password (SRP) and custom authentication through its API.
- **Prefix domain** `ta-<env>-<suffix>` (a random suffix from `local.mk`, so the address does
  not show the account id). A prefix domain has no certificate to manage.
- **No machine-to-machine client.** The project has no service caller.
- **Removal policy DESTROY.** `make destroy` deletes the pool with its users.

## Consequences

- The user's stable id is the `sub` claim. `src/team_assistant/identity.py` reads it from the
  token the Runtime has already verified.
- A token of this app client obtained without the binding, or bound to another resource, is
  refused by the gateway and the Runtime (both require the audience).
- Deleting the environment deletes the users; after a new deploy the operator creates them
  again, and every `sub` is new, so earlier conversation history is not reachable.
- A user who forgets the password asks the operator (`make user` again resets it). There is no
  self-service recovery.
- The login page is reachable by anyone who knows the domain; only operator-created users can
  log in.
