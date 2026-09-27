"""Tests for the eval (eval/run_eval.py). The scorers are called directly with
hand-made outputs and spans; the full runs use the scripted model from
test_agent.py with the real MCP tools, against a temporary MLflow store.
No API keys and no network."""

import asyncio
import functools
import os
import re
from types import SimpleNamespace

import mlflow
import mlflow.langchain
import pytest

import load_seed
from eval import run_eval
from test_agent import P1_ACCESS, REASON, VALID, scripted

EXPECT = {
    "expected_category": "billing",
    "expected_priority": "P2",
    "expected_tools": ["get_ticket", "get_customer_history"],
    "judge_notes": "Double charge is a money problem (P2).",
}


def fake_trace(*names):
    """A trace whose spans have the given names, starting in that order."""
    spans = [SimpleNamespace(name=name, start_time_ns=1000 * i) for i, name in enumerate(names, 1)]
    return SimpleNamespace(data=SimpleNamespace(spans=spans))


IN_ORDER = fake_trace("triage", "get_ticket", "get_customer_history")


def scores(outputs, expectations=EXPECT, trace=IN_ORDER):
    return {
        # Scorer.run passes each scorer only the arguments it takes, as MLflow does.
        s.name: s.run(inputs={"ticket_id": "T-1042"}, outputs=outputs, expectations=expectations, trace=trace)
        for s in run_eval.SCORERS
    }


ALL_ONE = dict.fromkeys(run_eval.SCORER_NAMES, 1)
ALL_ZERO = dict.fromkeys(run_eval.SCORER_NAMES, 0)


# --- Rows


def test_rows_cover_every_labelled_ticket():
    rows = run_eval.load_rows()

    assert len(rows) == 20
    assert len({r["inputs"]["ticket_id"] for r in rows}) == 20
    for row in rows:
        assert set(row) == {"inputs", "expectations"}
        assert set(row["inputs"]) == {"ticket_id"}
        assert set(row["expectations"]) == {"expected_category", "expected_priority", "expected_tools", "judge_notes"}
    first = rows[0]
    assert first["inputs"] == {"ticket_id": "T-1042"}
    assert first["expectations"]["expected_category"] == "billing"
    assert first["expectations"]["expected_priority"] == "P2"
    assert first["expectations"]["expected_tools"] == ["get_ticket", "get_customer_history"]


# --- Scorers


def test_correct_ticket_scores_one_on_all_four():
    assert scores(VALID) == ALL_ONE


@pytest.mark.parametrize(
    ("outputs", "zero"),
    [
        ({**VALID, "category": "bug", "route": "bug-team"}, "category_match"),
        ({**VALID, "priority": "P3"}, "priority_match"),
    ],
)
def test_wrong_label_scores_zero_only_on_that_scorer(outputs, zero):
    assert scores(outputs) == {**ALL_ONE, zero: 0}


@pytest.mark.parametrize(
    "outputs",
    [
        {k: v for k, v in VALID.items() if k != "rationale"},
        {**VALID, "route": "bug-team"},
        {**VALID, "extra": "x"},
        "not a decision",
    ],
)
def test_invalid_output_fails_valid_schema_without_crashing(outputs):
    assert scores(outputs)["valid_schema"] == 0


@pytest.mark.parametrize(
    "trace",
    [
        fake_trace("triage", "get_customer_history", "get_ticket"),
        fake_trace("triage", "get_customer_history", "get_ticket", "get_customer_history"),
        fake_trace("triage", "get_customer_history"),
        fake_trace("triage", "get_ticket"),
        fake_trace(),
        None,
    ],
)
def test_tool_order_is_zero_for_wrong_or_missing_tools(trace):
    assert scores(VALID, trace=trace)["tool_order"] == 0


def test_failed_prediction_scores_zero_on_all_four():
    assert scores(None) == ALL_ZERO


