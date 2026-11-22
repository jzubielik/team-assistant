# Security

Who can do what in one environment.

## Layers

1. **Login.** A user gets a token only from the environment's Cognito user pool, and only the
   operator creates users (docs/adr/0002). The login asks for an access token bound to this
   environment's agent: the token names it in its `aud` claim.
2. **Gateway.** Clients call an AgentCore Gateway, the only way to the agent. It checks the
   token's signature, issuer, expiry, `client_id` and `aud`, exchanges it at the user pool for
   a new access token of the same user bound to the agent (RFC 8693), and sends that to the
   Runtime. The Runtime accepts only requests whose identity chain includes this gateway
   (`AllowedWorkloadConfiguration`): a valid token sent straight to the Runtime is refused
   (`make smoke`, "direct call"). The exchange itself narrows nothing (the issued token has the
   same user, client, scope and audience, and anyone holding the user's token could exchange
   it); its purpose is the identity chain. The exchange provider holds a generated placeholder
   client secret, because AgentCore requires one and Cognito ignores it for the public login
   client.
3. **Runtime authorizer.** AgentCore Runtime checks the token's signature, issuer, expiry,
   `client_id` and `aud` before a request reaches the agent (docs/adr/0003). A request without
   a valid token never starts the agent; neither does a token of this app client that was
   issued without the binding.
4. **Agent.** The agent takes the user id from the verified token (`sub`) and opens only that
   user's conversation history. A request without a usable token is an error; there is no
   default user. The Runtime routes by session id alone, so the agent keeps each session id to
   the user who used it first for as long as the session's microVM runs, and refuses it to
   anyone else: two users never share a microVM. A stopped session's id is free again; its
   history stays per user.
5. **Permissions.** The agent's role (below) can call one model, read and write one Memory,
   apply one guardrail and get users' GitHub tokens from one provider; the gateway's role can
   only exchange tokens through its own provider and invoke this Runtime. Deployments run with a
   separate, limited role (docs/adr/0001).

## Runtime role

The role the agent's container runs with: `RuntimeExecutionRole` in the stack, path
`/team-assistant/`, limited by the boundary `TeamAssistantCdkPermissionsBoundary-<env>`.
`infra/tests/test_security_doc.py` fails when this table and the synthesized policy differ.

