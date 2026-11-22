"""Agent settings from config/agent.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "agent.toml"
# Limits of AgentCore Memory for event expiry (aws-cdk-lib 2.275.0, memory/memory.js).
EXPIRY_DAYS_MIN, EXPIRY_DAYS_MAX = 7, 365
_TOP_LEVEL = {"model_id", "max_output_tokens", "memory", "tracing"}
_TRACING = {"enabled"}
_MEMORY = {"event_expiry_days", "max_preferences", "facts_top_k", "summaries_top_k"}
# Upper limit of memory.max_preferences: each record is a line of the system prompt.
MAX_PREFERENCES = 100
# Upper limit of memory.facts_top_k and memory.summaries_top_k: the largest topK that
# RetrieveMemoryRecords accepts.
TOP_K_LIMIT = 100
# Both when the key is absent.
TOP_K_DEFAULT = 5
# The most turns of one request (Strands ``limits``). A turn is one model call and the tool
# calls it asks for.
MAX_TURNS = 6

# Where the browser returns after a user's consent in GitHub: a listener of that user's own
# make chat (scripts/chat.py). make deploy registers it on the agent's workload identity, and the
# agent names it in every token request. Users have no AWS credentials,
# so the client forwards the consent session to the agent, which completes the binding.
GITHUB_RETURN_URL = "http://localhost:8977/github/callback"
# The web page's return page: on the development server (make web), and on the
# hosted page, whose address the stack gives the Runtime as WEB_URL. The request names
# its client; the address always comes from here, so a request cannot send a consent elsewhere.
WEB_LOCAL_RETURN_URL = "http://localhost:5173/callback.html"
CLIENTS = ("cli", "web", "web-local")


def return_url(client: object, web_url: str | None) -> str:
    """The GitHub return URL for the request's client; ValueError for an unknown one."""
    if client is None or client == "cli":
        return GITHUB_RETURN_URL
    if client == "web-local":
        return WEB_LOCAL_RETURN_URL
    if client == "web" and web_url:
        return f"{web_url.rstrip('/')}/callback.html"
    raise ValueError(f"unknown client; expected one of {', '.join(CLIENTS)}")


# Where each long-term strategy stores its records, keyed by strategy name. It lives here, not
# in memory.py, because the CDK app imports this module and has no bedrock-agentcore package.
# The stack, the agent and the operator scripts all read it, so they cannot
# disagree. {actorId} and {sessionId} are filled in by the service; the leading and trailing
# slash keep one actor's prefix from matching another's (AgentCore devguide, "Memory
# organization").
NAMESPACES = {
    "preferences": "/preferences/{actorId}/",
    "facts": "/facts/{actorId}/",
    # A summary describes one session, so its namespace needs {sessionId}.
    "summaries": "/summaries/{actorId}/{sessionId}/",
}


@dataclass(frozen=True)
class AgentConfig:
    model_id: str
    max_output_tokens: int
    event_expiry_days: int
    # Whether the agent exports spans (stack.py turns this into the Runtime's environment).
    tracing: bool = False
    # How many of the user's newest preference records go into the system prompt; 0: none.
    max_preferences: int = 0
    # The most facts one search_memory call returns.
    facts_top_k: int = TOP_K_DEFAULT
    # The most summaries one recall_conversations call returns.
    summaries_top_k: int = TOP_K_DEFAULT


def load_config(path: Path = DEFAULT_PATH) -> AgentConfig:
    """Read and check the settings. ValueError names the key that is wrong."""
    data = tomllib.loads(path.read_text())
    memory = data.get("memory", {})
    # A misspelled key would otherwise be ignored and the default silently used.
    unknown = sorted(set(data) - _TOP_LEVEL) + sorted(f"memory.{k}" for k in set(memory) - _MEMORY)
    unknown += sorted(f"tracing.{k}" for k in set(data.get("tracing", {})) - _TRACING)
    if unknown:
        raise ValueError(f"unknown key(s) in {path.name}: {', '.join(unknown)}")
    model_id = data.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        raise ValueError("model_id must be a non-empty string")
    max_output_tokens = data.get("max_output_tokens")
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be a positive integer")
    expiry = memory.get("event_expiry_days")
    if type(expiry) is not int or not EXPIRY_DAYS_MIN <= expiry <= EXPIRY_DAYS_MAX:
        limits = f"{EXPIRY_DAYS_MIN} to {EXPIRY_DAYS_MAX}"
        raise ValueError(f"memory.event_expiry_days must be an integer from {limits}")
    # No [tracing] section means off; a section must say which.
    tracing = data["tracing"].get("enabled") if "tracing" in data else False
    if type(tracing) is not bool:
        raise ValueError("tracing.enabled must be true or false")
    # No max_preferences means none.
    preferences = memory.get("max_preferences", 0)
    if type(preferences) is not int or not 0 <= preferences <= MAX_PREFERENCES:
        raise ValueError(f"memory.max_preferences must be an integer from 0 to {MAX_PREFERENCES}")
    top_k = {}
    for key in ("facts_top_k", "summaries_top_k"):
        top_k[key] = memory.get(key, TOP_K_DEFAULT)
        if type(top_k[key]) is not int or not 1 <= top_k[key] <= TOP_K_LIMIT:
            raise ValueError(f"memory.{key} must be an integer from 1 to {TOP_K_LIMIT}")
    return AgentConfig(model_id, max_output_tokens, expiry, tracing, preferences, **top_k)
