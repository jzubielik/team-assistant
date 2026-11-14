"""Checks against a deployed environment.

- Conversation: the agent remembers a conversation, a stopped session is restored from
  Memory, nobody gets in without a token, and two users with the same session id do not see
  each other's messages.
- Long-term memory: a preference stated in one session is followed in a new one, and another
  user gets none of it.
- GitHub: a user who consented gets issues through the vaulted token; a user who did not is
  asked to.
- Gateway: every request goes through the gateway, and the same token sent straight to the
  Runtime is refused.
- Web page: the page's requests list the user's conversations with a
title and return a conversation's history, and another user's history of the same session id
holds none of it; the hosted page is served over HTTPS with its security headers and the
bucket refuses a direct request.

    python scripts/smoke.py --profile <p> --region <r> --gateway-url <url> \
        --gateway-target <name> --runtime-arn <arn> --memory-id <id> --login-domain <url> \
        --client-id <id> --env <env> --web-url <url> --web-bucket <name> \
        [--user-a alice] [--user-b bob]

Both users must be logged in (make login). Prints one line per check, PASS or FAIL, and exits
1 when any check failed. Each run costs a handful of model calls. Long-term records are
extracted asynchronously (about a minute), so the long-term memory checks poll for the
record for up to WAIT_S seconds.
"""

from __future__ import annotations

import argparse
import re
import secrets
import string
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import chat  # noqa: E402
import memory  # noqa: E402

from team_assistant.config import NAMESPACES  # noqa: E402

Ask = Callable[[str, str, str], str]  # (user, session id, prompt) -> answer text
# (user, session id, body) -> the first event of the answer to a request that is not a prompt
Read = Callable[[str, str, dict], dict]
# url -> (HTTP status, headers with lowercase names, body text); no token is sent
Fetch = Callable[[str], tuple[int, dict[str, str], str]]
Check = tuple[bool, str]
# (user, strategy name from NAMESPACES) -> texts of that user's long-term records
RecordsOf = Callable[[str, str], list[str]]
# Waits for a non-empty list from the function and returns its first item, or None.
Wait = Callable[[Callable[[], list]], object]
# How much of an unexpected answer a FAIL message shows.
PREVIEW = 200
# Every word a smoke run makes starts with this, so its records can be found and deleted.
MARKER = "smk"
MARKED_WORD = re.compile(rf"\b{MARKER}[a-z]{{5}}\b", re.IGNORECASE)
# How long to wait for a long-term record, and how often to look.
WAIT_S, INTERVAL_S = 300, 10
SIGNATURE_QUESTION = (
    "Which word should you sign your answers with? Reply with the word only. If none, say NONE."
)


def random_word() -> str:
    """Eight letters no model would guess, starting with MARKER so purge() can find them."""
    return MARKER + "".join(secrets.choice(string.ascii_lowercase) for _ in range(5))


def marked(records: list[dict]) -> list[dict]:
    """Long-term records that hold a word made by random_word()."""
    return [r for r in records if MARKED_WORD.search(r.get("content", {}).get("text", ""))]


def purge(client, memory_id: str, actors: list[str]) -> int:
    """Delete the long-term records smoke runs left behind; returns how many.

    The checks make the strategies extract real records (a signature word becomes a
    preference), and they would then shape every later answer and every measurement. Records
    are extracted about a minute after a run, so a record missed at the end of one run is
    deleted at the start of the next, and make measure purges before it starts.
    """
    deleted = 0
    for actor in actors:
        for template in NAMESPACES.values():
            path = memory.namespace_prefix(template, actor)
            for record in marked(memory.list_records(client, memory_id, path, prefix=True)):
                client.delete_memory_record(
                    memoryId=memory_id, memoryRecordId=record["memoryRecordId"]
                )
                deleted += 1
    return deleted


def answer_text(events) -> str:
    """The text of a streamed answer; ChatError when the agent reported an error."""
    parts = []
    for event in events:
        if event.get("type") == "text":
            parts.append(event.get("data", ""))
        elif event.get("type") == "error" or "error" in event:
            raise chat.ChatError(str(event.get("message") or event.get("error")))
    return "".join(parts)


def shown(answer: str) -> str:
    """An answer on one line, cut to PREVIEW characters, for a FAIL message."""
    line = " ".join(answer.split())
    return repr(line[:PREVIEW] + ("..." if len(line) > PREVIEW else ""))