| Action | Resource | Why |
|---|---|---|
| `bedrock:InvokeModel` | the configured inference profile and the model it routes to | answer a message |
| `bedrock:InvokeModelWithResponseStream` | the same | stream the answer |
| `bedrock-agentcore:CreateEvent` | this environment's Memory | store a message or a conversation's title |
| `bedrock-agentcore:GetEvent` | this environment's Memory | load the conversation |
| `bedrock-agentcore:ListEvents` | this environment's Memory | load the conversation; read a conversation's history and title for the web page |
| `bedrock-agentcore:ListSessions` | this environment's Memory | list the asking user's conversations for the web page (the actor comes from the token) |
| `bedrock-agentcore:DeleteEvent` | this environment's Memory | the session manager replaces stored agent state |
| `bedrock:ApplyGuardrail` | this environment's guardrail | check the GitHub tools' results for instructions to the assistant before the model reads them |
| `bedrock-agentcore:RetrieveMemoryRecords` | this environment's Memory | the model's memory tools search the user's facts, or the summaries of the user's conversations |
| `bedrock-agentcore:ListMemoryRecords` | this environment's Memory | list the user's preferences for the system prompt |
| `bedrock-agentcore:GetResourceOauth2Token` | the default token vault, this environment's GitHub provider, this Runtime's workload identity | get the asking user's GitHub token, or a consent URL |
| `bedrock-agentcore:CompleteResourceTokenAuth` | the same | bind a user's consent to the user named by the request's token |
| `secretsmanager:GetSecretValue` | this environment's GitHub client secret | AgentCore reads the OAuth App's secret with the caller's credentials |
| `logs:CreateLogGroup` | log groups `/aws/bedrock-agentcore/runtimes/*` | the Runtime creates its own log group |
| `logs:DescribeLogStreams` | the same | added by the Runtime construct |
| `logs:CreateLogStream` | log streams of those groups | write the log |
| `logs:PutLogEvents` | log streams of those groups | write the log |
| `logs:PutResourcePolicy` | this Runtime's log groups | AgentCore lets X-Ray deliver the agent's spans to the agent's own log group |
| `logs:DescribeLogGroups` | every log group (names only) | added by the Runtime construct; the action cannot name one group |
| `ecr:BatchCheckLayerAvailability` | the bootstrap's image repository | pull the agent image |
| `ecr:BatchGetImage` | the bootstrap's image repository | pull the agent image |
| `ecr:GetDownloadUrlForLayer` | the bootstrap's image repository | pull the agent image |
| `ecr:GetAuthorizationToken` | `*` | log in to the registry; the action has no resource |
| `bedrock-agentcore:GetWorkloadAccessToken` | the Runtime's workload identity | added by the Runtime construct; used by outbound credentials |
| `bedrock-agentcore:GetWorkloadAccessTokenForJWT` | the Runtime's workload identity | the same |
| `bedrock-agentcore:GetWorkloadAccessTokenForUserId` | the Runtime's workload identity | added by the Runtime construct; denied below |
| `xray:PutTraceSegments` | `*` | added by the Runtime construct; the agent's spans |
| `xray:PutTelemetryRecords` | `*` | the same |
| `xray:GetSamplingRules` | `*` | the same |
| `xray:GetSamplingTargets` | `*` | the same |
| `cloudwatch:PutMetricData` | `*`, namespace `bedrock-agentcore` only | added by the Runtime construct |

Statements on `*` are those whose action supports no resource (registry login, X-Ray, metrics).
The boundary narrows the log permissions to this environment's Runtime.

Explicitly denied, whatever an Allow says:

| Action | Resource | Why |
|---|---|---|
| `bedrock-agentcore:GetWorkloadAccessTokenForUserId` | `*` | names a user by a plain id, not by a verified token; every request here carries a token |
| `bedrock-agentcore:InvokeAgentRuntimeForUser` | `*` | the same |
| `bedrock-agentcore:GetWorkloadAccessToken` | every workload identity but this Runtime's | the construct allows all identities of the account; the agent acts only as itself |
| `bedrock-agentcore:GetWorkloadAccessTokenForJWT` | the same | the same |

## Gateway role

The role the gateway acts with: `GatewayRole` in the stack, path `/team-assistant/`, limited by
the same boundary. Only this environment's gateway can assume it (trust policy: the AgentCore
service, this account, a gateway named `team-assistant-<env>-gateway`).

| Action | Resource | Why |
|---|---|---|
| `bedrock-agentcore:InvokeAgentRuntime` | this environment's Runtime and its endpoints | relay a request the gateway has accepted |
| `bedrock-agentcore:GetWorkloadAccessToken` | the gateway's own workload identity | the gateway's identity for the token exchange |
| `bedrock-agentcore:GetWorkloadAccessTokenForJWT` | the same | the same, for the user named by the caller's token |
| `bedrock-agentcore:GetResourceOauth2Token` | the default token vault, this environment's exchange provider, the gateway's workload identity | exchange the caller's token for the one sent to the Runtime |
| `secretsmanager:GetSecretValue` | the exchange provider's placeholder secret | AgentCore reads the provider's client secret with the caller's credentials; the value is a generated placeholder that Cognito ignores |

Explicitly denied, whatever an Allow says:

| Action | Resource | Why |
|---|---|---|
| `bedrock-agentcore:GetWorkloadAccessTokenForUserId` | `*` | names a user by a plain id, not by a verified token; the gateway always has the caller's token |
| `bedrock-agentcore:InvokeAgentRuntimeForUser` | `*` | the same |

