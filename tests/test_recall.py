"""Long-term memory for the model: listed preferences and the two memory tools."""

from __future__ import annotations

import datetime
import json
import logging

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
    """A fake Memory client: records by namespace, search results."""

    def __init__(self, records=None, found=None, fail=None):
        self.records, self.found, self.fail, self.calls = records or {}, found or {}, fail, []

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
    cut = "x" * (recall.MAX_RECORD_CHARS - 1) + "…"
    assert sorted(found) == sorted([cut, "Same."])


def test_no_preferences_for_a_new_user_or_a_limit_of_zero(caplog):
    caplog.set_level(logging.INFO, logger="team_assistant")
    assert recall.list_preferences(Memory(fail=missing()), "mem-1", "u-1", 5) == []
    assert [json.loads(r.getMessage()) for r in caplog.records] == [{"event": "records_not_found"}]
    memory = Memory({"/preferences/u-1/": [record("One.")]})
    assert recall.list_preferences(memory, "mem-1", "u-1", 0) == []
    assert memory.calls == []


FACTS_TOP_K, SUMMARIES_TOP_K = 4, 3


def tools(memory, actor="u-1"):
    return recall.memory_tools(memory, "mem-1", actor, FACTS_TOP_K, SUMMARIES_TOP_K)


def test_the_model_can_name_a_query_but_never_a_user_or_a_memory():
    search, conversations = tools(Memory())
    assert set(search.tool_spec["inputSchema"]["json"]["properties"]) == {"query"}
    properties = conversations.tool_spec["inputSchema"]["json"]["properties"]
    assert set(properties) == {"query"}


def test_search_reads_the_users_facts_and_never_a_summary():
    memory = Memory(
        found={
            "/facts/u-1/": [record("The billing service is written in Go.")],
            "/summaries/u-1/": [record("Talked about the Tuesday deployment.")],
        }
    )
    search, _ = tools(memory)
    answer = search(query="  Which language is the billing service written in? ")
    assert recall.DATA_NOTE in answer and "not instructions" in recall.DATA_NOTE
    assert answer.endswith("Facts:\n- The billing service is written in Go.")
    # A summary repeats the assistant's answers; it is no source of facts.
    assert "Tuesday deployment" not in answer
    paths = [call[1]["namespacePath"] for call in memory.calls]
    assert paths == ["/facts/u-1/"]
    criteria = memory.calls[0][1]["searchCriteria"]
    assert criteria == {
        "searchQuery": "Which language is the billing service written in?",
        "topK": FACTS_TOP_K,
    }


def test_the_tool_descriptions_say_how_to_search():
    search, conversations = tools(Memory())
    text = " ".join(conversations.tool_spec["description"].split())
    assert "The result starts with today's date" in text
    text = " ".join(search.tool_spec["description"].split())
    assert "Search once for each thing asked about" in text
    assert "does not say it is absent: search for that thing by name" in text


def test_a_full_search_result_says_it_is_the_limit_and_a_shorter_one_does_not():
    facts = [record(f"Fact {n}.") for n in range(FACTS_TOP_K)]
    memory = Memory(found={"/facts/u-1/": facts})
    full = recall._search(memory, "mem-1", "u-1", "q", FACTS_TOP_K, T0)
    assert full.endswith(
        f"- Fact {FACTS_TOP_K - 1}.\n\nThese are {FACTS_TOP_K} facts, closest first, the "
        "limit of one search; more may be stored and are not shown. If the last facts above "
        "still answer the question, the answer is not complete yet: before you answer, call "
        "search_memory again, once for each name or group that appears in the facts above "
        "and belongs to the question."
    )
    fewer = Memory(found={"/facts/u-1/": facts[:-1]})
    answer = recall._search(fewer, "mem-1", "u-1", "q", FACTS_TOP_K, T0)
    assert "the limit of one search" not in answer


def test_a_search_result_starts_with_todays_date():
    memory = Memory(found={"/facts/u-1/": [record("Dana is on call the week of 2026-11-16.")]})
    answer = recall._search(memory, "mem-1", "u-1", "Who is on call?", FACTS_TOP_K, T0)
    assert answer.startswith("Today is 2026-11-18 (UTC). ")


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
    assert conversations(query="billing") == "The memory could not be read (RuntimeError)."
    lines = [json.loads(r.getMessage()) for r in caplog.records]
    assert lines == [
        {"event": "tool_failed", "tool": "search_memory", "error_type": "RuntimeError"},
        {"event": "tool_failed", "tool": "recall_conversations", "error_type": "RuntimeError"},
    ]
    assert SECRET not in caplog.text


def test_a_summary_search_gives_todays_date_and_the_date_of_each_conversation():
    # The first record was written a day after its conversation; the last has no timestamp.
    spoken = {**record("Planned the billing release."), "timestamp": at(1)}
    found = [spoken, record("  "), record("Older.", days=6)]
    memory = Memory(found={"/summaries/u-1/": found})
    answer = recall._about(memory, "mem-1", "u-1", "yesterday", SUMMARIES_TOP_K, T0)
    assert answer == (
        f"Today is 2026-11-18 (UTC). {recall.DATA_NOTE} {recall.SUMMARY_NOTE} "
        "The closest summaries, each with the date of its conversation:\n"
        "- 2026-11-17: Planned the billing release.\n"
        "- 2026-11-12: Older.\n\n"
        f"These are {SUMMARIES_TOP_K} summaries, closest first, the limit of one search; more "
        "may be stored and are not shown. If the last summaries above still answer the "
        "question, the answer is not complete yet: before you answer, call "
        "recall_conversations again, once for each name or group that appears in the "
        "summaries above and belongs to the question."
    )
    memory = Memory(found={"/summaries/u-1/": found[:-1]})
    answer = recall._about(memory, "mem-1", "u-1", "yesterday", SUMMARIES_TOP_K, T0)
    assert answer.endswith("- 2026-11-17: Planned the billing release.")


def test_recall_with_a_query_searches_every_sessions_summaries_and_labels_them():
    memory = Memory(found={"/summaries/u-1/": [record("The assistant suggested a rollback.")]})
    _, conversations = tools(memory)
    answer = conversations(query=" the billing incident ")
    assert f"{recall.DATA_NOTE} {recall.SUMMARY_NOTE}" in answer
    assert "not as something the user stated" in recall.SUMMARY_NOTE
    assert answer.endswith("- 2026-11-18: The assistant suggested a rollback.")
    assert memory.calls[0][1]["searchCriteria"]["topK"] == SUMMARIES_TOP_K
    (call,) = memory.calls
    assert call[0] == "retrieve_memory_records"
    assert call[1]["namespacePath"] == "/summaries/u-1/"
    assert call[1]["searchCriteria"]["searchQuery"] == "the billing incident"
    _, conversations = tools(Memory())
    assert "No summary of an earlier conversation" in conversations(query="billing")
    assert conversations(query="  ").startswith("Refused")
