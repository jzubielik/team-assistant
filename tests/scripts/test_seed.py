"""scripts/seed.py: the conversation file, the wait for Memory and the written data."""

from __future__ import annotations

import datetime
import io

import pytest
from test_smoke import load

seed = load("seed")


def test_the_shipped_conversations_load():
    conversations = seed.load_conversations()
    assert len(conversations) == 36
    assert all(1 <= len(messages) <= 2 for messages in conversations)
    said = " ".join(message for messages in conversations for message in messages)
    # Groups a question can have many answers in.
    assert said.count("written in Go") >= 9
    assert said.count("owned by the payments team") >= 7
    # The last conversation changes the preference the first preference conversation set.
    assert "use Python unless I say otherwise" in said
    assert conversations[-1] == ["From now on use TypeScript for code examples, not Python."]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "no conversation"),
        ("[[conversation]]\n", "messages"),
        ("[[conversation]]\nmessages = []\n", "messages"),
        ('[[conversation]]\nmessages = ["m", ""]\n', "messages"),
        ('[[conversation]]\nmessages = ["m"]\nx = 1\n', "unknown key.*x"),
    ],
)
def test_wrong_files_are_refused(tmp_path, text, message):
    path = tmp_path / "seed.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match=message):
        seed.load_conversations(path)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_the_wait_ends_once_the_count_has_been_quiet_long_enough():
    clock = Clock()
    counts = iter([0, 3, 5, 5, 5, 5, 5])
    found = seed.wait_until_stable(
        lambda: next(counts), interval_s=10, quiet_s=30, limit_s=600, clock=clock, sleep=clock.sleep
    )
    # Changed last at 20 s (the read that saw 5); quiet for 30 s at 50 s.
    assert found == (5, True)
    assert clock.now == 50


def test_the_wait_gives_up_at_the_limit_and_says_so():
    clock = Clock()
    counts = iter(range(100))
    found = seed.wait_until_stable(
        lambda: next(counts), interval_s=10, quiet_s=30, limit_s=60, clock=clock, sleep=clock.sleep
    )
    assert found == (6, False)


def test_each_conversation_gets_its_own_session_which_is_stopped():
    calls, sessions = [], iter(["s-1", "s-2"])
    out = io.StringIO()
    seed.seed(
        [["a1", "a2"], ["b1"]],
        say=lambda session, message: calls.append(("say", session, message)),
        stop=lambda session: calls.append(("stop", session)),
        new_session=lambda: next(sessions),
        out=out,
    )
    assert calls == [
        ("say", "s-1", "a1"),
        ("say", "s-1", "a2"),
        ("stop", "s-1"),
        ("say", "s-2", "b1"),
        ("stop", "s-2"),
    ]
    assert "conversation 2 of 2 sent" in out.getvalue()


T0 = datetime.datetime(2026, 11, 19, 12, 0, 0, tzinfo=datetime.UTC)


def test_written_data_has_utc_times_and_no_actor_id():
    plus_one = datetime.timezone(datetime.timedelta(hours=1))
    record = {
        "memoryRecordId": "mem-0001",
        "namespaces": ["/facts/user-123/"],
        "createdAt": datetime.datetime(2026, 11, 19, 13, 0, 0, tzinfo=plus_one),
        "content": {"text": "said by user-123"},
        "score": None,
    }
    data = seed.as_data({"records": {"facts": [record]}, "n": 2}, "user-123")
    assert data == {
        "records": {
            "facts": [
                {
                    "memoryRecordId": "mem-0001",
                    "namespaces": ["/facts/{actorId}/"],
                    "createdAt": "2026-11-19T12:00:00+00:00",
                    "content": {"text": "said by {actorId}"},
                    "score": None,
                }
            ]
        },
        "n": 2,
    }
