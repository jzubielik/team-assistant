"""memory.py: one conversation history per (memory, user, session)."""

from __future__ import annotations

from team_assistant import memory


def test_session_manager_is_built_from_the_three_ids_and_the_region(monkeypatch):
    built = []

    class Fake:
        def __init__(self, config, region_name=None):
            built.append((config, region_name))

    monkeypatch.setattr(memory, "AgentCoreMemorySessionManager", Fake)
    manager = memory.session_manager("mem-1", "user-1", "session-1", "eu-west-1")
    assert isinstance(manager, Fake)
    ((config, region),) = built
    assert (config.memory_id, config.actor_id, config.session_id, region) == (
        "mem-1",
        "user-1",
        "session-1",
        "eu-west-1",
    )
    # Every message is written at once: nothing is lost when the runtime session ends.
    assert config.batch_size == 1
    # Nothing is searched before a model call.
    assert config.retrieval_config is None
