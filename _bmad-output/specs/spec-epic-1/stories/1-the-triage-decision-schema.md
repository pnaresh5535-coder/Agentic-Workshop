---
title: 'The triage-decision schema'
type: 'feature'
created: '2026-09-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 1
baseline_commit: 'ab48e2e2befd6317e1024cba3252f9ba8c293227'
context:
  - '{project-root}/_bmad-output/specs/spec-epic-1/SPEC.md'
  - '{project-root}/TRIAGE_POLICY.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nothing defines what a valid triage decision is. Epic 2's agent needs a structured-output contract, and Epic 3's `valid_schema` scorer needs something to validate against.

**Approach:** Add a `triage.schema` module with a `TriageDecision` model and a `validate_decision` function. The function accepts a decision as a dict or JSON text and returns the validated model, or raises `TriageValidationError` naming the field and what was expected (SPEC CAP-1).

## Boundaries & Constraints

**Always:**
- The object has exactly four fields: `category`, `priority`, `route`, `rationale`.
- `category` ∈ {billing, bug, access, performance, how-to}; `priority` ∈ {P1, P2, P3, P4}.
- `route` must equal the fixed mapping for its category: billing→billing-team, bug→bug-team, access→access-team, performance→performance-team, how-to→how-to-team. The mapping lives in the code as one constant.
- `rationale` is a string that is non-empty after trimming whitespace, contains no `\n` or `\r`, and is under 200 characters (at most 199).
- Values must already be the right type; nothing is coerced (e.g. `"p2"` or `2` is rejected).
- Every rejection raises `TriageValidationError`, whose message names each offending field and what was expected.
- Standard library plus the existing `pydantic` dependency only.

**Never:**
- Grammar or sentence parsing of `rationale`.
- Filling in a missing `route` from the category: a missing field is rejected.
- Network calls, API keys, changes to `seed/`, `mcp/triage_server.py`, `TRIAGE_POLICY.md`, or the loader (story 2).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Valid dict | `{"category":"billing","priority":"P2","route":"billing-team","rationale":"Double charge puts money at stake (P2)."}` | Returns `TriageDecision` with the same values | N/A |
| Valid JSON text | The same object as a JSON string | Returns an equal `TriageDecision` | N/A |
| Unknown category | `category: "sales"` | Rejected | Error names `category` and lists the allowed values |
| Bad priority | `priority: "P5"`, `"p2"` or `2` | Rejected | Error names `priority` and lists P1–P4 |
| Route mismatch | `billing` + `bug-team` | Rejected | Error names `route` and says `billing-team` is expected for `billing` |
| Missing field | No `route` | Rejected | Error names `route` as missing |
| Extra field | Adds `"confidence": 0.9` | Rejected | Error names `confidence` as not allowed |
| Empty rationale | `""` or `"   "` | Rejected | Error names `rationale` |
| Multi-line rationale | Contains `\n` or `\r` | Rejected | Error names `rationale` (no line breaks) |
| Long rationale | 200 characters (199 passes) | Rejected | Error names `rationale` (under 200 characters) |
| Not an object | Invalid JSON text, a list or `None` | Rejected | `TriageValidationError`, never a raw `pydantic`/`json` error |

</frozen-after-approval>

## Code Map

- `triage/` -- new package. Epic 2's agent (`upstream/stage-3:agent.py`) imports `from triage.schema import TriageDecision, TriageValidationError`, so keep those names.
- `run_agent.py` -- has `from agent import triage` (a function). This is unrelated to the `triage` package; leave it unchanged.
- `mcp/triage_server.py`, `seed/`, `TRIAGE_POLICY.md` -- read-only, not touched.
- `pyproject.toml` -- `pydantic>=2.8` is already a dependency; `[tool.pytest.ini_options] testpaths = ["tests"]` exists, and `tests/` does not exist yet.

## Tasks & Acceptance

**Execution:**
- [x] `triage/__init__.py` -- create an empty file -- makes `triage` an importable package.
- [x] `triage/schema.py` -- define:
  - `ROUTE_BY_CATEGORY`, the category→route mapping.
  - `TriageDecision`, a pydantic model: forbids extra fields, uses strict `Literal` types, and a validator enforces the route and rationale rules.
  - `TriageValidationError(ValueError)`.
  - `validate_decision(data: dict | str) -> TriageDecision`, which turns every failure into `TriageValidationError` with a readable, per-field message.

  Rationale: one importable contract for Epics 2 and 3.
