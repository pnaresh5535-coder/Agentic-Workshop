---
title: 'The seed loader'
type: 'feature'
created: '2026-09-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '34e4b72741347b77e3fde871425e0e0b8b41cfca'
context:
  - '{project-root}/_bmad-output/specs/spec-epic-1/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `mcp/triage_server.py` reads `tickets` and `customers` from `app.db`, but nothing creates that file. Until it exists, the agent in Epic 2 has no data to read.

**Approach:** Add `load_seed.py` at the repo root. `uv run python load_seed.py` reads `seed/tickets.csv` and `seed/customers.csv`, checks every `open_tickets` value, then rebuilds both tables in `app.db` in one transaction (SPEC CAP-2, CAP-3).

## Boundaries & Constraints

**Always:**
- The table and column names are fixed: `tickets(ticket_id, customer_id, created_at, text)` and `customers(customer_id, name, plan, open_tickets)`. They match the CSV headers, and a CSV whose header differs stops the load with an error naming the file and the expected header.
- `open_tickets` is stored as an SQLite `INTEGER`, and every other column as `TEXT`. A value counts as valid only if it is ASCII digits and nothing else (`0`, `3`). Anything else (`-1`, `2.5`, `" 3"`, `""`, `three`) stops the load with an error naming the CSV line number, the `customer_id` and the bad value.
- The load replaces the tables and never appends to them. Both tables are dropped, recreated and filled inside one transaction. All validation runs before `app.db` is touched, so a failed load leaves the previous `app.db` as it was.
- The CSVs are read as UTF-8 with the `csv` module, which handles quoted commas.
- The loader uses only the standard library. The seed and database paths resolve from the repo root, whatever directory the command is run from.

**Never:**
- Writing to `seed/`, `TRIAGE_POLICY.md`, `mcp/triage_server.py` or `triage/`.
- Committing `app.db`, which `.gitignore` already covers.
- Network calls, API keys, or new dependencies.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| First load | The real `seed/`, no `app.db` | `app.db` holds 24 tickets and 20 customers, and prints one summary line | N/A |
| Second load | Run again on the result | The same tables and rows: no duplicates, no error | N/A |
| Types | After a load | `typeof(open_tickets)` is `integer` on every row | N/A |
| MCP tools | After a load | `get_ticket("T-1042")` returns customer `C-77`, and `get_customer_history("C-77")` returns Northwind, Enterprise, `open_tickets` 2 | N/A |
| Bad count | `open_tickets` of `-1`, `2.5`, `""` or `three` | Nothing is written | Error names the line, the `customer_id` and the value |
| Bad header | A CSV header that differs from the fixed columns | Nothing is written | Error names the file and the expected header |
| Failed reload | A good `app.db` exists, then a bad count | `app.db` is unchanged | Same error as "Bad count" |

</frozen-after-approval>

## Code Map

- `mcp/triage_server.py` -- read-only consumer. `DB_PATH` is `<repo>/app.db`, and `get_ticket` and `get_customer_history` query the fixed columns. Tests load it with `importlib.util.spec_from_file_location` and point `DB_PATH` at a temp file. Do not `import mcp.triage_server`, because the local `mcp/` directory collides with the installed `mcp` package.
- `seed/tickets.csv` (24 rows) and `seed/customers.csv` (20 rows) -- read-only inputs. They are clean today: no multi-line text, and every count is a non-negative integer. `T-1047`'s text contains a quoted comma.
- `run_agent.py`, `triage/` -- unrelated; leave unchanged.
- `pyproject.toml` -- `pythonpath = ["."]` already lets tests `import load_seed`.

## Tasks & Acceptance

