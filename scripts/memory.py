"""List what AgentCore Memory holds for one user and session (operator's view).

    python scripts/memory.py --profile <p> --region <r> --memory-id <id> --actor <sub> \
        --session <id>

Reads with the operator's AWS profile. The actor id is the user's ``sub`` (scripts/ops.sh takes
it from the user's saved login). Prints one line per stored message: time, role and the first
80 characters. The session manager also stores its own state as events; those are counted.
Then, for each namespace in config.NAMESPACES filled in for this actor and session, every
long-term record with its creation time and full text.
"""

from __future__ import annotations

import argparse
import json
import sys

PREVIEW = 80


def list_events(client, memory_id: str, actor_id: str, session_id: str) -> list[dict]:
    """Every event of the actor and session, oldest first."""
    events, token = [], None
    while True:
        page = client.list_events(
            memoryId=memory_id,
            actorId=actor_id,
            sessionId=session_id,
            includePayloads=True,
            maxResults=100,
            **({"nextToken": token} if token else {}),
        )
        events += page.get("events", [])
        token = page.get("nextToken")
        if not token:
            return sorted(events, key=lambda e: e["eventTimestamp"])


def message_text(stored: str) -> str:
    """The text of a message as the session manager stored it (JSON of a Strands message)."""
    try:
        content = json.loads(stored)["message"]["content"]
        return " ".join(block["text"] for block in content if "text" in block)
    except (ValueError, KeyError, TypeError):
        return stored


def messages(events: list[dict]) -> list[tuple[object, str, str]]:
    """(time, role, text) of every conversational event."""
    found = []
    for event in events:
        for item in event.get("payload", []):
            turn = item.get("conversational")
            if turn:
                text = message_text(turn.get("content", {}).get("text", ""))
                found.append((event["eventTimestamp"], turn.get("role", "?"), text))
    return found


def namespace_of(template: str, actor_id: str, session_id: str) -> str:
    """A namespace template from config.NAMESPACES with this actor and session filled in."""
    return template.format(actorId=actor_id, sessionId=session_id)


def namespace_prefix(template: str, actor_id: str) -> str:
    """The part of a template that holds one actor's records of every session."""
    return template.split("{sessionId}")[0].format(actorId=actor_id)


def list_records(client, memory_id: str, namespace: str, *, prefix: bool = False) -> list[dict]:
    """Every long-term record in exactly this namespace, oldest first.

    ``namespace`` is an exact match; with ``prefix`` it is sent as ``namespacePath`` and deeper
    namespaces (every session's summaries) are included.
    """
    where = {"namespacePath" if prefix else "namespace": namespace}
    records, token = [], None
    while True:
        page = client.list_memory_records(
            memoryId=memory_id,
            **where,
            maxResults=100,
            **({"nextToken": token} if token else {}),
        )
        records += page.get("memoryRecordSummaries", [])
        token = page.get("nextToken")
        if not token:
            return sorted(records, key=lambda r: r["createdAt"])


def record_lines(namespace: str, records: list[dict]) -> list[str]:
    """The namespace, then one line per record: creation time and full text on one line."""
    lines = [namespace]
    for found in records:
        text = " ".join(found.get("content", {}).get("text", "").split())
        lines.append(f"  {found['createdAt']:%Y-%m-%d %H:%M:%S}  {text}")
    return lines if records else [namespace, "  no records"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("profile", "region", "memory-id", "actor", "session"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    import boto3

    from team_assistant.config import NAMESPACES

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("bedrock-agentcore")
    events = list_events(client, args.memory_id, args.actor, args.session)
    found = messages(events)
    for timestamp, role, text in found:
        line = " ".join(text.split())
        print(f"{timestamp:%Y-%m-%d %H:%M:%S}  {role:<9}  {line[:PREVIEW]}")
    print(f"{len(found)} message(s); {len(events) - len(found)} other event(s) (session state)")
    print("\nLong-term records (extracted asynchronously, may lag the conversation):")
    for template in NAMESPACES.values():
        namespace = namespace_of(template, args.actor, args.session)
        print("\n".join(record_lines(namespace, list_records(client, args.memory_id, namespace))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
