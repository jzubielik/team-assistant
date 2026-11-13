"""The page's mock agent speaks the agent's format."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from team_assistant import events

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "web" / "mock" / "scenarios"
SCENARIOS = sorted(SCENARIO_DIR.glob("*.json"))


def _events_in(value: object) -> list[object]:
    """Every value under an "event" key, at any depth of a scenario file."""
    found: list[object] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "event":
                found.append(item)
            else:
                found.extend(_events_in(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_events_in(item))
    return found


def test_there_are_scenarios():
    assert {p.stem for p in SCENARIOS} >= {
        "answer",
        "consent",
        "consent_done",
        "conversations",
        "error",
        "history",
    }


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenarios_match_agent_events(path):
    found = _events_in(json.loads(path.read_text()))
    assert found, f"{path.name} has no events"
    for event in found:
        assert events.matches(event), (path.name, event)


def test_matches_refuses_extra_missing_and_unknown():
    assert events.matches(events.text("x"))
    assert not events.matches({"type": "text", "data": "x", "extra": 1})
    assert not events.matches({"type": "auth_url", "url": "u"})
    assert not events.matches({"type": "later_feature"})
    assert not events.matches(["text"])


def test_constructors_build_exactly_their_fields():
    built = [
        events.text("a"),
        events.auth_url("u", "s"),
        events.done(1, 2),
        events.error("m"),
        events.consent_done(),
        events.conversations([]),
        events.history([]),
    ]
    assert {e["type"] for e in built} == set(events.EVENT_FIELDS)
    assert all(events.matches(e) for e in built)
