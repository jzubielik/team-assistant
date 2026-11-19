"""The agent: the entry point AgentCore Runtime calls for every request.

A request is one user message, ``{"prompt": ...}``, or the forwarding of a GitHub consent,
``{"consent": {"session_uri": ...}}``. For a message, in this order:

1. check the message (a non-empty ``prompt``);
2. find out who is asking (identity.py: the ``sub`` claim of the verified token);
3. open that user's conversation history for this session (memory.py);
4. list the user's stored preferences for the system prompt (recall.py, prompts.py);
5. run the model with the history, the GitHub tools list_issues and get_issue (github.py) and
   the memory tools search_memory and recall_conversations (recall.py), streaming the answer.

Steps 1 and 2 fail before anything is read from memory or sent to the model. So does a session
id that another user used first in this process (``_other_users_session``). The answer is a
stream of events (built in events.py), each a JSON object: ``{"type": "text", "data": ...}``
for a piece of the answer, ``{"type": "tool", "name": ...}`` when the model calls a tool,
``{"type": "auth_url", "url": ..., "state": ...}`` when the user must consent in GitHub first,
then ``{"type": "done", "usage": {...}}``, or
``{"type": "error", ...}``.
A consent request makes no model call (consent.py) and answers ``{"type": "consent_done"}``.
The web page also asks ``{"conversations": {}}`` and ``{"history": {}}`` (conversations.py): no
model call, always for the token's user, answered by one ``conversations`` or ``history`` event.

Message content, URLs and tokens never go to the log: the one log line per request holds the
user id, the session id, the outcome and the token counts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Callable
from functools import partial
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.agent.conversation_manager import SummarizingConversationManager
from strands.models import BedrockModel, Model
from strands.types.exceptions import ContextWindowOverflowException, ModelThrottledException

from team_assistant import events
from team_assistant.config import AgentConfig, load_config, return_url
from team_assistant.consent import ConsentError, complete_consent
from team_assistant.conversations import history, list_conversations
from team_assistant.github import github_tools
from team_assistant.identity import IdentityError, actor_from_headers, bearer_from_headers
from team_assistant.logs import configure_stdout
from team_assistant.memory import session_manager
from team_assistant.prompts import system_prompt
from team_assistant.recall import list_preferences, memory_tools
from team_assistant.titles import title_once

app = BedrockAgentCoreApp()
log = logging.getLogger("team_assistant")

# Session id -> the user who used it first, for the life of this process.
_SESSION_USERS: dict[str, str] = {}
OTHER_USERS_SESSION = "this session id is in use by another user; start a new conversation"


def _other_users_session(session_id: str | None, actor: str) -> bool:
    """True when another user used this session id first in this process.

    AgentCore Runtime sends every request with one session id to the same microVM, whoever
    the user is, and leaves the mapping of sessions to users to the agent (developer guide,
    runtime-sessions). Without this check two users who send the same session id would share
    one microVM: the same process and the same role credentials. Memory would still be
    separate, because it is keyed by user. The Runtime runs one session per microVM, so in a
    deployment the dictionary has one entry.
    """
    return bool(session_id) and _SESSION_USERS.setdefault(session_id, actor) != actor


class ModelCallError(Exception):
    """A model call failed. Carries the original error's type name and none of its text."""

    def __init__(self, error_type: str):
        super().__init__(error_type)
        self.error_type = error_type


class QuietErrors:
    """Mixin for a Strands model: an error of a model call leaves with its type, not its text.

    Strands copies an error's text into the status of the model, cycle and agent spans, and no
    redaction setting covers a span status. Error texts of Bedrock and botocore can quote
    values of the request ("Value '...' at 'toolConfig...'", "Invalid type for parameter ...,
    value: ..."). ``from None`` drops the original error, so a traceback does not print it
    either. The two types Strands reacts to (retry, shorter conversation) are kept.
    """

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        try:
            async for event in super().stream(*args, **kwargs):  # type: ignore[misc]
                yield event
        except ModelThrottledException:
            raise ModelThrottledException("the model call was throttled") from None
        except ContextWindowOverflowException:
            raise ContextWindowOverflowException("the conversation is too long") from None
        except Exception as error:  # noqa: BLE001 - every other error loses its text
            raise ModelCallError(type(error).__name__) from None


