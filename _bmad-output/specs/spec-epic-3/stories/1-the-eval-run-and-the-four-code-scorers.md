---
title: 'The eval run and the four code scorers'
type: 'feature'
created: '2026-09-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'c1e5a3debc3c2fd871771c514aef7bbe51fb9a0b'
context:
  - '{project-root}/_bmad-output/specs/spec-epic-3/SPEC.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nothing measures the Epic 2 agent. There is no eval run over the 20 labelled tickets and no scores (Epic 3 CAP-1 to CAP-5, and CAP-8).

**Approach:** Add `eval/run_eval.py`. `uv run python eval/run_eval.py` runs the unchanged agent over every row of `eval/labelled_tickets.csv` through `mlflow.genai.evaluate` and logs one MLflow run to the `triage-agent` experiment in `sqlite:///mlflow.db`. The run is scored by `valid_schema`, `category_match`, `priority_match` and `tool_order`, and every escalation is approved automatically. The LLM judge, token totals and `eval/latest_report.json` are story 2.

## Boundaries & Constraints

**Always:**
- Before evaluating, the script sets the tracking URI `sqlite:///mlflow.db` (resolved from the repo root), the experiment `triage-agent` and `mlflow.langchain.autolog()`, and runs `load_dotenv()`.
- Tickets run one at a time: `MLFLOW_GENAI_EVAL_MAX_WORKERS` defaults to `1`, and `MLFLOW_GENAI_EVAL_MAX_RETRIES` defaults to `6`, so a rate-limited ticket is retried with MLflow's backoff. Both are set with `os.environ.setdefault` before `evaluate` runs, so a person can override them.
- Each row becomes `inputs={"ticket_id": ...}` and `expectations={"expected_category", "expected_priority", "expected_tools", "judge_notes"}`.
- The predict function is wrapped in `@mlflow.trace`, so each ticket, including an escalation's resumed call, is one trace. It calls `agent.triage(ticket_id, approve=<auto-approver>)` and returns the decision dict.
- The auto-approver returns `True`, never reads stdin, and counts approvals per ticket. The count is reset when a ticket's attempt starts, so a retried ticket counts only its final attempt. `run()` returns the total.
- Scorers return `1` or `0`. A ticket whose prediction failed (no outputs) scores `0` on all four.
  - `valid_schema`: `triage.schema.validate_decision(outputs)` succeeds.
  - `category_match` and `priority_match`: the output field equals the expectation.
  - `tool_order`: the trace has a `get_ticket` span that starts before a `get_customer_history` span.
- After the run, it prints the MLflow run ID, each scorer's mean and the escalation count.
- `run(rows=None, triage_fn=agent.triage)` is importable. `rows` limits which tickets run, and `triage_fn` can be swapped in tests.

**Never:**
- Changing `agent.py`, `run_agent.py`, `triage/`, `mcp/`, `TRIAGE_POLICY.md`, `seed/` or `eval/labelled_tickets.csv`.
- Writing any file outside MLflow's store, including `eval/latest_report.json` (story 2).
- A hand-written scoring loop, the judge, or reading `GEMINI_API_KEY` directly.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Rows | `eval/labelled_tickets.csv` | 20 eval items with the inputs and expectations above | N/A |
| Correct ticket | Output `billing`/`P2`/`billing-team`, labels billing/P2, tools in order | All four scorers give 1 | N/A |
| Wrong labels | Output category or priority differs from the label | Only that scorer gives 0 | N/A |
| Invalid output | Output misses a field, or the route does not match | `valid_schema` gives 0 | No crash |
| Tool order | Trace with `get_customer_history` first, or missing `get_ticket` | `tool_order` gives 0 | N/A |
| Failed prediction | `triage_fn` raises for one ticket | That ticket scores 0 on all four; the run finishes | Error recorded by MLflow |
| Escalation | A ticket's run requests escalation | Auto-approved, counted once, no stdin read | N/A |
| One run | Offline run on two tickets with a scripted model | Exactly one new MLflow run, and each ticket's trace has the tool spans | N/A |

</frozen-after-approval>

## Code Map

- `agent.py` -- `async triage(ticket_id, model=None, approve=ask_at_terminal) -> dict`. It returns only the four decision fields and raises `TriageValidationError`. Call it through `asyncio.run` inside the traced predict function. Do not change it.
- `triage/schema.py` -- `validate_decision(data)` raises `TriageValidationError` on any invalid input, including `None`.
- `eval/labelled_tickets.csv` -- columns `ticket_id, expected_category, expected_priority, expected_tools, judge_notes`, 20 rows. Read-only.
- Installed `mlflow/genai/evaluation/harness.py` -- the predict function runs under `call_with_retry`: 429 errors are retried, and any other exception becomes the item's `error_message`. Workers come from `MLFLOW_GENAI_EVAL_MAX_WORKERS` (default 10). Scorers take `outputs`, `expectations` and `trace`, and the `@scorer` decorator comes from `mlflow.genai.scorers`.
- Autolog records the MCP tools as spans named `get_ticket` and `get_customer_history` (checked in the Epic 2 traces).
- `tests/test_agent.py` -- the `ScriptedModel` pattern and `load_seed.load()` give an offline agent with real tools. Reuse them for the offline run test.

## Tasks & Acceptance

