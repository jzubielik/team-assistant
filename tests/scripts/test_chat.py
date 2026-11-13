"""scripts/chat.py: the request to the Runtime and the stream that comes back."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ta_chat", ROOT / "scripts" / "chat.py")
chat = importlib.util.module_from_spec(_spec)
sys.modules["ta_chat"] = chat
_spec.loader.exec_module(chat)

ARN = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/team_assistant_dev_runtime-abc"
GATEWAY = "https://team-assistant-dev-gateway-abc.gateway.bedrock-agentcore.eu-west-1.amazonaws.com"
ARGS = [
    "--gateway-url", GATEWAY, "--gateway-target", "agent",
    "--login-domain", "https://login.example.com",
    "--client-id", "client-1", "--env", "dev", "--user", "alice",
]  # fmt: skip


@pytest.fixture
def logged_in(monkeypatch):
    monkeypatch.setattr(chat.auth, "access_token", lambda *args, **kwargs: "token-1")


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("unexpected request")

    monkeypatch.setattr(chat.urllib.request, "urlopen", refuse)


def test_invoke_url_is_the_gateways_target():
    assert chat.invoke_url(GATEWAY, "agent") == f"{GATEWAY}/agent/invocations"
    # Only the origin of the stack output is used: a gateway address may end in a path.
    assert chat.invoke_url(GATEWAY + "/mcp", "agent") == f"{GATEWAY}/agent/invocations"
    for bad in ("http://gateway.example.com", "gateway.example.com", ""):
        with pytest.raises(chat.ChatError, match="gateway"):
            chat.invoke_url(bad, "agent")


def test_runtime_url_is_the_direct_address_of_the_runtime():
    url = chat.runtime_url("eu-west-1", ARN)
    assert url.startswith("https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/arn%3Aaws%3A")
    assert url.endswith("%2Fteam_assistant_dev_runtime-abc/invocations?qualifier=DEFAULT")


def test_stop_url_goes_through_the_gateway():
    # The Runtime accepts only the gateway, so a session is stopped through it too
    # (<gateway>/<target>/stopruntimesession).
    assert chat.stop_url(GATEWAY, "agent") == f"{GATEWAY}/agent/stopruntimesession"
    with pytest.raises(chat.ChatError, match="gateway"):
        chat.stop_url("http://gateway.example.com", "agent")


def test_new_session_ids_are_long_enough_and_differ():
    first, second = chat.new_session_id(), chat.new_session_id()
    assert len(first) >= chat.MIN_SESSION_ID_LEN and first != second
    assert chat.check_session_id(first) == first


@pytest.mark.parametrize("session", ["short", "x" * 32, "x" * 257])
def test_a_session_id_of_the_wrong_length_stops_before_any_request(
    session, logged_in, no_network, capsys
):
    assert chat.main([*ARGS, "--session", session, "--prompt", "hi"]) == 1
    assert "SESSION must have 33 to 256 characters" in capsys.readouterr().err


def test_sse_parsing_takes_data_lines_and_skips_the_rest():
    lines = [
        b'data: {"type": "text", "data": "Hel"}\n',
        b"\n",
        b": keep-alive comment\n",
        b"event: message\n",
        b'data:{"type": "text", "data": "lo"}\r\n',
        b"data: not json\n",
        b'data: ["a list, not an event"]\n',
        b'data: {"type": "done", "usage": {"input": 3, "output": 2}}\n',
    ]
    assert list(chat.parse_sse(lines)) == [
        {"type": "text", "data": "Hel"},
        {"type": "text", "data": "lo"},
        {"type": "done", "usage": {"input": 3, "output": 2}},
    ]


def test_request_carries_the_token_the_session_and_the_prompt(monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["body"] = json.loads(request.data)
        return Response(b'data: {"type": "text", "data": "ok"}\n')

    monkeypatch.setattr(chat.urllib.request, "urlopen", fake_urlopen)
    session = "session-" + "1" * 30
    events = list(chat.invoke("https://agent.example.com/x", "token-1", session, "hello"))
    assert events == [{"type": "text", "data": "ok"}]
    assert seen["headers"]["authorization"] == "Bearer token-1"
    assert seen["headers"][chat.SESSION_HEADER.lower()] == session
    assert seen["headers"]["content-type"] == "application/json"
    assert seen["body"] == {"prompt": "hello"}


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_is_reported_as_not_authorised_with_the_services_text(monkeypatch, status):
    def refuse(request, timeout):
        body = io.BytesIO(json.dumps({"message": "Token has expired"}).encode())
        raise urllib.error.HTTPError(request.full_url, status, "refused", {}, body)

    monkeypatch.setattr(chat.urllib.request, "urlopen", refuse)
    with pytest.raises(chat.ChatError) as error:
        list(chat.invoke("https://agent.example.com/x", "token-1", "s" * 33, "hello"))
    assert "not authorised" in str(error.value) and "Token has expired" in str(error.value)
    assert "token-1" not in str(error.value)


def test_missing_login_prints_the_hint_and_exits_1(monkeypatch, no_network, capsys):
    def need_login(*args, **kwargs):
        raise chat.auth.LoginRequired(
            "'alice' is not logged in; run: make login ENV=dev USER=alice"
        )

    monkeypatch.setattr(chat.auth, "access_token", need_login)
    assert chat.main([*ARGS, "--prompt", "hi"]) == 1
    assert "make login ENV=dev USER=alice" in capsys.readouterr().err


def test_one_prompt_prints_the_session_the_answer_and_the_usage(monkeypatch, logged_in, capsys):
    events = [
        {"type": "text", "data": "Hello"},
        {"type": "text", "data": ", Alice"},
        {"type": "done", "usage": {"input": 11, "output": 7}},
    ]
    monkeypatch.setattr(chat, "invoke", lambda *args: iter(events))
    assert chat.main([*ARGS, "--prompt", "hi"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("session: session-")
    assert "Hello, Alice\n[tokens: 11 in, 7 out]" in out


def test_an_error_event_makes_the_single_prompt_exit_1(monkeypatch, logged_in, capsys):
    monkeypatch.setattr(chat, "invoke", lambda *args: iter([{"type": "error", "message": "no"}]))
    assert chat.main([*ARGS, "--prompt", "hi"]) == 1
    assert "error: no" in capsys.readouterr().out


URI = "urn:ietf:params:oauth:request_uri:abc"


def test_the_consent_session_is_read_from_the_return_and_the_state_must_match():
    path = f"/github/callback?session_id={URI}&state=s-1"
    assert chat.session_from_return(path, "s-1") == URI
    with pytest.raises(chat.ChatError, match="different state"):
        chat.session_from_return(path, "s-2")


def test_a_return_without_state_is_accepted_with_a_warning(capsys):
    assert chat.session_from_return(f"/github/callback?session_id={URI}", "s-1") == URI
    assert "carried no state" in capsys.readouterr().err


def test_a_return_without_a_session_names_the_parameters_it_carried():
    with pytest.raises(chat.ChatError, match=r"\['code', 'state'\]"):
        chat.session_from_return("/github/callback?code=x&state=s-1", "s-1")


def test_a_consent_request_sends_the_body_and_no_prompt(monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    def fake_urlopen(request, timeout):
        seen["body"] = json.loads(request.data)
        return Response(b'data: {"type": "consent_done"}\n')

    monkeypatch.setattr(chat.urllib.request, "urlopen", fake_urlopen)
    body = {"consent": {"session_uri": URI}}
    events = list(chat.invoke("https://a.example/x", "t", "s" * 40, None, body=body))
    assert seen["body"] == body and events == [{"type": "consent_done"}]


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([{"type": "consent_done"}], "consent recorded"),
        ([{"type": "error", "message": "nope"}], "consent failed: nope"),
        ([], "consent failed: no answer"),
    ],
)
def test_forward_consent_reports_one_sentence(events, expected):
    sent = []
    assert chat.forward_consent(lambda body: sent.append(body) or events, URI) == expected
    assert sent == [{"consent": {"session_uri": URI}}]


def test_an_auth_url_event_is_printed_and_handed_to_the_consent_handler():
    out, handed = io.StringIO(), []
    events = [{"type": "auth_url", "url": "https://github.example/x", "state": "s-1"}]
    chat.print_answer(events, out=out, on_auth_url=lambda url, state: handed.append((url, state)))
    assert handed == [("https://github.example/x", "s-1")]
