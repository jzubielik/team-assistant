"""Conversation history in AgentCore Memory, one history per user and session.

The session manager is the class AWS ships in bedrock-agentcore for Strands agents. It writes
every message as an event under (memory id, actor id, session id) and loads the events of that
triple when the agent starts. The actor id is the user's id from the verified token
(identity.py), so one user cannot read another user's history by reusing a session id.

Long-term memory: strategies in the stack extract records into config.NAMESPACES. Before each
model call the session manager searches the namespaces named in the retrieval settings with the
user's message and prepends what it finds to that message, inside <user_context>
(bedrock-agentcore 1.26.0, session_manager.py, retrieve_customer_context). A failed search is
logged by the library (WARNING "Memory retrieval failed ...") and the agent answers without
those records.
"""

from __future__ import annotations

from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)

from team_assistant.config import RetrievalSetting


def memory_config(
    memory_id: str, actor_id: str, session_id: str, retrieval: dict[str, RetrievalSetting]
) -> AgentCoreMemoryConfig:
    """Settings for one conversation. An empty id is an error, never a shared default."""
    for name, value in (
        ("memory_id", memory_id),
        ("actor_id", actor_id),
        ("session_id", session_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    retrieval_config = {
        namespace: RetrievalConfig(top_k=s.top_k, relevance_score=s.relevance_score)
        for namespace, s in retrieval.items()
    }
    return AgentCoreMemoryConfig(
        memory_id=memory_id,
        actor_id=actor_id,
        session_id=session_id,
        # None, not {}: no retrieval settings mean no search before the model call.
        retrieval_config=retrieval_config or None,
    )


def session_manager(
    memory_id: str,
    actor_id: str,
    session_id: str,
    region: str,
    retrieval: dict[str, RetrievalSetting],
) -> AgentCoreMemorySessionManager:
    """A session manager for one conversation. Creating it reads the session from AWS.

    Use it as a context manager, so messages still buffered are written when the request ends.
    """
    return AgentCoreMemorySessionManager(
        memory_config(memory_id, actor_id, session_id, retrieval), region_name=region
    )
