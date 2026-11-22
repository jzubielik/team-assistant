"""guard.py: the results of the GitHub tools reach the model only after the guardrail."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from team_assistant import guard

RESULT = {"toolUseId": "t-1", "status": "success", "content": [{"text": "#1 A bug"}]}


class Client:
    def __init__(self, action="NONE", error=None):
        self.action, self.error, self.calls = action, error, []

    def apply_guardrail(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"action": self.action}


def check(client, tool="list_issues", guardrail_id="g-1", result=RESULT, cancelled=None):
    event = SimpleNamespace(tool_use={"name": tool}, result=dict(result), cancel_message=cancelled)
    asyncio.run(guard.guard_hook(lambda: client, guardrail_id)(event))
    return event.result


def test_a_result_the_guardrail_lets_through_reaches_the_model_unchanged():
    client = Client("NONE")
    assert check(client) == RESULT
    (call,) = client.calls
    assert call["guardrailIdentifier"] == "g-1" and call["source"] == "INPUT"
    assert call["content"] == [{"text": {"text": "#1 A bug"}}]


def test_a_result_the_guardrail_intervenes_on_is_replaced():
    result = check(Client("GUARDRAIL_INTERVENED"))
    assert result["content"] == [{"text": guard.REFUSED}] and result["status"] == "error"


def test_a_failed_check_withholds_the_result():
    result = check(Client(error=RuntimeError("down")))
    assert result["content"] == [{"text": guard.UNCHECKED}]


def test_without_a_guardrail_id_the_result_is_withheld_and_nothing_is_called():
    client = Client()
    assert check(client, guardrail_id=None)["content"] == [{"text": guard.UNCHECKED}]
    assert client.calls == []


def test_other_tools_are_not_checked():
    client = Client("GUARDRAIL_INTERVENED")
    assert check(client, tool="search_memory") == RESULT
    assert client.calls == []


def test_a_result_without_text_and_a_cancelled_call_are_not_checked():
    client = Client("GUARDRAIL_INTERVENED")
    empty = {"toolUseId": "t-1", "status": "success", "content": []}
    assert check(client, result=empty) == empty
    # The result of a cancelled call is the canceller's text, not the tool's.
    assert check(client, cancelled="Refused: the arguments do not fit.") == RESULT
    assert client.calls == []


def test_one_client_serves_every_result_of_a_request():
    made, client = [], Client("NONE")
    hook = guard.guard_hook(lambda: made.append(1) or client, "g-1")
    for _ in range(3):
        event = SimpleNamespace(tool_use={"name": "get_issue"}, result=dict(RESULT))
        asyncio.run(hook(event))
    assert len(made) == 1 and len(client.calls) == 3
