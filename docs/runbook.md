# Runbook

Every command is a `make` target (`make help` lists them). Commands that touch AWS need
`ENV=<name>` on the command line.

## Before you start

1. Install Docker, GNU make and the AWS CLI v2 on the host. Everything else runs in the tool
   runner image.
2. `cp local.mk.example local.mk` and fill in `PROFILE`, `REGION` and `ACCOUNT_<env>`.
   `local.mk` is gitignored. Settings are never read from the shell environment.
   Add `LOGIN_SUFFIX_<env> = <output of openssl rand -hex 4>` once per environment and keep
   it: it ends the Cognito login domain (`ta-<env>-<suffix>`), which must be unique across AWS,
   without putting the account id into the login address.
3. `make runner` builds the runner image. `make hooks` turns on the git hooks (once per clone).
4. `make check` runs the linters and tests. It makes no AWS call.
5. Log in: `aws sso login --profile <profile>`.

`make help ENV=<env>` shows which settings make will use and where each came from. The account
id is shown as `<set>`.

## Prepare the account

Once per account and region:

```
make bootstrap ENV=dev
```

Run it again after an update of this repository that changes
`infra/bootstrap/execution-policies.yaml` (a deployment of a new resource type fails without
it; see "Troubleshoot").

It creates this project's CDK bootstrap: the stacks `team-assistant-cdk-policies` and
`TeamAssistantToolkit` (docs/adr/0001). It does not touch a default CDK bootstrap if the
account has one, and it creates no customer KMS key. Check:

```
make status ENV=dev
```

Expected: `TeamAssistantToolkit CREATE_COMPLETE` and `team-assistant-cdk-policies
CREATE_COMPLETE`.

## GitHub OAuth App (once per environment)

The agent's GitHub tool acts with each user's own GitHub token. GitHub needs an OAuth App:

1. In GitHub: https://github.com/settings/applications/new ("Register a new OAuth app").
   Application name `team-assistant-<env>`, Homepage URL `http://localhost`. Leave "Redirect
   URIs" with a placeholder (the real one is set after the first deploy) and "Allow wildcard
   matching" off. "Expire user access tokens" on.
2. Copy the Client ID into `local.mk`: `GITHUB_CLIENT_ID_<env> = <client id>`.
3. Generate a client secret and keep it for `make github` below; GitHub shows it once.

## Deploy

```
make arm64             # once per boot on an x86-64 host: the agent image is linux/arm64
make diff ENV=dev      # read-only: what would change
make deploy ENV=dev
make status ENV=dev
```

`make deploy` builds the agent image (docker/agent.Dockerfile) and pushes it to the bootstrap's
image repository. When the deploy changes IAM, CDK shows the changes and asks before it goes on.

