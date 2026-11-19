"""Long-term memory for the model: listed preferences and the two memory tools."""

from __future__ import annotations

import datetime
import json
import logging

import pytest
from botocore.exceptions import ClientError

from team_assistant import recall

T0 = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.UTC)
SECRET = "the payroll password"


def at(days):
    return T0 - datetime.timedelta(days=days)


def record(text, days=0):
    return {"content": {"text": text}, "createdAt": at(days)}


def missing():
    return ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "ListMemoryRecords")


class Memory:
    """A fake Memory client: records by namespace, sessions of one actor, search results."""

    def __init__(self, records=None, sessions=(), found=None, fail=None):
        self.records, self.sessions = records or {}, list(sessions)
        self.found, self.fail, self.calls = found or {}, fail, []

    def _call(self, name, kwargs):
        self.calls.append((name, kwargs))
        if self.fail:
            raise self.fail

    def list_memory_records(self, **kwargs):
        self._call("list_memory_records", kwargs)
        return {"memoryRecordSummaries": self.records.get(kwargs["namespace"], [])}

    def retrieve_memory_records(self, **kwargs):
        self._call("retrieve_memory_records", kwargs)
        return {"memoryRecordSummaries": self.found.get(kwargs["namespacePath"], [])}

    def list_sessions(self, **kwargs):
        self._call("list_sessions", kwargs)
        return {"sessionSummaries": self.sessions}

    def list_events(self, **kwargs):  # the title of a session (conversations.title_of)
        self._call("list_events", kwargs)
        return {"events": []}


def test_preferences_are_the_newest_ones_oldest_first_and_only_the_preference_text():
    stored = json.dumps({"context": "said in a chat", "preference": "Use Python for code."})
    memory = Memory(
        {
            "/preferences/u-1/": [
                record("Prefers short answers.", days=3),
                record(stored, days=1),
                record("An old one.", days=9),
            ]
        }
    )
    found = recall.list_preferences(memory, "mem-1", "u-1", limit=2)
    assert found == ["Prefers short answers.", "Use Python for code."]
    (call,) = memory.calls
    assert call[1]["memoryId"] == "mem-1" and call[1]["namespace"] == "/preferences/u-1/"


def test_preferences_are_cut_deduplicated_and_empty_ones_dropped():
    long = "x" * 2000
    memory = Memory(
        {"/preferences/u-1/": [record(long), record("Same."), record("Same."), record(" ")]}
    )
    found = recall.list_preferences(memory, "mem-1", "u-1", limit=10)
    assert sorted(found) == sorted(["x" * recall.MAX_RECORD_CHARS, "Same."])


def test_no_preferences_for_a_new_user_or_a_limit_of_zero():
    assert recall.list_preferences(Memory(fail=missing()), "mem-1", "u-1", 5) == []
    memory = Memory({"/preferences/u-1/": [record("One.")]})
    assert recall.list_preferences(memory, "mem-1", "u-1", 0) == []
    assert memory.calls == []


def tools(memory, actor="u-1", session="s-now"):
    search, conversations = recall.memory_tools(memory, "mem-1", actor, session)
    return search, conversations


def test_the_model_can_name_a_query_days_and_a_topic_but_never_a_user_or_a_memory():
    search, conversations = tools(Memory())
    assert set(search.tool_spec["inputSchema"]["json"]["properties"]) == {"query"}
    properties = conversations.tool_spec["inputSchema"]["json"]["properties"]
    assert set(properties) == {"days", "topic"}


def test_search_reads_the_users_facts_and_never_a_summary():
    memory = Memory(
        found={
            "/facts/u-1/": [record("The billing service is written in Go.")],
            "/summaries/u-1/": [record("Talked about the Tuesday deployment.")],
        }
    )
    search, _ = tools(memory)
    answer = search(query="  Which language is the billing service written in? ")
    assert answer.startswith(recall.DATA_NOTE) and "not instructions" in recall.DATA_NOTE
    assert "Facts:\n- The billing service is written in Go." in answer
    # A summary repeats the assistant's answers; it is no source of facts.
    assert "Tuesday deployment" not in answer
    paths = [call[1]["namespacePath"] for call in memory.calls]
    assert paths == ["/facts/u-1/"]
    criteria = memory.calls[0][1]["searchCriteria"]
    assert criteria == {
        "searchQuery": "Which language is the billing service written in?",
        "topK": recall.SEARCH_TOP_K,
    }