**Execution:**
- [x] `load_seed.py` -- add `load(seed_dir: Path = SEED_DIR, db_path: Path = DB_PATH) -> tuple[int, int]` (returns the ticket and customer counts), `SeedError(ValueError)` for every validation failure, and `main()`, which prints `Loaded N tickets and M customers into app.db` and exits non-zero with the error message on `SeedError`. -- One importable, testable entry point.
- [x] `tests/test_load_seed.py` -- one test per I/O Matrix row. The error cases write small CSVs under `tmp_path`; the others load the real `seed/` into a `tmp_path` database. -- Covers CAP-2 and CAP-3.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest` runs, then every test passes, including the existing Epic 1 schema tests.
- Given a fresh clone, when `uv run python load_seed.py` runs twice, then both runs exit 0 and `app.db` has 24 tickets and 20 customers.

## Implementation Notes

- Implemented by a subagent from this spec. Files: `load_seed.py`, `tests/test_load_seed.py`. Beyond the spec, a row with the wrong number of fields also stops the load with a `SeedError` naming the file and line.
- Review pass 1 patches: `csv.Error` becomes `SeedError`; the CSVs are read as `utf-8-sig`, so a BOM is accepted; there are new tests for the in-transaction rollback, wrong field counts, and missing or non-UTF-8 CSVs. A mutation check (BEGIN/COMMIT/ROLLBACK removed) makes the rollback test fail, as intended.
- `uv run pytest -q`: 56 passed. `load_seed.py` run twice prints `Loaded 24 tickets and 20 customers into app.db` both times; `typeof(open_tickets)` is `integer` on all 20 rows.

## Spec Change Log

## Review Triage Log

Review pass 1 (2026-09-26). Layers: Blind Hunter (BH), Edge Case Hunter (ECH), Verification Gap (VG). Findings raised by more than one layer are logged once.

| # | Finding | Layers | Verdict | Evidence | Route |
|---|---------|--------|---------|----------|-------|
| 1 | The in-transaction rollback is untested; removing BEGIN/COMMIT/ROLLBACK keeps all tests green | VG, BH | medium | Pre-verified by VG. Every failing test fails during validation, before `connect` | patch |
| 2 | The field-count and read-failure branches of `_read_csv` are untested | VG, BH | low | Pre-verified by VG. No test has a short or long row, a missing file or non-UTF-8 bytes | patch |
| 3 | `csv.Error` escapes as a traceback instead of `SeedError` | BH, ECH | low | Only `OSError` and `UnicodeDecodeError` are caught; `csv.Error` is a validation failure, and the fix is one name in a tuple | patch |
| 4 | A CSV with a UTF-8 BOM fails the header check | BH, ECH | low | `encoding="utf-8"` keeps `\ufeff` on the first header field; `utf-8-sig` is a one-token fix | patch |
| 5 | `sqlite3.Error` (locked or corrupt `app.db`) is a traceback, not an `Error:` line | BH, ECH | low | Real, but rare locally, and it still exits non-zero with SQLite's message; the fix adds a branch | reject |
| 6 | `ROLLBACK` can hide the original error after SQLite has already rolled back itself | BH, ECH, VG | low | Only on `SQLITE_FULL` or I/O errors, which are unlikely here; the fix adds a guard | reject |
| 7 | A blank line fails with "expected 4 fields, got 0" | BH, ECH | low | It fails loudly on an input the read-only seed does not contain | reject |
| 8 | "Works from any directory" is not tested through a subprocess | BH | low | Checked by hand from `C:\` during step 3, and the paths come from `__file__`; a subprocess test adds complexity | reject |
| 9 | Duplicate IDs and orphaned tickets are not checked | BH, ECH | false | The intent is to load every CSV row as-is, and the read-only seed has no duplicates; rejecting rows would contradict CAP-2 | reject |
| 10 | Line numbers point to the end of multi-line records | BH, ECH | low | Counts are only in `customers.csv`, which has no quoted multi-line fields; the fix adds tracking | reject |
| 11 | `open_tickets` above 2^63-1 overflows at insert | ECH | low | Unrealistic for a ticket count; the fix adds a bound check | reject |
| 12 | A DB-stage failure on the very first load leaves an empty `app.db` | ECH | low | Needs a disk or SQLite failure after validation; the fix adds cleanup logic | reject |
| 13 | A header-only CSV replaces the tables with empty ones | ECH | false | Loading zero rows from a zero-row CSV is exactly "every CSV row" (CAP-2/CAP-3) | reject |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `uv run python load_seed.py && uv run python load_seed.py` -- expected: both runs print `Loaded 24 tickets and 20 customers into app.db`.
- `uv run python -c "import sqlite3; c=sqlite3.connect('app.db'); print(c.execute('select count(*), min(typeof(open_tickets)), max(typeof(open_tickets)) from customers').fetchone())"` -- expected: `(20, 'integer', 'integer')`.