@pytest.mark.parametrize("field", ["category", "priority"])
def test_missing_field_and_missing_label_do_not_match(field):
    outputs = {k: v for k, v in VALID.items() if k != field}
    expectations = {k: v for k, v in EXPECT.items() if k != f"expected_{field}"}

    assert scores(outputs, expectations=expectations)[f"{field}_match"] == 0


# --- Auto-approver


def test_auto_approver_counts_only_the_final_attempt():
    approver = run_eval.AutoApprover()

    first = approver.start("T-1044")
    assert first({"name": "escalate_to_human", "args": {}}) is True
    retry = approver.start("T-1044")  # the attempt is retried
    assert retry({"name": "escalate_to_human", "args": {}}) is True
    approver.start("T-1042")

    assert approver.counts == {"T-1044": 1, "T-1042": 0}
    assert approver.total == 1


# --- Full runs against a temporary MLflow store


EVAL_ENV = (
    "MLFLOW_GENAI_EVAL_MAX_WORKERS",
    "MLFLOW_GENAI_EVAL_MAX_RETRIES",
    "MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION",
)


@pytest.fixture
def eval_store(tmp_path, monkeypatch):
    """A temporary sqlite MLflow store with autologging, all undone afterwards."""
    load_seed.load()
    for name in EVAL_ENV:
        # setenv first so monkeypatch records an undo even when the variable was unset;
        # run() sets these with setdefault and they must not leak into later tests.
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)  # keep MLflow's artifacts out of the repo
    previous_uri = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}")
    experiment = mlflow.set_experiment("triage-agent")
    mlflow.langchain.autolog()
    # No stdin: any attempt to ask a person fails the test.
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("the eval read stdin"))
    yield experiment
    mlflow.langchain.autolog(disable=True)
    mlflow.set_tracking_uri(previous_uri)


def rows_for(*ticket_ids):
    rows = {r["inputs"]["ticket_id"]: r for r in run_eval.load_rows()}
    return [rows[t] for t in ticket_ids]


def scripted_triage(models):
    """A triage_fn that runs the real agent with the scripted model for each ticket."""

    def triage_fn(ticket_id, approve):
        return run_eval.agent.triage(ticket_id, model=models[ticket_id](), approve=approve)

    return triage_fn


def per_ticket_scores(result):
    df = result.result_df
    out = {}
    for _, row in df.iterrows():
        ticket_id = row["request"]["ticket_id"]
        out[ticket_id] = {name: row[f"{name}/value"] for name in run_eval.SCORER_NAMES}
    return out


def test_one_run_with_tool_spans(eval_store):
    models = {
        "T-1042": lambda: scripted(VALID),
        "T-1043": lambda: scripted(VALID, ticket_id="T-1043", customer_id="C-12"),
    }

    runs_before = len(mlflow.search_runs([eval_store.experiment_id]))
    result, escalations = run_eval.run(rows=rows_for("T-1042", "T-1043"), triage_fn=scripted_triage(models))

    assert os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] == "1"
    assert os.environ["MLFLOW_GENAI_EVAL_MAX_RETRIES"] == "6"
    runs = mlflow.search_runs([eval_store.experiment_id])
    assert len(runs) == runs_before + 1
    assert runs.iloc[0]["run_id"] == result.run_id
    assert escalations == 0
    assert result.metrics["tool_order/mean"] == 1
    assert result.metrics["valid_schema/mean"] == 1
    assert result.metrics["category_match/mean"] == 0.5  # T-1043 is labelled bug
    traces = mlflow.search_traces(locations=[eval_store.experiment_id], run_id=result.run_id, return_type="list")
    assert len(traces) == 2
    for trace in traces:
        names = [s.name for s in trace.data.spans]
        assert "get_ticket" in names and "get_customer_history" in names


