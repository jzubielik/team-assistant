"""make eval: the cases, the two checks, the stored tool calls and the result rows."""

from __future__ import annotations

import datetime
import json

import pytest
from test_smoke import load

evals = load("evals")


def test_the_committed_cases_load_and_have_unique_ids():
    cases = evals.load_cases()
    assert len({case["id"] for case in cases}) == len(cases) >= 17
    assert all(case["all_of"] or case["none_of"] or case["tool"] for case in cases)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "no case"),
        ('[[case]]\nid = "a"\n', "question must be"),
        ('[[case]]\nid = "a"\nquestion = "q"\nexpect = []\n', "unknown key"),
        ('[[case]]\nid = "a"\nquestion = "q"\nall_of = "x"\n', "all_of must be"),
        ('[[case]]\nid = "a"\nquestion = "q"\n[[case]]\nid = "a"\nquestion = "q"\n', "twice"),
    ],
)
def test_wrong_case_files_are_refused(tmp_path, text, message):
    path = tmp_path / "c.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match=message):
        evals.load_cases(path)


def test_an_entry_is_a_whole_word_either_of_two_or_in_its_exact_case():
    assert evals.has("ledger", "The Ledger service.")
    assert not evals.has("auth", "the gateway-auth service") and not evals.has("tax", "taxes")
    assert evals.has("two|2", "It needs 2 approvals.")
    assert evals.has("=Go", "written in Go") and not evals.has("=Go", "go ahead")
    assert evals.has("Monday", "on Mondays") and not evals.has("=Go", "Golang")


def stored(message: dict) -> dict:
    text = json.dumps({"message": message})
    return {"payload": [{"conversational": {"content": {"text": text}}}]}


def test_tool_calls_are_read_from_the_events_with_their_results():
    use = {"toolUseId": "t-1", "name": "search_memory", "input": {"query": "Who owns billing?"}}
    result = {"toolUseId": "t-1", "status": "success", "content": [{"text": "Facts:\n- x"}]}
    blob = json.dumps([json.dumps({"message": {"content": [{"toolResult": result}]}}), "user"])
    events = [
        stored({"content": [{"text": "Who owns billing?"}]}),
        stored({"content": [{"toolUse": use}]}),
        {"payload": [{"blob": blob}]},
        {"payload": [{"blob": json.dumps({"title": "Billing"})}]},
    ]
    assert evals.tool_calls(events) == [
        {
            "name": "search_memory",
            "input": {"query": "Who owns billing?"},
            "status": "success",
            "result": "Facts:\n- x",
        }
    ]


CASES = [
    {"id": "go", "question": "Which language?", "expected": "It is written in Go.",
     "all_of": ["=Go"], "none_of": ["Java"], "tool": "search_memory"},
    {"id": "free", "question": "Hello?", "expected": "", "all_of": [], "none_of": [], "tool": ""},
]  # fmt: skip


def test_every_case_is_asked_runs_times_and_each_run_has_every_check():
    asked = []

    def ask(question):
        asked.append(question)
        wrong = len(asked) == 2  # the second run of the first case
        return {
            "answer": "It is Java." if wrong else "It is Go.",
            "tools": [] if wrong else [{"name": "search_memory"}],
            "usage": {"input": 10, "output": 2},
            "seconds": 0.1,
        }

    rows = evals.evaluate(CASES, 2, ask)
    assert asked == ["Which language?", "Which language?", "Hello?", "Hello?"]
    assert [(row["id"], row["run"]) for row in rows] == [
        ("go", 1),
        ("go", 2),
        ("free", 1),
        ("free", 2),
    ]
    assert rows[0]["checks"] == {
        "Answered": {"passed": True, "reason": "answered"},
        "Words": {"passed": True, "reason": "missing: none; unwanted: none"},
        "Tool": {"passed": True, "reason": "wanted: search_memory; called: search_memory"},
    }
    assert rows[1]["checks"] == {
        "Answered": {"passed": True, "reason": "answered"},
        "Words": {"passed": False, "reason": "missing: =Go; unwanted: Java"},
        "Tool": {"passed": False, "reason": "wanted: search_memory; called: none"},
    }
    assert rows[1]["answer"] == "It is Java." and rows[1]["usage"] == {"input": 10, "output": 2}
    # A case without words or a tool passes when there is an answer.
    assert all(check["passed"] for check in rows[2]["checks"].values())


