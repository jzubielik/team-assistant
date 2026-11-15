"""The title of a conversation, for the web page's list.

After the first answer of a session the agent asks the configured model for a title of a few
words (one Converse call; the agent's role may already invoke that model) and stores it in the
session as an event of its own: a ``blob`` payload ``{"title": ...}`` with metadata
``kind = title`` (conversations.TITLE_KIND). The Strands session manager reads only
conversational payloads and two-element blob lists back as messages
(``bedrock_agentcore`` 1.26.0, ``bedrock_converter.py``), so the title never enters the
conversation. The question and answer go to the model only; nothing of them is logged.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from team_assistant.conversations import TITLE_KIND, title_of

MAX_WORDS = 6
MAX_CHARS = 60
# What of the exchange the model sees: enough for a topic, little to pay for.
QUESTION_CHARS, ANSWER_CHARS = 500, 700
INSTRUCTION = (
    "Give this conversation a title of at most six words, in the language of the question. "
    "Reply with the title only, without quotes."
)


def _clean(reply: str) -> str:
    text = " ".join(reply.split())
    text = re.sub(r"^(title|tytuł)\s*:\s*", "", text, flags=re.IGNORECASE)
    # Quotes and end punctuation in any order ('Title'. or "Title!"): strip until nothing changes.
    while True:
        stripped = text.strip(" \"'“”‘’`*").rstrip(" .!?:;,")
        if stripped == text:
            break
        text = stripped
    text = " ".join(text.split()[:MAX_WORDS])
    return text[:MAX_CHARS].rstrip()


def make_title(converse, model_id: str, prompt: str, answer: str) -> str:
    """A short title for the exchange; "" when the model gives nothing usable."""
    exchange = (
        f"{INSTRUCTION}\n\nQuestion: {prompt[:QUESTION_CHARS]}\n\nAnswer: {answer[:ANSWER_CHARS]}"
    )
    response = converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": exchange}]}],
        inferenceConfig={"maxTokens": 24, "temperature": 0},
    )
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    reply = " ".join(b["text"] for b in blocks if isinstance(b, dict) and "text" in b)
    return _clean(reply)


def store_title(client, memory_id: str, actor: str, session_id: str, title: str) -> None:
    """The title as an event of the session, found later by its metadata."""
    client.create_event(
        memoryId=memory_id,
        actorId=actor,
        sessionId=session_id,
        eventTimestamp=datetime.now(UTC),
        payload=[{"blob": json.dumps({"title": title})}],
        metadata={"kind": {"stringValue": TITLE_KIND}},
    )


def title_once(
    memory_client,
    converse,
    model_id: str,
    memory_id: str,
    actor: str,
    session_id: str,
    prompt: str,
    answer: str,
) -> None:
    """Title the session unless it has a title already."""
    if title_of(memory_client, memory_id, actor, session_id) is not None:
        return
    title = make_title(converse, model_id, prompt, answer)
    if title:
        store_title(memory_client, memory_id, actor, session_id, title)
