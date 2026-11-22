# 0007. A Bedrock guardrail checks what the GitHub tools return

Status: accepted · 2026-11-22

## Context

`list_issues` and `get_issue` return text that anyone can write: issue titles, labels and
bodies. Such text can hold instructions to the assistant. A guardrail attached to the model
call does not cover it: the prompt attack filter does not evaluate tool results (Bedrock
user guide, "Detect prompt attacks with Amazon Bedrock Guardrails").

## Decision

- **The agent sends the result of a GitHub tool to the environment's guardrail before the
  model reads it** (`guard.py`): `ApplyGuardrail` with source `INPUT`, from a Strands
  `AfterToolCallEvent` hook.
- **The guardrail has one filter**, `PROMPT_ATTACK`, at strength `LOW`, and no published
  version: the agent names the working draft.
- **A result the guardrail flags, or one that cannot be checked, is withheld**: the model
  gets a fixed refusal instead. A call that was cancelled before the tool ran is not checked.
- **Only the GitHub tools are checked.** The memory tools return records of the user's own
  conversations.

## Consequences

- One `ApplyGuardrail` call for every result of a GitHub tool.
- The agent's role needs `bedrock:ApplyGuardrail` on this guardrail, and the deploy role the
  guardrail's actions (docs/security.md, docs/adr/0001).
- When the guardrail cannot be reached, the GitHub tools give no result.
- A flagged result is lost to the user even when it holds no attack; the log line
  `"event": "guardrail"` names the tool and the outcome, never the text.
- The filter lowers the risk and does not remove it (docs/security.md, residual risks).
