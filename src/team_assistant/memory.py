"""Conversation history in AgentCore Memory, one history per user and session.

The session manager is the class AWS ships in bedrock-agentcore for Strands agents. It writes
every message as an event under (memory id, actor id, session id) and loads the events of that
triple when the agent starts. The actor id is the user's id from the verified token
(identity.py), so one user cannot read another user's history by reusing a session id.

Long-term memory is in recall.py.
"""

from __future__ import annotations

from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)


def session_manager(
    memory_id: str, actor_id: str, session_id: str, region: str
) -> AgentCoreMemorySessionManager:
    """A session manager for one conversation. Creating it reads the session from AWS.

    Use it as a context manager, so messages still buffered are written when the request ends.
    """
    config = AgentCoreMemoryConfig(memory_id=memory_id, actor_id=actor_id, session_id=session_id)
    return AgentCoreMemorySessionManager(config, region_name=region)
