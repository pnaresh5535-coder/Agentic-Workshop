"""Tests for the eval (eval/run_eval.py). The scorers are called directly with
hand-made outputs and spans; the full runs use the scripted model from
test_agent.py with the real MCP tools, against a temporary MLflow store.
The rationale judge is a fake. No API keys and no network."""

import asyncio
import functools
import json
import os
import re
from types import SimpleNamespace

import mlflow
import mlflow.langchain
import pandas as pd
import pytest
from mlflow.tracing.constant import SpanAttributeKey

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


def run_scorer(s, outputs, expectations=EXPECT, trace=IN_ORDER):
    # Scorer.run passes each scorer only the arguments it takes, as MLflow does.
    return s.run(inputs={"ticket_id": "T-1042"}, outputs=outputs, expectations=expectations, trace=trace)


def scores(outputs, expectations=EXPECT, trace=IN_ORDER):
    """The four code scorers' values."""
    return {
        s.name: run_scorer(s, outputs, expectations, trace)
        for s in run_eval.SCORERS
        if s.name in run_eval.CODE_SCORER_NAMES
    }


ALL_ONE = dict.fromkeys(run_eval.CODE_SCORER_NAMES, 1)
ALL_ZERO = dict.fromkeys(run_eval.CODE_SCORER_NAMES, 0)


class FakeJudge:
    """Stands in for ChatGroq: answers every structured-output call with a fixed verdict.

    It raises for any prompt containing `fail_on`, and for every prompt when `error` is set.
    """

    def __init__(self, verdict="pass", reason="The rationale matches the notes.", fail_on=None, error=None):
        self.verdict, self.reason, self.fail_on, self.error = verdict, reason, fail_on, error
        self.schemas = []
        self.prompts = []

    def with_structured_output(self, schema):
        self.schemas.append(schema)
        return self

    def invoke(self, messages):
        prompt = "\n".join(text for _, text in messages)
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        if self.fail_on and self.fail_on in prompt:
            raise RuntimeError("the judge fell over")
        return run_eval.JudgeVerdict(verdict=self.verdict, reason=self.reason)


@pytest.fixture
def judge(monkeypatch):
    """Installs a fake judge the way run() does, for scorer calls outside a run."""

    def install(fake):
        monkeypatch.setattr(run_eval, "_judge_llm", fake)
        return fake

    return install


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


# --- The rationale judge


def test_scorers_include_the_judge_last():
    assert run_eval.SCORER_NAMES == (*run_eval.CODE_SCORER_NAMES, "rationale_judge")
    assert [s.name for s in run_eval.SCORERS] == list(run_eval.SCORER_NAMES)


def test_sound_rationale_passes_with_the_judges_one_line_reason(judge):
    fake = judge(FakeJudge("pass", "Money at stake,\n  so P2 is right."))

    feedback = run_scorer(run_eval.rationale_judge, VALID)

    assert feedback.value == "pass"
    assert feedback.rationale == "Money at stake, so P2 is right."
    assert fake.schemas == [run_eval.JudgeVerdict]
    [prompt] = fake.prompts
    # The decision's four fields and the ticket's judge_notes, and never the ticket text.
    for text in (VALID["category"], VALID["priority"], VALID["route"], VALID["rationale"], EXPECT["judge_notes"]):
        assert text in prompt
    assert "charged twice" not in prompt


def test_unsound_rationale_fails_with_the_reason(judge):
    judge(FakeJudge("fail", "Ignores the Enterprise bump."))

    feedback = run_scorer(run_eval.rationale_judge, VALID)

    assert (feedback.value, feedback.rationale) == ("fail", "Ignores the Enterprise bump.")


def test_no_output_fails_without_calling_the_judge(judge):
    fake = judge(FakeJudge(error=AssertionError("Groq must not be called")))

    feedback = run_scorer(run_eval.rationale_judge, None)

    assert (feedback.value, feedback.rationale) == ("fail", "no decision to judge")
    assert fake.prompts == []