def test_search_says_so_when_nothing_is_stored_and_refuses_an_empty_query():
    memory = Memory()
    search, _ = tools(memory)
    assert "Nothing about this is stored" in search(query="Who owns billing?")
    assert search(query="   ").startswith("Refused")
    assert len(memory.calls) == 1


def test_a_failed_search_is_a_short_text_and_the_log_names_only_the_error_type(caplog):
    caplog.set_level(logging.WARNING, logger="team_assistant")
    search, conversations = tools(Memory(fail=RuntimeError(SECRET)))
    assert search(query="Who owns billing?") == "The memory could not be read (RuntimeError)."
    assert conversations(days=2) == "The memory could not be read (RuntimeError)."
    lines = [json.loads(r.getMessage()) for r in caplog.records]
    assert lines == [
        {"event": "tool_failed", "tool": "search_memory", "error_type": "RuntimeError"},
        {"event": "tool_failed", "tool": "recall_conversations", "error_type": "RuntimeError"},
    ]
    assert SECRET not in caplog.text


def now():
    return datetime.datetime.now(datetime.UTC)


def test_recall_lists_the_users_other_sessions_of_the_last_days_with_their_summaries():
    sessions = [
        {"sessionId": "s-now", "createdAt": now()},
        {"sessionId": "s-yesterday", "createdAt": now() - datetime.timedelta(days=1)},
        {"sessionId": "s-old", "createdAt": now() - datetime.timedelta(days=5)},
    ]
    memory = Memory(
        {"/summaries/u-1/s-yesterday/": [record("Planned the billing release.")]},
        sessions=sessions,
    )
    _, conversations = tools(memory)
    answer = conversations(days=2)
    assert answer.startswith(
        f"Today is {now():%Y-%m-%d} (UTC). {recall.DATA_NOTE} {recall.SUMMARY_NOTE}"
    )
    (line,) = [line for line in answer.splitlines() if line.startswith("- ")]
    assert "untitled: Planned the billing release." in line
    assert f"{now() - datetime.timedelta(days=1):%Y-%m-%d}" in line
    listed = [call for call in memory.calls if call[0] == "list_sessions"]
    assert listed[0][1]["actorId"] == "u-1"
    # The running session and the one outside the days are not read.
    read = [c[1]["namespace"] for c in memory.calls if c[0] == "list_memory_records"]
    assert read == ["/summaries/u-1/s-yesterday/"]


def test_recall_by_topic_searches_every_sessions_summaries_and_labels_them():
    memory = Memory(found={"/summaries/u-1/": [record("The assistant suggested a rollback.")]})
    _, conversations = tools(memory)
    answer = conversations(topic=" the billing incident ")
    assert answer.startswith(f"{recall.DATA_NOTE} {recall.SUMMARY_NOTE}")
    assert "not as something the user stated" in recall.SUMMARY_NOTE
    assert "- The assistant suggested a rollback." in answer
    (call,) = memory.calls
    assert call[0] == "retrieve_memory_records"
    assert call[1]["namespacePath"] == "/summaries/u-1/"
    assert call[1]["searchCriteria"]["searchQuery"] == "the billing incident"
    _, conversations = tools(Memory())
    assert "No summary of an earlier conversation" in conversations(topic="billing")
    assert conversations(topic=5).startswith("Refused")


def test_recall_says_when_a_summary_is_not_stored_yet_or_nothing_is_found():
    sessions = [{"sessionId": "s-1", "createdAt": now() - datetime.timedelta(hours=2)}]
    _, conversations = tools(Memory(sessions=sessions))
    assert "no summary stored yet" in conversations(days=1)
    _, conversations = tools(Memory())
    assert "No earlier conversation in the last 7 day(s)." in conversations()
    _, conversations = tools(Memory(fail=missing()))
    assert "No earlier conversation" in conversations(days=3)


@pytest.mark.parametrize("days", [0, 31, True, "2"])
def test_recall_refuses_days_outside_its_limits(days):
    memory = Memory()
    _, conversations = tools(memory)
    assert conversations(days=days).startswith("Refused: days must be")
    assert memory.calls == []
