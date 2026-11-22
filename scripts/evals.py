"""Ask the deployed agent the cases of scripts/evals.toml and write down what happened.

    python scripts/evals.py --profile <p> --region <r> --gateway-url <url> \
        --gateway-target <name> --memory-id <id> --login-domain <url> --client-id <id> \
        --env <env> --user <user> --label <label> --runs <n> --out <file>

For a user whose memory make seed has filled. Every case is asked ``runs`` times, each time
in a new session that is stopped afterwards. Strands Evals runs the cases and four checks:
``Answered``, ``Words`` and ``Tool`` need no model; ``Judge`` asks the agent's own model
whether the answer agrees with the case's ``expected`` answer. The result is one JSON file:

- ``run``: the commit and the settings of the working tree the command ran from, and the
  hashes of the system prompt, scripts/seed.toml and scripts/evals.toml. The deployed agent
  has these settings only if it was deployed from this tree.
- ``cases``: for every case and run the question, the answer, every tool call with its
  arguments and its result (read from the session's stored events), the tokens, the seconds
  and each check with its reason. A check that did not apply has ``passed: null``.

The user's id is replaced by ``{actorId}``. Compare two files with scripts/compare.py.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

from strands import Agent
from strands_evals import Case, Experiment
from strands_evals.evaluators import Evaluator, OutputEvaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import chat  # noqa: E402
import memory  # noqa: E402
from seed import as_data  # noqa: E402

from team_assistant.config import MAX_TURNS, load_config  # noqa: E402
from team_assistant.prompts import SYSTEM_PROMPT  # noqa: E402

HERE = Path(__file__).resolve().parent
CASES_PATH, SEED_PATH = HERE / "evals.toml", HERE / "seed.toml"
_KEYS = {"id", "question", "expected", "all_of", "none_of", "tool"}
LABEL = re.compile(r"[a-z0-9][a-z0-9-]*")
# The events of a session can lag behind the answer: how often and how long to read again.
READS, READ_PAUSE_S = 6, 2


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    """The cases of the file. ValueError names what is wrong."""
    items = tomllib.loads(path.read_text()).get("case", [])
    if not items:
        raise ValueError(f"{path.name}: no case")
    seen: set[str] = set()
    for number, item in enumerate(items, 1):
        where = f"{path.name}, case {number}"
        unknown = sorted(set(item) - _KEYS)
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}")
        for key in ("id", "question"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"{where}: {key} must be a non-empty string")
        if item["id"] in seen:
            raise ValueError(f"{where}: id '{item['id']}' is used twice")
        seen.add(item["id"])
        for key in ("all_of", "none_of"):
            words = item.setdefault(key, [])
            if not isinstance(words, list) or not all(isinstance(w, str) and w for w in words):
                raise ValueError(f"{where}: {key} must be a list of non-empty strings")
        for key in ("tool", "expected"):
            if not isinstance(item.setdefault(key, ""), str):
                raise ValueError(f"{where}: {key} must be a string")
    return items


def has(entry: str, text: str) -> bool:
    """Whether one entry stands in the text as a whole word, also with a plural s ("a|b":
    either; "=Go": this case)."""
    for option in entry.split("|"):
        exact = option.startswith("=")
        pattern = r"(?<![\w-])" + re.escape(option.removeprefix("=")) + r"s?(?![\w-])"
        if re.search(pattern, text, 0 if exact else re.IGNORECASE):
            return True
    return False


class Words(Evaluator[str, str]):
    """Every ``all_of`` entry of the case is in the answer and no ``none_of`` entry."""

    def evaluate(self, evaluation_case: EvaluationData[str, str]) -> list[EvaluationOutput]:
        case, answer = evaluation_case.metadata or {}, str(evaluation_case.actual_output or "")
        missing = [w for w in case.get("all_of", []) if not has(w, answer)]
        unwanted = [w for w in case.get("none_of", []) if has(w, answer)]
        passed = not missing and not unwanted
        reason = (
            f"missing: {', '.join(missing) or 'none'}; unwanted: {', '.join(unwanted) or 'none'}"
        )
        return [EvaluationOutput(score=float(passed), test_pass=passed, reason=reason)]


class Answered(Evaluator[str, str]):
    """The agent answered: there is a text and no error."""

    def evaluate(self, evaluation_case: EvaluationData[str, str]) -> list[EvaluationOutput]:
        passed = bool(str(evaluation_case.actual_output or "").strip())
        reason = "answered" if passed else "no answer, or the agent reported an error"
        return [EvaluationOutput(score=float(passed), test_pass=passed, reason=reason)]


class Tool(Evaluator[str, str]):
    """The model called the case's ``tool``; a case without one passes."""

    def evaluate(self, evaluation_case: EvaluationData[str, str]) -> list[EvaluationOutput]:
        wanted = (evaluation_case.metadata or {}).get("tool", "")
        called = [call["name"] for call in evaluation_case.actual_trajectory or []]
        passed = not wanted or wanted in called
        reason = f"wanted: {wanted or 'any'}; called: {', '.join(called) or 'none'}"
        return [EvaluationOutput(score=float(passed), test_pass=passed, reason=reason)]