def test_a_run_with_an_error_or_an_exception_fails_and_the_other_runs_are_kept():
    def ask(question):
        if question == "Hello?":
            raise RuntimeError("credentials expired")
        tools = [{"name": "search_memory"}]
        return {"answer": "It is G", "tools": tools, "usage": {}, "seconds": 1.0, "error": "x"}

    stopped, raised = evals.evaluate(CASES, 1, ask)
    # The partial answer is kept, but the run is checked as one without an answer.
    assert stopped["answer"] == "It is G" and stopped["error"] == "x"
    assert not stopped["checks"]["Answered"]["passed"] and not stopped["checks"]["Words"]["passed"]
    assert raised["error"] == "RuntimeError: credentials expired" and raised["tools"] == []
    assert not raised["checks"]["Answered"]["passed"]


def judged(monkeypatch, fails=False):
    """A Judge whose model call is replaced. The stand-in passes an answer that is not about
    Java, uses 7 + 3 tokens, and keeps the prompts it got in ``judge.prompts``."""
    judge = evals.Judge(model="some-model")
    judge.prompts = []

    class Agent:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt, structured_output_model):
            if fails:
                raise RuntimeError("access denied")
            judge.prompts.append(prompt)
            passed = "Java" not in prompt
            found = structured_output_model(
                score=float(passed), test_pass=passed, reason="compared"
            )
            usage = {"inputTokens": 7, "outputTokens": 3}
            metrics = type("Metrics", (), {"accumulated_usage": usage})
            return type("Result", (), {"structured_output": found, "metrics": metrics})

    monkeypatch.setattr(evals, "Agent", Agent)
    return judge


def answers(text):
    return lambda question: {"answer": text, "tools": [], "usage": {}, "seconds": 0.1}


def test_the_judge_compares_the_answer_with_the_expected_one_and_notes_its_tokens(monkeypatch):
    judge = judged(monkeypatch)
    right, free = evals.evaluate(CASES, 1, answers("It is Go."), judge)
    assert right["checks"]["Judge"] == {
        "passed": True,
        "reason": "compared",
        "tokens": {"input": 7, "output": 3},
    }
    # The judge got the question, the answer, the expected answer and the rubric.
    (prompt,) = judge.prompts
    for part in ("Which language?", "It is Go.", "It is written in Go.", evals.RUBRIC):
        assert part in prompt
    # A case without an expected answer is not judged.
    assert free["checks"]["Judge"] == {"passed": None, "reason": "no expected answer"}
    wrong, _ = evals.evaluate(CASES, 1, answers("It is Java."), judged(monkeypatch))
    assert wrong["checks"]["Judge"]["passed"] is False


def test_a_run_without_an_answer_and_a_failed_judge_are_not_judged(monkeypatch):
    judge = judged(monkeypatch)
    empty, _ = evals.evaluate(CASES, 1, answers(""), judge)
    assert empty["checks"]["Judge"] == {"passed": None, "reason": "no answer"}
    assert judge.prompts == []
    failed, _ = evals.evaluate(CASES, 1, answers("It is Go."), judged(monkeypatch, fails=True))
    assert failed["checks"]["Judge"] == {
        "passed": None,
        "reason": "the judge failed (RuntimeError)",
    }
    # The other checks of that run are kept.
    assert failed["checks"]["Words"]["passed"] and failed["checks"]["Answered"]["passed"]


def test_every_judged_case_of_the_file_has_an_expected_answer():
    cases = {case["id"]: case for case in evals.load_cases()}
    without = sorted(name for name, case in cases.items() if not case["expected"])
    assert without == ["recall-today", "recall-when", "recall-yesterday"]


def test_the_header_names_the_commit_the_settings_and_the_files():
    now = datetime.datetime(2026, 11, 21, 12, 0, tzinfo=datetime.UTC)
    header = evals.run_header("dev", "first", 3, now)
    assert header["label"] == "first" and header["runs"] == 3 and header["written"] == now
    assert len(header["commit"]) == 40 and isinstance(header["uncommitted_changes"], bool)
    assert header["facts_top_k"] == 20 and header["max_turns"] == 6
    assert header["judge_model_id"] == header["model_id"]
    for key in ("system_prompt_sha256", "seed_sha256", "cases_sha256"):
        assert len(header[key]) == 12