def check_same_session(ask: Ask, user: str, session: str, word: str) -> Check:
    ask(user, session, f"Remember this word for later: {word}. Reply with OK only.")
    answer = ask(user, session, "Which word did I ask you to remember? Reply with the word only.")
    if word not in answer.lower():
        return False, f"the second answer lacks the word {word}; it was: {shown(answer)}"
    return True, "the second answer contains the word from the first message"


def check_restored_session(
    ask: Ask, stop: Callable[[str, str], None], user: str, session: str, word: str
) -> Check:
    stop(user, session)
    answer = ask(user, session, "Which word did I ask you to remember? Reply with the word only.")
    if word not in answer.lower():
        return False, f"after the stop the answer lacks the word {word}; it was: {shown(answer)}"
    return True, "after the runtime session was stopped, the answer still contains the word"


def check_no_token(call: Callable[[str | None], object], token: str) -> Check:
    """``call(token)`` sends a request; it must raise 'not authorised' without a valid token."""
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    for label, value in (("no token", None), ("tampered token", tampered)):
        try:
            call(value)
        except chat.ChatError as error:
            if "not authorised" not in str(error):
                return False, f"{label}: refused, but not as unauthorised ({error})"
        else:
            return False, f"{label}: the request was accepted"
    return True, "a request without a token and one with a tampered token are refused"


def check_direct_call(call: Callable[[], object]) -> Check:
    """``call()`` sends a valid token straight to the Runtime; it must be refused."""
    try:
        call()
    except chat.ChatError as error:
        if "not authorised" not in str(error):
            return False, f"refused, but not as unauthorised ({error})"
        return True, "the Runtime refuses a valid token that did not come through the gateway"
    return False, "the Runtime answered a direct call: the gateway is not the only way in"


def check_other_user(
    ask: Ask,
    texts_of: Callable[[str, str], list[str]],
    user_a: str,
    user_b: str,
    session: str,
    word: str,
) -> Check:
    answer = ask(user_b, session, "Which word did I ask you to remember? If none, say NONE.")
    if word in answer.lower():
        return False, f"{user_b} was told {user_a}'s word"
    if not any(word in text.lower() for text in texts_of(user_a, session)):
        return False, f"{user_a}'s stored messages do not contain the word (cannot compare)"
    if any(word in text.lower() for text in texts_of(user_b, session)):
        return False, f"{user_b}'s stored messages contain {user_a}'s word"
    return (
        True,
        f"{user_b} with {user_a}'s session id gets no word, and stores only their own messages",
    )


def check_conversations(read: Read, user: str, session: str) -> Check:
    """The web page's list: the smoke session is the user's, with the title the agent stored.
    Asked in the smoke session itself: a new session id would start a runtime session that
    idles until its timeout."""
    event = read(user, session, {"conversations": {}})
    if event.get("type") != "conversations":
        return False, f"the agent answered {shown(str(event))}"
    item = next((i for i in event.get("items", []) if i.get("session_id") == session), None)
    if item is None:
        return (
            False,
            f"{user}'s list lacks the smoke session ({len(event.get('items', []))} listed)",
        )
    if not item.get("title"):
        return False, "the smoke session is listed without a title"
    return True, f"{user}'s list has the smoke session, titled {shown(item['title'])}"


def check_history(read: Read, user: str, session: str, word: str) -> Check:
    """The web page's history: the smoke session's turns, oldest first, text only."""
    event = read(user, session, {"history": {}})
    if event.get("type") != "history":
        return False, f"the agent answered {shown(str(event))}"
    turns = event.get("turns", [])
    roles = [t.get("role") for t in turns]
    if len(turns) < 4 or roles[:4] != ["user", "assistant", "user", "assistant"]:
        return False, f"the history has the roles {roles}"
    if word not in turns[0].get("text", "").lower():
        return False, "the first turn is not the message with the word"
    return True, f"the history has {len(turns)} turns in order, the first with the word"


def check_history_isolation(read: Read, user_a: str, user_b: str, session: str, word: str) -> Check:
    """The other user asked with the same session id (check "other user"): their history of
    it has their own turns and not one with the first user's word."""
    event = read(user_b, session, {"history": {}})
    if event.get("type") != "history":
        return False, f"the agent answered {shown(str(event))}"
    texts = [t.get("text", "").lower() for t in event.get("turns", [])]
    if any(word in text for text in texts):
        return False, f"{user_b}'s history of the session holds {user_a}'s word"
    if not texts:
        return False, f"{user_b}'s history of the session is empty (cannot compare)"
    return True, f"{user_b}'s history of the same session id holds only their own turns"