class QuietBedrockModel(QuietErrors, BedrockModel):
    """The Bedrock model the agent uses."""


def build_agent(
    config: AgentConfig,
    session_mgr: Any,
    actor: str,
    session_id: str,
    model: Model | None = None,
    tools: list | None = None,
    preferences: list[str] | None = None,
) -> Agent:
    """The Strands agent for one request. Tests pass their own ``model``."""
    return Agent(
        model=model
        or QuietBedrockModel(model_id=config.model_id, max_tokens=config.max_output_tokens),
        system_prompt=system_prompt(preferences or []),
        session_manager=session_mgr,
        tools=list(tools or []),
        # A long conversation keeps its beginning as a summary: before a model call that would
        # fill 70 % of the model's context window, the oldest messages are replaced by a
        # summary the same model writes. The summary is stored with the agent's state and
        # restored with the session. (The default manager drops messages beyond the last 40.)
        conversation_manager=SummarizingConversationManager(proactive_compression=True),
        # The default handler prints the answer to stdout, which is the Runtime's log.
        callback_handler=None,
        # Strands puts these on every span it creates (agent, cycle, model call, tool call), so
        # a trace can be found by user or by session. Ids only: no content goes into spans.
        trace_attributes={"actor.id": actor, "session.id": session_id},
    )


def _log(outcome: str, actor: str | None, session: str | None, usage: dict, **extra: str) -> None:
    line = {"event": "invocation", "outcome": outcome, "actor": actor, "session": session}
    log.info(json.dumps({**line, **usage, **extra}))


def _default_tools(on_auth_url: Callable[[str], None], state: str, return_to: str) -> list:
    return github_tools(on_auth_url, state, return_to)


def _default_recall(
    region: str, memory_id: str, actor: str, session_id: str, limit: int
) -> tuple[list[str], list]:
    """The user's preferences for the system prompt and the memory tools of this request.

    If the listing fails, the request is answered without preferences; the log names the
    error's type only (its message can quote a record).
    """
    client = _memory_client(region)
    try:
        preferences = list_preferences(client, memory_id, actor, limit)
    except Exception as error:  # noqa: BLE001
        line = {"event": "preferences_failed", "actor": actor, "session": session_id}
        log.warning(json.dumps({**line, "error_type": type(error).__name__}))
        preferences = []
    return preferences, memory_tools(client, memory_id, actor, session_id)


def _store_title(
    region: str, model_id: str, memory_id: str, actor: str, session: str, prompt: str, answer: str
) -> None:
    """Title the session after its first answer (titles.py), with the agent's own clients."""
    import boto3

    converse = boto3.client("bedrock-runtime", region_name=region).converse
    memory_client = boto3.client("bedrock-agentcore", region_name=region)
    title_once(memory_client, converse, model_id, memory_id, actor, session, prompt, answer)


async def _consent(
    payload: dict, context: Any, complete: Callable[..., None]
) -> AsyncIterator[dict]:
    """Forwarded GitHub consent: bind it to the user of this request's token."""
    usage = {"input_tokens": 0, "output_tokens": 0}
    try:
        actor = actor_from_headers(context.request_headers)
        token = bearer_from_headers(context.request_headers)
    except IdentityError as error:
        _log("rejected", None, None, usage, reason="identity")
        yield events.error(f"cannot tell who is asking: {error}")
        return
    if _other_users_session(context.session_id, actor):
        _log("rejected", actor, context.session_id, usage, reason="session of another user")
        yield events.error(OTHER_USERS_SESSION)
        return
    body = payload.get("consent")
    session_uri = body.get("session_uri") if isinstance(body, dict) else None
    try:
        complete(session_uri, token, os.environ.get("AWS_REGION", ""))
    except ConsentError as error:
        _log("rejected", actor, context.session_id, usage, reason="consent")
        yield events.error(str(error))
        return
    except Exception as error:  # noqa: BLE001 - the service's refusal becomes one error event
        code = getattr(error, "response", {}).get("Error", {}).get("Code", type(error).__name__)
        _log("consent_failed", actor, context.session_id, usage, error_code=code)
        yield events.error(f"the consent could not be completed ({code})")
        return
    _log("consent", actor, context.session_id, usage)
    yield events.consent_done()


