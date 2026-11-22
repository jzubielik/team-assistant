"""The prompt-attack check of tool results that hold text written by others.

list_issues and get_issue return text anyone can write: issue titles, labels and bodies.
Before the model reads such a result, it goes to this environment's Bedrock guardrail
(filter PROMPT_ATTACK, docs/security.md). When the guardrail intervenes, or cannot be asked,
the model gets a refusal instead of the result.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from strands.hooks import AfterToolCallEvent

log = logging.getLogger(__name__)

GUARDED_TOOLS = frozenset({"list_issues", "get_issue"})
REFUSED = "Refused: the result was withheld because it may hold instructions to the assistant."
UNCHECKED = "Refused: the result could not be checked, so it was withheld."


def _text(result: dict) -> str:
    return "\n".join(block["text"] for block in result.get("content", []) if "text" in block)


def guard_hook(make_client: Callable[[], Any], guardrail_id: str | None):
    """The AfterToolCallEvent callback that checks the results of GUARDED_TOOLS.

    A call that a hook cancelled before the tool ran is not checked: its result is the
    canceller's own text. ``make_client`` is called once, for the first result to check.
    """
    clients: list[Any] = []

    async def check(event: AfterToolCallEvent) -> None:
        name = event.tool_use["name"]
        text = _text(event.result)
        if name not in GUARDED_TOOLS or not text or getattr(event, "cancel_message", None):
            return
        try:
            if not guardrail_id:
                raise ValueError("GUARDRAIL_ID is not set")
            if not clients:
                clients.append(make_client())
            answer = await asyncio.to_thread(
                clients[0].apply_guardrail,
                guardrailIdentifier=guardrail_id,
                guardrailVersion="DRAFT",
                source="INPUT",
                content=[{"text": {"text": text}}],
            )
            if answer.get("action") == "NONE":
                return
            message, outcome = REFUSED, "intervened"
        except Exception as error:  # noqa: BLE001 - any failure withholds the result
            message, outcome = UNCHECKED, type(error).__name__
        log.info(json.dumps({"event": "guardrail", "tool": name, "outcome": outcome}))
        event.result = {**event.result, "status": "error", "content": [{"text": message}]}

    return check
