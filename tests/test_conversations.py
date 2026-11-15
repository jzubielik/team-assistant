"""The user's conversations and their history, as the page asks for them."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from team_assistant import conversations

ALICE = "sub-alice"


def message(role: str, *blocks: dict) -> str:
    """A message as the Strands session manager stores it: the JSON of a SessionMessage."""
    return json.dumps({"message": {"role": role, "content": list(blocks)}, "message_id": 0})


def conversational(role: str, *blocks: dict, at: int) -> dict:
    text = message(role, *blocks)
    return {
        "eventTimestamp": datetime(2026, 11, 15, 12, 0, at, tzinfo=UTC),
        "payload": [{"conversational": {"content": {"text": text}, "role": role.upper()}}],
    }


class FakeMemory:
    def __init__(self, sessions=None, events=None, titles=None):
        self.sessions = sessions or []
        self.events = events or {}
        self.titles = titles or {}
        self.calls = []

    def list_sessions(self, **kwargs):
        self.calls.append(("list_sessions", kwargs))
        return {"sessionSummaries": self.sessions}

    def list_events(self, **kwargs):
        self.calls.append(("list_events", kwargs))
        if "filter" in kwargs:
            title = self.titles.get(kwargs["sessionId"])
            blob = json.dumps({"title": title})
            return {"events": [{"payload": [{"blob": blob}]}] if title else []}
        return {"events": self.events.get(kwargs["sessionId"], [])}


def summary(session_id: str, day: int) -> dict:
    return {
        "sessionId": session_id,
        "actorId": ALICE,
        "createdAt": datetime(2026, 11, day, 9, 0, tzinfo=UTC),
    }


def test_conversations_newest_first_with_titles_for_the_actor_only():
    memory = FakeMemory(
        sessions=[summary("session-a", 12), summary("session-b", 15), summary("session-c", 14)],
        titles={"session-b": "Release checklist"},
    )
    items = conversations.list_conversations(memory, "mem-1", ALICE)
    assert items == [
        {
            "session_id": "session-b",
            "created_at": "2026-11-15T09:00:00Z",
            "title": "Release checklist",
        },
        {"session_id": "session-c", "created_at": "2026-11-14T09:00:00Z", "title": None},
        {"session_id": "session-a", "created_at": "2026-11-12T09:00:00Z", "title": None},
    ]
    assert all(call[1]["actorId"] == ALICE for call in memory.calls)
    (listing,) = [c for name, c in memory.calls if name == "list_sessions"]
    assert listing["memoryId"] == "mem-1"
    title_calls = [c for name, c in memory.calls if name == "list_events"]
    assert all(
        c["filter"] == conversations.TITLE_FILTER and c["maxResults"] == 100 for c in title_calls
    )


def test_conversations_are_capped_at_the_limit():
    memory = FakeMemory(sessions=[summary(f"session-{d}", d) for d in range(1, 29)])
    items = conversations.list_conversations(memory, "mem-1", ALICE, limit=3)
    assert [i["session_id"] for i in items] == ["session-28", "session-27", "session-26"]


def test_no_conversations():
    assert conversations.list_conversations(FakeMemory(), "mem-1", ALICE) == []


def test_history_keeps_user_and_assistant_text_in_order_and_drops_tool_calls():
    # The GitHub question: the model calls the tool, gets its result, then answers.
    events = [
        conversational("user", {"text": "Open issues of aws/aws-cdk?"}, at=1),
        conversational(
            "assistant", {"toolUse": {"toolUseId": "t1", "name": "list_issues", "input": {}}}, at=2
        ),
        conversational(
            "user", {"toolResult": {"toolUseId": "t1", "content": [{"text": "#1 x"}]}}, at=3
        ),
        conversational("assistant", {"text": "There are"}, {"text": "two issues."}, at=4),
    ]
    events.reverse()  # ListEvents order is not relied on
    memory = FakeMemory(events={"session-x": events})
    assert conversations.history(memory, "mem-1", ALICE, "session-x") == [
        {"role": "user", "text": "Open issues of aws/aws-cdk?"},
        {"role": "assistant", "text": "There are two issues."},
    ]
    (call,) = [c for name, c in memory.calls if name == "list_events"]
    assert call["actorId"] == ALICE and call["sessionId"] == "session-x"
    assert call["includePayloads"] is True


def test_history_reads_a_large_message_stored_as_a_blob_and_skips_the_title():
    big = message("assistant", {"text": "long answer"})
    events = [
        conversational("user", {"text": "q"}, at=1),
        {
            "eventTimestamp": datetime(2026, 11, 15, 12, 0, 2, tzinfo=UTC),
            "payload": [{"blob": json.dumps([big, "ASSISTANT"])}],
        },
        {
            "eventTimestamp": datetime(2026, 11, 15, 12, 0, 3, tzinfo=UTC),
            "payload": [{"blob": json.dumps({"title": "A title"})}],
        },
    ]
    memory = FakeMemory(events={"session-x": events})
    assert conversations.history(memory, "mem-1", ALICE, "session-x") == [
        {"role": "user", "text": "q"},
        {"role": "assistant", "text": "long answer"},
    ]


def test_history_skips_what_it_cannot_read():
    broken = {
        "eventTimestamp": datetime(2026, 11, 15, 12, 0, 1, tzinfo=UTC),
        "payload": [{"conversational": {"content": {"text": "not json"}, "role": "USER"}}],
    }
    memory = FakeMemory(events={"session-x": [broken]})
    assert conversations.history(memory, "mem-1", ALICE, "session-x") == []


class MissingActor:
    """Memory's answer for a user with no events yet: ResourceNotFoundException."""

    def __init__(self, code="ResourceNotFoundException"):
        self.code = code

    def _fail(self, **kwargs):
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": self.code, "Message": "x"}}, "ListSessions")

    list_sessions = _fail
    list_events = _fail


def test_a_new_user_without_events_has_no_conversations_and_no_history():
    assert conversations.list_conversations(MissingActor(), "mem-1", ALICE) == []
    assert conversations.history(MissingActor(), "mem-1", ALICE, "session-x") == []
    assert conversations.title_of(MissingActor(), "mem-1", ALICE, "session-x") is None


def test_other_memory_errors_still_fail():
    import pytest
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        conversations.list_conversations(MissingActor("AccessDeniedException"), "mem-1", ALICE)


def test_the_oldest_title_wins_when_a_session_has_two():
    class TwoTitles:
        def list_events(self, **kwargs):
            def event(second, title):
                return {
                    "eventTimestamp": datetime(2026, 11, 15, 12, 0, second, tzinfo=UTC),
                    "payload": [{"blob": json.dumps({"title": title})}],
                }

            return {"events": [event(9, "Newer"), event(1, "First")]}

    assert conversations.title_of(TwoTitles(), "mem-1", ALICE, "s") == "First"


def test_a_title_on_a_later_page_is_found():
    """The filter applies per page: an empty first page with a token is not the answer."""

    class Paged:
        def list_events(self, **kwargs):
            if "nextToken" not in kwargs:
                return {"events": [], "nextToken": "t2"}
            blob = json.dumps({"title": "Found later"})
            return {"events": [{"payload": [{"blob": blob}]}]}

    assert conversations.title_of(Paged(), "mem-1", ALICE, "s") == "Found later"
