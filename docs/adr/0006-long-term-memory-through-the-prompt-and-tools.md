# 0006. Long-term memory: preferences in the system prompt, the rest through tools

Status: accepted · 2026-11-18

## Context

The model needs the user's long-term records in later conversations.

## Decision

- **Preferences go into the system prompt, without a search.** For every request the agent
  lists the user's preference records (`ListMemoryRecords`) and appends the newest
  `max_preferences` (`config/agent.toml`) to the system prompt (`prompts.py`).
- **Facts: the tool `search_memory(query)`.** The model writes the query from the whole
  conversation: for "And who owns it?" it searches "Who owns the search service?". The tool
  returns the facts closest to the query, at most `facts_top_k` (`config/agent.toml`).
- **A result that reaches its limit says so** and tells the model to search again for each
  part of the question.
- **Summaries: the tool `recall_conversations(query)`**, for questions about earlier
  conversations. It returns the summaries closest to the query, at most `summaries_top_k`
  (`config/agent.toml`), each with the date of its conversation, after today's date; the
  model picks by date for a question about a day. One conversation can have several
  summaries.
- **At most six turns per message** (`MAX_TURNS` in `config.py`, passed to Strands as
  `limits`). A turn is one model call and the tool calls it asks for; the system prompt
  tells the model the limit.
- **A fact search never returns summaries.** A summary repeats the assistant's answers;
  returned as a fact, an earlier guess of the assistant reads as something the user stated.
  `recall_conversations` tells the model that a summary repeats the assistant's past
  suggestions.
- **The tools take no user parameter.** The user id comes from the verified token, so the
  model cannot ask for another user's records.

## Consequences

- A message that needs a fact takes at least two model calls: one asks for the search, one
  answers with its result.
- A fact is found only if the model calls the tool; the system prompt says when.
- A message whose sixth turn still asks for a tool gets an error event instead of an answer.
- The agent's role needs `ListMemoryRecords` (docs/security.md).
- A summary can hold text from outside, such as a GitHub issue the assistant summarised, and
  `recall_conversations` can return it in a later conversation. Both tools therefore mark
  the records they return as data, not instructions (docs/security.md, residual risks).
- The summary search ranks by content, not by date: a summary of the asked day is returned
  only if it is among the `summaries_top_k` closest.
- Memory writes a summary some time after a conversation, so the newest part of a
  conversation can be missing from `recall_conversations`.