`make deploy` creates the stack `team-assistant-<env>-boundary` (the permissions boundary of
the environment's roles) and then `TeamAssistant-<env>`, and registers the GitHub return URL on
the agent's workload identity. Every command first checks that the profile points at the
account set as `ACCOUNT_<env>` and stops otherwise; `deploy` and `diff` also stop without
`GITHUB_CLIENT_ID_<env>`.

Then store the GitHub client secret and point the OAuth App at this deployment:

```
make github ENV=dev    # hidden prompt for the client secret; prints the redirect URI
```

In the OAuth App's settings, set the Redirect URI to the printed address (replace the earlier
one: every deployment's provider has a new address).

## Users and a first conversation

```
make user ENV=dev USER=alice     # asks twice for a password; no message is sent to anyone
make login ENV=dev USER=alice    # opens Cognito's managed login page; saves the tokens
make chat ENV=dev USER=alice     # prints the session id, then a prompt
```

Expected in `make chat`: tell the agent something, ask about it in the next message, and the
answer uses it. `make chat ... SESSION=<id>` continues an earlier conversation.
`make ask ENV=dev USER=alice PROMPT='...' [SESSION=<id>]` sends one question and prints the
answer, without the interactive prompt.

A second user: `make user ENV=dev USER=bob`, then `make login ENV=dev USER=bob PRINT=1` and
open the printed address in a private window (the browser still holds the first user's login).
Close every private window first: all private windows of one browser share their cookies until
the last one is closed, so an open one may still hold the first user's login.

Check the environment and look into Memory:

```
make smoke ENV=dev                               # sixteen PASS lines; alice and bob logged in, alice consented to GitHub
make memory ENV=dev USER=alice SESSION=<id>      # messages and long-term records of a user
```

Long-term records appear some time after a conversation (the strategies run in the
background). `make smoke` therefore takes a few minutes: each of its long-term memory checks
waits up to five minutes for a record.

`make smoke` makes the strategies store real records (one check asks the agent to sign its
answers with a word). Its words start with `smk`, and it deletes the records holding them at
its start and end (`purge: N record(s) ... deleted`); records extracted after one run are
deleted at the start of the next.

## GitHub consent (once per user and deployment)

```
make chat ENV=dev USER=alice
alice> List open issues in aws/bedrock-agentcore-sdk-python
```

The first time, the client prints a consent address. Open it in a browser logged in to GitHub
and authorize the app; the browser shows "Consent received", the terminal
`[consent recorded; waiting for the answer]`, then the issues. `make smoke` expects alice to
have consented and bob not to.

## The web page

`make deploy` ends with the page's address (`https://<id>.cloudfront.net`, stack output
`WebUrl`). The operator creates users with `make user`; there is no registration. The first
question about GitHub shows a consent card in the chat; the consent opens GitHub in a new tab.

```
make web MOCK=1          # the page on http://localhost:5173 with the mock agent, no AWS
make web ENV=dev         # the page on http://localhost:5173 against the deployed agent
make web-sync ENV=dev    # build and upload the page only, no stack update
```

`make web` writes `web/public/config.json` (gitignored) from the stack outputs.

## Deploy again after a teardown

Before the first deploy after an update of this repository that changed
`infra/bootstrap/execution-policies.yaml`, run `make bootstrap ENV=dev`.

`make deploy ENV=dev` creates a new user pool, Memory, guardrail, Runtime, gateway and GitHub
provider. Users,
conversations and GitHub consents of the earlier deployment are gone, and saved logins no
longer fit: `make github`, set the new Redirect URI in the OAuth App, create the users again
(`make user`), log in again (`make login`) and let alice consent again. `make chat` says so
when it meets an old login.

## Teardown

```
make destroy ENV=dev
make status ENV=dev
```

`make destroy` asks you to type the stack name, then deletes both secrets of the environment
at once (no recovery window), empties the page's bucket, deletes `TeamAssistant-<env>`, every
agent image in the bootstrap's image repository, the Runtime's log groups and
`team-assistant-<env>-boundary`. Expected afterwards: both stacks "does not exist". Users and
conversations are deleted with the stack. The agent images are kept while another environment
is still deployed, because it may use them; the last `make destroy` removes them all.

`make status` ends with the list of what outlasts an environment: what stays on purpose and
what cannot be deleted but expires (CloudWatch metrics after 15 months, CloudTrail event
history after 90 days).

What stays:

- **The CDK bootstrap** (`TeamAssistantToolkit`, `team-assistant-cdk-policies`). No fixed fee.
  Its bucket keeps the templates of earlier deploys (kilobytes).
- **The token vault** (one per account and region, created by AgentCore) and **the GitHub OAuth
  App** (created by hand in GitHub).

To remove the bootstrap as well, once no environment of the account and region is left:

```
make unbootstrap ENV=dev     # ENV only picks the account to check (ACCOUNT_dev)
```

It refuses while any `TeamAssistant-*` stack exists, asks you to type `TeamAssistantToolkit`,
deletes any agent image left in its image repository, deletes `TeamAssistantToolkit`, then the
assets bucket that stack keeps (the bootstrap template sets `DeletionPolicy: Retain` and
versioning, so every version is deleted first), then `team-assistant-cdk-policies`. Other CDK
bootstraps of the account (`CDKToolkit`, other projects' qualifiers) are not touched.
`make bootstrap` sets it up again.

## Troubleshoot

- **`could not read the identity of profile ...`**: the login expired. Run
  `aws sso login --profile <profile>`.
- **`profile ... is not in the account set as ACCOUNT_<env>`**: `PROFILE` and `ACCOUNT_<env>`
  in `local.mk` do not belong together. Nothing was changed.
- **`... is not logged in` or `the saved login ... is for an earlier deployment`**: run the
  `make login` command the message names. A login lasts one day.
- **A smoke check that asks for a word FAILs with an answer naming another word**: a long-term
  record from an earlier run was retrieved and the model followed it instead of the session.
  Run `make smoke` again (it deletes its own marked records first). Records without the `smk`
  marker must be deleted by hand (`aws bedrock-agentcore delete-memory-record`).
- **The agent answers without what it should remember**: look in the Runtime's log for a
  line with `"event": "preferences_failed"` or `"event": "tool_failed"` (each names the error
  type), or `"event": "records_not_found"` (Memory found no preference records); then
  `make memory ... USER=<name> SESSION=<id>` shows whether the records exist. A fact or an
  earlier conversation reaches the answer only when the model calls `search_memory` or
  `recall_conversations`.
- **`GITHUB_CLIENT_ID_<env> is not set`**: add the OAuth App's client id to `local.mk`.
- **`WARNING ExplicitAuthFlows.1: 'ALLOW_TOKEN_EXCHANGE_IMPERSONATION' is not one of [...]`**
  at synth or deploy: CDK's validation predates Cognito's token exchange; the service accepts the
  value. Expected; the deploy goes on.
- **The page's list says "Conversations unavailable"**: the agent could not read Memory; the
  text names the service's error code. A user without any conversation yet gets an empty list
  (Memory answers `ResourceNotFoundException`, which the agent reads as "nothing yet").
- **GitHub skips its approval screen**: the GitHub account approved the OAuth App before;
  revoke it in GitHub (Settings, Applications, Authorized OAuth Apps) to see the screen again.
- **`make login` for a second user signs the first one in again**: Cognito remembers the first
  sign-in in a cookie; use `PRINT=1` and a private window.
- **`LOGIN_SUFFIX_<env> is not set`**: add `LOGIN_SUFFIX_<env> = <output of openssl rand -hex 4>`
  to `local.mk`. If a deploy fails because the login domain is taken (by another AWS account),
  make a new suffix.
- **GitHub shows "The redirect_uri is not associated with this application"**: the OAuth App's
  Redirect URI is from an earlier deployment; set the one `make github` prints.
- **`error: the return carried no session_id` or `a different state`** in `make chat`: the
  browser came back from another consent attempt; ask again and use the new address.
- **`consent failed: ...`**: the agent could not complete the binding; the text names the
  service's error code. The Runtime's log has a line with `"outcome": "consent_failed"`.
- **The `github tool` smoke check says alice has not consented**: do the GitHub consent step.
- **The agent says a GitHub result was withheld or could not be checked**: the guardrail
  flagged the result, or the call to it failed. The Runtime's log has a line
  `"event": "guardrail"` with the tool and the outcome: `intervened`, or the error type (for
  example `AccessDeniedException`: run `make bootstrap`, then `make deploy`, again).
- **`the browser logged in as another user`**: the browser kept another user's session. Use
  `make login ... PRINT=1` and a new private window, after closing all others (or another browser).
- **`not authorised (HTTP 401)` in `make chat`**: the token is not accepted by the gateway or
  the Runtime. Log in again; if it persists, the stack and the user pool do not belong together
  (`make deploy` again). The text `Transaction token required` means the request reached the
  Runtime without the gateway in its identity chain: the gateway target is not exchanging the
  token (docs/adr/0004).
- **`make login` shows "Login pages unavailable"**: the app client has no managed login style;
  the stack creates one (`LoginStyle`), so `make deploy` again.
- **The image build fails with `exec format error` or hangs at the first `RUN`**: arm64
  emulation is not registered on this boot. Run `make arm64`.
- **`make destroy` ends with `DELETE_FAILED`**: fix the cause the event names (for a missing
  permission: the next entry), then run `make destroy ENV=<env>` again. CloudFormation
  continues with what is left, and the script then removes the image, the logs and the boundary.
- **`AccessDenied` in a stack event of `make deploy`**: the execution policy does not cover a
  new resource type. Add a scoped statement to `infra/bootstrap/execution-policies.yaml` and
  run `make bootstrap ENV=<env>` again (docs/adr/0001).
