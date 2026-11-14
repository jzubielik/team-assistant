"""What each long-term strategy extracts, how fast, and what reaches the next session's prompt.

    python scripts/measure.py --profile <p> --region <r> --gateway-url <url> \
        --gateway-target <name> --memory-id <id> --login-domain <url> --client-id <id> \
        --env <env> --out <file> [--user alice]

Runs the conversations in scripts/conversations.toml as one logged-in user, each in a new
session. For each namespace in config.NAMESPACES it waits for records created since the
conversation began and notes the seconds from the last answer to the first record. Then it
searches the retrieval namespaces with the follow-up question under the operator's profile,
with the settings from config/agent.toml, the way the session manager does (namespacePath,
top_k, then the relevance threshold): a client-side reproduction, labelled so in the report,
because the agent writes no memory content to logs. Last, it asks the follow-up in another new
session. Writes a Markdown report; namespaces appear as templates, never with an actor id.
"""

from __future__ import annotations

import argparse
import datetime
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import chat  # noqa: E402
import memory  # noqa: E402
import smoke  # noqa: E402

from team_assistant.config import NAMESPACES, load_config  # noqa: E402

DEFAULT_PATH = Path(__file__).resolve().parent / "conversations.toml"
_KEYS = {"name", "messages", "followup"}


@dataclass(frozen=True)
class Conversation:
    name: str
    messages: list[str]
    followup: str


@dataclass(frozen=True)
class NamespaceResult:
    namespace: str  # template, e.g. /facts/{actorId}/
    records: list[str]
    seconds_to_first: float | None  # None: no new record within the wait


@dataclass(frozen=True)
class Retrieved:
    namespace: str  # template
    top_k: int
    relevance_score: float
    found: list[tuple[float, str]]  # (score, text), as the search returned them


@dataclass(frozen=True)
class ConversationResult:
    name: str
    messages: list[str]
    usage: list[tuple[int, int]]  # (input, output) tokens per message
    namespaces: list[NamespaceResult]
    retrieved: list[Retrieved]
    followup: str
    answer: str
    followup_usage: tuple[int, int]


def load_conversations(path: Path = DEFAULT_PATH) -> list[Conversation]:
    """Read and check the conversation file. ValueError names what is wrong."""
    items = tomllib.loads(path.read_text()).get("conversation", [])
    if not items:
        raise ValueError(f"{path.name}: no conversation")
    found = []
    for number, item in enumerate(items, 1):
        where = f"{path.name}, conversation {number}"
        unknown = sorted(set(item) - _KEYS)
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}")
        name, messages, followup = item.get("name"), item.get("messages"), item.get("followup")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{where}: name must be a non-empty string")
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"{where}: messages must be a non-empty list")
        if not isinstance(followup, str) or not followup:
            raise ValueError(f"{where}: followup must be a non-empty string")
        found.append(Conversation(name, [str(m) for m in messages], followup))
    return found


def measured_namespaces() -> list[str]:
    return list(NAMESPACES.values())


def new_records(records: list[dict], since: datetime.datetime) -> list[dict]:
    """Records created at or after ``since``; older ones come from earlier conversations."""
    return [r for r in records if r["createdAt"] >= since]