The gateway exchanges the caller's token at the user pool and sends the issued token to the
Runtime, which verifies it again. The role holds `InvokeAgentRuntime`, as the official samples
grant it.

## Who can read conversations

- **The agent**, for the user of the current request only: the actor id comes from the verified
  token, never from the request body.
- **The operator**, with the admin profile: AgentCore Memory's API lists any actor's events.
- **Not the deploy role**: the CloudFormation execution policy has no event actions.

## Who can use a user's GitHub token

- **The agent**, for the user of the current request only: AgentCore Identity keeps the token
  for (this agent's workload identity, this user), and only the Runtime can act as the agent
  ("WorkloadIdentity is linked to a service" for any other caller).
- **Completing a consent** binds it to the user named by the token of the forwarding request;
  the client checks the `state` the agent sent, so a return from another consent is not
  forwarded. Only the agent's role holds `CompleteResourceTokenAuth`.
- **The token never reaches the model**: the tools' schemas have only `repository` and
  `state` (`list_issues`) or `repository` and `number` (`get_issue`).
  It is not logged, not streamed and not written outside the vault.
- **Scope**: the OAuth App requests no scope, so the token reads public data only.
- **The OAuth App's client secret** is in Secrets Manager; the deploy role can read it (AgentCore
  reads an EXTERNAL secret with the caller's credentials when the provider is created), and so
  can the agent's role.

## Traces

Off by default (`[tracing] enabled = false` in `config/agent.toml`): the Runtime's environment
then disables the OpenTelemetry SDK and every exporter, and the agent sends no span, metric or
log record. When switched on, the settings in `TRACING_ENVIRONMENT` (stack.py) apply:

- Spans carry `actor.id` and `session.id`, names of tools, the model id, timings and token
  counts. Strands replaces prompts, answers, the system prompt and tool input and output with
  `[REDACTED]` (`tests/test_tracing.py` runs a request and searches every span for them).
- An error's text never reaches a span from the model (the model class keeps only the error's
  type), from the GitHub tool (it returns a fixed text) or from tool arguments of a wrong type
  (the call is cancelled before the tool, with a fixed text). Strands would otherwise copy the
  text into the span status, which no redaction setting covers, and such texts can quote
  request values.
- No span per AWS call or HTTP request (their status holds the error text of the call, their
  attributes the address); no custom metrics (they cannot be deleted); spans go to the
  agent's own log group, which `make destroy` deletes.

A known gap when tracing is on:

- An error of the memory layer (writing or reading the conversation) passes through Strands,
  which writes its text into the status of the agent's span.

## Logs

The Runtime stores the container's stdout in its log group; that cannot be switched off.

- The agent's own lines are JSON with ids, token counts and error types, never message text.
- A library's line is written as its wording only (`src/team_assistant/logs.py`): the logger,
  the level, the message as it stands in the library's code with its `%s` placeholders, and
  the types of any exceptions. Values and tracebacks are dropped, because libraries put the
  text of an error into the line, and such a text can quote a stored message.
- Limit: a library that builds a value into the message itself (an f-string) is not caught.
  bedrock-agentcore 1.26.0 and strands-agents 1.64.0 have no such warning or error line.

## Web page

- **Tokens** stay in the tab's `sessionStorage` under a key with the app client id, so they die
  with the tab and a redeployed environment starts signed out. The access token is refreshed
  five minutes before it expires; a refused refresh clears them. The page never holds AWS
  credentials.