# The label of a check that did not apply; the result then has passed: null.
NOT_JUDGED = "NOT_JUDGED"
RUBRIC = (
    "The expected output is the reference answer. Pass the answer only if it states everything "
    "the reference states and nothing that contradicts it. Remarks beyond the reference are "
    "fine when they contradict nothing in it. When the reference says that nothing is stored, "
    "the answer passes only if it says so and gives no value of its own. Wording, order and "
    "formatting do not matter. In the reason, name every statement of the answer that the "
    "reference does not state."
)


class Judge(OutputEvaluator):
    """A model compares the answer with the case's ``expected`` answer.

    Not judged: a case without ``expected``, a run without an answer, and a judge call that
    failed (the reason says which). The tokens of the call are in the result's metadata.
    """

    def __init__(self, model):
        super().__init__(rubric=RUBRIC, model=model)

    def evaluate(self, evaluation_case: EvaluationData[str, str]) -> list[EvaluationOutput]:
        def skipped(reason: str) -> list[EvaluationOutput]:
            return [EvaluationOutput(score=0.0, test_pass=False, reason=reason, label=NOT_JUDGED)]

        if not evaluation_case.expected_output:
            return skipped("no expected answer")
        if not str(evaluation_case.actual_output or "").strip():
            return skipped("no answer")
        try:
            agent = Agent(model=self.model, system_prompt=self.system_prompt, callback_handler=None)
            result = agent(
                self._build_prompt(evaluation_case), structured_output_model=EvaluationOutput
            )
            usage = result.metrics.accumulated_usage
            tokens = {"input": usage.get("inputTokens", 0), "output": usage.get("outputTokens", 0)}
            return [result.structured_output.model_copy(update={"label": None, "metadata": tokens})]
        except Exception as failure:  # noqa: BLE001 - a failed judge must not lose the run
            return skipped(f"the judge failed ({type(failure).__name__})")

    async def evaluate_async(
        self, evaluation_case: EvaluationData[str, str]
    ) -> list[EvaluationOutput]:
        return await asyncio.to_thread(self.evaluate, evaluation_case)


def tool_calls(events: list[dict]) -> list[dict]:
    """The tool calls stored in a session's events, oldest first: name, arguments, and the
    status and text of the result. The session manager stores every Strands message as JSON,
    in a ``conversational`` payload or, when large, in a ``blob`` holding ``[json, role]``."""
    calls: dict[str, dict] = {}
    for event in events:
        for item in event.get("payload", []):
            stored = item.get("conversational", {}).get("content", {}).get("text", "")
            try:
                if "blob" in item:
                    stored = json.loads(item["blob"])[0]
                content = json.loads(stored)["message"]["content"]
            except (ValueError, KeyError, TypeError, IndexError):
                continue
            for block in content:
                if "toolUse" in block:
                    use = block["toolUse"]
                    calls[use.get("toolUseId", "")] = {
                        "name": use.get("name", ""),
                        "input": use.get("input"),
                        "status": None,
                        "result": None,
                    }
                elif "toolResult" in block:
                    result = block["toolResult"]
                    call = calls.get(result.get("toolUseId", ""))
                    if call is not None:
                        parts = result.get("content", [])
                        call["status"] = result.get("status")
                        call["result"] = "\n".join(p.get("text", json.dumps(p)) for p in parts)
    return list(calls.values())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def run_header(env: str, label: str, runs: int, now: datetime.datetime) -> dict:
    """What a result must carry to be compared with another one."""
    config = load_config()
    return {
        "env": env,
        "label": label,
        "written": now,
        "runs": runs,
        "commit": _git("rev-parse", "HEAD"),
        "uncommitted_changes": bool(_git("status", "--porcelain")),
        "model_id": config.model_id,
        "judge_model_id": config.model_id,
        "max_preferences": config.max_preferences,
        "facts_top_k": config.facts_top_k,
        "summaries_top_k": config.summaries_top_k,
        "max_turns": MAX_TURNS,
        "system_prompt_sha256": _sha(SYSTEM_PROMPT),
        "seed_sha256": _sha(SEED_PATH.read_text()),
        "cases_sha256": _sha(CASES_PATH.read_text()),
    }