def _memory_client(region: str) -> Any:
    import boto3  # the agent's own role reads Memory (ListSessions, ListEvents)

    return boto3.client("bedrock-agentcore", region_name=region)


async def _read_memory(
    payload: dict, context: Any, make_client: Callable[[str], Any]
) -> AsyncIterator[dict]:
    """The web page's requests: the user's conversations, or the history of the request's
    session. The actor is the token's, whatever the body says; no model call."""
    usage = {"input_tokens": 0, "output_tokens": 0}
    kind = "conversations" if "conversations" in payload else "history"
    try:
        actor = actor_from_headers(context.request_headers)
    except IdentityError as error:
        _log("rejected", None, None, usage, reason="identity")
        yield events.error(f"cannot tell who is asking: {error}")
        return
    memory_id, region = os.environ.get("MEMORY_ID"), os.environ.get("AWS_REGION")
    session_id = context.session_id
    if not memory_id or not region or (kind == "history" and not session_id):
        _log("rejected", actor, session_id, usage, reason="session id or agent settings")
        yield events.error("the request has no session id, or the agent is not configured")
        return
    if _other_users_session(session_id, actor):
        _log("rejected", actor, session_id, usage, reason="session of another user")
        yield events.error(OTHER_USERS_SESSION)
        return
    client = make_client(region)
    try:
        if kind == "conversations":
            items = await asyncio.to_thread(list_conversations, client, memory_id, actor)
            result = events.conversations(items)
        else:
            turns = await asyncio.to_thread(history, client, memory_id, actor, session_id)
            result = events.history(turns)
    except Exception as error:  # noqa: BLE001 - the service's refusal becomes one error event
        code = getattr(error, "response", {}).get("Error", {}).get("Code", type(error).__name__)
        _log("read_failed", actor, session_id, usage, error_code=code, request=kind)
        yield events.error(f"the {kind} could not be read ({code})")
        return
    _log(kind, actor, session_id, usage)
    yield result


