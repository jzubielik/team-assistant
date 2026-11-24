# 0008. Memory behind a gateway that checks the user's token

Status: accepted · 2026-11-22

## Context

The agent's role could call Memory for any user: which user's history and records a request
reached was decided by the agent's code alone. Memory's API takes only AWS credentials, so
Memory cannot tell which user a call is for. A gateway in front of Memory can: it checks the
user's token and applies access rules to every call (AgentCore developer guide, "Fine-grained
access control for Memory").

## Decision

- **A second gateway, for Memory only**, with inbound `CUSTOM_JWT`: the same pool, login
  client and audience as the first (docs/adr/0004). Its one target is the connector
  `agentcore-memory` for this environment's Memory; the gateway calls Memory with its own role
  (`GATEWAY_IAM_ROLE`). A gateway of its own, because access rules refuse every call that no
  rule permits.
- **Access rules** (Cedar, a policy engine in mode `ENFORCE`): a call for events or sessions
  passes only when its `actorId` equals the token's `sub`; a call for records only when its
  namespace equals a claim of the token. One more read passes, of the events of an actor
  `session_<id>`: for a new session the SDK's session manager looks there for an event of
  its earlier layout, and no rule lets anyone write there. The rules cover what the agent
  calls: no single event is read or deleted.
- **The namespaces are claims of the token.** A rule cannot build `/facts/<sub>/` from `sub`.
  A Lambda function (pre token generation, event version 2) adds `memory_preferences`,
  `memory_facts` and `memory_summaries` to every access token: at login, at refresh and at
  the first gateway's token exchange.
- **`make deploy` creates and deletes one more rule for the history and one for the
  conversation list.** The developer guide says the rules get `actorId` from the request's
  address. The gateway gives it to them for an operation only after a rule naming that
  operation was deleted; after one was created it gives none, the comparison with `sub`
  has nothing to compare, and the user's own call is refused. A deployment only creates
  rules. So after every deploy `make deploy` creates a rule that permits nothing and deletes
  it, once for a conversation's history (ListEvents) and once for the conversation list
  (ListSessions).
- **The agent calls `<memory gateway>/memory/memories/...`** with the Memory client of boto3:
  the gateway as its endpoint, no signature, the request's token as the bearer token. The
  session manager, the memory tools, the title and the page's reads use that client. The
  agent's role has no Memory action.

## Consequences

- A call that names another user's actor id or namespace is refused by the gateway, whatever
  the agent's code does (`make smoke`, "memory gateway").
- A signed-in user can call the Memory gateway with their own token: read and add their own
  events and read their own records, nothing of another user. A gateway with a JWT
  authorizer cannot be limited to the agent.
- The agent holds the token of the user it is answering, so a taken-over agent can reach that
  user's memory, and no other's.
- A token without the namespace claims is refused for records; events still pass.
- The agent uses the token it got with the request for the whole request: if the token
  expires before the answer is stored, the answer was streamed and the request ends with an
  error.
- The operator's scripts call Memory with the operator's credentials, as before: the Memory
  has no resource policy.
- The agent works only after `make deploy`'s step with the two rules, which rests on
  behaviour of the service that no document describes. Without it the agent answers no
  message and the page lists no conversation. Another user's data is refused either way. A
  rule that someone creates for one of the two operations brings the refusal back until
  `make deploy` runs again. While the step runs, the user's own reads are refused.
- A Lambda function, a policy engine with four policies, a gateway with its target and role,
  and a sixth execution policy; `make bootstrap` before the deploy.
