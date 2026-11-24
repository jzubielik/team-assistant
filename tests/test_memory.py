"""memory.py: one conversation history per (memory, user, session), reached through the
Memory gateway with the user's token."""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from botocore.awsrequest import AWSResponse

from team_assistant import memory

MEMORY_ID = "team_assistant_dev_memory-0123456789"
ACTOR = "11111111-2222-3333-4444-555555555555"
SESSION = "s" * 33


class _Body:
    def __init__(self):
        self.data = b"{}"

    def stream(self, *args, **kwargs):
        yield self.read()

    def read(self, *args, **kwargs):
        data, self.data = self.data, b""
        return data


def sent(client) -> list:
    """Make ``client`` answer every request with {} and return the list its requests go to."""
    requests = []

    def answer(request, **kwargs):
        requests.append(request)
        return AWSResponse(request.url, 200, {"content-type": "application/json"}, _Body())

    client.meta.events.register("before-send.bedrock-agentcore.*", answer)
    return requests


@pytest.mark.parametrize(
    "gateway_url", ["https://gw-1.gateway.example", "https://gw-1.gateway.example/mcp"]
)
def test_gateway_client_calls_the_gateways_memory_target_with_the_users_token(gateway_url):
    client = memory.gateway_client(gateway_url, "eu-west-1", "TOKEN")
    requests = sent(client)
    client.list_events(memoryId=MEMORY_ID, actorId=ACTOR, sessionId=SESSION)
    client.retrieve_memory_records(
        memoryId=MEMORY_ID, namespacePath=f"/facts/{ACTOR}/", searchCriteria={"searchQuery": "q"}
    )
    client.create_event(
        memoryId=MEMORY_ID,
        actorId=ACTOR,
        sessionId=SESSION,
        eventTimestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        payload=[{"blob": "x"}],
    )
    base = f"https://gw-1.gateway.example/memory/memories/{MEMORY_ID}"
    assert [(r.method, r.url) for r in requests] == [
        ("POST", f"{base}/actor/{ACTOR}/sessions/{SESSION}"),
        ("POST", f"{base}/retrieve"),
        ("POST", f"{base}/events"),
    ]
    for request in requests:
        assert request.headers["Authorization"] == "Bearer TOKEN"
        # Not signed with the agent's role: the gateway takes the token only.
        assert "X-Amz-Date" not in request.headers
        assert "X-Amz-Security-Token" not in request.headers


def test_session_manager_is_built_from_the_three_ids_and_uses_the_given_client(monkeypatch):
    built = []

    class Fake:
        def __init__(self, config, region_name=None, boto_session=None):
            built.append((config, region_name, boto_session))

    monkeypatch.setattr(memory, "AgentCoreMemorySessionManager", Fake)
    client = object()
    manager = memory.session_manager("mem-1", "user-1", "session-1", "eu-west-1", client)
    assert isinstance(manager, Fake)
    ((config, region, session),) = built
    assert (config.memory_id, config.actor_id, config.session_id, region) == (
        "mem-1",
        "user-1",
        "session-1",
        "eu-west-1",
    )
    assert session.client("bedrock-agentcore", region_name="eu-west-1") is client
    assert session.client("bedrock-agentcore-control", region_name="eu-west-1") is not client
    # Every message is written at once: nothing is lost when the runtime session ends.
    assert config.batch_size == 1
    # Nothing is searched before a model call.
    assert config.retrieval_config is None


def test_a_new_sessions_calls_name_the_user_or_an_actor_the_gateway_lets_everyone_read():
    """The actors the access rules must allow (stack.py, _memory_rules): the user, and for
    one read the SDK's earlier layout."""

    class Recording:
        def __init__(self):
            self.calls = []
            self.meta = SimpleNamespace(region_name="eu-west-1")

        def list_events(self, **kwargs):
            self.calls.append(("list_events", kwargs["actorId"]))
            return {"events": []}

        def create_event(self, **kwargs):
            self.calls.append(("create_event", kwargs["actorId"]))
            return {"event": {"eventId": "e-1", "eventTimestamp": kwargs["eventTimestamp"]}}

    client = Recording()
    memory.session_manager(MEMORY_ID, ACTOR, SESSION, "eu-west-1", client)
    assert ("list_events", ACTOR) in client.calls and ("create_event", ACTOR) in client.calls
    others = {call for call in client.calls if call[1] != ACTOR}
    assert others == {("list_events", f"session_{SESSION}")}


def test_the_session_managers_first_read_goes_through_the_given_client():
    class Refusing:
        def __init__(self):
            self.asked = []
            self.meta = SimpleNamespace(region_name="eu-west-1")

        def __getattr__(self, name):
            def call(**kwargs):
                self.asked.append(name)
                raise ConnectionError(name)

            return call

    client = Refusing()
    with pytest.raises(Exception):  # noqa: B017, PT011 - whatever the SDK makes of the refusal
        memory.session_manager(MEMORY_ID, ACTOR, SESSION, "eu-west-1", client)
    assert client.asked and client.asked[0] == "list_events"
