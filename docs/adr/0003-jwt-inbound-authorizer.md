# 0003. JWT inbound authorizer on the Runtime

Status: accepted · 2026-11-14

## Context

The agent must know which user is asking, and nobody without an account may reach it. AgentCore
Runtime accepts either IAM-signed requests or JWT bearer tokens, not both on one Runtime.

## Decision

- **JWT authorizer** with the environment's Cognito user pool: discovery URL of the pool, the
  app client as the only allowed client (`allowedClients`, compared with the token's
  `client_id`) and the environment's audience (`allowedAudience`, compared with `aud`,
  docs/adr/0002). "If both client_id and aud is provided, the agent runtime authorizer will
  verify both" (AgentCore developer guide, inbound JWT authorizer). The access token carries
  both claims, so it is the token the client sends.
- **The gateway must be in the request's identity chain**, so a direct call with a valid token
  is refused (docs/adr/0004).
- **The `Authorization` header is allowlisted** for the agent
  (`request_header_configuration`), so the agent can read the `sub` claim. The agent does not
  verify the signature again.
- **No IAM invocation path.** The header `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` lets a
  trusted caller name any user without proof; it is not used.

## Consequences

- A request without a token, or with a token of another pool, client or audience, is refused
  by the Runtime with 401 before the container starts; the agent's own check is a second line.
- Only the bound access token passes: the id token has no `client_id`, and a token without
  the audience is refused.