def check_page_served(fetch: Fetch, web_url: str, bucket_url: str, gateway_url: str) -> Check:
    """The hosted page: HTTPS with the security headers, this environment's settings, and a
    bucket that answers nobody but CloudFront."""
    status, headers, body = fetch(f"{web_url}/")
    if status != 200:
        return False, f"the page answered HTTP {status}"
    policy = headers.get("content-security-policy", "")
    if "frame-ancestors 'none'" not in policy or "script-src 'self'" not in policy:
        return False, f"the page lacks the security headers (policy: {policy!r})"
    gateway = gateway_url.split("/", 3)[2]
    if f"connect-src 'self' https://{gateway}" not in body:
        return False, "the page's HTML lacks the policy naming this environment's gateway"
    status, _, config = fetch(f"{web_url}/config.json")
    if status != 200 or gateway not in config:
        return False, f"config.json answered HTTP {status} without this environment's gateway"
    status, _, _ = fetch(bucket_url)
    if status != 403:
        return False, f"the bucket answered a direct request with HTTP {status}, not 403"
    return True, "the page is served over HTTPS with its headers and settings; the bucket refuses"


def new_session_id() -> str:
    return f"smoke-{uuid.uuid4()}"


def wait_for_record(
    list_fn: Callable[[], list],
    timeout_s: float = WAIT_S,
    interval_s: float = INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
):
    """The first item ``list_fn`` returns, polling until ``timeout_s``; None if none came."""
    deadline = clock() + timeout_s
    while True:
        found = list_fn()
        if found:
            return found[0]
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        sleep(min(interval_s, remaining))


def with_word(texts: list[str], word: str) -> list[str]:
    return [text for text in texts if word in text.lower()]


def check_new_session(ask: Ask, records_of: RecordsOf, wait: Wait, user: str, word: str) -> Check:
    ask(user, new_session_id(), f"Please remember my preference: sign every answer with {word}.")
    if wait(lambda: with_word(records_of(user, "preferences"), word)) is None:
        return False, f"no record with the word after {WAIT_S} s in {user}'s preferences namespace"
    answer = ask(user, new_session_id(), SIGNATURE_QUESTION)
    return word in answer.lower(), "a new session follows the preference stated in an earlier one"


def check_isolation(ask: Ask, records_of: RecordsOf, user_a: str, user_b: str, word: str) -> Check:
    if not with_word(records_of(user_a, "preferences"), word):
        return False, f"{user_a} has no preference record with the word (cannot compare)"
    if word in ask(user_b, new_session_id(), SIGNATURE_QUESTION).lower():
        return False, f"{user_b} was told {user_a}'s preference"
    for strategy in ("preferences", "facts"):
        if with_word(records_of(user_b, strategy), word):
            return False, f"{user_b}'s {strategy} contain {user_a}'s word"
    return True, f"{user_b} in a new session gets none of {user_a}'s preference, in no namespace"


# A public repository with open issues; any such repository will do.
GITHUB_REPOSITORY = "aws/bedrock-agentcore-sdk-python"
ISSUES_PROMPT = f"List open issues in {GITHUB_REPOSITORY}. Answer with their numbers."
AskEvents = Callable[[str, str, str], list]  # (user, session id, prompt) -> the events


def until_consent(events) -> list[dict]:
    """The events of one answer, stopping at a consent URL: the agent would then wait for
    the user's consent for up to 600 s, and nothing more comes until then."""
    found = []
    for event in events:
        found.append(event)
        if event.get("type") == "auth_url":
            break
    return found


def check_github_tool(ask_events: AskEvents, user_a: str, user_b: str) -> Check:
    first = ask_events(user_a, new_session_id(), ISSUES_PROMPT)
    if any(e.get("type") == "auth_url" for e in first):
        return False, (
            f"{user_a} has not consented to GitHub in this deployment: run the consent step "
            "in docs/runbook.md, then make smoke again"
        )
    text = answer_text(first)
    if not re.search(r"\d+", text):
        return False, f"the answer for {user_a} has no issue numbers; it was: {shown(text)}"
    second = ask_events(user_b, new_session_id(), ISSUES_PROMPT)
    if not any(e.get("type") == "auth_url" for e in second):
        return False, f"{user_b} got an answer without a consent URL: {shown(answer_text(second))}"
    return True, (
        f"{user_a} gets issues with the vaulted GitHub token; {user_b}, without consent, "
        "is asked to consent"
    )


