"""The actor id is the ``sub`` claim of the bearer token; anything else is an error."""

from __future__ import annotations

import base64
import json

import pytest

from team_assistant.identity import IdentityError, actor_from_headers, bearer_from_headers


def b64(claims) -> str:
    """base64url of the JSON claims, without padding, as in a JWT."""
    raw = json.dumps(claims).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def bearer(claims) -> dict[str, str]:
    return {"Authorization": f"Bearer header.{b64(claims)}.signature"}


def test_returns_sub():
    assert actor_from_headers(bearer({"sub": "u-123", "aud": "x"})) == "u-123"


@pytest.mark.parametrize("name", ["authorization", "AUTHORIZATION", "Authorization"])
def test_header_name_is_case_insensitive(name):
    headers = {name: f"Bearer a.{b64({'sub': 'u-123'})}.c"}
    assert actor_from_headers(headers) == "u-123"


def test_payload_length_that_needs_padding_is_decoded():
    for sub in ("a", "ab", "abc", "abcd"):
        assert actor_from_headers(bearer({"sub": sub})) == sub


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"Authorization": ""},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer abc"},
        {"Authorization": "Bearer a.b"},
        {"Authorization": "Bearer a.b.c.d"},
        {"Authorization": "Bearer a..c"},
        {"Authorization": "Bearer a.!!!.c"},
        {"Authorization": "Bearer a." + b64(["sub"]) + ".c"},
        {"Authorization": "Bearer a." + b64({"aud": "x"}) + ".c"},
        {"Authorization": "Bearer a." + b64({"sub": ""}) + ".c"},
        {"Authorization": "Bearer a." + b64({"sub": 5}) + ".c"},
        {"Authorization": "Bearer a." + b64({"sub": None}) + ".c"},
        {"X-Other": "Bearer a." + b64({"sub": "u-123"}) + ".c"},
    ],
)
def test_rejects(headers):
    with pytest.raises(IdentityError):
        actor_from_headers(headers)


@pytest.mark.parametrize(
    "value",
    [
        "Bearer SECRETHEADER.SECRETPAYLOAD.SECRETSIGNATURE",
        "Basic SECRETCREDENTIALS",
        "Bearer SECRETHEADER." + b64({"aud": "SECRETCLAIM"}) + ".SECRETSIGNATURE",
    ],
)
def test_error_message_never_contains_the_token(value):
    with pytest.raises(IdentityError) as error:
        actor_from_headers({"Authorization": value})
    message = str(error.value)
    assert "SECRET" not in message
    assert value.split(" ", 1)[1] not in message


def test_bearer_from_headers_returns_the_token_unchanged():
    token = f"header.{b64({'sub': 'u-1'})}.signature"
    assert bearer_from_headers({"Authorization": f"Bearer {token}"}) == token


def test_bearer_from_headers_refuses_what_actor_from_headers_refuses():
    with pytest.raises(IdentityError, match="missing"):
        bearer_from_headers({})
    with pytest.raises(IdentityError, match="three"):
        bearer_from_headers({"Authorization": "Bearer abc"})
