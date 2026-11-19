"""The entry point: who is asking, what is refused, what is stored and what is logged."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from conftest import FakeModel, bearer, collect, collect_events
from team_assistant import app
from team_assistant.config import AgentConfig, RetrievalSetting

CONFIG = AgentConfig("fake-model", 100, 30)
SECRET_PROMPT = "my salary is 12345"


def run(payload, context, managers, model=None):
    model = model or FakeModel()
    stream = app.respond(
        payload,
        context,
        config=CONFIG,
        model=model,
        make_session_manager=managers,
        make_tools=lambda on_auth_url, state: [],
    )
    return collect_events(stream), model


def test_streams_text_then_done(context, managers, agent_env):
    events, model = run({"prompt": "hi"}, context, managers)
    assert events == [
        {"type": "text", "data": "Hello"},
        {"type": "text", "data": ", world"},
        {"type": "done", "usage": {"input": 11, "output": 7}},
    ]
    assert len(model.calls) == 1
    assert model.calls[0]["system_prompt"] == app.SYSTEM_PROMPT


def test_session_manager_is_built_with_sub_and_session_id_and_stores_both_turns(
    context, managers, agent_env
):
    run({"prompt": "hi"}, context, managers)
    (manager,) = managers.built
    assert manager.opened_with == ("mem-1", "u-1", context.session_id, "eu-west-1")
    assert [m["role"] for m in manager.messages] == ["user", "assistant"]
    assert manager.closed


def test_session_manager_gets_the_retrieval_settings_from_the_config(context, managers, agent_env):
    setting = RetrievalSetting(top_k=3, relevance_score=0.4)
    config = AgentConfig("fake-model", 100, 30, {"/facts/{actorId}/": setting})
    stream = app.respond(
        {"prompt": "hi"},
        context,
        config=config,
        model=FakeModel(),
        make_session_manager=managers,
        make_tools=lambda on_auth_url, state: [],
    )
    collect_events(stream)
    (manager,) = managers.built
    assert manager.retrieval == {"/facts/{actorId}/": setting}


class SmallWindowModel(FakeModel):
    """A model whose context window a short conversation fills."""

    def get_config(self):
        return {"model_id": "fake", "context_window_limit": 400}


def test_a_long_conversation_keeps_its_beginning_as_a_summary(managers):
    model = SmallWindowModel(chunks=("The user counted from 0 to 13.",))
    manager = managers("mem-1", "u-1", "s-1", "eu-west-1", {})
    agent = app.build_agent(CONFIG, manager, "u-1", "s-1", model)
    for n in range(14):
        role = "user" if n % 2 == 0 else "assistant"
        agent.messages.append({"role": role, "content": [{"text": f"number {n} " * 30}]})
    agent("and the next one?")
    # Two model calls: the summary of the oldest messages, then the answer.
    assert len(model.calls) == 2
    first = agent.messages[0]
    assert first["role"] == "user" and "counted from 0 to 13" in first["content"][0]["text"]
    assert len(agent.messages) < 16
    assert agent.messages[-2]["content"][0]["text"] == "and the next one?"
    state = agent.conversation_manager.get_state()
    assert state["summary_message"] == first and state["removed_message_count"] > 0


def test_a_short_conversation_is_sent_whole(managers):
    model = FakeModel()
    manager = managers("mem-1", "u-1", "s-1", "eu-west-1", {})
    agent = app.build_agent(CONFIG, manager, "u-1", "s-1", model)
    for n in range(50):
        role = "user" if n % 2 == 0 else "assistant"
        agent.messages.append({"role": role, "content": [{"text": f"number {n}"}]})
    agent("and the next one?")
    assert len(model.calls) == 1 and len(model.calls[0]["messages"]) == 51


REFUSED = [{"type": "error", "message": app.OTHER_USERS_SESSION}]


def test_a_session_id_used_by_one_user_is_refused_to_another_before_memory_and_model(
    context, managers, agent_env, caplog
):
    # The Runtime sends both users' requests to the same microVM (this process).
    other = SimpleNamespace(session_id=context.session_id, request_headers=bearer({"sub": "u-2"}))
    caplog.set_level(logging.INFO, logger="team_assistant")
    run({"prompt": "hi"}, context, managers)
    events, model = run({"prompt": SECRET_PROMPT}, other, managers)
    assert events == REFUSED and model.calls == []
    assert [m.opened_with[1] for m in managers.built] == ["u-1"]
    line = json.loads(caplog.records[-1].getMessage())
    assert (line["outcome"], line["actor"], line["reason"]) == (
        "rejected",
        "u-2",
        "session of another user",
    )
    # The first user goes on, and the other user is free in a session of their own.
    assert run({"prompt": "again"}, context, managers)[0][-1]["type"] == "done"
    other.session_id = "session-" + "9" * 30
    assert run({"prompt": "hi"}, other, managers)[0][-1]["type"] == "done"


def test_another_users_session_is_refused_for_reads_and_consents_too(context, managers, agent_env):
    other = SimpleNamespace(session_id=context.session_id, request_headers=bearer({"sub": "u-2"}))
    run({"prompt": "hi"}, context, managers)
    calls = []
    assert consent_run(CONSENT, other, managers, lambda *a: calls.append(a)) == REFUSED
    reader = MemoryReader()
    for body in ({"history": {}}, {"conversations": {}}):
        assert read_run(body, other, managers, reader) == REFUSED
    assert calls == [] and reader.calls == []


@pytest.mark.parametrize("headers", [None, {}, {"Authorization": "Basic abc"}])
def test_missing_authorization_yields_error_and_no_model_or_memory_call(
    headers, context, managers, agent_env
):
    context.request_headers = headers
    events, model = run({"prompt": "hi"}, context, managers)
    assert [e["type"] for e in events] == ["error"]
    assert model.calls == [] and managers.built == []


@pytest.mark.parametrize(
    "payload", [{}, {"prompt": ""}, {"prompt": 5}, {"prompt": "   "}, None, "hi", {"text": "hi"}]
)
def test_bad_prompt_yields_error_and_no_memory_use(payload, context, managers, agent_env):
    events, model = run(payload, context, managers)
    assert [e["type"] for e in events] == ["error"]
    assert "prompt" in events[0]["message"]
    assert model.calls == [] and managers.built == []


def test_missing_session_id_or_settings_yields_error(context, managers, agent_env, monkeypatch):
    context.session_id = None
    assert [e["type"] for e in run({"prompt": "hi"}, context, managers)[0]] == ["error"]
    context.session_id = "session-" + "1" * 30
    monkeypatch.delenv("MEMORY_ID")
    assert [e["type"] for e in run({"prompt": "hi"}, context, managers)[0]] == ["error"]
    assert managers.built == []


def test_session_manager_is_closed_when_the_model_raises(context, managers, agent_env):
    model = FakeModel(error=RuntimeError(f"upstream said: {SECRET_PROMPT}"))
    events, _ = run({"prompt": SECRET_PROMPT}, context, managers, model)
    assert [e["type"] for e in events] == ["error"]
    assert SECRET_PROMPT not in events[0]["message"]
    assert managers.built[0].closed


def test_log_line_has_actor_and_usage_but_no_message_text(context, managers, agent_env, caplog):
    caplog.set_level(logging.INFO, logger="team_assistant")
    run({"prompt": SECRET_PROMPT}, context, managers, FakeModel(chunks=("the answer is 42",)))
    (record,) = [r for r in caplog.records if r.name == "team_assistant"]
    line = json.loads(record.getMessage())
    assert line == {
        "event": "invocation",
        "outcome": "ok",
        "actor": "u-1",
        "session": context.session_id,
        "input_tokens": 11,
        "output_tokens": 7,
    }
    assert SECRET_PROMPT not in caplog.text and "the answer is 42" not in caplog.text


def test_failure_log_names_the_error_type_only(context, managers, agent_env, caplog):
    caplog.set_level(logging.INFO, logger="team_assistant")
    model = FakeModel(error=RuntimeError(f"upstream said: {SECRET_PROMPT}"))
    run({"prompt": SECRET_PROMPT}, context, managers, model)
    line = json.loads([r for r in caplog.records if r.name == "team_assistant"][0].getMessage())
    assert line["outcome"] == "failed" and line["error_type"] == "RuntimeError"
    assert SECRET_PROMPT not in caplog.text


def test_the_entrypoint_is_registered_and_takes_the_context():
    assert app.app.handlers["main"] is app.invoke


def test_the_real_agent_has_no_tools_and_prints_nothing(managers, capsys):
    agent = app.build_agent(CONFIG, managers("m", "u", "s", "r", {}), "u", "s", FakeModel())
    assert agent.tool_names == []
    collect(agent.stream_async("hi"))
    assert capsys.readouterr().out == ""


def test_over_http_the_answer_is_a_stream_of_sse_data_lines(managers, agent_env, monkeypatch):
    """The whole path the Runtime uses: POST /invocations with the session and token headers."""
    from starlette.testclient import TestClient

    monkeypatch.setattr(app, "session_manager", managers)
    monkeypatch.setattr(app, "QuietBedrockModel", lambda **config: FakeModel())
    # The real tool's decorator builds an AWS client; this test is about the HTTP path.
    monkeypatch.setattr(app, "_default_tools", lambda on_auth_url, state, return_to: [])
    session_id = "session-" + "2" * 30
    headers = {
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        "authorization": bearer({"sub": "u-9"})["Authorization"],
    }
    response = TestClient(app.app).post("/invocations", json={"prompt": "hi"}, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = [line for line in response.text.splitlines() if line]
    assert all(line.startswith("data: ") for line in lines)
    events = [json.loads(line.removeprefix("data: ")) for line in lines]
    assert events[0] == {"type": "text", "data": "Hello"}
    assert events[-1] == {"type": "done", "usage": {"input": 11, "output": 7}}
    assert managers.built[0].opened_with == ("mem-1", "u-9", session_id, "eu-west-1")


def test_over_http_a_request_without_a_token_gets_an_error_event(managers, agent_env, monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setattr(app, "session_manager", managers)
    headers = {"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "session-" + "3" * 30}
    response = TestClient(app.app).post("/invocations", json={"prompt": "hi"}, headers=headers)
    (line,) = [line for line in response.text.splitlines() if line]
    assert json.loads(line.removeprefix("data: "))["type"] == "error"
    assert managers.built == []


def test_a_consent_url_reaches_the_client_at_once_with_the_requests_state(
    context, managers, agent_env, caplog
):
    made = {}

    def tools(on_auth_url, state):
        made["state"] = state
        on_auth_url("https://github.example/consent?x=1")  # as the tool does when it must wait
        return []

    caplog.set_level(logging.INFO, logger="team_assistant")
    stream = app.respond(
        {"prompt": "list issues"},
        context,
        config=CONFIG,
        model=FakeModel(),
        make_session_manager=managers,
        make_tools=tools,
    )
    events = collect_events(stream)
    assert events[0] == {
        "type": "auth_url",
        "url": "https://github.example/consent?x=1",
        "state": made["state"],
    }
    assert len(made["state"]) >= 16
    assert events[-1]["type"] == "done"
    assert "github.example" not in caplog.text


def test_the_default_tools_are_the_github_tools_with_the_requests_state(
    context, managers, agent_env, monkeypatch
):
    built = []
    monkeypatch.setattr(
        app, "github_tools", lambda on_auth_url, state, to: built.append((on_auth_url, state, to))
    )
    app._default_tools(print, "s-1", "http://localhost:8977/github/callback")
    assert built == [(print, "s-1", "http://localhost:8977/github/callback")]


CONSENT = {"consent": {"session_uri": "urn:ietf:params:oauth:request_uri:abc"}}


def consent_run(payload, context, managers, complete):
    stream = app.respond(payload, context, make_session_manager=managers, complete=complete)
    return collect_events(stream)


def test_a_consent_is_completed_with_the_requests_token_and_no_model_or_memory(
    context, managers, agent_env, caplog
):
    calls = []
    caplog.set_level(logging.INFO, logger="team_assistant")
    events = consent_run(CONSENT, context, managers, lambda *a: calls.append(a))
    token = context.request_headers["Authorization"].removeprefix("Bearer ")
    assert calls == [("urn:ietf:params:oauth:request_uri:abc", token, "eu-west-1")]
    assert events == [{"type": "consent_done"}]
    assert managers.built == []
    assert token not in caplog.text and '"outcome": "consent"' in caplog.text


def test_a_malformed_consent_is_refused(context, managers, agent_env):
    def complete(*args):
        raise app.ConsentError("the consent session URI is missing or malformed")

    events = consent_run({"consent": "x"}, context, managers, complete)
    assert events == [_error_event("the consent session URI is missing or malformed")]


def test_a_refused_consent_names_only_the_services_error_code(context, managers, agent_env):
    class Refused(Exception):
        response = {"Error": {"Code": "AccessDeniedException", "Message": "secret details"}}

    def complete(*args):
        raise Refused()

    events = consent_run(CONSENT, context, managers, complete)
    assert events == [_error_event("the consent could not be completed (AccessDeniedException)")]


def test_a_consent_without_a_token_is_refused(context, managers, agent_env):
    context.request_headers = {}
    events = consent_run(CONSENT, context, managers, lambda *a: pytest.fail("completed"))
    assert events[0]["type"] == "error" and "cannot tell who is asking" in events[0]["message"]


def _error_event(message):
    return {"type": "error", "message": message}


class MemoryReader:
    """A fake Memory client that records who was asked for."""

    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.calls = []

    def list_sessions(self, **kwargs):
        self.calls.append(("list_sessions", kwargs))
        if self.fail:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": self.fail, "Message": "secret detail"}}, "ListSessions"
            )
        return {"sessionSummaries": []}

    def list_events(self, **kwargs):
        self.calls.append(("list_events", kwargs))
        return {"events": []}


def read_run(payload, context, managers, reader):
    stream = app.respond(
        payload, context, make_session_manager=managers, make_memory_client=lambda region: reader
    )
    return collect_events(stream)


def test_conversations_are_listed_for_the_tokens_user_whatever_the_body_says(
    context, managers, agent_env
):
    reader = MemoryReader()
    events = read_run({"conversations": {"actor": "u-2"}}, context, managers, reader)
    assert events == [{"type": "conversations", "items": []}]
    assert [c[1]["actorId"] for c in reader.calls] == ["u-1"]
    assert reader.calls[0][1]["memoryId"] == "mem-1"
    assert managers.built == []


def test_history_is_read_from_the_requests_session_for_the_tokens_user(
    context, managers, agent_env
):
    reader = MemoryReader()
    events = read_run({"history": {"session_id": "other"}}, context, managers, reader)
    assert events == [{"type": "history", "turns": []}]
    (call,) = reader.calls
    assert call[1]["sessionId"] == context.session_id and call[1]["actorId"] == "u-1"


def test_a_refused_read_names_only_the_services_error_code(context, managers, agent_env, caplog):
    caplog.set_level(logging.INFO, logger="team_assistant")
    events = read_run(
        {"conversations": {}}, context, managers, MemoryReader("AccessDeniedException")
    )
    assert events == [_error_event("the conversations could not be read (AccessDeniedException)")]
    assert "secret detail" not in caplog.text


def test_a_read_without_a_token_is_refused(context, managers, agent_env):
    context.request_headers = {}
    reader = MemoryReader()
    events = read_run({"conversations": {}}, context, managers, reader)
    assert events[0]["type"] == "error" and "cannot tell who is asking" in events[0]["message"]
    assert reader.calls == []


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (None, "http://localhost:8977/github/callback"),
        ("web-local", "http://localhost:5173/callback.html"),
    ],
)
def test_the_tools_return_url_follows_the_requests_client(
    client, expected, context, managers, agent_env, monkeypatch
):
    seen = []
    monkeypatch.setattr(
        app, "_default_tools", lambda on_auth_url, state, return_to: seen.append(return_to) or []
    )
    payload = {"prompt": "hi"} if client is None else {"prompt": "hi", "client": client}
    collect_events(
        app.respond(
            payload, context, config=CONFIG, model=FakeModel(), make_session_manager=managers
        )
    )
    assert seen == [expected]


def test_an_unknown_client_is_refused_before_the_model(context, managers, agent_env):
    model = FakeModel()
    stream = app.respond(
        {"prompt": "hi", "client": "https://evil.example"},
        context,
        config=CONFIG,
        model=model,
        make_session_manager=managers,
    )
    (event,) = collect_events(stream)
    assert event["type"] == "error" and "unknown client" in event["message"]
    assert managers.built == []


def test_the_title_is_stored_before_done_with_the_question_and_answer(
    context, managers, agent_env, titles_stored
):
    events, _ = run({"prompt": "Count to three"}, context, managers)
    assert events[-1]["type"] == "done"
    ((region, model_id, memory_id, actor, session, prompt, answer),) = titles_stored
    assert (region, model_id, memory_id, actor) == ("eu-west-1", "fake-model", "mem-1", "u-1")
    assert session == context.session_id and prompt == "Count to three"
    assert answer == "".join(e["data"] for e in events if e["type"] == "text")


def test_a_failed_title_keeps_the_answer_and_logs_the_type_only(
    context, managers, agent_env, monkeypatch, caplog
):
    def fail(*args):
        raise RuntimeError("the prompt was: my secret")

    monkeypatch.setattr(app, "_store_title", fail)
    caplog.set_level(logging.INFO, logger="team_assistant")
    events, _ = run({"prompt": "hi"}, context, managers)
    assert events[-1]["type"] == "done"
    assert '"outcome": "title_failed"' in caplog.text and "RuntimeError" in caplog.text
    assert "my secret" not in caplog.text


def test_only_the_first_answer_of_a_session_gets_a_title(context, agent_env, titles_stored):
    from conftest import FakeSessionManager

    class Restoring(FakeSessionManager):
        """A session that already holds a conversation, as the real manager restores it."""

        def initialize(self, agent, **kwargs):
            agent.messages.append({"role": "user", "content": [{"text": "earlier"}]})
            agent.messages.append({"role": "assistant", "content": [{"text": "answer"}]})

    stream = app.respond(
        {"prompt": "again"},
        context,
        config=CONFIG,
        model=FakeModel(),
        make_session_manager=Restoring,
        make_tools=lambda on_auth_url, state: [],
    )
    assert collect_events(stream)[-1]["type"] == "done"
    assert titles_stored == []