- **Hosting.** The bucket blocks public access, enforces TLS and is readable only by this
  distribution (origin access control, the distribution's ARN in the bucket policy). CloudFront
  redirects to HTTPS and adds a response headers policy: CSP (scripts, styles, fonts and images
  from the page's origin only, no frames, no `object`, `base-uri 'none'`), HSTS one year,
  `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` (the consent
  return carries a session id). `make deploy` writes a second CSP as a meta tag whose
  `connect-src` names exactly the gateway and the login domain; the browser enforces both.
  `scripts/web_files.py` refuses an HTML file with an inline script; only origins of the stack
  outputs reach the policy.
- **Model text** is rendered by `markdown-it` with raw HTML off, images off and only `https`
  links to `github.com`, which open with `noopener noreferrer`; any other address stays text,
  so text the agent read cannot hand the user a link to another site.
- **GitHub consent.** `callback.html` holds no token and only relays `session_id` and `state` to
  the chat tab; the chat tab sends a consent only for the state its question waits for. The
  agent takes the return URL from its own list (`cli`, `web-local`, `web` from the stack's
  `WEB_URL`); an unknown client is refused before the model.
- **Conversations and history** are always the token's user's (`ListSessions`, `ListEvents`
  with the actor from the token); the body cannot name another user.
- **get_issue** gives the model an issue's text, cut at 2,000 characters and marked as the
  author's words, not instructions.
- **Deployment** of the page uses a fourth bootstrap policy, `TeamAssistantCdkExecutionWeb`:
  S3 on buckets named `ta-*-web-*`; CloudFront distributions that carry the tag
  `project=team-assistant` (created with it, changed and deleted only with it; reads of any
  distribution of the account); this account's origin access controls and response headers
  policies, which have neither tags nor names in their ARNs. Actions from the CloudFormation
  resource schemas.

## Residual risks

- Separation between users rests on the agent's code: the role can read every actor's events
  and long-term records in the Memory, and the agent chooses the actor from the token. IAM has
  condition keys for the actor (`bedrock-agentcore:actorId`) and, on `RetrieveMemoryRecords`
  and `ListMemoryRecords`, for the namespace (`bedrock-agentcore:namespace`,
  `bedrock-agentcore:namespacePath`), but one role serves all users, so none of them can be
  bound to the caller here.
- A failed read of long-term memory does not stop the answer: without the listed preferences
  the agent answers without them (log line `preferences_failed`), and a memory tool that
  fails tells the model so (log line `tool_failed`). Both lines name the error type only; no
  alarm watches them (the project has no alarms).
- Long-term records are text extracted from the user's earlier conversations. The preferences
  among them enter the system prompt of that user's later requests, so a user can shape the
  instructions of their own conversations, not of anyone else's.
- The model is reachable through any region the global inference profile routes to.
- The Runtime's log groups are created by the service without a retention period; `make destroy`
  deletes them (logs are kept until then, no longer and no shorter).
- The gateway's token exchange narrows nothing: the issued token has the same user, client,
  scope and audience, and anyone who holds a user's token could exchange it. What protects the
  agent is that the Runtime accepts only the gateway; a stolen token still works through the
  gateway until it expires (60 minutes).
- The login client has the token exchange flow, which any holder of its tokens can use; it
  yields nothing beyond the token presented.
- The token is passed to the agent's process. The agent never logs it.
- The page's header CSP allows `connect-src` to any https host; only the meta tag written by
  `make deploy` narrows it to the gateway and the login domain. A page served without that step
  (a manual upload) would be looser.
- Script injected into the page could read the tab's tokens from `sessionStorage`; the CSP
  (no inline script, scripts only from the page's origin) and the text-only rendering are what
  prevent such script.
- An issue's text can try to instruct the model. The GitHub tools' results are marked as data
  and checked by the environment's guardrail (PROMPT_ATTACK, LOW) before the model reads them;
  a result it flags, or one that cannot be checked, is withheld. Neither removes the risk. The
  tools only read public repositories, and the page links only to github.com.
- What the assistant writes, about an issue or as its own suggestion, becomes part of that
  conversation's summary. Summaries are returned only by `recall_conversations`, for questions
  about earlier conversations, marked as data and as the assistant's past suggestions; the
  fact search never reads them. The text stays within that user's records.
- The token vault is one per account and region, shared by every environment in the account;
  separate vaults need separate accounts. It uses the AWS owned key (no customer managed key in
  this project).