def run(checks: list[tuple[str, Callable[[], Check]]], out=None) -> int:
    """Run the checks in order; a check that raises fails. Returns the exit code."""
    out = out or sys.stdout
    failed = 0
    for name, check in checks:
        try:
            passed, detail = check()
        except (chat.ChatError, auth.LoginRequired) as error:
            passed, detail = False, str(error)
        failed += not passed
        print(f"{'PASS' if passed else 'FAIL'}  {name}: {detail}", file=out)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in (
        "profile",
        "region",
        "gateway-url",
        "gateway-target",
        "runtime-arn",
        "memory-id",
        "login-domain",
        "client-id",
        "env",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--web-url", required=True)
    parser.add_argument("--web-bucket", required=True)
    parser.add_argument("--user-a", default="alice")
    parser.add_argument("--user-b", default="bob")
    args = parser.parse_args(argv)
    endpoints = auth.Endpoints(args.login_domain, args.client_id)
    url = chat.invoke_url(args.gateway_url, args.gateway_target)
    stop_url = chat.stop_url(args.gateway_url, args.gateway_target)
    direct_url = chat.runtime_url(args.region, args.runtime_arn)
    session, word, signature = new_session_id(), random_word(), random_word()

    def token(user: str) -> str:
        return auth.access_token(endpoints, args.env, user)

    def ask(user: str, session_id: str, prompt: str) -> str:
        return answer_text(chat.invoke(url, token(user), session_id, prompt))

    def ask_events(user: str, session_id: str, prompt: str) -> list[dict]:
        return until_consent(chat.invoke(url, token(user), session_id, prompt))

    def stop(user: str, session_id: str) -> None:
        list(chat.invoke(stop_url, token(user), session_id, None))

    def fetch(target: str) -> tuple[int, dict[str, str], str]:
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(target, timeout=30) as response:  # noqa: S310 - https
                lower = {k.lower(): v for k, v in response.headers.items()}
                return response.status, lower, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, {k.lower(): v for k, v in error.headers.items()}, ""

    bucket_url = f"https://{args.web_bucket}.s3.{args.region}.amazonaws.com/index.html"

    def read(user: str, session_id: str, body: dict) -> dict:
        events = list(chat.invoke(url, token(user), session_id, None, body=body))
        return events[0] if events else {}

    def client():
        import boto3

        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        return session.client("bedrock-agentcore")

    def texts_of(user: str, session_id: str) -> list[str]:
        actor = auth._claims(token(user))["sub"]
        events = memory.list_events(client(), args.memory_id, actor, session_id)
        return [text for _, _, text in memory.messages(events)]

    def records_of(user: str, strategy: str) -> list[str]:
        actor = auth._claims(token(user))["sub"]
        namespace = memory.namespace_of(NAMESPACES[strategy], actor, "")
        found = memory.list_records(client(), args.memory_id, namespace)
        return [r.get("content", {}).get("text", "") for r in found]

    a, b = args.user_a, args.user_b
    actors = [auth._claims(token(user))["sub"] for user in (a, b)]
    print(f"purge: {purge(client(), args.memory_id, actors)} record(s) of earlier runs deleted")
    code = run(
        [
            ("same session", lambda: check_same_session(ask, a, session, word)),
            ("restored session", lambda: check_restored_session(ask, stop, a, session, word)),
            (
                "no token",
                lambda: check_no_token(
                    lambda t: list(chat.invoke(url, t, session, "hi")), token(a)
                ),
            ),
            (
                "direct call",
                lambda: check_direct_call(
                    lambda: list(chat.invoke(direct_url, token(a), new_session_id(), "hi"))
                ),
            ),
            ("other user", lambda: check_other_user(ask, texts_of, a, b, session, word)),
            ("conversations", lambda: check_conversations(read, a, session)),
            ("history", lambda: check_history(read, a, session, word)),
            ("history isolation", lambda: check_history_isolation(read, a, b, session, word)),
            (
                "new session",
                lambda: check_new_session(ask, records_of, wait_for_record, a, signature),
            ),
            ("isolation", lambda: check_isolation(ask, records_of, a, b, signature)),
            ("github tool", lambda: check_github_tool(ask_events, a, b)),
            (
                "page served",
                lambda: check_page_served(fetch, args.web_url, bucket_url, args.gateway_url),
            ),
        ]
    )
    print(f"purge: {purge(client(), args.memory_id, actors)} record(s) of this run deleted")
    return code


if __name__ == "__main__":
    sys.exit(main())