def one_line(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def render_report(results: list[ConversationResult], env: str, date: str) -> str:
    lines = [
        f"# Memory measurement, {env}, {date}",
        "",
        "Written by `make measure` (scripts/measure.py) from scripts/conversations.toml.",
    ]
    for r in results:
        lines += ["", f"## {r.name}", "", "Messages, each answered in one session:", ""]
        for message, (tokens_in, tokens_out) in zip(r.messages, r.usage, strict=True):
            lines.append(f"- {one_line(message)} ({tokens_in} in, {tokens_out} out)")
        lines += [
            "",
            "Records created by the strategies (time from the last answer to the first record):",
            "",
            "| Namespace | First record after | Records |",
            "|---|---|---|",
        ]
        for n in r.namespaces:
            after = "no new record" if n.seconds_to_first is None else f"{n.seconds_to_first:.0f} s"
            text = "<br>".join(one_line(t) for t in n.records)
            lines.append(f"| `{n.namespace}` | {after} | {text} |".replace("|  |", "| |"))
        lines += [
            "",
            "Retrieval for the follow-up question: client-side reproduction of the session "
            "manager's retrieval, with the settings from config/agent.toml.",
            "",
            "| Namespace | Score | Into the prompt | Record |",
            "|---|---|---|---|",
        ]
        for s in r.retrieved:
            for score, text in s.found:
                below = f"dropped (below {s.relevance_score})"
                kept = "kept" if score >= s.relevance_score else below
                lines.append(f"| `{s.namespace}` | {score:.2f} | {kept} | {one_line(text)} |")
            if not s.found:
                lines.append(f"| `{s.namespace}` | | nothing found | |")
        tokens_in, tokens_out = r.followup_usage
        lines += [
            "",
            f"Follow-up in a new session: {one_line(r.followup)}",
            "",
            f"Answer: {one_line(r.answer)} ({tokens_in} in, {tokens_out} out)",
        ]
    return "\n".join(lines) + "\n"


def ask_with_usage(events) -> tuple[str, tuple[int, int]]:
    """Answer text and (input, output) tokens of one streamed answer."""
    parts, usage = [], (0, 0)
    for event in events:
        if event.get("type") == "text":
            parts.append(event.get("data", ""))
        elif event.get("type") == "done":
            found = event.get("usage", {})
            usage = (found.get("input", 0), found.get("output", 0))
        elif event.get("type") == "error" or "error" in event:
            raise chat.ChatError(str(event.get("message") or event.get("error")))
    return "".join(parts), usage


def measure(
    conversation: Conversation,
    ask: Callable[[str, str], tuple[str, tuple[int, int]]],
    list_records: Callable[[str], list[dict]],
    search: Callable[[str, str, int], list[tuple[float, str]]],
    actor: str,
) -> ConversationResult:
    """One conversation: run it, wait for records, reproduce retrieval, ask the follow-up.

    ``ask(session, prompt)``, ``list_records(namespace)`` and ``search(namespace, query,
    top_k)`` take resolved namespaces; results name namespaces by template.
    """
    session = smoke.new_session_id()
    began = datetime.datetime.now(datetime.UTC)
    usage = [ask(session, message)[1] for message in conversation.messages]
    answered = datetime.datetime.now(datetime.UTC)
    namespaces = []
    for template in measured_namespaces():
        resolved = memory.namespace_of(template, actor, session)
        first = smoke.wait_for_record(lambda r=resolved: new_records(list_records(r), began))
        records = new_records(list_records(resolved), began) if first else []
        seconds = (first["createdAt"] - answered).total_seconds() if first else None
        texts = [r.get("content", {}).get("text", "") for r in records]
        namespaces.append(NamespaceResult(template, texts, seconds))
    retrieved = []
    for template, setting in load_config().retrieval.items():
        resolved = memory.namespace_of(template, actor, session)
        found = search(resolved, conversation.followup, setting.top_k)
        retrieved.append(Retrieved(template, setting.top_k, setting.relevance_score, found))
    answer, followup_usage = ask(smoke.new_session_id(), conversation.followup)
    return ConversationResult(
        conversation.name,
        conversation.messages,
        usage,
        namespaces,
        retrieved,
        conversation.followup,
        answer,
        followup_usage,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in (
        "profile",
        "region",
        "gateway-url",
        "gateway-target",
        "memory-id",
        "login-domain",
        "client-id",
        "env",
        "out",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--user", default="alice")
    args = parser.parse_args(argv)
    import boto3

    client = boto3.Session(profile_name=args.profile, region_name=args.region).client(
        "bedrock-agentcore"
    )
    endpoints = auth.Endpoints(args.login_domain, args.client_id)
    url = chat.invoke_url(args.gateway_url, args.gateway_target)

    def token() -> str:
        return auth.access_token(endpoints, args.env, args.user)

    def ask(session: str, prompt: str) -> tuple[str, tuple[int, int]]:
        return ask_with_usage(chat.invoke(url, token(), session, prompt))

    def search(namespace: str, query: str, top_k: int) -> list[tuple[float, str]]:
        # The same call as MemoryClient.retrieve_memories in bedrock-agentcore 1.26.0.
        found = client.retrieve_memory_records(
            memoryId=args.memory_id,
            namespacePath=namespace,
            searchCriteria={"searchQuery": query, "topK": top_k},
        ).get("memoryRecordSummaries", [])
        return [(r.get("score", 0.0), r.get("content", {}).get("text", "")) for r in found]

    def list_records(namespace: str) -> list[dict]:
        return memory.list_records(client, args.memory_id, namespace)

    actor = auth._claims(token())["sub"]
    # Records left by make smoke would shape the answers being measured.
    print(f"purge: {smoke.purge(client, args.memory_id, [actor])} smoke record(s) deleted")
    results = []
    for conversation in load_conversations():
        print(f"--- {conversation.name}: running, then waiting for records", flush=True)
        results.append(measure(conversation, ask, list_records, search, actor))
    today = datetime.date.today().isoformat()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(results, args.env, today))
    print(f"report written to {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (chat.ChatError, auth.LoginRequired) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