- [x] `pyproject.toml` -- add `pythonpath = ["."]` under `[tool.pytest.ini_options]` -- lets tests import `triage` from the repo root.
- [x] `tests/test_schema.py` -- write one test per I/O Matrix row -- covers CAP-1's accept and reject cases.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest` runs, then every test in `tests/test_schema.py` passes.
- Given any rejected input, when it is validated, then the exception is a `TriageValidationError` and `str(exc)` contains the offending field name.
- Given `TriageDecision`, when it is passed to LangChain structured output in Epic 2, then it works as a standard pydantic `BaseModel` with no extra wrapping.

### Review Findings

Code review 2026-09-26 (Blind Hunter, Edge Case Hunter, Verification Gap, Acceptance Auditor). Diff: staged work on `ab48e2e`.

- [x] [Review][Patch] Add field descriptions and a rationale `max_length` to the JSON schema so the LLM sees the rules, and keep the "under 200 characters" error text (decision 1a) [triage/schema.py:36] (medium)
- [x] [Review][Patch] Reject any line break `str.splitlines()` recognises, including `\u2028`, `\x0b`, `\x0c` and `\x85` (decision 2a) [triage/schema.py:54] (low)
- [x] [Review][Patch] Make `TriageDecision` frozen so a validated decision cannot be edited into an invalid one (decision 3a) [triage/schema.py:34] (low)
- [x] [Review][Patch] The route mapping is only tested against itself, so swapping the bug and access routes leaves all 24 tests passing — pin `ROUTE_BY_CATEGORY` to the literal `TRIAGE_POLICY.md` table, and use literal pairs in `test_every_category_with_its_route_is_accepted` [tests/test_schema.py:36] (medium)
- [x] [Review][Patch] Rejection tests check too little — `rejects()` only checks that the field name appears somewhere in the message, and the extra-field message contains every field name. Assert on `f"{field}:"`, and assert the expected text in the 200-character and line-break tests [tests/test_schema.py:19] (low)
- [x] [Review][Patch] `MAX_RATIONALE_CHARS = 199`, but the error text hard-codes "200" — build the text from the constant [triage/schema.py:57] (low)

**Rejected**
- `false` — Missing-field message says "is required", not "missing". The AC only requires that `str(exc)` names the field, which it does ("route: is required").
- `false` — The route is not reported when the category is also invalid. This is by design: a route cannot be judged without a valid category.
- `false` — Non-object input is labelled `decision`. There is no field to name, and it still raises `TriageValidationError`.
- `false` — The LangChain AC has no test. `TriageDecision` is a plain `BaseModel`, so the AC is met, and Epic 2 exercises it.
- `false` — The `dict | str` type hint is too narrow. That signature is the one the story specifies.
- `low` — `Category`, `Route` and `ROUTE_BY_CATEGORY` repeat the same values. The values are fixed by read-only policy, so drift is unlikely.
- `low` — No test combines two errors. Behaviour verified correct: a bad priority plus a multi-line rationale names both fields.
- `low` — No field-level error test goes through the JSON text path. Behaviour verified correct: `"priority": 2` and a route mismatch sent as JSON are both rejected.
- `low` — The guard for an invalid category with a mismatched route has no test. Behaviour verified correct: only `category` is reported, with no raw `KeyError`.
- `low` — Duplicate JSON keys are accepted (the last one wins). LLM structured output does not realistically produce them, and a fix would need an extra parsing path.

## Implementation Notes

- Implemented directly in the session (no subagent). Files: `triage/__init__.py`, `triage/schema.py`, `tests/test_schema.py`, `pyproject.toml` (`pythonpath`).
- The route check is a `route` field validator that reads the already-validated `category`, so the error names `route`. It is skipped when the category itself is invalid.
- The error text strips pydantic's `Value error, ` prefix and rewrites `missing` and `extra_forbidden` errors as "is required" and "is not allowed".
- `uv run pytest -q`: 24 passed. Every I/O Matrix row is covered by a passing test.

## Spec Change Log

## Review Triage Log

- 2026-09-26, code review 1: 6 patches applied (3 came from decisions 1a, 2a and 3a), 10 findings rejected, 0 deferred. `uv run pytest -q`: 32 passed.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass, 0 failures.
- `uv run python -c "from triage.schema import TriageDecision, TriageValidationError, validate_decision, ROUTE_BY_CATEGORY"` -- expected: no error.
