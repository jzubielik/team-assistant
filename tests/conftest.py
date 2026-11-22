"""Test doubles for the agent: a model that answers with fixed text and an in-memory session
manager. Neither calls AWS."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from strands.models import Model
from strands.session.session_manager import SessionManager


@pytest.fixture(autouse=True)
def _no_sessions_from_other_tests():
    """Each test starts as a new process would: no session id belongs to a user yet."""
    from team_assistant import app

    app._SESSION_USERS.clear()


# What app._memory_client returns in every test.
MEMORY_CLIENT = object()


@pytest.fixture(autouse=True)
def _no_long_term_memory(monkeypatch):
    """No stored preferences, no memory tools and no AWS client unless a test passes its own."""
    from team_assistant import app

    monkeypatch.setattr(app, "_default_recall", lambda *args: ([], []))
    monkeypatch.setattr(app, "_memory_client", lambda region: MEMORY_CLIENT)


class FakeModel(Model):
    """Streams ``chunks`` as the answer and reports ``usage``; raises ``error`` if given."""

    def __init__(self, chunks=("Hello", ", world"), usage=(11, 7), error=None):
        self.chunks, self.usage, self.error = chunks, usage, error
        self.calls = []

    def update_config(self, **model_config):
        pass

    def get_config(self):
        # With a known window, Strands does not warn that it has to assume one.
        return {"model_id": "fake", "context_window_limit": 200_000}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append({"messages": messages, "system_prompt": system_prompt})
        if self.error:
            raise self.error
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        for chunk in self.chunks:
            yield {"contentBlockDelta": {"delta": {"text": chunk}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        tokens_in, tokens_out = self.usage
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": tokens_in,
                    "outputTokens": tokens_out,
                    "totalTokens": tokens_in + tokens_out,
                },
                "metrics": {"latencyMs": 1},
            }
        }


class FakeSessionManager(SessionManager):
    """Keeps the messages in a list; records how it was opened and that it was closed."""

    def __init__(self, memory_id, actor_id, session_id, region):
        self.opened_with = (memory_id, actor_id, session_id, region)
        self.messages, self.closed = [], False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def initialize(self, agent, **kwargs):
        pass

    def append_message(self, message, agent, **kwargs):
        self.messages.append(message)

    def redact_latest_message(self, redact_message, agent, **kwargs):
        pass

    def sync_agent(self, agent, **kwargs):
        pass


class SessionManagers:
    """Stands in for memory.session_manager and keeps what it built."""

    def __init__(self):
        self.built = []

    def __call__(self, *args):
        self.built.append(FakeSessionManager(*args))
        return self.built[-1]


def bearer(claims) -> dict[str, str]:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return {"Authorization": f"Bearer header.{body}.signature"}


@pytest.fixture
def context():
    """What the Runtime passes to the entry point: the session id and the allowed headers."""
    return SimpleNamespace(session_id="session-" + "1" * 30, request_headers=bearer({"sub": "u-1"}))


@pytest.fixture
def managers():
    return SessionManagers()


@pytest.fixture
def agent_env(monkeypatch):
    monkeypatch.setenv("MEMORY_ID", "mem-1")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")


def collect(stream) -> list:
    """Run an async generator to its end and return what it yielded."""

    async def run():
        return [item async for item in stream]

    return asyncio.run(run())


def collect_events(stream) -> list:
    """collect() for the agent's answer: every event must have exactly the fields of its type
    (team_assistant.events.EVENT_FIELDS), the format the page and its mock agent rely on."""
    from team_assistant.events import matches

    events = collect(stream)
    for event in events:
        assert matches(event), event
    return events


@pytest.fixture(autouse=True)
def titles_stored(monkeypatch):
    """No test reaches AWS for a title: app._store_title is replaced by a recorder."""
    calls = []
    monkeypatch.setattr("team_assistant.app._store_title", lambda *args: calls.append(args))
    return calls