def test_failed_prediction_scores_zero_and_the_run_finishes(eval_store, monkeypatch):
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_MAX_WORKERS", "3")  # a person's override is kept

    def boom():
        raise RuntimeError("the model fell over")

    models = {"T-1042": boom, "T-1043": lambda: scripted(VALID, ticket_id="T-1043", customer_id="C-12")}

    result, _ = run_eval.run(rows=rows_for("T-1042", "T-1043"), triage_fn=scripted_triage(models))

    assert os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] == "3"
    assert os.environ["MLFLOW_GENAI_EVAL_MAX_RETRIES"] == "6"
    scored = per_ticket_scores(result)
    assert scored["T-1042"] == ALL_ZERO
    states = {row["request"]["ticket_id"]: row["state"] for _, row in result.result_df.iterrows()}
    assert states == {"T-1042": "ERROR", "T-1043": "OK"}
    assert scored["T-1043"]["valid_schema"] == 1
    assert scored["T-1043"]["tool_order"] == 1


def test_escalation_is_auto_approved_and_counted_once(eval_store):
    attempts = []

    def triage_fn(ticket_id, approve):
        attempts.append(ticket_id)
        if len(attempts) == 1:
            # First attempt: approve, then hit a rate limit so MLflow retries the ticket.
            approve({"name": "escalate_to_human", "args": {"ticket_id": ticket_id, "reason": REASON}})

            async def rate_limited():
                raise RuntimeError("429 rate limit exceeded")

            return rate_limited()
        model = scripted(P1_ACCESS, ticket_id="T-1044", customer_id="C-91", escalate=REASON)
        return run_eval.agent.triage(ticket_id, model=model, approve=approve)

    result, escalations = run_eval.run(rows=rows_for("T-1044"), triage_fn=triage_fn)

    assert attempts == ["T-1044", "T-1044"]
    assert escalations == 1
    assert per_ticket_scores(result)["T-1044"] == ALL_ONE
    [trace_id] = result.result_df["trace_id"]  # the scored trace is the final attempt's
    trace = mlflow.get_trace(trace_id)
    assert trace.info.state == "OK"
    names = [s.name for s in trace.data.spans]
    # The approved escalation resumes the agent in a second call; it lands in the same trace.
    assert names.count("escalate_to_human") >= 1
    assert names.index("get_ticket") < names.index("escalate_to_human")


def test_main_prints_summary_and_logs_to_triage_agent(eval_store, monkeypatch, capsys):
    store_uri = mlflow.get_tracking_uri()
    monkeypatch.setattr(run_eval, "TRACKING_URI", store_uri)
    monkeypatch.setattr(run_eval, "load_dotenv", lambda *a, **kw: None)  # keep real .env keys out
    models = {
        "T-1042": lambda: scripted(VALID),
        "T-1044": lambda: scripted(P1_ACCESS, ticket_id="T-1044", customer_id="C-91", escalate=REASON),
    }
    monkeypatch.setattr(
        run_eval,
        "run",
        functools.partial(run_eval.run, rows=rows_for("T-1042", "T-1044"), triage_fn=scripted_triage(models)),
    )
    # Start elsewhere with autolog off, so main() has to set both up itself.
    mlflow.set_experiment("somewhere-else")
    mlflow.langchain.autolog(disable=True)

    run_eval.main()

    out = capsys.readouterr().out
    run_id = re.search(r"^MLflow run: (\w+)$", out, re.M).group(1)
    for name in run_eval.SCORER_NAMES:
        assert re.search(rf"^{name}: \d\.\d\d$", out, re.M), name
    assert re.search(r"^Escalations auto-approved: 1$", out, re.M)
    run = mlflow.get_run(run_id)
    assert run.info.experiment_id == eval_store.experiment_id  # triage-agent
    traces = mlflow.search_traces(locations=[eval_store.experiment_id], run_id=run_id, return_type="list")
    assert len(traces) == 2
    for trace in traces:
        names = [s.name for s in trace.data.spans]
        assert "get_ticket" in names and "get_customer_history" in names


def test_triage_fn_defaults_to_the_agent():
    import inspect

    assert inspect.signature(run_eval.run).parameters["triage_fn"].default is run_eval.agent.triage
    assert run_eval.TRACKING_URI.endswith("/mlflow.db")
    assert asyncio.iscoroutinefunction(run_eval.agent.triage)
