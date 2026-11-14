"""scripts/smoke.py and scripts/memory.py: the check functions, with the agent stubbed."""

from __future__ import annotations

import datetime
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(f"ta_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"ta_{name}"] = module
    spec.loader.exec_module(module)
    return module


smoke = load("smoke")
memory = smoke.memory
WORD = "qzxvkjwp"


class Agent:
    """Answers from a per-(user, session) history, like the real agent, or leaks if told to."""

    def __init__(self, leak=False, forgets_after_stop=False):
        self.history, self.leak, self.forgets = {}, leak, forgets_after_stop

    def ask(self, user, session, prompt):
        key = (user, session)
        seen = " ".join(self.history.get(key, []))
        if self.leak:
            seen = " ".join(text for texts in self.history.values() for text in texts)
        self.history.setdefault(key, []).append(prompt)
        return WORD if WORD in seen else "NONE"

    def stop(self, user, session):
        if self.forgets:
            self.history.pop((user, session), None)

    def texts_of(self, user, session):
        return self.history.get((user, session), [])


def test_same_session_passes_when_the_word_comes_back_and_fails_otherwise():
    agent = Agent()
    assert smoke.check_same_session(agent.ask, "alice", "s", WORD)[0]
    assert not smoke.check_same_session(lambda *args: "no idea", "alice", "s", WORD)[0]


def test_restored_session_fails_when_the_history_is_lost_with_the_runtime_session():
    agent = Agent()
    smoke.check_same_session(agent.ask, "alice", "s", WORD)
    assert smoke.check_restored_session(agent.ask, agent.stop, "alice", "s", WORD)[0]
    forgetful = Agent(forgets_after_stop=True)
    smoke.check_same_session(forgetful.ask, "alice", "s", WORD)
    assert not smoke.check_restored_session(forgetful.ask, forgetful.stop, "alice", "s", WORD)[0]


def test_no_token_passes_only_when_both_requests_are_refused_as_unauthorised():
    def refuse(token):
        raise smoke.chat.ChatError("not authorised (HTTP 401): missing token")

    assert smoke.check_no_token(refuse, "aaa.bbb.cccccc")[0]
    assert not smoke.check_no_token(lambda token: None, "aaa.bbb.cccccc")[0]

    def other_error(token):
        raise smoke.chat.ChatError("the agent answered HTTP 500: boom")

    assert not smoke.check_no_token(other_error, "aaa.bbb.cccccc")[0]


def test_direct_call_passes_only_when_the_runtime_refuses_a_valid_token():
    def refuse():
        raise smoke.chat.ChatError("not authorised (HTTP 403): no gateway in the chain")

    def other_error():
        raise smoke.chat.ChatError("the agent endpoint answered HTTP 500")

    assert smoke.check_direct_call(refuse)[0]
    accepted, detail = smoke.check_direct_call(lambda: [{"type": "text", "data": "hi"}])
    assert not accepted and "answered a direct call" in detail
    assert not smoke.check_direct_call(other_error)[0]


def test_no_token_sends_none_and_then_a_token_that_differs_from_the_real_one():
    sent = []

    def refuse(token):
        sent.append(token)
        raise smoke.chat.ChatError("not authorised (HTTP 401)")

    smoke.check_no_token(refuse, "aaa.bbb.cccccc")
    assert sent[0] is None and sent[1] != "aaa.bbb.cccccc" and sent[1].startswith("aaa.bbb.")


def test_other_user_passes_for_separate_histories_and_fails_on_a_leak():
    agent = Agent()
    smoke.check_same_session(agent.ask, "alice", "s", WORD)
    assert smoke.check_other_user(agent.ask, agent.texts_of, "alice", "bob", "s", WORD)[0]
    leaky = Agent(leak=True)
    smoke.check_same_session(leaky.ask, "alice", "s", WORD)
    passed, detail = smoke.check_other_user(leaky.ask, leaky.texts_of, "alice", "bob", "s", WORD)
    assert not passed and "bob was told alice's word" in detail


def test_other_user_fails_when_the_stored_messages_cannot_be_compared():
    agent = Agent()
    assert not smoke.check_other_user(agent.ask, lambda user, s: [], "alice", "bob", "s", WORD)[0]


class Clock:
    """A fake clock: sleep() moves time forward instead of waiting."""

    def __init__(self):
        self.now, self.slept = 0.0, []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_wait_returns_the_first_record_as_soon_as_there_is_one():
    clock, answers = Clock(), [[], [], ["record"]]
    found = smoke.wait_for_record(
        lambda: answers.pop(0), timeout_s=300, interval_s=10, clock=clock.time, sleep=clock.sleep
    )
    assert found == "record"
    assert clock.slept == [10, 10]


def test_wait_gives_up_after_the_timeout_without_sleeping_past_it():
    clock, calls = Clock(), []

    def nothing():
        calls.append(clock.now)
        return []

    found = smoke.wait_for_record(
        nothing, timeout_s=25, interval_s=10, clock=clock.time, sleep=clock.sleep
    )
    assert found is None
    assert calls == [0, 10, 20, 25]
    assert clock.now == 25


class LongTermAgent:
    """Keeps a per-user long-term memory: a stated signature word is visible in any session."""

    def __init__(self, leak=False, extracts=True):
        self.records, self.leak, self.extracts = {}, leak, extracts

    def ask(self, user, session, prompt):
        if "sign every answer" in prompt and self.extracts:
            word = prompt.rstrip(".").split()[-1]
            self.records.setdefault(user, []).append(f"Signs answers with {word}")
        known = self.records_of(user, "preferences")
        if self.leak:
            known = [r for texts in self.records.values() for r in texts]
        return WORD if any(WORD in r for r in known) else "NONE"

    def records_of(self, user, strategy):
        return list(self.records.get(user, [])) if strategy == "preferences" else []


def no_wait(list_fn):
    found = list_fn()
    return found[0] if found else None


def test_new_session_passes_when_a_new_session_knows_the_preference():
    agent = LongTermAgent()
    passed, detail = smoke.check_new_session(agent.ask, agent.records_of, no_wait, "alice", WORD)
    assert passed, detail


def test_new_session_fails_naming_the_namespace_when_no_record_appears():
    agent = LongTermAgent(extracts=False)
    passed, detail = smoke.check_new_session(agent.ask, agent.records_of, no_wait, "alice", WORD)
    assert not passed
    assert "no record" in detail and "preferences" in detail


def test_isolation_passes_for_separate_users_and_fails_on_a_leak():
    agent = LongTermAgent()
    smoke.check_new_session(agent.ask, agent.records_of, no_wait, "alice", WORD)
    assert smoke.check_isolation(agent.ask, agent.records_of, "alice", "bob", WORD)[0]
    leaky = LongTermAgent(leak=True)
    smoke.check_new_session(leaky.ask, leaky.records_of, no_wait, "alice", WORD)
    passed, detail = smoke.check_isolation(leaky.ask, leaky.records_of, "alice", "bob", WORD)
    assert not passed and "bob was told alice's preference" in detail


def test_isolation_fails_when_bob_has_a_record_with_alices_word():
    agent = LongTermAgent()
    smoke.check_new_session(agent.ask, agent.records_of, no_wait, "alice", WORD)
    agent.records["bob"] = [f"Signs answers with {WORD}"]
    passed, detail = smoke.check_isolation(
        lambda *a: "NONE", agent.records_of, "alice", "bob", WORD
    )
    assert not passed and "bob's preferences" in detail


def test_isolation_fails_when_alice_has_no_record_to_compare_with():
    agent = LongTermAgent(extracts=False)
    passed, detail = smoke.check_isolation(agent.ask, agent.records_of, "alice", "bob", WORD)
    assert not passed and "cannot compare" in detail


def test_run_prints_one_line_per_check_and_exits_1_on_any_failure():
    def raising():
        raise smoke.chat.ChatError("could not reach the agent")

    out = io.StringIO()
    code = smoke.run([("one", lambda: (True, "fine")), ("two", raising)], out=out)
    assert code == 1
    assert out.getvalue().splitlines() == [
        "PASS  one: fine",
        "FAIL  two: could not reach the agent",
    ]
    assert smoke.run([("one", lambda: (True, "fine"))], out=io.StringIO()) == 0


def test_answer_text_joins_the_chunks_and_raises_on_an_error_event():
    events = [{"type": "text", "data": "a"}, {"type": "text", "data": "b"}, {"type": "done"}]
    assert smoke.answer_text(events) == "ab"
    with pytest.raises(smoke.chat.ChatError, match="no"):
        smoke.answer_text([{"type": "error", "message": "no"}])


def test_random_words_differ_and_carry_the_smoke_marker():
    word = smoke.random_word()
    assert len(word) == 8 and word.startswith("smk") and word != smoke.random_word()


def test_marked_finds_records_with_a_smoke_word_only():
    records = [
        {"memoryRecordId": "1", "content": {"text": "Sign every answer with smkabcde."}},
        {"memoryRecordId": "2", "content": {"text": "Prefers answers in Polish."}},
        {"memoryRecordId": "3", "content": {"text": "Remember SMKQWERT please"}},
    ]
    assert [r["memoryRecordId"] for r in smoke.marked(records)] == ["1", "3"]


def test_namespace_prefix_stops_before_the_session():
    assert memory.namespace_prefix("/summaries/{actorId}/{sessionId}/", "a-1") == "/summaries/a-1/"
    assert memory.namespace_prefix("/facts/{actorId}/", "a-1") == "/facts/a-1/"


def test_purge_deletes_marked_records_of_each_actor_in_every_namespace():
    class Client:
        def __init__(self):
            self.listed, self.deleted = [], []

        def list_memory_records(self, **kwargs):
            self.listed.append(kwargs["namespacePath"])
            text = "smkabcde" if kwargs["namespacePath"].startswith("/facts/") else "keep"
            return {
                "memoryRecordSummaries": [
                    {
                        "memoryRecordId": kwargs["namespacePath"],
                        "createdAt": 0,
                        "content": {"text": text},
                    }
                ]
            }

        def delete_memory_record(self, **kwargs):
            self.deleted.append((kwargs["memoryId"], kwargs["memoryRecordId"]))

    client = Client()
    assert smoke.purge(client, "mem-1", ["a-1", "b-2"]) == 2
    assert set(client.listed) == {
        f"/{kind}/{actor}/"
        for kind in ("preferences", "facts", "summaries")
        for actor in ("a-1", "b-2")
    }
    assert client.deleted == [("mem-1", "/facts/a-1/"), ("mem-1", "/facts/b-2/")]


def event(minute, role, text, blob=False):
    stored = json.dumps({"message": {"role": role.lower(), "content": [{"text": text}]}})
    payload = (
        {"blob": "{}"} if blob else {"conversational": {"role": role, "content": {"text": stored}}}
    )
    return {"eventTimestamp": datetime.datetime(2026, 1, 1, 10, minute), "payload": [payload]}


def test_memory_lists_messages_oldest_first_and_skips_state_events():
    class Client:
        def __init__(self):
            self.calls = []

        def list_events(self, **kwargs):
            self.calls.append(kwargs)
            if "nextToken" not in kwargs:
                return {"events": [event(2, "ASSISTANT", "hello")], "nextToken": "t"}
            return {"events": [event(1, "USER", "hi"), event(0, "USER", "", blob=True)]}

    client = Client()
    events = memory.list_events(client, "mem-1", "actor-1", "session-1")
    assert [e["eventTimestamp"].minute for e in events] == [0, 1, 2]
    assert client.calls[0]["actorId"] == "actor-1" and client.calls[0]["sessionId"] == "session-1"
    assert [(role, text) for _, role, text in memory.messages(events)] == [
        ("USER", "hi"),
        ("ASSISTANT", "hello"),
    ]


def record(minute, text):
    return {"createdAt": datetime.datetime(2026, 1, 1, 10, minute), "content": {"text": text}}


def test_memory_namespace_is_filled_in_for_the_actor_and_session():
    template = "/summaries/{actorId}/{sessionId}/"
    assert memory.namespace_of(template, "actor-1", "session-1") == "/summaries/actor-1/session-1/"


def test_memory_records_join_pages_oldest_first_in_the_exact_namespace():
    class Client:
        def __init__(self):
            self.calls = []

        def list_memory_records(self, **kwargs):
            self.calls.append(kwargs)
            if "nextToken" not in kwargs:
                return {"memoryRecordSummaries": [record(2, "second")], "nextToken": "t"}
            return {"memoryRecordSummaries": [record(1, "first")]}

    client = Client()
    found = memory.list_records(client, "mem-1", "/preferences/actor-1/")
    assert [r["content"]["text"] for r in found] == ["first", "second"]
    # Exact match, not a prefix: the operator sees one user's namespace only.
    assert client.calls[0]["namespace"] == "/preferences/actor-1/"
    assert "namespacePath" not in client.calls[0]


def test_memory_prints_no_records_for_an_empty_namespace():
    assert memory.record_lines("/preferences/actor-1/", []) == [
        "/preferences/actor-1/",
        "  no records",
    ]


def test_memory_prints_time_and_text_of_each_record():
    lines = memory.record_lines("/preferences/actor-1/", [record(5, "Prefers  short\nanswers")])
    assert lines == ["/preferences/actor-1/", "  2026-01-01 10:05:00  Prefers short answers"]


def test_memory_text_falls_back_to_the_raw_value():
    assert memory.message_text("plain text") == "plain text"


def test_a_failed_word_check_shows_the_answer():
    passed, detail = smoke.check_restored_session(
        lambda *a: "Nie pamiętam.", lambda *a: None, "alice", "s", WORD
    )
    assert not passed and "'Nie pamiętam.'" in detail and WORD in detail


def github_agent(consented):
    """Answers the issues prompt with a list for users who consented, else a consent URL."""

    def ask_events(user, session, prompt):
        if user in consented:
            return [{"type": "text", "data": "Open issues: #741, #727."}, {"type": "done"}]
        return [{"type": "auth_url", "url": "https://consent.example/x", "state": "s"}]

    return ask_events


def test_github_tool_passes_when_alice_gets_issues_and_bob_is_asked_to_consent():
    passed, detail = smoke.check_github_tool(github_agent({"alice"}), "alice", "bob")
    assert passed, detail


def test_github_tool_fails_with_the_runbook_step_when_alice_has_not_consented():
    passed, detail = smoke.check_github_tool(github_agent(set()), "alice", "bob")
    assert not passed and "has not consented" in detail and "runbook" in detail


def test_github_tool_fails_when_bob_gets_issues_without_consent():
    passed, detail = smoke.check_github_tool(github_agent({"alice", "bob"}), "alice", "bob")
    assert not passed and "bob got an answer without a consent URL" in detail


def test_github_tool_fails_when_the_answer_has_no_issue_numbers():
    def ask_events(user, session, prompt):
        if user == "alice":
            return [{"type": "text", "data": "I cannot do that."}, {"type": "done"}]
        return [{"type": "auth_url", "url": "u", "state": "s"}]

    passed, detail = smoke.check_github_tool(ask_events, "alice", "bob")
    assert not passed and "no issue numbers" in detail and "I cannot do that." in detail


def test_until_consent_stops_reading_at_the_first_consent_url():
    def events():
        yield {"type": "auth_url", "url": "u", "state": "s"}
        pytest.fail("read past the consent URL")

    assert smoke.until_consent(events()) == [{"type": "auth_url", "url": "u", "state": "s"}]


def reader(answers):
    """A fake request for the web page's reads: (user, session) -> event."""
    calls = []

    def read(user, session, body):
        calls.append((user, session, body))
        return answers.get((user, next(iter(body))), {"type": "error", "message": "x"})

    return read, calls


def test_conversations_pass_when_the_session_is_listed_with_a_title():
    item = {"session_id": "s-1", "created_at": "2026-11-15T12:00:00Z", "title": "Word game"}
    read, calls = reader({("alice", "conversations"): {"type": "conversations", "items": [item]}})
    assert smoke.check_conversations(read, "alice", "s-1")[0]
    assert calls[0][2] == {"conversations": {}} and calls[0][1] == "s-1"


def test_conversations_fail_without_the_session_or_its_title():
    untitled = {"session_id": "s-1", "created_at": "", "title": None}
    read, _ = reader({("alice", "conversations"): {"type": "conversations", "items": [untitled]}})
    assert not smoke.check_conversations(read, "alice", "s-1")[0]
    read, _ = reader({("alice", "conversations"): {"type": "conversations", "items": []}})
    passed, detail = smoke.check_conversations(read, "alice", "s-1")
    assert not passed and "lacks" in detail


def history_of(*texts):
    roles = ["user", "assistant"] * len(texts)
    turns = [{"role": roles[i], "text": text} for i, text in enumerate(texts)]
    return {"type": "history", "turns": turns}


def test_history_passes_for_the_turns_in_order_and_fails_otherwise():
    good = history_of(f"Remember this word for later: {WORD}.", "OK", "Which word?", WORD)
    read, calls = reader({("alice", "history"): good})
    assert smoke.check_history(read, "alice", "s-1", WORD)[0]
    assert calls[0][1] == "s-1"
    read, _ = reader({("alice", "history"): history_of("hi", "hello")})
    assert not smoke.check_history(read, "alice", "s-1", WORD)[0]


def test_history_isolation_fails_when_bob_sees_alices_word_and_passes_otherwise():
    read, _ = reader({("bob", "history"): history_of("Which word did I ask?", "NONE")})
    assert smoke.check_history_isolation(read, "alice", "bob", "s-1", WORD)[0]
    read, _ = reader({("bob", "history"): history_of("Which word?", WORD)})
    assert not smoke.check_history_isolation(read, "alice", "bob", "s-1", WORD)[0]
    read, _ = reader({("bob", "history"): {"type": "history", "turns": []}})
    assert not smoke.check_history_isolation(read, "alice", "bob", "s-1", WORD)[0]


GATEWAY = "https://gw-1.gateway.example/agent"
POLICY = "connect-src 'self' https://gw-1.gateway.example https://login.example"
GOOD_HTML = f'<meta http-equiv="Content-Security-Policy" content="{POLICY}" />'
HEADERS = {
    "content-security-policy": "default-src 'self'; script-src 'self'; frame-ancestors 'none'"
}


def site(
    page=(200, HEADERS, GOOD_HTML),
    config=(200, {}, '{"gatewayUrl": "https://gw-1.gateway.example"}'),
    bucket=403,
):
    answers = {
        "https://d1.example/": page,
        "https://d1.example/config.json": config,
        "https://bucket.example/index.html": (bucket, {}, ""),
    }
    return lambda url: answers[url]


def page_served(fetch):
    return smoke.check_page_served(
        fetch, "https://d1.example", "https://bucket.example/index.html", GATEWAY
    )


def test_page_served_passes_for_https_headers_settings_and_a_closed_bucket():
    assert page_served(site())[0]


def test_page_served_fails_on_missing_headers_policy_settings_or_an_open_bucket():
    assert not page_served(site(page=(200, {}, GOOD_HTML)))[0]
    assert not page_served(site(page=(200, HEADERS, "<html></html>")))[0]
    assert not page_served(site(config=(404, {}, "")))[0]
    passed, detail = page_served(site(bucket=200))
    assert not passed and "200" in detail
