"""The user's conversations and the history of one, for the web page.

Always for the actor the caller passes, which app.py takes from the verified token, never from
the request. A conversation is a Memory session of that actor: ``ListSessions`` lists them,
and its title is an event of its own (titles.py) found by its metadata. A history is the
session's events as the Strands session manager stored them (``bedrock_agentcore`` 1.26.0,
``bedrock_converter.py``): each message is the JSON of a ``SessionMessage``, in a
``conversational`` payload, or in a ``blob`` holding ``[json, role]`` when it has 100,000
characters or more. Only user and assistant text goes to the page: tool calls and tool
results have no text block and are left out.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC

TITLE_KIND = "title"
# Metadata of the title event; a metadata value takes only [a-zA-Z0-9\s._:/=+@-], so the title
# itself is in the event's payload (botocore service model, MetadataValueStringValueString).
TITLE_FILTER = {
    "eventMetadata": [
        {
            "left": {"metadataKey": "kind"},
            "operator": "EQUALS_TO",
            "right": {"metadataValue": {"stringValue": TITLE_KIND}},
        }
    ]
}
# At most this many pages of 100 (ListSessions and ListEvents allow 100 per page).
MAX_PAGES = 10
# Title reads run in parallel for the list; a boto3 client is safe to share between threads.
TITLE_READERS = 8
ROLES = {"user", "assistant"}


def _missing(error: Exception) -> bool:
    """Memory answers ResourceNotFoundException for an actor or a session it has no events
    for yet (a new user's first visit), not an empty list."""
    code = getattr(error, "response", {}).get("Error", {}).get("Code")
    return code == "ResourceNotFoundException"


def _pages(call, **kwargs) -> list[dict]:
    pages, token = [], None
    for _ in range(MAX_PAGES):
        page = call(maxResults=100, **kwargs, **({"nextToken": token} if token else {}))
        pages.append(page)
        token = page.get("nextToken")
        if not token:
            break
    return pages


def _iso(moment) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def title_of(client, memory_id: str, actor: str, session_id: str) -> str | None:
    """The stored title of a session, or None. The metadata filter applies page by page, so a
    page can come back empty while a later one holds the title: every page is read, as the
    SDK's own list_events does (bedrock_agentcore 1.26.0, memory/client.py). Should a session
    hold more than one title event, the oldest wins, so a title once shown does not change."""
    try:
        pages = _pages(
            client.list_events,
            memoryId=memory_id,
            actorId=actor,
            sessionId=session_id,
            includePayloads=True,
            filter=TITLE_FILTER,
        )
    except Exception as error:
        if _missing(error):
            return None
        raise
    events = [e for page in pages for e in page.get("events", [])]
    for event in sorted(events, key=lambda e: str(e.get("eventTimestamp", ""))):
        for item in event.get("payload", []):
            try:
                value = json.loads(item.get("blob", ""))
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and isinstance(value.get("title"), str):
                return value["title"]
    return None


def list_conversations(client, memory_id: str, actor: str, limit: int = 30) -> list[dict]:
    """The actor's latest ``limit`` conversations, newest first:
    ``session_id``, ``created_at`` (ISO 8601, UTC) and ``title`` (None until one is stored)."""
    try:
        pages = _pages(client.list_sessions, memoryId=memory_id, actorId=actor)
    except Exception as error:
        if _missing(error):
            return []
        raise
    sessions = [s for page in pages for s in page.get("sessionSummaries", [])]
    newest = sorted(sessions, key=lambda s: s["createdAt"], reverse=True)[:limit]
    with ThreadPoolExecutor(max_workers=TITLE_READERS) as pool:
        titles = list(
            pool.map(lambda s: title_of(client, memory_id, actor, s["sessionId"]), newest)
        )
    return [
        {"session_id": s["sessionId"], "created_at": _iso(s["createdAt"]), "title": title}
        for s, title in zip(newest, titles, strict=True)
    ]


def _turn(stored: str) -> dict | None:
    """A user or assistant turn with text, from one stored message; None otherwise."""
    try:
        message = json.loads(stored)["message"]
        role = message["role"]
        text = " ".join(
            b["text"] for b in message["content"] if isinstance(b, dict) and "text" in b
        )
    except (TypeError, ValueError, KeyError):
        return None
    return {"role": role, "text": text} if role in ROLES and text.strip() else None


def _stored_messages(event: dict) -> list[str]:
    found = []
    for item in event.get("payload", []):
        if "conversational" in item:
            found.append(item["conversational"].get("content", {}).get("text", ""))
        elif "blob" in item:
            try:
                value = json.loads(item["blob"])
            except (TypeError, ValueError):
                continue
            # A large message; a title event holds a dict instead and is skipped.
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                found.append(value[0])
    return found


def history(client, memory_id: str, actor: str, session_id: str) -> list[dict]:
    """The turns of one conversation, oldest first: ``role`` and ``text``."""
    try:
        pages = _pages(
            client.list_events,
            memoryId=memory_id,
            actorId=actor,
            sessionId=session_id,
            includePayloads=True,
        )
    except Exception as error:
        if _missing(error):
            return []
        raise
    events = sorted(
        (e for page in pages for e in page.get("events", [])), key=lambda e: e["eventTimestamp"]
    )
    turns = []
    for event in events:
        for stored in _stored_messages(event):
            turn = _turn(stored)
            if turn:
                turns.append(turn)
    return turns
