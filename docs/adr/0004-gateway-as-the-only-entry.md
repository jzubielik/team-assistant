# 0004. AgentCore Gateway as the only entry to the Runtime

Status: accepted · 2026-11-15

## Context

The Runtime checks the user's token itself (docs/adr/0003). The AgentCore security guide
recommends fronting a Runtime with a gateway as its single entry point, which only helps when
the Runtime cannot be reached around it. The agent needs the user's identity in every request:
per-user memory and the user's GitHub token depend on it.

## Decision

- **A gateway without a protocol type** with inbound `CUSTOM_JWT`: the pool's discovery URL,
  the login client, the audience (docs/adr/0002). Clients call
  `<gateway>/agent/invocations` and stop sessions at `<gateway>/agent/stopruntimesession`.
- **One target, the Runtime, with token exchange** (`OAUTH`, `TOKEN_EXCHANGE`): the gateway
  exchanges the caller's token at the user pool (RFC 8693) through a `CustomOauth2` credential
  provider and sends the issued token. Only then does the request carry the gateway in its
  identity chain.
- **The Runtime accepts only that gateway** (`AllowedWorkloadConfiguration` with the gateway's
  ARN on its JWT authorizer).

Facts the documentation did not give, found by probes on a deployed environment:

- With `JWT_PASSTHROUGH` the gateway adds no identity chain, and the restricted Runtime refuses
  its requests ("Transaction token required").
- Cognito exchanges a token only for the app client that issued it; the login client (public)
  therefore has `ALLOW_TOKEN_EXCHANGE_IMPERSONATION`, and the provider acts as that client.
- AgentCore requires a client secret for the provider; Cognito ignores one sent in the request
  body by a public client. The provider holds a generated placeholder (`team-assistant/<env>/
  exchange`), and the client id and secret go in the body (`CLIENT_SECRET_POST`).
- Cognito refuses AgentCore's default subject token type (`...:jwt`); the target sets
  `subject_token_type` to the access-token URN and `audience` in its custom parameters, scope
  `openid`.
- The issued token has the same `sub`, `client_id`, scope and the audience, so the Runtime's
  authorizer, the agent's identity, memory and the GitHub vault work unchanged.

## Consequences

- A valid token sent straight to the Runtime is refused (`make smoke`, "direct call").
- The exchange narrows nothing; the protection is the restriction of the Runtime
  (docs/security.md, residual risks).
- One more secret while deployed and one more credential provider.
- Three new Cognito and AgentCore features carry the design (token exchange from 2026-11-12,
  workload restriction, Runtime targets); CDK's validation does not know the exchange flow yet
  and warns at synth.
