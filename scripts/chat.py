"""Talk to the deployed agent from a terminal.

    python scripts/chat.py --gateway-url <url> --gateway-target <name> --login-domain <url> \
        --client-id <id> --env <env> --user <name> [--session <id>] [--prompt <text>]

Without --prompt it is an interactive loop; with it, one question and one answer. Each request
carries the user's access token (scripts/auth.py; run make login first) and a session id. The
agent keeps one conversation history per user and session id, so the same --session continues
an earlier conversation.

Requests go to the environment's AgentCore Gateway (stack output GatewayUrl), which checks the
token and relays the request to the Runtime: <gateway>/<target>/invocations (docs/adr/0004).
Headers and the session id limit: AgentCore developer guide,
"Authenticate and authorize with Inbound Auth and Outbound Auth" (invoke with a bearer token),
and the InvokeAgentRuntime shape in botocore 1.44.2 (runtimeSessionId: 33 to 256 characters).
Standard library only.

GitHub consent: when the agent streams an ``auth_url`` event, this client listens on
--return-url (127.0.0.1 only) before printing the address. After the user's consent in GitHub
the browser returns there with the consent session; the client checks the state the agent sent
and forwards the session to the agent in a separate request with the same user's token
(``{"consent": {"session_uri": ...}}``), in a new runtime session because the first request is
still waiting for the token. The agent completes the binding with its own role.
"""

from __future__ import annotations

import argparse
import http.server
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402

# The agent's tool waits this long for the token
# (bedrock-agentcore 1.26.0, services/identity.py, DEFAULT_POLLING_TIMEOUT_SECONDS).
CONSENT_TIMEOUT = 600
DEFAULT_RETURN_URL = "http://localhost:8977/github/callback"
MIN_SESSION_ID_LEN = 33
MAX_SESSION_ID_LEN = 256
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


class ChatError(Exception):
    """The agent could not be reached or refused the request."""


def new_session_id() -> str:
    """A fresh conversation: a UUID (36 characters) with a readable prefix."""
    return f"session-{uuid.uuid4()}"


def check_session_id(session_id: str) -> str:
    if not MIN_SESSION_ID_LEN <= len(session_id) <= MAX_SESSION_ID_LEN:
        raise ChatError(
            f"SESSION must have {MIN_SESSION_ID_LEN} to {MAX_SESSION_ID_LEN} characters "
            f"(got {len(session_id)}); leave it out to start a new conversation"
        )
    return session_id


def invoke_url(gateway_url: str, target: str, operation: str = "invocations") -> str:
    """The agent's address behind the gateway. Only the origin of ``gateway_url`` is used: the
    address a gateway reports may end in a path of its own."""
    parts = urllib.parse.urlsplit(gateway_url)
    if parts.scheme != "https" or not parts.netloc:
        raise ChatError("the gateway address must be an https URL (stack output GatewayUrl)")
    return f"https://{parts.netloc}/{urllib.parse.quote(target, safe='')}/{operation}"


def parse_sse(lines: Iterable[bytes]) -> Iterator[dict]:
    """The JSON object of every ``data:`` line; blank lines and comments are skipped."""
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[len("data:") :])
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _error_text(error: urllib.error.HTTPError) -> str:
    body = error.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
        return str(data.get("message") or data.get("error") or body)[:300]
    except ValueError:
        return body[:300]


def runtime_url(region: str, runtime_arn: str) -> str:
    """The Runtime's own address, not through the gateway. The smoke test uses it to check
    that a direct call is refused."""
    arn = urllib.parse.quote(runtime_arn, safe="")
    host = f"bedrock-agentcore.{region}.amazonaws.com"
    return f"https://{host}/runtimes/{arn}/invocations?qualifier=DEFAULT"


def stop_url(gateway_url: str, target: str) -> str:
    """Ends a runtime session: its container is stopped; the conversation stays in Memory.
    Through the gateway, which the Runtime requires."""
    return invoke_url(gateway_url, target, "stopruntimesession")


def invoke(
    url: str, token: str | None, session_id: str, prompt: str | None, body: dict | None = None
) -> Iterator[dict]:
    """Send one message; yield the agent's events as they arrive.

    ``token=None`` sends no Authorization header and ``prompt=None`` an empty body: the smoke
    test uses them to check that the Runtime refuses a caller without a token and to call the
    stop-session URL.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        SESSION_HEADER: session_id,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is None:
        body = {} if prompt is None else {"prompt": prompt}
    request = urllib.request.Request(  # noqa: S310 - https URL built by invoke_url
        url, data=json.dumps(body).encode(), method="POST", headers=headers
    )
    try:
        # Silence allowed on the connection: longer than the tool's wait for a consent.
        with urllib.request.urlopen(request, timeout=CONSENT_TIMEOUT + 60) as response:  # noqa: S310
            yield from parse_sse(response)
    except urllib.error.HTTPError as error:
        text = _error_text(error)
        if error.code in (401, 403):
            raise ChatError(f"not authorised (HTTP {error.code}): {text}") from None
        raise ChatError(f"the agent answered HTTP {error.code}: {text}") from None
    except (urllib.error.URLError, OSError) as error:
        raise ChatError(f"could not reach the agent: {getattr(error, 'reason', error)}") from None


def session_from_return(path: str, expected_state: str) -> str:
    """The consent session URI from the browser's return; ChatError when it is not ours."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    state = query.get("state", [None])[0]
    if state is None:
        print("warning: the return carried no state; it cannot be matched", file=sys.stderr)
    elif not secrets.compare_digest(state, expected_state):
        raise ChatError("the return carried a different state; consent not forwarded")
    session = query.get("session_id", [""])[0]
    if not session:
        raise ChatError(f"the return carried no session_id (parameters: {sorted(query)})")
    return session


