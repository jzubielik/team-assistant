"""scripts/auth.py: PKCE login, the token cache and its refresh."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import stat
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ta_auth", ROOT / "scripts" / "auth.py")
auth = importlib.util.module_from_spec(_spec)
sys.modules["ta_auth"] = auth  # dataclasses looks the module up by name
_spec.loader.exec_module(auth)

EP = auth.Endpoints("https://login.example.com", "client-1")
AUDIENCE = "https://team-assistant-dev.agent"
NOW = 1_000_000.0


def jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


@pytest.fixture(autouse=True)
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "CACHE_ROOT", tmp_path / "cache")
    return tmp_path / "cache"


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("unexpected network call")

    monkeypatch.setattr(auth, "_post_form", refuse)


def save(user="alice", *, expires_at=NOW + 3600, client_id="client-1"):
    auth._write_cache(
        auth._cache_path("dev", user),
        {
            "client_id": client_id,
            "access_token": "access-old",
            "id_token": "id-old",
            "refresh_token": "refresh-1",
            "expires_at": expires_at,
        },
    )


def test_pkce_challenge_is_the_sha256_of_the_verifier():
    verifier, challenge = auth.pkce_pair()
    assert 43 <= len(verifier) <= 128
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert "=" not in challenge
    assert auth.pkce_pair()[0] != verifier


def test_authorize_url():
    url = urllib.parse.urlsplit(auth.authorize_url(EP, "challenge-1", "state-1", AUDIENCE))
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://login.example.com/oauth2/authorize"
    assert dict(urllib.parse.parse_qsl(url.query)) == {
        "response_type": "code",
        "client_id": "client-1",
        "redirect_uri": "http://localhost:8976/callback",
        "scope": "openid",
        "state": "state-1",
        "code_challenge": "challenge-1",
        "code_challenge_method": "S256",
        # The access token is bound to this resource: it comes back as the aud claim.
        "resource": AUDIENCE,
    }


def test_callback_returns_the_code_for_the_right_state():
    assert auth.code_from_callback("/callback?code=abc&state=state-1", "state-1") == "abc"


@pytest.mark.parametrize(
    "path",
    [
        "/callback?code=abc&state=other",
        "/callback?code=abc",
        "/callback?state=state-1",
        "/callback?error=access_denied&state=state-1",
    ],
)
def test_callback_with_a_wrong_state_an_error_or_no_code_is_refused(path):
    with pytest.raises(auth.LoginError):
        auth.code_from_callback(path, "state-1")


def test_cache_is_private(cache_root):
    save()
    path = cache_root / "dev" / "alice.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(cache_root.stat().st_mode) == 0o700


def test_fresh_token_is_returned_without_a_network_call(no_network):
    save()
    assert auth.access_token(EP, "dev", "alice", now=lambda: NOW) == "access-old"


def test_expiring_token_is_refreshed_once_and_saved(monkeypatch, cache_root):
    save(expires_at=NOW + 30)
    calls = []

    def token_endpoint(url, fields):
        calls.append((url, fields))
        return {"access_token": "access-new", "id_token": "id-new", "expires_in": 3600}

    monkeypatch.setattr(auth, "_post_form", token_endpoint)
    assert auth.access_token(EP, "dev", "alice", now=lambda: NOW) == "access-new"
    assert calls == [
        (
            "https://login.example.com/oauth2/token",
            {"grant_type": "refresh_token", "client_id": "client-1", "refresh_token": "refresh-1"},
        )
    ]
    saved = json.loads((cache_root / "dev" / "alice.json").read_text())
    assert saved["access_token"] == "access-new"
    assert saved["refresh_token"] == "refresh-1"
    assert saved["expires_at"] == NOW + 3600


@pytest.mark.parametrize("code", ["invalid_grant", "invalid_client", "unreachable"])
def test_failed_refresh_says_how_to_log_in_again(monkeypatch, code):
    save(expires_at=NOW - 10)

    def token_endpoint(url, fields):
        raise auth.TokenError(code)

    monkeypatch.setattr(auth, "_post_form", token_endpoint)
    with pytest.raises(auth.LoginRequired) as error:
        auth.access_token(EP, "dev", "alice", now=lambda: NOW)
    assert "make login ENV=dev USER=alice" in str(error.value)
    assert "refresh-1" not in str(error.value)


def test_login_domain_that_does_not_exist_is_reported_as_unreachable(monkeypatch):
    def unreachable(*args, **kwargs):
        raise urllib.error.URLError("name not known")

    monkeypatch.setattr(auth.urllib.request, "urlopen", unreachable)
    with pytest.raises(auth.TokenError) as error:
        auth._post_form("https://gone.example.com/oauth2/token", {"a": "b"})
    assert error.value.code == "unreachable"


def test_no_cache_and_a_cache_of_an_earlier_deployment_need_a_login(no_network):
    with pytest.raises(auth.LoginRequired, match="make login ENV=dev USER=alice"):
        auth.access_token(EP, "dev", "alice")
    save(client_id="client-of-a-destroyed-pool")
    with pytest.raises(auth.LoginRequired, match="earlier deployment"):
        auth.access_token(EP, "dev", "alice", now=lambda: NOW)


@pytest.mark.parametrize(
    ("env", "user"), [("dev", "../alice"), ("dev", "Alice"), ("../x", "alice")]
)
def test_names_that_are_not_plain_are_refused(env, user, no_network):
    with pytest.raises(ValueError):
        auth.access_token(EP, env, user)


def tokens_for(username):
    return {
        "access_token": jwt({"username": username}),
        "id_token": jwt({"cognito:username": username}),
        "refresh_token": "refresh-1",
        "expires_in": 3600,
    }


def test_login_exchanges_the_code_with_the_verifier_and_saves_the_tokens(monkeypatch, cache_root):
    opened, posted = [], []
    monkeypatch.setattr(auth, "wait_for_code", lambda state: "code-1")

    def token_endpoint(url, fields):
        posted.append(fields)
        return tokens_for("alice")

    monkeypatch.setattr(auth, "_post_form", token_endpoint)
    auth.login(EP, "dev", "alice", AUDIENCE, open_browser=lambda url: opened.append(url) or True)
    (fields,) = posted
    assert fields["grant_type"] == "authorization_code"
    assert fields["code"] == "code-1"
    assert fields["redirect_uri"] == "http://localhost:8976/callback"
    assert "client_secret" not in fields
    # The resource goes only to the login page; a refreshed token keeps its aud (docs/adr/0002).
    assert "resource" not in fields
    assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))["resource"] == (
        AUDIENCE
    )
    # The verifier sent to the token endpoint matches the challenge sent to the login page.
    challenge = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))[
        "code_challenge"
    ]
    digest = hashlib.sha256(fields["code_verifier"].encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    saved = json.loads((cache_root / "dev" / "alice.json").read_text())
    assert saved["refresh_token"] == "refresh-1" and saved["client_id"] == "client-1"


def test_login_as_another_user_saves_nothing(monkeypatch, cache_root):
    monkeypatch.setattr(auth, "wait_for_code", lambda state: "code-1")
    monkeypatch.setattr(auth, "_post_form", lambda url, fields: tokens_for("bob"))
    with pytest.raises(auth.LoginError, match="another user"):
        auth.login(EP, "dev", "alice", AUDIENCE, open_browser=lambda url: True)
    assert not (cache_root / "dev" / "alice.json").exists()
