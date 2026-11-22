"""Fill one user's long-term memory with scripted conversations and write down what was stored.

    python scripts/seed.py --profile <p> --region <r> --gateway-url <url> \
        --gateway-target <name> --memory-id <id> --login-domain <url> --client-id <id> \
        --env <env> --user <user> --out <file>

Runs every conversation of scripts/seed.toml as the logged-in user, each in a new session
that is stopped afterwards. AgentCore Memory extracts the long-term records by itself, some
time after the messages, and may join or split what was said; so the script waits until the
number of the user's fact and summary records has stopped changing. Then it writes one JSON
file: the conversations as they were sent, and the user's fact, preference and summary
records with every field the service returns, oldest first. The file is data to evaluate,
not a report. The user's id is replaced by ``{actorId}`` in it.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import chat  # noqa: E402
import memory  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from team_assistant.config import NAMESPACES  # noqa: E402

DEFAULT_PATH = Path(__file__).resolve().parent / "seed.toml"
# The strategies whose records are written down.
STRATEGIES = ("facts", "preferences", "summaries")
# The wait for Memory: the count of fact and summary records is read every INTERVAL_S
# seconds and the wait ends once it has not changed for QUIET_S seconds, or after LIMIT_S
# seconds.
INTERVAL_S, QUIET_S, LIMIT_S = 15, 120, 900


def load_conversations(path: Path = DEFAULT_PATH) -> list[list[str]]:
    """The messages of every conversation in the file. ValueError names what is wrong."""
    items = tomllib.loads(path.read_text()).get("conversation", [])
    if not items:
        raise ValueError(f"{path.name}: no conversation")
    found = []
    for number, item in enumerate(items, 1):
        where = f"{path.name}, conversation {number}"
        unknown = sorted(set(item) - {"messages"})
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}")
        messages = item.get("messages")
        if (
            not isinstance(messages, list)
            or not messages
            or not all(isinstance(m, str) and m for m in messages)
        ):
            raise ValueError(f"{where}: messages must be a non-empty list of strings")
        found.append(messages)
    return found


def seed(
    conversations: list[list[str]],
    say: Callable[[str, str], None],
    stop: Callable[[str], None],
    new_session: Callable[[], str] = chat.new_session_id,
    out=None,
) -> None:
    """Run every conversation in a session of its own and stop that session.

    ``say(session, message)`` sends one message and reads the whole answer.
    """
    out = out or sys.stdout
    for number, messages in enumerate(conversations, 1):
        session = new_session()
        for message in messages:
            say(session, message)
        stop(session)
        print(f"--- conversation {number} of {len(conversations)} sent", file=out, flush=True)


def wait_until_stable(
    count: Callable[[], int],
    interval_s: float = INTERVAL_S,
    quiet_s: float = QUIET_S,
    limit_s: float = LIMIT_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, bool]:
    """Poll ``count`` until it has not changed for ``quiet_s`` seconds.

    Returns the last count and whether it became stable; False means ``limit_s`` ran out
    while the count was still changing.
    """
    started = changed = clock()
    last = count()
    while True:
        now = clock()
        if now - changed >= quiet_s:
            return last, True
        if now - started >= limit_s:
            return last, False
        sleep(interval_s)
        current = count()
        if current != last:
            last, changed = current, clock()


def as_data(value, actor: str):
    """``value`` as JSON data: times in UTC as ISO 8601, the actor's id replaced."""
    if isinstance(value, dict):
        return {key: as_data(item, actor) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_data(item, actor) for item in value]
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.UTC).isoformat(timespec="seconds")
    if isinstance(value, str):
        return value.replace(actor, "{actorId}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    for name in (
        "profile",
        "region",
        "gateway-url",
        "gateway-target",
        "memory-id",
        "login-domain",
        "client-id",
        "env",
        "user",
        "out",
    ):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    import boto3

    client = boto3.Session(profile_name=args.profile, region_name=args.region).client(
        "bedrock-agentcore"
    )
    endpoints = auth.Endpoints(args.login_domain, args.client_id)
    url = chat.invoke_url(args.gateway_url, args.gateway_target)
    stop_url = chat.stop_url(args.gateway_url, args.gateway_target)

    def token() -> str:
        return auth.access_token(endpoints, args.env, args.user)

    def say(session: str, message: str) -> None:
        for event in chat.invoke(url, token(), session, message):
            if event.get("type") == "error" or "error" in event:
                raise chat.ChatError(str(event.get("message") or event.get("error")))

    def stop(session: str) -> None:
        list(chat.invoke(stop_url, token(), session, None))

    actor = auth._claims(token())["sub"]

    def records(strategy: str) -> list[dict]:
        # Summaries are stored per session: they are read under the user's prefix.
        namespace = memory.namespace_prefix(NAMESPACES[strategy], actor)
        deeper = "{sessionId}" in NAMESPACES[strategy]
        return memory.list_records(client, args.memory_id, namespace, prefix=deeper)

    conversations = load_conversations()
    seed(conversations, say, stop)
    print("--- waiting until the number of fact and summary records stops changing", flush=True)
    count, stable = wait_until_stable(lambda: len(records("facts")) + len(records("summaries")))
    print(f"--- {count} record(s){'' if stable else ' (still changing)'}", flush=True)
    found = {
        "env": args.env,
        "written": datetime.datetime.now(datetime.UTC),
        "memory_had_finished": stable,
        "conversations": conversations,
        "records": {strategy: records(strategy) for strategy in STRATEGIES},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(as_data(found, actor), indent=2, ensure_ascii=False) + "\n")
    print(f"records written to {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (chat.ChatError, auth.LoginRequired) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