**Execution:**
- [x] `eval/run_eval.py` -- the setup, rows, traced predict function, auto-approver, four scorers, `run()` and a `main` that prints the summary, as above. -- CAP-1 to CAP-5 and CAP-8.
- [x] `tests/test_eval.py` -- one test per matrix row. The scorers are called directly with hand-made outputs and spans. The "one run" row runs `run(rows=..., triage_fn=...)` with a scripted model against a temporary `sqlite` tracking URI. No API keys and no network. -- Proves the eval without live model calls.

**Acceptance Criteria:**
- Given no API keys, when `uv run pytest` runs, then every test passes, including the Epic 1 and 2 tests.
- Given a working Groq key and quota, when `PROVIDER=groq uv run python eval/run_eval.py` runs, then it finishes with no input from a person, logs one run with the four scorer means, and reports at least 3 escalations (T-1044, T-1048, T-1057).

## Implementation Notes

- Implemented by a subagent from this spec. Files: `eval/run_eval.py`, `tests/test_eval.py`. `run()` returns `EvalRun(result, escalations)` rather than only the count, so `main()` and story 2 get the run ID and metrics. `main()`, not `run()`, sets up tracking, which keeps tests on a temporary store.
- Beyond the spec: `MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION` defaults to `true` (with `setdefault`). The predict function is already traced, and MLflow's check would otherwise call the agent an extra time on ticket 1 and abort the run if that call failed.
- Review pass 1 patches:
  - Added a test that runs `main()` end to end offline.
  - The worker and retry defaults are pinned by tests, and a preset value is kept.
  - The fixture restores the environment variables on teardown.
  - `tool_order` compares the first `get_ticket` with the first `get_customer_history`.
  - A missing label scores 0.
- Tests: `uv run pytest -q` with no API keys gives 105 passed.
- Live run (before the review patches): `PROVIDER=groq uv run python eval/run_eval.py` exited 0 as run `b73e47476023409090add3b0b5cb9bfe` in `triage-agent`. All four means were 0.95, and 3 escalations were auto-approved.
  - T-1045 scored 0 on all four: Groq returned 400 `tool_use_failed` when the model called a tool named `json`.
  - Re-scoring those 21 live traces with the patched `tool_order` still gives order 1 on 20 of them.
- For story 2: a ticket retried after a 429 leaves its failed attempt's trace linked to the run (T-1050, so 21 traces for 20 tickets). Add up tokens from the traces MLflow scored (`result_df` trace IDs), not every trace in the run.

## Spec Change Log

## Review Triage Log

Review pass 1 (2026-09-26/27). Layers: Blind Hunter (BH), Edge Case Hunter (ECH), Verification Gap (VG). Findings raised by more than one layer are logged once.

| # | Finding | Layers | Verdict | Evidence | Route |
|---|---------|--------|---------|----------|-------|
| 1 | `main()` and the CLI setup never run under test | VG, BH | medium | Pre-verified by VG. Dropping `autolog()` would zero `tool_order` on all 20 tickets, and every test would stay green | patch |
| 2 | The worker and retry defaults are not pinned by any test | VG | low | Pre-verified by VG. Deleting either `setdefault` keeps every test passing | patch |
| 3 | `run()`'s environment defaults leak into later tests | BH, ECH, VG | low | `monkeypatch.delenv(raising=False)` records no undo when a variable is unset | patch |
| 4 | `tool_order` compares `min(get_ticket) < max(get_customer_history)` | BH | low | Order history, ticket, history scores 1; CAP-5 means the first history call must follow `get_ticket`, and the fix is a one-token change | patch |
| 5 | `_field_matches` scores 1 when both the field and the label are missing | ECH | low | `None == None`, and the fix is one direct condition | patch |
| 6 | Skipping trace validation is not in the spec | BH | low | Deliberate; it prevents an extra agent call on ticket 1. Fixing it means editing this spec, so it is recorded in Implementation Notes instead | reject |
| 7 | The escalation test doesn't prove the resumed call is in the same trace | BH | false | `escalate_to_human` runs only after the resume, so its span in the scored trace proves it | reject |
| 8 | Approvals from a ticket that finally fails are still counted | BH, ECH | false | The escalation was approved; the count reports approvals, as CAP-8 asks | reject |
| 9 | The approval count assumes one worker, or unique ticket IDs | BH, ECH | low | Needs a person to override workers and duplicate rows; the labelled CSV has unique IDs | reject |
| 10 | Several escalations in one attempt raise the count | ECH | false | Each one is a separate approval, and the "counted once" row is about retries | reject |
| 11 | `expected_tools` is unused, and an empty cell becomes `[""]` | BH, ECH | low | The read-only CSV has every cell filled; the column is carried as an expectation for story 2 and the UI | reject |
| 12 | The retry test relies on MLflow's private 429 heuristic and sleeps for real | BH | low | Adds about 1 second; this pins the installed MLflow 3.16 behaviour | reject |
| 13 | Tied span start times would score `tool_order` 0 | ECH | low | The calls are sequential, with a network round trip between them | reject |
| 14 | Scorers return 0 or 1 with no rationale | BH | low | A feature request; 0/1 is the spec contract | reject |

## Verification

**Commands:**
- `uv run pytest -q` (with no API keys set) -- expected: all tests pass.
- `PROVIDER=groq uv run python eval/run_eval.py` -- expected: a run ID, four means and an escalation count of 3 or more. This takes several minutes because tickets run one at a time under Groq's per-minute token limit.
