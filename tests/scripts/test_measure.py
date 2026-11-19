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
    names = [c.name for c in conversations]
    assert names == ["preference", "fact", "mixed", "short-followup"]
    assert all(c.messages and c.followup for c in conversations)
    # One question is a list of one; the last conversation asks a short second question.
    assert [len(c.followup) for c in conversations] == [1, 1, 1, 2]
    assert conversations[-1].followup[1] == "And the search one?"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[[conversation]]\nname = "a"\nfollowup = "q"\n', "messages"),
        ('[[conversation]]\nname = "a"\nmessages = []\nfollowup = "q"\n', "messages"),
        ('[[conversation]]\nname = "a"\nmessages = ["m"]\n', "followup"),
        ('[[conversation]]\nname = "a"\nmessages = ["m"]\nfollowup = []\n', "followup"),
        ('[[conversation]]\nname = "a"\nmessages = ["m"]\nfollowup = ["q", ""]\n', "followup"),
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
        followups=[
            measure.Followup(
                "Which language?",
                [
                    measure.Retrieved(
                        "/facts/{actorId}/", 5, 0.3, [(0.71, "Billing is written in Go.")]
                    ),
                    measure.Retrieved(
                        "/preferences/{actorId}/", 5, 0.2, [(0.12, "Prefers Polish.")]
                    ),
                ],
                "Go.",
                (95, 3),
                ["search_memory"],
            ),
            measure.Followup(
                "And who owns it?",
                [measure.Retrieved("/facts/{actorId}/", 5, 0.3, [])],
                "The payments team.",
                (120, 5),
                [],
            ),
        ],
    )


def test_each_followup_is_searched_alone_and_asked_in_the_same_new_session():
    asked, searched = [], []

    def ask(session, prompt):
        asked.append((session, prompt))
        return "ok", (1, 1), ["search_memory"]

    def search(namespace, query, top_k):
        searched.append(query)
        return []

    conversation = measure.Conversation("c", ["m"], ["Which language?", "And who owns it?"])
    record = {"createdAt": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)}
    found = measure.measure(conversation, ask, lambda namespace: [record], search, "actor-1")
    assert [f.question for f in found.followups] == conversation.followup
    assert [f.tools for f in found.followups] == [["search_memory"], ["search_memory"]]
    # No retrieval in config/agent.toml: preferences and facts are searched all the same.
    assert [r.namespace for r in found.followups[0].retrieved] == [
        "/preferences/{actorId}/",
        "/facts/{actorId}/",
    ]
    assert set(searched) == set(conversation.followup)
    (first, _), (second, q2), (third, q3) = asked
    assert second == third != first and (q2, q3) == tuple(conversation.followup)


def test_report_has_a_section_per_conversation_and_the_measured_values():
    report = measure.render_report([result()], env="dev", date="2026-11-14")
    assert report.startswith("# Memory measurement, dev, 2026-11-14")
    assert "## fact" in report
    assert "| `/facts/{actorId}/` | 63 s | Billing is written in Go. |" in report
    assert "| `/preferences/{actorId}/` | no new record | |" in report
    assert "Answer: Go." in report
    assert "95 in, 3 out" in report
    # The short second question has its own search result and answer.
    second = report.split("Follow-up: And who owns it?")[1]
    assert "| `/facts/{actorId}/` | | nothing found | |" in second
    assert "Answer: The payments team. (120 in, 5 out)" in second


def test_report_labels_retrieval_as_a_reproduction_and_marks_dropped_records():
    report = measure.render_report([result()], env="dev", date="2026-11-14")
    assert "client-side search with the question alone" in report
    assert "| `/facts/{actorId}/` | 0.71 | yes | Billing is written in Go. |" in report
    assert "| `/preferences/{actorId}/` | 0.12 | no (below 0.2) | Prefers Polish. |" in report
    assert "Tools the model called: search_memory" in report
    assert "Tools the model called: none" in report


def test_results_name_namespaces_by_template_so_no_actor_id_reaches_the_report():
    # measure() fills in the actor only for the AWS calls; what it stores is the template.
    namespaces = measure.measured_namespaces()
    assert namespaces == [
        "/preferences/{actorId}/",
        "/facts/{actorId}/",
        "/summaries/{actorId}/{sessionId}/",
    ]
