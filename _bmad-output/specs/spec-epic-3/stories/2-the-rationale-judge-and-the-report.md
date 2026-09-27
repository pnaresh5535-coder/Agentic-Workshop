---
title: 'The rationale judge and the report'
type: 'feature'
created: '2026-09-27'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '36294a0b350d92c41bbfd5d0f327c7fbc2c91363'
context:
  - '{project-root}/_bmad-output/specs/spec-epic-3/SPEC.md'
  - '{project-root}/_bmad-output/specs/spec-epic-3/stories/1-the-eval-run-and-the-four-code-scorers.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The eval from story 1 scores only in code. Nothing judges whether each rationale is sound, and the numbers exist only in MLflow (Epic 3 CAP-6, CAP-7, and the reporting part of CAP-8).

**Approach:** Add a fifth scorer, `rationale_judge`, to `eval/run_eval.py`. It asks a Groq model whether each ticket's rationale is sound, given that ticket's `judge_notes`. After the run, the script prints the five means, the agent's total tokens and the escalation count, and writes the same numbers to `eval/latest_report.json`.

## Boundaries & Constraints

**Always:**
- `rationale_judge` uses `ChatGroq(model=JUDGE_MODEL or "openai/gpt-oss-120b", temperature=0)`, which takes its key from `GROQ_API_KEY`. It does this whatever `PROVIDER` is set to, and never reads `GEMINI_API_KEY`.
- It returns `Feedback(value="pass" | "fail", rationale=<one line>)` for every ticket. It sends the model the decision's category, priority, route and rationale, plus the ticket's `judge_notes`, never the ticket text. The model answers through structured output with a verdict and a reason.
- A ticket with no output (a failed prediction) gets `fail` with the reason `no decision to judge`, without calling Groq. If the Groq call itself fails after MLflow's retries, the ticket counts as `fail`, and the report counts it under `judge_errors`.
- The means are:
  - the four code scorers: `result.metrics["<name>/mean"]`;
  - `rationale_judge`: the pass rate (`pass` = 1, `fail` = 0) over every scored ticket, computed from `result.result_df`, because MLflow does not average `pass`/`fail` strings.
- `total_tokens` is the sum of `trace.info.token_usage["total_tokens"]` over the trace IDs in `result.result_df` only, so the extra trace of a retried attempt and the judge's own calls are not counted. A trace with no usage counts as 0.
- `eval/latest_report.json` is written with 2-space indentation as `{"run_id", "means": {all five}, "total_tokens", "escalations", "judge_errors"}`. Stdout prints the same numbers, with means to two decimal places.
- `run()` takes `judge_llm=None` (default: the `ChatGroq` above), so tests can pass a fake.

**Never:**
- Changing `agent.py`, `triage/`, `mcp/`, `TRIAGE_POLICY.md`, `seed/` or `eval/labelled_tickets.csv`.
- Changing story 1's four scorers, predict function or auto-approver, apart from wiring in the fifth scorer.
- Writing any other file outside MLflow's store, or committing `eval/latest_report.json` (it is in `.gitignore`).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Sound rationale | The fake judge answers pass | `pass` with the judge's one-line reason | N/A |
| Unsound rationale | The fake judge answers fail | `fail` with the reason | N/A |
| No output | Failed prediction | `fail`, "no decision to judge", Groq not called | N/A |
| Judge error | The fake judge raises | That ticket counts as `fail`; `judge_errors` is 1; the run finishes | No crash |
| Judge setup | `PROVIDER=gemini`, `JUDGE_MODEL` unset | `ChatGroq` with `openai/gpt-oss-120b`; `GEMINI_API_KEY` never read | N/A |
| Tokens | Scored traces with known usage, plus one extra retried-attempt trace | The sum covers the scored traces only | Missing usage counts as 0 |
| Report | Offline run on two tickets | Stdout and `latest_report.json` hold the same five means, tokens, escalations and judge errors | N/A |

</frozen-after-approval>

## Code Map

- `eval/run_eval.py` (story 1):
  - `SCORERS` and `SCORER_NAMES`: add `rationale_judge`.
  - `run(rows, triage_fn)` returns `EvalRun(result, escalations)`: add `judge_llm`.
  - `main()` prints the summary: extend it with the report.
  - `REPO_ROOT` gives the path to `eval/latest_report.json`.
- `mlflow.entities.Feedback` -- the scorer's return type, with `value` and `rationale`.
- Installed `mlflow/genai/scorers/aggregation.py` -- only numbers, booleans and `yes`/`no` are averaged, so `pass`/`fail` gets no metric.
- Installed `mlflow/genai/evaluation/harness.py` -- `clean_up_extra_traces` removes traces made during scoring, and `result_df` has one row per eval item with its `trace_id`.
- `trace.info.token_usage` -- `{"input_tokens", "output_tokens", "total_tokens", ...}`; the story 1 live traces average about 3,500 tokens.
- `tests/test_eval.py` -- the `eval_store` fixture, the scripted `triage_fn` and `main()` test patterns. Extend them.