async def respond(
    payload: Any,
    context: Any,
    *,
    config: AgentConfig | None = None,
    model: Model | None = None,
    make_session_manager: Callable[..., Any] | None = None,
    make_tools: Callable[[Callable[[str], None], str], list] | None = None,
    complete: Callable[..., None] = complete_consent,
    make_memory_client: Callable[[str], Any] = _memory_client,
    store_title: Callable[..., None] | None = None,
    recall: Callable[..., tuple[list[str], list]] | None = None,
) -> AsyncIterator[dict]:
    """Handle one request (see the module docstring). The keyword arguments are for tests."""
    if isinstance(payload, dict) and "consent" in payload:
        async for event in _consent(payload, context, complete):
            yield event
        return
    if isinstance(payload, dict) and ("conversations" in payload or "history" in payload):
        async for event in _read_memory(payload, context, make_memory_client):
            yield event
        return
    usage = {"input_tokens": 0, "output_tokens": 0}
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        _log("rejected", None, None, usage, reason="prompt")
        yield events.error("the request needs a non-empty 'prompt'")
        return
    try:
        actor = actor_from_headers(context.request_headers)
    except IdentityError as error:
        _log("rejected", None, None, usage, reason="identity")
        yield events.error(f"cannot tell who is asking: {error}")
        return
    session_id = context.session_id
    memory_id, region = os.environ.get("MEMORY_ID"), os.environ.get("AWS_REGION")
    if not session_id or not memory_id or not region:
        _log("rejected", actor, session_id, usage, reason="session id or agent settings")
        yield events.error("the request has no session id, or the agent is not configured")
        return
    if _other_users_session(session_id, actor):
        _log("rejected", actor, session_id, usage, reason="session of another user")
        yield events.error(OTHER_USERS_SESSION)
        return
    try:
        # Where the browser returns after a GitHub consent: chosen by the client's name from a
        # fixed list, never an address from the request.
        return_to = return_url(payload.get("client"), os.environ.get("WEB_URL"))
    except ValueError as error:
        _log("rejected", actor, session_id, usage, reason="client")
        yield events.error(str(error))
        return
    answer: list[str] = []
    try:
        # The context manager writes anything still buffered when the request ends.
        open_session = make_session_manager or session_manager
        config = config or load_config()
        # The tool may need the user's consent: it then hands the consent URL to on_auth_url
        # and waits for the token. The URL must reach the client at once, so the agent's events
        # and consent URLs go through one queue, read here in order of arrival.
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        state = secrets.token_urlsafe(16)

        def on_auth_url(url: str) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, ("auth_url", url))

        tools = (make_tools or partial(_default_tools, return_to=return_to))(on_auth_url, state)
        preferences, memory = await asyncio.to_thread(
            recall or _default_recall, region, memory_id, actor, session_id, config.max_preferences
        )
        tools = [*tools, *memory]
        called: set[str] = set()
        with open_session(memory_id, actor, session_id, region, config.retrieval) as session_mgr:
            agent = build_agent(config, session_mgr, actor, session_id, model, tools, preferences)
            # The session manager has restored the conversation into the agent: no message
            # means this is the session's first answer, the only one that gets a title. (Asking
            # Memory for a title event instead finds it only after a delay, so each answer would
            # store a new title.)
            first_answer = not agent.messages

            async def run() -> None:
                try:
                    async for event in agent.stream_async(prompt):
                        await queue.put(("agent", event))
                finally:
                    await queue.put(("end", None))

            task = asyncio.create_task(run())
            while True:
                kind, event = await queue.get()
                if kind == "end":
                    break
                if kind == "auth_url":
                    yield events.auth_url(event, state)
                elif isinstance(event.get("data"), str):
                    answer.append(event["data"])
                    yield events.text(event["data"])
                elif "current_tool_use" in event:
                    # One event per tool call: Strands repeats this for every piece of input.
                    use = event["current_tool_use"]
                    if use.get("toolUseId") and use["toolUseId"] not in called:
                        called.add(use["toolUseId"])
                        yield events.tool(str(use.get("name", "")))
                elif "result" in event:
                    total = event["result"].metrics.accumulated_usage
                    usage = {
                        "input_tokens": total.get("inputTokens", 0),
                        "output_tokens": total.get("outputTokens", 0),
                    }
            await task  # re-raises an error of the agent run
    except Exception as error:  # noqa: BLE001 - every failure becomes one error event
        # Only the error's type: its text can quote the prompt or the answer.
        error_type = getattr(error, "error_type", type(error).__name__)
        _log("failed", actor, session_id, usage, error_type=error_type)
        yield events.error("the agent could not answer; the operator can find the cause in the log")
        return
    # The title before done, so the page's list, refreshed on done, shows it. A failure costs
    # the title only, never the answer; the log names the error type.
    try:
        if first_answer:
            await asyncio.to_thread(
                store_title or _store_title,
                region,
                config.model_id,
                memory_id,
                actor,
                session_id,
                prompt,
                "".join(answer),
            )
    except Exception as error:  # noqa: BLE001 - only the type: the text can quote the exchange
        _log("title_failed", actor, session_id, usage, error_type=type(error).__name__)
    _log("ok", actor, session_id, usage)
    yield events.done(usage["input_tokens"], usage["output_tokens"])


@app.entrypoint
async def invoke(payload, context):
    async for event in respond(payload, context):
        yield event


if __name__ == "__main__":
    configure_stdout()
    # All interfaces: this runs only as the container's command, and the Runtime reaches the
    # container over its network. Left to itself, run() binds 127.0.0.1 unless it finds Docker.
    app.run(host="0.0.0.0")  # noqa: S104