def evaluate(
    cases: list[dict], runs: int, ask: Callable[[str], dict], judge: Evaluator | None = None
) -> list[dict]:
    """Ask every case ``runs`` times and check the answers, with ``judge`` as a fourth check.

    ``ask(question)`` returns ``answer``, ``tools`` (see tool_calls), ``usage``, ``seconds``
    and, when the question failed, ``error``. A run with an error is checked as if it had no
    answer. An exception of ``ask`` becomes such a run: Strands Evals would otherwise drop it.
    """
    asked: dict[str, dict] = {}

    def task(case: Case) -> dict:
        try:
            found = ask(case.input)
        except Exception as failure:  # noqa: BLE001 - one failed question must not lose the run
            error = f"{type(failure).__name__}: {failure}"
            found = {"answer": "", "tools": [], "usage": {}, "seconds": 0.0, "error": error}
        asked[case.name] = found
        return {"output": "" if "error" in found else found["answer"], "trajectory": found["tools"]}

    experiment = Experiment[str, str](
        cases=[
            Case(
                name=f"{case['id']}#{run}",
                input=case["question"],
                expected_output=case["expected"] or None,
                metadata=case,
            )
            for case in cases
            for run in range(1, runs + 1)
        ],
        evaluators=[Answered(), Words(), Tool(), *([judge] if judge else [])],
    )
    report = experiment.run_evaluations(task)
    checks: dict[str, dict] = {}
    for row, (verdict,) in zip(report.cases, report.detailed_results, strict=True):
        check = {"passed": None if verdict.label == NOT_JUDGED else verdict.test_pass}
        check["reason"] = verdict.reason
        if verdict.metadata:
            check["tokens"] = verdict.metadata
        checks.setdefault(row["name"], {})[row["evaluator"]] = check
    rows = []
    for case in cases:
        for run in range(1, runs + 1):
            name = f"{case['id']}#{run}"
            row = {"id": case["id"], "run": run, "question": case["question"], **asked[name]}
            rows.append({**row, "checks": checks.get(name, {})})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in (
        "profile",
        "region",
        "gateway-url",
        "gateway-target",
        "memory-id",
        "login-domain",
        "client-id",
        "env",
        "user",
        "label",
        "out",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args(argv)
    if not LABEL.fullmatch(args.label):
        parser.error("--label takes lower-case letters, digits and hyphens")
    if args.runs < 1:
        parser.error("--runs must be 1 or more")
    cases = load_cases()
    import boto3
    from strands.models import BedrockModel

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("bedrock-agentcore")
    # The judge is the agent's model, called with the operator's profile.
    judge = Judge(BedrockModel(model_id=load_config().model_id, boto_session=session))
    endpoints = auth.Endpoints(args.login_domain, args.client_id)
    url = chat.invoke_url(args.gateway_url, args.gateway_target)
    stop_url = chat.stop_url(args.gateway_url, args.gateway_target)

    def token() -> str:
        return auth.access_token(endpoints, args.env, args.user)

    actor = auth._claims(token())["sub"]

    def stored_calls(session: str, announced: int) -> list[dict]:
        calls: list[dict] = []
        for _ in range(READS):
            calls = tool_calls(memory.list_events(client, args.memory_id, actor, session))
            if len(calls) >= announced and all(call["status"] for call in calls):
                break
            time.sleep(READ_PAUSE_S)
        return calls

    def ask(question: str) -> dict:
        session, started = chat.new_session_id(), time.monotonic()
        parts, usage, announced, error = [], {}, 0, None
        try:
            for event in chat.invoke(url, token(), session, question):
                if event.get("type") == "text":
                    parts.append(event.get("data", ""))
                elif event.get("type") == "tool":
                    announced += 1
                elif event.get("type") == "done":
                    usage = event.get("usage", {})
                elif event.get("type") == "error" or "error" in event:
                    error = str(event.get("message") or event.get("error"))
        except chat.ChatError as failure:
            error = str(failure)
        seconds = round(time.monotonic() - started, 1)
        # A failure after the answer keeps the answer; the row says what is missing.
        tools: list[dict] = []
        try:
            tools = stored_calls(session, announced)
        except Exception as failure:  # noqa: BLE001
            error = error or f"tool calls not read ({type(failure).__name__})"
        try:
            list(chat.invoke(stop_url, token(), session, None))
        except Exception as failure:  # noqa: BLE001
            print(f"    session not stopped ({type(failure).__name__})", flush=True)
        print(f"    {question[:70]}", flush=True)
        found = {"answer": "".join(parts), "tools": tools, "usage": usage, "seconds": seconds}
        return {**found, "error": error} if error else found

    print(f"--- {len(cases)} case(s), {args.runs} run(s) each", flush=True)
    header = run_header(args.env, args.label, args.runs, datetime.datetime.now(datetime.UTC))
    found = {"run": header, "cases": evaluate(cases, args.runs, ask, judge)}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(as_data(found, actor), indent=2, ensure_ascii=False) + "\n")
    failed = sum(
        not row["checks"] or any(c["passed"] is False for c in row["checks"].values())
        for row in found["cases"]
    )
    print(f"{len(found['cases']) - failed} of {len(found['cases'])} passed every check")
    print(f"result written to {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (chat.ChatError, auth.LoginRequired, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
