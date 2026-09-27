"""Evaluate the triage agent over the labelled tickets with MLflow.

Usage: uv run python eval/run_eval.py

Runs the unchanged Epic 2 agent over every row of eval/labelled_tickets.csv
through `mlflow.genai.evaluate`, logs one run to the `triage-agent` experiment
in sqlite:///mlflow.db, and scores it with four code scorers: valid_schema,
category_match, priority_match and tool_order. Every escalation the agent asks
for is approved automatically and counted.
"""

import asyncio
import csv
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))  # `python eval/run_eval.py` only puts eval/ on the path

import mlflow  # noqa: E402
import mlflow.langchain  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from mlflow.genai.scorers import scorer  # noqa: E402

import agent  # noqa: E402
from triage.schema import TriageValidationError, validate_decision  # noqa: E402

LABELS_PATH = REPO_ROOT / "eval" / "labelled_tickets.csv"
TRACKING_URI = "sqlite:///" + (REPO_ROOT / "mlflow.db").as_posix()
EXPERIMENT = "triage-agent"
SCORER_NAMES = ("valid_schema", "category_match", "priority_match", "tool_order")


def load_rows(path: Path = LABELS_PATH) -> list[dict]:
    """One eval item per labelled ticket: the ticket ID as input, the labels as expectations."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [
            {
                "inputs": {"ticket_id": row["ticket_id"]},
                "expectations": {
                    "expected_category": row["expected_category"],
                    "expected_priority": row["expected_priority"],
                    "expected_tools": [t.strip() for t in row["expected_tools"].split(",")],
                    "judge_notes": row["judge_notes"],
                },
            }
            for row in csv.DictReader(f)
        ]


class AutoApprover:
    """Approves every escalation without asking anyone, and counts approvals per ticket.

    `start(ticket_id)` begins a ticket's attempt: it resets that ticket's count,
    so a ticket MLflow retries counts only its final attempt.
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def start(self, ticket_id: str) -> Callable[[dict], bool]:
        with self._lock:
            self.counts[ticket_id] = 0

        def approve(action: dict) -> bool:
            with self._lock:
                self.counts[ticket_id] += 1
            return True

        return approve

    @property
    def total(self) -> int:
        return sum(self.counts.values())


# --- Scorers: each returns 1 or 0. A failed prediction (no outputs) scores 0 on all four.


@scorer
def valid_schema(outputs) -> int:
    if outputs is None:
        return 0
    try:
        validate_decision(outputs)
    except TriageValidationError:
        return 0
    return 1


def _field_matches(outputs, expectations, field: str) -> int:
    expected = (expectations or {}).get(f"expected_{field}")
    if not isinstance(outputs, dict) or expected is None:
        return 0
    return int(outputs.get(field) == expected)


@scorer
def category_match(outputs, expectations) -> int:
    return _field_matches(outputs, expectations, "category")


@scorer
def priority_match(outputs, expectations) -> int:
    return _field_matches(outputs, expectations, "priority")


@scorer
def tool_order(outputs, trace) -> int:
    """1 when the first get_ticket span starts before the first get_customer_history span."""
    if outputs is None or trace is None:
        return 0
    spans = trace.data.spans
    ticket_starts = [s.start_time_ns for s in spans if s.name == "get_ticket"]
    history_starts = [s.start_time_ns for s in spans if s.name == "get_customer_history"]
    if not ticket_starts or not history_starts:
        return 0
    return int(min(ticket_starts) < min(history_starts))


SCORERS = [valid_schema, category_match, priority_match, tool_order]


class EvalRun(NamedTuple):
    result: object  # mlflow.genai EvaluationResult: run_id, metrics, result_df
    escalations: int  # total auto-approved escalations across all tickets


def run(rows: list[dict] | None = None, triage_fn=agent.triage) -> EvalRun:
    """Evaluate `triage_fn` over `rows` (default: every labelled ticket) in one MLflow run.

    Uses whatever tracking URI and experiment are active; `main()` sets them.
    Returns the evaluation result and the total number of auto-approved escalations.
    """
    rows = load_rows() if rows is None else rows
    approver = AutoApprover()

    @mlflow.trace
    def predict(ticket_id: str) -> dict:
        # One trace per ticket, so an escalation's resumed agent call lands in it too.
        return asyncio.run(triage_fn(ticket_id, approve=approver.start(ticket_id)))

    os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", "1")
    os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_RETRIES", "6")
    # predict is already traced; skip MLflow's check, which would call the agent an
    # extra time on the first ticket and abort the whole run if that ticket fails.
    os.environ.setdefault("MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "true")

    result = mlflow.genai.evaluate(data=rows, predict_fn=predict, scorers=SCORERS)
    return EvalRun(result, approver.total)


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    mlflow.set_tracking_uri(TRACKING_URI)
    mlflow.set_experiment(EXPERIMENT)
    mlflow.langchain.autolog()

    result, escalations = run()

    print(f"MLflow run: {result.run_id}")
    for name in SCORER_NAMES:
        mean = result.metrics.get(f"{name}/mean")
        print(f"{name}: {'n/a' if mean is None else f'{mean:.2f}'}")
    print(f"Escalations auto-approved: {escalations}")


if __name__ == "__main__":
    main()
