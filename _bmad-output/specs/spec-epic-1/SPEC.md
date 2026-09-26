---
id: SPEC-epic-1
companions: [../../../mcp/triage_server.py, ../../../TRIAGE_POLICY.md]
sources: [../../../INTENT.md]
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Epic 1: triage data and schema

## Why

The triage agent (Epic 2) and its eval (Epic 3) need two things that do not exist yet: a strict definition of what a triage decision is, and the ticket and customer data in `app.db` where `mcp/triage_server.py` already expects it. Epic 1 is the foundation workshop attendees build first. Without it, the agent has nothing to read and no contract for its output.

## Capabilities

- **CAP-1**
  - **intent:** A triage decision is checked against a schema: a JSON object with a `category`, a `priority`, a `route` and a one-sentence `rationale`.
  - **success:** An object passes when it meets all four rules below. Any other object is rejected with an error naming the offending field and what was expected.
    - `category` is one of billing, bug, access, performance, how-to.
    - `priority` is one of P1, P2, P3, P4.
    - `route` is the route the fixed mapping gives for that category.
    - `rationale` is non-empty, has no line breaks and is under 200 characters.

- **CAP-2**
  - **intent:** One command, `uv run python load_seed.py`, loads `seed/tickets.csv` and `seed/customers.csv` into the local SQLite file `app.db`.
  - **success:** After the command, `app.db` has tables `tickets` and `customers` whose columns match the CSV headers, holding every CSV row. `customers.open_tickets` holds integers of 0 or more, and a value that is not one stops the load with an error naming the row and the value. `get_ticket("T-1042")` and `get_customer_history("C-77")` from `mcp/triage_server.py` return data.

- **CAP-3**
  - **intent:** The load is repeatable.
  - **success:** Running `load_seed.py` twice leaves `app.db` with the same tables and rows as running it once: no duplicate rows, no error.

## Constraints

- Python 3.12 or newer, managed with uv; packages are added with `uv add`.
- `seed/` is read-only: the loader reads it and never writes to it.
- No network calls and no API keys anywhere in this epic.
- `mcp/triage_server.py` must keep working unchanged, so these names are fixed: `tickets(ticket_id, customer_id, created_at, text)` and `customers(customer_id, name, plan, open_tickets)`.
- `app.db` is a local artifact and is never committed.
- Route is derived from category by one fixed mapping, the table in `TRIAGE_POLICY.md`: billing → billing-team, bug → bug-team, access → access-team, performance → performance-team, how-to → how-to-team.
- The `rationale` check is structural only; there is no grammar or sentence parsing.

## Non-goals

- The agent and its model calls (Epic 2).
- The MCP tools: `mcp/triage_server.py` already exists and is not changed here.
- Evals and the judge (Epic 3).
- Any user interface.

## Success signal

On a fresh clone, `uv run python load_seed.py` run twice produces an `app.db` that `mcp/triage_server.py` serves T-1042 and customer C-77 from, with `open_tickets` stored as integers. `uv run pytest` shows two things:
- The schema accepts a valid decision (`billing` / `P2` / `billing-team` / a short single-line rationale).
- The schema rejects each of these with a clear error: an unknown category, a missing field, an extra field, `billing` routed to `bug-team`, and a rationale containing a line break or 200+ characters.

## Assumptions

- "Anything else is rejected" covers missing fields, extra fields, wrong types and values outside the allowed sets.
- "Running it twice gives the same database" means the loader rebuilds both tables from the CSVs on each run (replace, not append).
