"""Log in to the environment's Cognito user pool from a terminal and keep the tokens.

    python scripts/auth.py --login-domain <url> --client-id <id> --audience <url> --env <env> \
        --user <name>

Login is the OAuth authorization code flow with PKCE: the browser opens the hosted login page,
the user signs in there, and Cognito sends the browser back to a listener on this machine with
a one-time code. The script exchanges the code for tokens and caches them. The password is
typed only into Cognito's page; this script never sees it. The login asks for an access token
bound to the agent (the stack output Audience); the token carries it as the aud claim, which the
agent's side requires.

Standard library only, so it runs on the host without the project's dependencies: the
listener and the browser must be on the same machine.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

PORT = 8976
REDIRECT_URI = f"http://localhost:{PORT}/callback"
CACHE_ROOT = Path.home() / ".config" / "team-assistant"
# Refresh when the access token has less than this many seconds left.
MIN_VALIDITY = 60
LOGIN_TIMEOUT = 300
USERNAME = re.compile(r"[a-z][a-z0-9._-]{0,31}")
ENV = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")


@dataclass(frozen=True)
class Endpoints:
    """From the stack outputs LoginDomain and UserPoolClientId."""

    login_domain: str
    client_id: str


class LoginError(Exception):
    """The login in the browser did not complete."""


class LoginRequired(Exception):  # noqa: N818 - a state the caller reports, not a failure
    """No usable token is cached; the message says which command to run."""


class TokenError(Exception):
    """The token endpoint refused the request or could not be reached."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def pkce_pair() -> tuple[str, str]:
    """(verifier, challenge): the challenge is base64url(SHA-256(verifier)) without padding."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(ep: Endpoints, challenge: str, state: str, audience: str) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": ep.client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": "openid",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # RFC 8707 resource indicator: Cognito puts it into the access token as aud.
            "resource": audience,
        }
    )
    return f"{ep.login_domain}/oauth2/authorize?{query}"


def code_from_callback(path: str, expected_state: str) -> str:
    """The authorization code from the callback request; LoginError when it is not ours."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    # The state proves the callback answers the request this process started.
    if not secrets.compare_digest(query.get("state", [""])[0], expected_state):
        raise LoginError("the callback carried a different state; login refused")
    if "error" in query:
        raise LoginError(f"the login page reported: {query['error'][0]}")
    code = query.get("code", [""])[0]
    if not code:
        raise LoginError("the callback carried no code")
    return code


def wait_for_code(state: str, timeout: float = LOGIN_TIMEOUT) -> str:
    """Listen on 127.0.0.1 only, until the callback arrives or the time is up."""
    result: dict[str, object] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - name required by http.server
            if urllib.parse.urlsplit(self.path).path != "/callback":
                self.send_error(404)  # e.g. the browser asking for /favicon.ico
                return
            try:
                result["code"] = code_from_callback(self.path, state)
                body = "Logged in. You can close this tab."
            except LoginError as error:
                result["error"] = error
                body = "Login failed. See the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):  # the request line contains the code: keep it out of logs
            pass

    deadline = time.monotonic() + timeout
    with http.server.HTTPServer(("127.0.0.1", PORT), Handler) as server:
        while not result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LoginError(f"no login within {int(timeout)} seconds")
            server.timeout = remaining
            server.handle_request()
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return str(result["code"])


def _post_form(url: str, fields: dict[str, str]) -> dict:
    """POST a form to the token endpoint. TokenError carries only the OAuth error code."""
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=data, method="POST")  # noqa: S310 - https URL
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            code = json.load(error).get("error", "")
        except ValueError:
            code = ""
        raise TokenError(code or f"http_{error.code}") from None
    except (urllib.error.URLError, OSError):
        raise TokenError("unreachable") from None


def _claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def _cache_path(env: str, user: str) -> Path:
    # Both become path components: refuse anything that is not a plain name.
    if not ENV.fullmatch(env) or not USERNAME.fullmatch(user):
        raise ValueError("invalid ENV or USER")
    return CACHE_ROOT / env / f"{user}.json"