class SentinelEnv(dict):
    """An environment that fails the test if anything reads GEMINI_API_KEY."""

    def _check(self, key):
        if key == "GEMINI_API_KEY":
            pytest.fail("GEMINI_API_KEY was read")

    def __getitem__(self, key):
        self._check(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._check(key)
        return super().get(key, default)

    def __contains__(self, key):
        self._check(key)
        return super().__contains__(key)


def test_judge_setup_is_chatgroq_on_the_default_model_whatever_the_provider(monkeypatch):
    from langchain_groq import ChatGroq

    env = SentinelEnv(os.environ)
    dict.update(env, PROVIDER="gemini", GROQ_API_KEY="test-groq-key", GEMINI_API_KEY="must-not-be-read")
    dict.pop(env, "JUDGE_MODEL", None)
    monkeypatch.setattr(os, "environ", env)

    llm = run_eval.make_judge_llm()

    assert isinstance(llm, ChatGroq)
    assert llm.model_name == "openai/gpt-oss-120b"
    assert llm.temperature <= 1e-8  # ChatGroq stores temperature=0 as 1e-8
    assert llm.groq_api_key.get_secret_value() == "test-groq-key"


def test_judge_model_comes_from_judge_model(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("JUDGE_MODEL", "llama-3.3-70b-versatile")

    assert run_eval.make_judge_llm().model_name == "llama-3.3-70b-versatile"


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
        out[ticket_id] = {name: row[f"{name}/value"] for name in run_eval.CODE_SCORER_NAMES}
    return out


def judge_values(result):
    return {row["request"]["ticket_id"]: row["rationale_judge/value"] for _, row in result.result_df.iterrows()}


TWO_TICKETS = {
    "T-1042": lambda: scripted(VALID),
    "T-1043": lambda: scripted(VALID, ticket_id="T-1043", customer_id="C-12"),
}


def test_one_run_with_tool_spans(eval_store, monkeypatch):
    fake = FakeJudge("pass")
    monkeypatch.setattr(run_eval, "make_judge_llm", lambda: fake)  # the default judge

    runs_before = len(mlflow.search_runs([eval_store.experiment_id]))
    result, escalations = run_eval.run(rows=rows_for("T-1042", "T-1043"), triage_fn=scripted_triage(TWO_TICKETS))

    assert os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] == "1"
    assert os.environ["MLFLOW_GENAI_EVAL_MAX_RETRIES"] == "6"
    runs = mlflow.search_runs([eval_store.experiment_id])
    assert len(runs) == runs_before + 1
    assert runs.iloc[0]["run_id"] == result.run_id
    assert escalations == 0
    assert result.metrics["tool_order/mean"] == 1
    assert result.metrics["valid_schema/mean"] == 1
    assert result.metrics["category_match/mean"] == 0.5  # T-1043 is labelled bug
    assert len(fake.prompts) == 2
    # The real get_ticket tool returned T-1042's text ("charged twice"); the judge never sees it,
    # and each ticket's judge_notes reach exactly one prompt.
    assert not any("charged twice" in prompt for prompt in fake.prompts)
    notes = [row["expectations"]["judge_notes"] for row in rows_for("T-1042", "T-1043")]
    assert sorted(sum(note in prompt for prompt in fake.prompts) for note in notes) == [1, 1]
    assert all(any(note in prompt for note in notes) for prompt in fake.prompts)
    assert judge_values(result) == {"T-1042": "pass", "T-1043": "pass"}
    assert run_eval._judge_llm is None  # run() does not leave its judge behind
    traces = mlflow.search_traces(locations=[eval_store.experiment_id], run_id=result.run_id, return_type="list")
    assert len(traces) == 2
    for trace in traces:
        names = [s.name for s in trace.data.spans]
        assert "get_ticket" in names and "get_customer_history" in names


def test_failed_prediction_scores_zero_and_the_run_finishes(eval_store, monkeypatch):
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_MAX_WORKERS", "3")  # a person's override is kept

    def boom():
        raise RuntimeError("the model fell over")

    models = {"T-1042": boom, "T-1043": TWO_TICKETS["T-1043"]}
    fake = FakeJudge("pass")

    result, _ = run_eval.run(rows=rows_for("T-1042", "T-1043"), triage_fn=scripted_triage(models), judge_llm=fake)

    assert os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] == "3"
    assert os.environ["MLFLOW_GENAI_EVAL_MAX_RETRIES"] == "6"
    scored = per_ticket_scores(result)
    assert scored["T-1042"] == ALL_ZERO
    states = {row["request"]["ticket_id"]: row["state"] for _, row in result.result_df.iterrows()}
    assert states == {"T-1042": "ERROR", "T-1043": "OK"}
    assert scored["T-1043"]["valid_schema"] == 1
    assert scored["T-1043"]["tool_order"] == 1
    # The failed prediction fails the judge without a Groq call; the other is judged.
    assert judge_values(result) == {"T-1042": "fail", "T-1043": "pass"}
    assert len(fake.prompts) == 1
    assert run_eval.judge_outcomes(result) == (0.5, 0)


def test_judge_error_counts_as_fail_and_judge_error(eval_store):
    # T-1042's judge_notes mention the money problem; the judge raises on that ticket only.
    fake = FakeJudge("pass", fail_on="money problem")

    eval_run = run_eval.run(
        rows=rows_for("T-1042", "T-1043"), triage_fn=scripted_triage(TWO_TICKETS), judge_llm=fake
    )
    report = run_eval.build_report(eval_run)

    values = judge_values(eval_run.result)
    assert values["T-1043"] == "pass"
    assert values["T-1042"] not in ("pass", "fail")  # MLflow recorded the error instead
    assert report["means"]["rationale_judge"] == 0.5
    assert report["judge_errors"] == 1
    assert report["means"]["valid_schema"] == 1  # the run finished and scored the rest


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

    result, escalations = run_eval.run(rows=rows_for("T-1044"), triage_fn=triage_fn, judge_llm=FakeJudge())

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


def record_usage(total):
    """Adds a chat-model span with the given token usage to the active trace."""
    with mlflow.start_span("llm", span_type="CHAT_MODEL") as span:
        span.set_attribute(
            SpanAttributeKey.CHAT_USAGE, {"input_tokens": total - 1, "output_tokens": 1, "total_tokens": total}
        )