def consent_listener(return_url: str) -> http.server.HTTPServer:
    """Bound before the consent address is printed, so a quick return is not missed."""
    parts = urllib.parse.urlsplit(return_url)
    return http.server.HTTPServer(
        ("127.0.0.1", parts.port or 80), http.server.BaseHTTPRequestHandler
    )


def wait_for_return(server, return_url: str, state: str, timeout: float = CONSENT_TIMEOUT) -> str:
    """Serve on ``server`` until the browser returns to ``return_url``'s path."""
    path = urllib.parse.urlsplit(return_url).path
    result: dict[str, object] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - name required by http.server
            if urllib.parse.urlsplit(self.path).path != path:
                self.send_error(404)
                return
            try:
                result["session"] = session_from_return(self.path, state)
                text = "Consent received. You can close this tab and return to the terminal."
            except ChatError as error:
                result["error"] = error
                text = "Consent not accepted. See the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(text.encode())

        def log_message(self, *args):  # the request line holds the session: keep it quiet
            pass

    server.RequestHandlerClass = Handler
    deadline = time.monotonic() + timeout
    with server:
        while not result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ChatError(f"no return from GitHub within {int(timeout)} seconds")
            server.timeout = remaining
            server.handle_request()
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return str(result["session"])


def forward_consent(send, session_uri: str) -> str:
    """Send the consent session to the agent; one sentence for the terminal."""
    for event in send({"consent": {"session_uri": session_uri}}):
        if event.get("type") == "consent_done":
            return "consent recorded"
        if event.get("type") == "error" or "error" in event:
            return f"consent failed: {event.get('message') or event.get('error')}"
    return "consent failed: no answer"


def print_answer(events: Iterable[dict], out=None, on_auth_url=None) -> bool:
    """Print the streamed answer. False when the agent reported an error."""
    out = out or sys.stdout
    ok = True
    for event in events:
        if event.get("type") == "auth_url":
            if on_auth_url:
                on_auth_url(event.get("url", ""), event.get("state", ""))
        elif event.get("type") == "text":
            print(event.get("data", ""), end="", flush=True, file=out)
        elif event.get("type") == "done":
            usage = event.get("usage", {})
            print(f"\n[tokens: {usage.get('input', 0)} in, {usage.get('output', 0)} out]", file=out)
        elif event.get("type") == "error" or "error" in event:
            ok = False
            print(f"\nerror: {event.get('message') or event.get('error')}", file=out)
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--gateway-target", required=True)
    parser.add_argument("--login-domain", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--session", default="")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--return-url", default=DEFAULT_RETURN_URL)
    args = parser.parse_args(argv)
    endpoints = auth.Endpoints(args.login_domain, args.client_id)
    try:
        url = invoke_url(args.gateway_url, args.gateway_target)
    except ChatError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    def on_auth_url(consent_url: str, state: str) -> None:
        """Listen first, print the address, then forward the consent from a thread."""
        server = consent_listener(args.return_url)
        print(
            "\nGitHub needs your consent first. Open this address in the browser where you\n"
            f"are logged in to GitHub (valid for 10 minutes):\n\n{consent_url}\n",
            flush=True,
        )

        def complete() -> None:
            try:
                session_uri = wait_for_return(server, args.return_url, state)
                token = auth.access_token(endpoints, args.env, args.user)

                def send(body):
                    return invoke(url, token, new_session_id(), None, body=body)

                print(f"[{forward_consent(send, session_uri)}; waiting for the answer]", flush=True)
            except (ChatError, auth.LoginRequired) as error:
                print(f"\nerror: {error}", file=sys.stderr, flush=True)

        threading.Thread(target=complete, daemon=True).start()

    try:
        # Checked before any request: a wrong session id or a missing login costs nothing.
        session_id = check_session_id(args.session) if args.session else new_session_id()
        auth.access_token(endpoints, args.env, args.user)
        print(f"session: {session_id}")
        if args.prompt:
            token = auth.access_token(endpoints, args.env, args.user)
            events = invoke(url, token, session_id, args.prompt)
            return 0 if print_answer(events, on_auth_url=on_auth_url) else 1
        print("Type a message and press Enter. Empty line or Ctrl-D ends the conversation.")
        while True:
            try:
                prompt = input(f"{args.user}> ").strip()
            except EOFError:
                print()
                return 0
            if not prompt:
                return 0
            # Asked again for every message: the token is refreshed when it is about to expire.
            token = auth.access_token(endpoints, args.env, args.user)
            print_answer(invoke(url, token, session_id, prompt), on_auth_url=on_auth_url)
    except auth.LoginRequired as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (ChatError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
