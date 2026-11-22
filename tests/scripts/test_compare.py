"""make compare: two results of make eval side by side."""

from __future__ import annotations

import json

from test_smoke import load

compare = load("compare")


def row(case, run, passed, tools=1, tokens=100, judge=None):
    checks = {"Words": {"passed": passed, "reason": ""}, "Tool": {"passed": True, "reason": ""}}
    checks["Judge"] = {"passed": judge, "reason": ""}
    calls = [{"name": "search_memory"}] * tools
    return {"id": case, "run": run, "tools": calls, "usage": {"input": tokens}, "checks": checks}


def result(label, rows, **settings):
    run = {"label": label, "written": "2026-11-21T12:00:00+00:00", "facts_top_k": 20, **settings}
    return {"run": run, "cases": rows}


A = result("before", [row("go", 1, True), row("go", 2, False), row("old", 1, True)])
B = result(
    "after",
    [
        row("go", 1, True, tools=2, tokens=300, judge=True),
        row("go", 2, True, tools=1, tokens=100, judge=False),
    ],
    facts_top_k=50,
)


def test_a_case_counts_the_runs_that_passed_every_check_and_averages_tools_and_tokens():
    assert compare.summary(B, {})["go"] == {
        "runs": 2, "passed": 2, "judge": 1, "judged": 2, "right": None, "tools": 1.5, "in": 200,
    }  # fmt: skip
    assert (
        compare.summary(A, {"go#1": {"verdict": "right"}, "go#2": {"verdict": "wrong"}})["go"][
            "right"
        ]
        == 1
    )


def test_a_run_without_checks_or_tokens_does_not_pass_and_does_not_lower_the_average():
    failed = {"id": "go", "run": 3, "tools": [], "usage": {}, "checks": {}}
    found = compare.summary(result("x", [*B["cases"], failed]), {})["go"]
    assert (found["runs"], found["passed"], found["in"]) == (3, 2, 200)


def test_a_file_that_is_no_result_is_an_error_message(tmp_path, capsys):
    good, bad = tmp_path / "a.json", tmp_path / "b.json"
    good.write_text(json.dumps(A))
    bad.write_text("[]")
    assert compare.main([str(good), str(bad)]) == 1
    assert compare.main([str(good), str(tmp_path / "missing.json")]) == 1
    assert capsys.readouterr().err.count("error: ") == 2


def test_only_settings_that_differ_are_listed_and_never_the_label_or_the_time():
    assert compare.differences(A["run"], B["run"]) == ["facts_top_k: 20 -> 50"]


def test_the_table_has_a_row_per_case_of_either_result():
    lines = compare.render(A, {}, B, {}).splitlines()
    assert lines[0].startswith("A: before") and lines[1].startswith("B: after")
    assert "  facts_top_k: 20 -> 50" in lines
    table = {line.split()[0]: line.split()[1:] for line in lines[6:]}
    # A: not judged. B: the judge passed one of two runs.
    assert table["go"] == ["1/2", "-", "-", "1.0", "100", "2/2", "1/2", "-", "1.5", "200"]
    assert table["old"] == ["1/1", "-", "-", "1.0", "100", "-", "-", "-", "-", "-"]


def test_verdicts_are_read_from_the_file_beside_the_result(tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(A))
    b.write_text(json.dumps(B))
    (tmp_path / "b-verdicts.json").write_text(json.dumps({"go#1": {"verdict": "right"}}))
    assert compare.main([str(a), str(b)]) == 0
    out = capsys.readouterr().out
    assert "1/2" in [line.split()[8] for line in out.splitlines() if line.startswith("go")]
    assert compare.main([str(a)]) == 2
