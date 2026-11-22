"""Compare two results of make eval, case by case.

    python scripts/compare.py <a.json> <b.json>

For every case: in how many runs every check without a model passed, in how many of the
judged runs the judge passed, the tool calls per run and the input tokens per run, in A and
in B. Above the table: the settings that differ between the two
runs. A file ``<result>-verdicts.json`` beside a result, if there is one, holds a reader's
verdict per run, ``{"<case id>#<run>": {"verdict": "right" | "wrong" | "incomplete",
"note": "..."}}``; the table then also counts the runs judged right.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Not compared: they differ between any two runs.
_OWN = {"label", "written"}


def load(path: Path) -> tuple[dict, dict]:
    """The result and its verdicts ({} without a verdicts file)."""
    verdicts = path.with_name(f"{path.stem}-verdicts.json")
    result = json.loads(path.read_text())
    if not isinstance(result, dict) or not {"run", "cases"} <= set(result):
        raise ValueError(f"{path}: not a result of make eval")
    found = json.loads(verdicts.read_text()) if verdicts.exists() else {}
    if not isinstance(found, dict) or not all(isinstance(v, dict) for v in found.values()):
        raise ValueError(f"{verdicts}: must map '<case id>#<run>' to an object with a verdict")
    return result, found


def summary(result: dict, verdicts: dict) -> dict[str, dict]:
    """Per case id: runs, runs that passed every check without a model, runs the judge
    passed and runs it judged, runs a reader judged right (None without verdicts), tool calls
    per run and input tokens per run. The tokens are averaged over the runs that have them: a
    run that ended in an error has none."""
    found: dict[str, dict] = {}
    for row in result["cases"]:
        case = found.setdefault(
            row["id"],
            {"runs": 0, "passed": 0, "judge": 0, "judged": 0, "right": 0, "tools": 0, "in": 0}
            | {"counted": 0},
        )
        case["runs"] += 1
        checks = [check for name, check in row["checks"].items() if name != "Judge"]
        case["passed"] += bool(checks) and all(check["passed"] for check in checks)
        judge = row["checks"].get("Judge", {}).get("passed")
        case["judged"] += judge is not None
        case["judge"] += judge is True
        case["right"] += verdicts.get(f"{row['id']}#{row['run']}", {}).get("verdict") == "right"
        case["tools"] += len(row["tools"])
        if "input" in row["usage"]:
            case["in"] += row["usage"]["input"]
            case["counted"] += 1
    for case in found.values():
        case["tools"] = round(case["tools"] / case["runs"], 1)
        case["in"] = round(case["in"] / case.pop("counted")) if case["counted"] else 0
        if not verdicts:
            case["right"] = None
    return found


def differences(a: dict, b: dict) -> list[str]:
    keys = sorted((set(a) | set(b)) - _OWN)
    return [f"{key}: {a.get(key)} -> {b.get(key)}" for key in keys if a.get(key) != b.get(key)]


def _cell(case: dict | None) -> list[str]:
    if case is None:
        return ["-"] * 5
    right = "-" if case["right"] is None else f"{case['right']}/{case['runs']}"
    judge = f"{case['judge']}/{case['judged']}" if case["judged"] else "-"
    checks = f"{case['passed']}/{case['runs']}"
    return [checks, judge, right, str(case["tools"]), str(case["in"])]


def render(a: dict, a_verdicts: dict, b: dict, b_verdicts: dict) -> str:
    lines = [f"A: {a['run']['label']} ({a['run']['written']})"]
    lines.append(f"B: {b['run']['label']} ({b['run']['written']})")
    lines += ["", "Settings that differ, A -> B:"]
    lines += [f"  {line}" for line in differences(a["run"], b["run"])] or ["  none"]
    one, two = summary(a, a_verdicts), summary(b, b_verdicts)
    columns = ("checks", "judge", "right", "tools", "in")
    head = ["case", *(f"{side} {column}" for side in "AB" for column in columns)]
    rows = [head] + [
        [name, *_cell(one.get(name)), *_cell(two.get(name))] for name in {**one, **two}
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(head))]
    lines.append("")
    lines += [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: compare.py <a.json> <b.json>", file=sys.stderr)
        return 2
    try:
        (a, a_verdicts), (b, b_verdicts) = (load(Path(path)) for path in args)
        table = render(a, a_verdicts, b, b_verdicts)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (KeyError, TypeError, AttributeError) as error:
        print(
            f"error: not a result of make eval ({type(error).__name__}: {error})", file=sys.stderr
        )
        return 1
    print(table, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