def test_total_tokens_counts_the_scored_traces_only(eval_store):
    attempts = []

    def triage_fn(ticket_id, approve):
        attempts.append(ticket_id)
        if ticket_id == "T-1042" and attempts.count("T-1042") == 1:
            record_usage(1000)  # the attempt MLflow retries: its trace must not count

            async def rate_limited():
                raise RuntimeError("429 rate limit exceeded")

            return rate_limited()
        if ticket_id == "T-1042":
            record_usage(42)
        # T-1043 records no usage at all, so it counts as 0.
        return scripted_triage(TWO_TICKETS)(ticket_id, approve)

    eval_run = run_eval.run(rows=rows_for("T-1042", "T-1043"), triage_fn=triage_fn, judge_llm=FakeJudge())

    traces = mlflow.search_traces(
        locations=[eval_store.experiment_id], run_id=eval_run.result.run_id, return_type="list"
    )
    usages = sorted((t.info.token_usage or {}).get("total_tokens", 0) for t in traces)
    assert usages == [0, 42, 1000]  # the retried attempt's trace is still in the run
    assert run_eval.total_tokens(eval_run.result) == 42


def test_total_tokens_treats_missing_usage_and_traces_as_zero(monkeypatch):
    infos = {
        "a": SimpleNamespace(info=SimpleNamespace(token_usage={"input_tokens": 5, "total_tokens": 7})),
        "b": SimpleNamespace(info=SimpleNamespace(token_usage=None)),
        "c": SimpleNamespace(info=SimpleNamespace(token_usage={"input_tokens": 5})),
    }
    monkeypatch.setattr(run_eval.mlflow, "get_trace", lambda trace_id: infos.get(trace_id))
    result = SimpleNamespace(result_df=pd.DataFrame({"trace_id": ["a", "b", "c", "gone"]}))

    assert run_eval.total_tokens(result) == 7


def test_main_prints_the_report_and_writes_the_same_numbers(eval_store, monkeypatch, capsys, tmp_path):
    store_uri = mlflow.get_tracking_uri()
    report_path = tmp_path / "latest_report.json"
    monkeypatch.setattr(run_eval, "TRACKING_URI", store_uri)
    monkeypatch.setattr(run_eval, "REPORT_PATH", report_path)
    monkeypatch.setattr(run_eval, "load_dotenv", lambda *a, **kw: None)  # keep real .env keys out
    models = {
        "T-1042": lambda: scripted(VALID),
        "T-1044": lambda: scripted(P1_ACCESS, ticket_id="T-1044", customer_id="C-91", escalate=REASON),
    }
    # T-1042's judge_notes mention the money problem: the judge raises there, so 1 judge error.
    fake = FakeJudge("pass", fail_on="money problem")
    monkeypatch.setattr(
        run_eval,
        "run",
        functools.partial(
            run_eval.run, rows=rows_for("T-1042", "T-1044"), triage_fn=scripted_triage(models), judge_llm=fake
        ),
    )
    # Start elsewhere with autolog off, so main() has to set both up itself.
    mlflow.set_experiment("somewhere-else")
    mlflow.langchain.autolog(disable=True)

    run_eval.main()

    out = capsys.readouterr().out
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report_path.read_text(encoding="utf-8").startswith('{\n  "run_id"')  # 2-space indent
    assert set(report) == {"run_id", "means", "total_tokens", "escalations", "judge_errors"}
    assert set(report["means"]) == set(run_eval.SCORER_NAMES)

    run_id = re.search(r"^MLflow run: (\w+)$", out, re.M).group(1)
    assert report["run_id"] == run_id
    for name in run_eval.SCORER_NAMES:
        printed = re.search(rf"^{name}: (\d\.\d\d)$", out, re.M).group(1)
        assert printed == f"{report['means'][name]:.2f}", name
    assert report["means"]["rationale_judge"] == 0.5
    assert re.search(rf"^Total tokens: {report['total_tokens']}$", out, re.M)
    assert re.search(r"^Escalations auto-approved: 1$", out, re.M) and report["escalations"] == 1
    assert re.search(r"^Judge errors: 1$", out, re.M) and report["judge_errors"] == 1

    run = mlflow.get_run(run_id)
    assert run.info.experiment_id == eval_store.experiment_id  # triage-agent
    traces = mlflow.search_traces(locations=[eval_store.experiment_id], run_id=run_id, return_type="list")
    assert len(traces) == 2
    for trace in traces:
        names = [s.name for s in trace.data.spans]
        assert "get_ticket" in names and "get_customer_history" in names


def test_defaults():
    import inspect

    params = inspect.signature(run_eval.run).parameters
    assert params["triage_fn"].default is run_eval.agent.triage
    assert params["judge_llm"].default is None
    assert run_eval.TRACKING_URI.endswith("/mlflow.db")
    assert run_eval.REPORT_PATH == run_eval.REPO_ROOT / "eval" / "latest_report.json"
    assert asyncio.iscoroutinefunction(run_eval.agent.triage)
