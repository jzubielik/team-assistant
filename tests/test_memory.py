"""memory.py: one conversation history per (memory, user, session)."""

from __future__ import annotations

import pytest

from team_assistant import memory
from team_assistant.config import RetrievalSetting

RETRIEVAL = {
    "/preferences/{actorId}/": RetrievalSetting(top_k=5, relevance_score=0.2),
    "/facts/{actorId}/": RetrievalSetting(top_k=3, relevance_score=0.3),
}


def test_config_carries_the_three_ids():
    config = memory.memory_config("mem-1", "user-1", "session-1", {})
    assert (config.memory_id, config.actor_id, config.session_id) == (
        "mem-1",
        "user-1",
        "session-1",
    )
    # Every message is written at once: nothing is lost when the runtime session ends.
    assert config.batch_size == 1


def test_no_retrieval_settings_mean_no_retrieval():
    assert memory.memory_config("mem-1", "user-1", "session-1", {}).retrieval_config is None


def test_retrieval_settings_are_passed_per_namespace():
    config = memory.memory_config("mem-1", "user-1", "session-1", RETRIEVAL)
    found = {
        namespace: (setting.top_k, setting.relevance_score)
        for namespace, setting in config.retrieval_config.items()
    }
    assert found == {"/preferences/{actorId}/": (5, 0.2), "/facts/{actorId}/": (3, 0.3)}


@pytest.mark.parametrize("bad", ["", "   ", None, 5])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_an_empty_id_is_refused(bad, position):
    ids = ["mem-1", "user-1", "session-1"]
    ids[position] = bad
    with pytest.raises(ValueError):
        memory.memory_config(*ids, {})


def test_session_manager_is_built_from_the_config_and_region(monkeypatch):
    built = []

    class Fake:
        def __init__(self, config, region_name=None):
            built.append((config, region_name))

    monkeypatch.setattr(memory, "AgentCoreMemorySessionManager", Fake)
    manager = memory.session_manager("mem-1", "user-1", "session-1", "eu-west-1", {})
    assert isinstance(manager, Fake)
    ((config, region),) = built
    assert (config.actor_id, config.session_id, region) == ("user-1", "session-1", "eu-west-1")


def test_session_manager_refuses_an_empty_actor_before_any_aws_call(monkeypatch):
    monkeypatch.setattr(
        memory, "AgentCoreMemorySessionManager", lambda *a, **k: pytest.fail("built")
    )
    with pytest.raises(ValueError, match="actor_id"):
        memory.session_manager("mem-1", "", "session-1", "eu-west-1", {})
