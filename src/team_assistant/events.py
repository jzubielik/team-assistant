"""The events the agent streams to its clients, in one place.

Each is a JSON object with a ``type``. ``EVENT_FIELDS`` lists the exact fields of each type; the
agent builds its events with the functions below, its tests check every event against
``EVENT_FIELDS``, and so do the tests of the page's mock agent (web/mock/scenarios), so the
mock cannot drift from the agent. The page reads them in web/src/events.ts.
"""

from __future__ import annotations

EVENT_FIELDS: dict[str, frozenset[str]] = {
    "text": frozenset({"type", "data"}),
    "auth_url": frozenset({"type", "url", "state"}),
    "done": frozenset({"type", "usage"}),
    "error": frozenset({"type", "message"}),
    "consent_done": frozenset({"type"}),
    "conversations": frozenset({"type", "items"}),
    "history": frozenset({"type", "turns"}),
}


def text(data: str) -> dict:
    """A piece of the answer."""
    return {"type": "text", "data": data}


def auth_url(url: str, state: str) -> dict:
    """The user must consent in GitHub first; ``state`` comes back with the browser's return."""
    return {"type": "auth_url", "url": url, "state": state}


def done(input_tokens: int, output_tokens: int) -> dict:
    """The answer is complete; the tokens it used."""
    return {"type": "done", "usage": {"input": input_tokens, "output": output_tokens}}


def error(message: str) -> dict:
    """The request failed; ``message`` never quotes the prompt or the answer."""
    return {"type": "error", "message": message}


def consent_done() -> dict:
    """A forwarded consent was bound to the user."""
    return {"type": "consent_done"}


def conversations(items: list[dict]) -> dict:
    """The user's conversations: ``session_id``, ``created_at``, ``title`` each."""
    return {"type": "conversations", "items": items}


def history(turns: list[dict]) -> dict:
    """The turns of one conversation: ``role`` (user or assistant) and ``text`` each."""
    return {"type": "history", "turns": turns}


def matches(event: object) -> bool:
    """Whether ``event`` has a known type and exactly the fields of that type."""
    if not isinstance(event, dict):
        return False
    fields = EVENT_FIELDS.get(event.get("type"))  # type: ignore[arg-type]
    return fields is not None and set(event) == fields