## Tasks & Acceptance

**Execution:**
- [x] `eval/run_eval.py` -- add `rationale_judge`, `judge_llm`, the means and token calculation, and the report in `main()`, as above. -- CAP-6, CAP-7 and the reporting part of CAP-8.
- [x] `tests/test_eval.py` -- one test per matrix row, using a fake judge and no network. The judge-setup row checks the class, the model and that `GEMINI_API_KEY` is not read (for example with a sentinel environment that fails on access). -- Offline proof.

**Acceptance Criteria:**
- Given no API keys, when `uv run pytest` runs, then every test passes.
- Given a Groq key with enough quota, when `PROVIDER=groq uv run python eval/run_eval.py` runs, then it finishes unattended, prints five means, the total tokens, the escalation count and the judge errors, and `eval/latest_report.json` holds the same numbers.

## Implementation Notes

- Implemented by a subagent from this spec. Files: `eval/run_eval.py` (`rationale_judge`, `make_judge_llm`, the `judge_llm` parameter on `run()`, `judge_outcomes`, `total_tokens`, `build_report`, and the report in `main()`) and `tests/test_eval.py`. Output that is not a dict is also treated as "no decision to judge". The story 1 `main()` test was renamed and extended to cover the report, and it keeps its experiment, autolog and tool-span checks.
- Review pass 1 patch: `test_one_run_with_tool_spans` now checks that the judge prompts never contain T-1042's ticket text and do contain each ticket's `judge_notes`.
- `uv run pytest -q` with no API keys: 114 passed.
- Live `PROVIDER=groq uv run python eval/run_eval.py` (before the test-only patch): run `7a6b9956a6174d5bb5ed4e5a9f59a6e2`. `valid_schema` 0.95, `category_match` 0.90, `priority_match` 0.90, `tool_order` 0.95, `rationale_judge` 0.85, `total_tokens` 73,461, escalations 3, judge errors 0. `eval/latest_report.json` matches the printed summary.
- Judge fails: T-1046 had no decision. T-1059's rationale cites an invoice change the notes don't mention. T-1044 was failed because the judge read "escalate to a person" as a routing rule, which is probably the judge's mistake; tuning the judge prompt would need a spec change.

## Spec Change Log

## Review Triage Log

Review pass 1 (2026-09-27). Layers: Blind Hunter (BH), Edge Case Hunter (ECH), Verification Gap (VG). Findings raised by more than one layer are logged once.

| # | Finding | Layers | Verdict | Evidence | Route |
|---|---------|--------|---------|----------|-------|
| 1 | The "never send ticket text to the judge" test cannot fail | VG, BH | medium | Pre-verified by VG. The unit test's fake trace has no ticket text, and the real-run tests never inspect the prompts. This guards an Always rule and the AGENTS.md untrusted-text rule | patch |
| 2 | The judge pass rate, tokens and escalations are not logged to the MLflow run | BH | low | CAP-7 asks for them outside the UI (stdout and JSON), which is done; logging them to MLflow adds surface | reject |
| 3 | The docstring's retry claim is too broad | BH | false | MLflow does retry 429s and records a final failure, as the docstring says | reject |
| 4 | Nothing limits how fast the judge calls Groq | BH | low | Live run: `judge_errors` 0; 429s are retried; a limit adds configuration | reject |
| 5 | Missing or empty `judge_notes` is judged against nothing | BH, ECH | low | Every row of the read-only CSV has notes | reject |
| 6 | An empty or very long judge reason gets through | BH | low | Not seen live, and the fix adds guards | reject |
| 7 | `total_tokens` assumes a `trace_id` column with no gaps | BH, ECH | low | MLflow's `result_df` always has it, and failed predictions still have a trace | reject |
| 8 | The report can contain NaN | BH, ECH | low | Scorers always return a value, so a mean cannot be NaN unless every scorer errors | reject |
| 9 | The judge is passed through the module-level `_judge_llm` | BH | low | One run per process; the `finally` resets it | reject |
| 10 | The `GEMINI_API_KEY` guard in the test covers only some ways of reading the environment | BH | low | `ChatGroq` reads the environment through `.get` or `[]`; test-only | reject |
| 11 | The judge-error tests depend on the CSV's wording | BH | low | The CSV is read-only | reject |
| 12 | No test checks that an injected rationale reaches the judge only as quoted data | BH | low | The rationale is JSON-encoded in the prompt, and #1 covers the ticket-text path | reject |
| 13 | A missing `GROQ_API_KEY` aborts the run before `evaluate` | ECH | false | Failing loudly before any agent tokens are spent is correct for missing configuration | reject |

## Verification

**Commands:**
- `uv run pytest -q` (with no API keys set) -- expected: all tests pass.
- `PROVIDER=groq uv run python eval/run_eval.py` -- expected: the summary above, and `eval/latest_report.json` matching it. This is slow, because it runs 20 tickets one at a time within Groq's rate limits.
