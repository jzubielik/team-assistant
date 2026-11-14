"""scripts/measure.py: the scripted conversations, the record filter and the report."""

from __future__ import annotations

import datetime

import pytest
from test_smoke import load

measure = load("measure")
T0 = datetime.datetime(2026, 11, 14, 12, 0, 0, tzinfo=datetime.UTC)


def at(seconds):
    return T0 + datetime.timedelta(seconds=seconds)


def test_the_shipped_conversations_load():
    conversations = measure.load_conversations()
    assert [c.name for c in conversations] == ["preference", "fact", "mixed"]
    assert all(c.messages and c.followup for c in conversations)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[[conversation]]\nname = "a"\nfollowup = "q"\n', "messages"),
        ('[[conversation]]\nname = "a"\nmessages = []\nfollowup = "q"\n', "messages"),
        ('[[conversation]]\nname = "a"\nmessages = ["m"]\n', "followup"),
        ('[[conversation]]\nmessages = ["m"]\nfollowup = "q"\n', "name"),
        ('[[conversation]]\nname = "a"\nmessages = ["m"]\nfollowup = "q"\nx = 1\n', "x"),
        ("", "no conversation"),
    ],
)
def test_wrong_conversation_files_are_refused(tmp_path, text, message):
    path = tmp_path / "c.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match=message):
        measure.load_conversations(path)


def test_only_records_created_since_the_conversation_began_count():
    records = [
        {"createdAt": at(-60), "content": {"text": "old"}},
        {"createdAt": at(70), "content": {"text": "new"}},
    ]
    assert [r["content"]["text"] for r in measure.new_records(records, since=at(0))] == ["new"]


def result():
    return measure.ConversationResult(
        name="fact",
        messages=["Billing is in Go."],
        usage=[(40, 10)],
        namespaces=[
            measure.NamespaceResult("/facts/{actorId}/", ["Billing is written in Go."], 63.0),
            measure.NamespaceResult("/preferences/{actorId}/", [], None),
        ],
        retrieved=[
            measure.Retrieved("/facts/{actorId}/", 5, 0.3, [(0.71, "Billing is written in Go.")]),
            measure.Retrieved("/preferences/{actorId}/", 5, 0.2, [(0.12, "Prefers Polish.")]),
        ],
        followup="Which language?",
        answer="Go.",
        followup_usage=(95, 3),
    )


def test_report_has_a_section_per_conversation_and_the_measured_values():
    report = measure.render_report([result()], env="dev", date="2026-11-14")
    assert report.startswith("# Memory measurement, dev, 2026-11-14")
    assert "## fact" in report
    assert "| `/facts/{actorId}/` | 63 s | Billing is written in Go. |" in report
    assert "| `/preferences/{actorId}/` | no new record | |" in report
    assert "Answer: Go." in report
    assert "95 in, 3 out" in report


def test_report_labels_retrieval_as_a_reproduction_and_marks_dropped_records():
    report = measure.render_report([result()], env="dev", date="2026-11-14")
    assert "client-side reproduction of the session manager's retrieval" in report
    assert "| `/facts/{actorId}/` | 0.71 | kept | Billing is written in Go. |" in report
    assert "| `/preferences/{actorId}/` | 0.12 | dropped (below 0.2) | Prefers Polish. |" in report


def test_results_name_namespaces_by_template_so_no_actor_id_reaches_the_report():
    # measure() fills in the actor only for the AWS calls; what it stores is the template.
    namespaces = measure.measured_namespaces()
    assert namespaces == [
        "/preferences/{actorId}/",
        "/facts/{actorId}/",
        "/summaries/{actorId}/{sessionId}/",
    ]