def _write_cache(path: Path, content: dict) -> None:
    """Directory 0700, file 0600: the tokens are readable by this OS user only."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (path.parent.parent, path.parent):
        os.chmod(directory, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(content, handle)
    os.chmod(path, 0o600)


def _login_hint(env: str, user: str) -> str:
    return f"run: make login ENV={env} USER={user}"


def login(ep: Endpoints, env: str, user: str, audience: str, open_browser=webbrowser.open) -> float:
    """Log ``user`` in through the browser and cache the tokens. Returns the expiry time."""
    path = _cache_path(env, user)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    url = authorize_url(ep, challenge, state, audience)
    if not open_browser(url):
        print(f"Open this address in a browser on this machine:\n{url}")
    code = wait_for_code(state)
    try:
        tokens = _post_form(
            f"{ep.login_domain}/oauth2/token",
            {
                "grant_type": "authorization_code",
                "client_id": ep.client_id,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
    except TokenError as error:
        raise LoginError(f"the token endpoint refused the code ({error.code})") from None
    # The browser may still hold another user's session: the cache file is named after USER,
    # so it must not receive someone else's tokens.
    names = {
        _claims(tokens.get("id_token", "")).get("cognito:username"),
        _claims(tokens.get("access_token", "")).get("username"),
    } - {None}
    if names and user not in names:
        raise LoginError(
            f"the browser logged in as another user, not '{user}'; nothing saved. "
            "Sign out in the browser (or use a private window) and run make login again"
        )
    expires_at = time.time() + int(tokens["expires_in"])
    _write_cache(
        path,
        {
            "client_id": ep.client_id,
            "access_token": tokens["access_token"],
            "id_token": tokens["id_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": expires_at,
        },
    )
    return expires_at


def access_token(ep: Endpoints, env: str, user: str, now=time.time) -> str:
    """The cached access token, refreshed when it is about to expire. LoginRequired otherwise."""
    path = _cache_path(env, user)
    hint = _login_hint(env, user)
    try:
        cache = json.loads(path.read_text())
    except (OSError, ValueError):
        raise LoginRequired(f"'{user}' is not logged in to env {env}; {hint}") from None
    if cache.get("client_id") != ep.client_id:
        # The environment was destroyed and deployed again: a new user pool and app client.
        raise LoginRequired(f"the saved login of '{user}' is for an earlier deployment; {hint}")
    if cache["expires_at"] - now() > MIN_VALIDITY:
        return cache["access_token"]
    try:
        tokens = _post_form(
            f"{ep.login_domain}/oauth2/token",
            {
                "grant_type": "refresh_token",
                "client_id": ep.client_id,
                "refresh_token": cache["refresh_token"],
            },
        )
    except TokenError as error:
        reason = "could not reach the login domain" if error.code == "unreachable" else "expired"
        raise LoginRequired(f"the login of '{user}' {reason} ({error.code}); {hint}") from None
    cache.update(
        access_token=tokens["access_token"],
        id_token=tokens.get("id_token", cache["id_token"]),
        expires_at=now() + int(tokens["expires_in"]),
    )
    _write_cache(path, cache)
    return cache["access_token"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--login-domain", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="print the login address instead of opening the browser (for a private window)",
    )
    args = parser.parse_args(argv)
    # Returning False makes login() print the address.
    open_browser = (lambda url: False) if args.print_url else webbrowser.open
    try:
        endpoints = Endpoints(args.login_domain, args.client_id)
        expires_at = login(endpoints, args.env, args.user, args.audience, open_browser=open_browser)
    except (LoginError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: could not listen on 127.0.0.1:{PORT}: {error.strerror}", file=sys.stderr)
        return 1
    until = time.strftime("%H:%M", time.localtime(expires_at))
    print(f"logged in as {args.user}, token valid until {until}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
