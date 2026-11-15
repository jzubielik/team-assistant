"""The conversation title: one short model call after the first answer, stored once in Memory."""

from __future__ import annotations

import json

import pytest
from bedrock_agentcore.memory.integrations.strands.bedrock_converter import (
    AgentCoreMemoryConverter,
)

from team_assistant import conversations, titles


class Converse:
    def __init__(self, reply: str):
        self.reply, self.calls = reply, []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"role": "assistant", "content": [{"text": self.reply}]}}}


class Memory:
    def __init__(self, title: str | None = None):
        self.title, self.created = title, []

    def list_events(self, **kwargs):
        blob = json.dumps({"title": self.title})
        return {"events": [{"payload": [{"blob": blob}]}] if self.title else []}

    def create_event(self, **kwargs):
        self.created.append(kwargs)
        return {"event": {"eventId": "e-1"}}


@pytest.mark.parametrize(
    ("reply", "title"),
    [
        ("Issues in agentcore-sdk", "Issues in agentcore-sdk"),
        ('"Release checklist."', "Release checklist"),
        ("'Word Memory Test'.", "Word Memory Test"),
        ('**"Issues!"**', "Issues"),
        ("Title: Memory question!", "Memory question"),
        ("  Open\nissues   of the SDK  ", "Open issues of the SDK"),
        ("one two three four five six seven eight", "one two three four five six"),
        ("x" * 80, "x" * 60),
    ],
)
def test_the_models_reply_is_cut_to_a_short_title(reply, title):
    assert titles.make_title(Converse(reply), "model-1", "q", "a") == title


def test_the_title_call_is_one_short_request_with_the_question_and_the_answer_start():
    converse = Converse("A title")
    titles.make_title(converse, "model-1", "What open issues?", "There are 12. " * 200)
    (call,) = converse.calls
    assert call["modelId"] == "model-1"
    assert call["inferenceConfig"]["maxTokens"] <= 30
    text = call["messages"][0]["content"][0]["text"]
    assert "What open issues?" in text
    assert len(text) < 2000


def test_an_empty_reply_gives_no_title():
    assert titles.make_title(Converse('  ""  '), "model-1", "q", "a") == ""


def test_the_title_is_stored_as_a_blob_marked_by_metadata():
    memory = Memory()
    titles.store_title(memory, "mem-1", "u-1", "session-1", "Release checklist")
    (event,) = memory.created
    assert (event["memoryId"], event["actorId"], event["sessionId"]) == (
        "mem-1",
        "u-1",
        "session-1",
    )
    assert event["payload"] == [{"blob": json.dumps({"title": "Release checklist"})}]
    assert event["metadata"] == {"kind": {"stringValue": conversations.TITLE_KIND}}
    assert event["eventTimestamp"].tzinfo is not None


def test_a_session_with_a_title_is_not_titled_again():
    memory = Memory(title="Old title")
    converse = Converse("New title")
    titles.title_once(memory, converse, "model-1", "mem-1", "u-1", "session-1", "q", "a")
    assert converse.calls == [] and memory.created == []


def test_a_session_without_a_title_gets_one():
    memory = Memory()
    titles.title_once(
        memory, Converse("Word game"), "model-1", "mem-1", "u-1", "session-1", "q", "a"
    )
    assert json.loads(memory.created[0]["payload"][0]["blob"]) == {"title": "Word game"}


def test_the_session_manager_does_not_read_the_title_back_as_a_message():
    event = {"payload": [{"blob": json.dumps({"title": "Release checklist"})}]}
    assert AgentCoreMemoryConverter.events_to_messages([event]) == []
