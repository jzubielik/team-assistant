"""Conversation history in AgentCore Memory, one history per user and session.

The session manager is the class AWS ships in bedrock-agentcore for Strands agents. It writes
every message as an event under (memory id, actor id, session id) and loads the events of that
triple when the agent starts. The actor id is the user's id from the verified token
(identity.py).

Every call to Memory goes to the Memory gateway with the user's token, not to Memory with the
agent's role: the gateway permits a call only for the token's user (docs/adr/0008).

Long-term memory is in recall.py.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import boto3
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from botocore import UNSIGNED
from botocore.config import Config

from team_assistant.config import MEMORY_TARGET


def gateway_client(gateway_url: str, region: str, token: str) -> Any:
    """A Memory client (boto3 ``bedrock-agentcore``) whose requests go to the gateway's Memory
    target, unsigned, with ``token`` as the bearer token. Only the host of ``gateway_url`` is
    used: the target's path follows it.
    """
    client = boto3.client(
        "bedrock-agentcore",
        region_name=region,
        endpoint_url=f"https://{urlsplit(gateway_url).netloc}/{MEMORY_TARGET}",
        config=Config(signature_version=UNSIGNED),
    )

    def authorize(request, **kwargs) -> None:
        request.headers["Authorization"] = f"Bearer {token}"

    client.meta.events.register("before-send.bedrock-agentcore.*", authorize)
    return client


class _Session(boto3.Session):
    """A boto3 session that hands out ``client`` as its Memory client."""

    def __init__(self, client: Any, region: str) -> None:
        super().__init__(region_name=region)
        self._memory_client = client

    def client(self, service_name: str, *args: Any, **kwargs: Any) -> Any:
        if service_name == "bedrock-agentcore":
            return self._memory_client
        return super().client(service_name, *args, **kwargs)


def session_manager(
    memory_id: str, actor_id: str, session_id: str, region: str, client: Any
) -> AgentCoreMemorySessionManager:
    """A session manager for one conversation that calls Memory with ``client``
    (gateway_client). Creating it reads the session.

    Use it as a context manager, so messages still buffered are written when the request ends.
    """
    config = AgentCoreMemoryConfig(memory_id=memory_id, actor_id=actor_id, session_id=session_id)
    # The manager builds its clients from the session when it is created.
    return AgentCoreMemorySessionManager(
        config, region_name=region, boto_session=_Session(client, region)
    )
