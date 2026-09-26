---
title: 'Human-gated escalation'
type: 'feature'
created: '2026-09-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '9391f601a2f8b64b5dfc33804e2d4e4640d2d267'
context:
  - '{project-root}/_bmad-output/specs/spec-epic-2/SPEC.md'
  - '{project-root}/TRIAGE_POLICY.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The policy says to escalate a P1 for an Enterprise customer to a person. The agent from story 1 has no way to escalate, and nothing makes a person approve an escalation first (Epic 2 CAP-5).

**Approach:** Add a local `escalate_to_human` tool to `agent.py`, gated by LangChain's `HumanInTheLoopMiddleware`. Every call pauses the run until an approval callback answers yes or no. By default the callback asks at the terminal, and Epic 3's eval can pass its own. `triage()` still returns only the four-field decision.

## Boundaries & Constraints

**Always:**
- `escalate_to_human(ticket_id: str, reason: str) -> str` is a local `@tool` in `agent.py`. It has no real side effect and returns a one-line confirmation. It is gated by `HumanInTheLoopMiddleware(interrupt_on={"escalate_to_human": {"allowed_decisions": ["approve", "reject"]}})`, with an `InMemorySaver` checkpointer and a `thread_id` per run.
- `triage(ticket_id, model=None, approve=ask_at_terminal)`:
  - `approve` receives each action request (`name` and `args`) and returns a bool.
  - `True` resumes with `approve`, and the tool runs.
  - `False` resumes with `reject` and the message `A person declined the escalation.`, and the tool never runs.
  - The run resumes until no interrupt is left, then returns the decision dict as before.
- `ask_at_terminal` shows the ticket ID and reason and asks `Escalate? [y/N]`. Only `y` or `yes` (any case, spaces trimmed) counts as approval. Anything else, including an empty answer or end of input, counts as no. It then prints `Escalated.` or `Not escalated.`
- The working steps in the system prompt gain one step: if the policy says to escalate (final priority P1 and an Enterprise customer), call `escalate_to_human` before returning the decision.
- Story 1's behaviour is unchanged: one retry, `TriageValidationError`, the provider switch, the tool order and the rule that ticket text is data.

**Never:**
- Escalating without an explicit yes, or deciding escalation in code rather than through the model and the policy.
- Returning or printing extra keys in the decision; the output must still pass the Epic 1 schema for Epic 3.
- Changing `run_agent.py`, `mcp/triage_server.py`, `triage/`, `TRIAGE_POLICY.md` or `seed/`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Approved | T-1044, the scripted model calls `escalate_to_human`, and the approver says yes | The approver is asked once with `ticket_id` T-1044 and the reason; the tool runs; the P1 decision is returned | N/A |
| Declined | Same, and the approver says no | The tool does not run; the model sees the decline message; the decision is still returned | N/A |
| No escalation | T-1042, no escalation call | The approver is never called; the result is the same as in story 1 | N/A |
| Terminal answers | `yes`, `Y`, ` yes ` / `no`, `""`, `maybe`, EOF | Approve / decline, and prints `Escalated.` or `Not escalated.` | EOF is a no, never a crash |
| Live yes | `echo yes \| PROVIDER=groq uv run python run_agent.py T-1044` | The prompt appears, then `Escalated.`, then a P1 decision | N/A |
| Live no | The same with `echo no` | `Not escalated.`, then a P1 decision | N/A |

</frozen-after-approval>

## Code Map

- `agent.py` (story 1) -- `triage()` builds `create_agent` with the MCP tools, `SYSTEM_PROMPT` (the policy plus `WORKING_STEPS`) and `ToolStrategy(TriageDecision, handle_errors=...)`. Add the tool, the middleware, the checkpointer, the `approve` loop and `ask_at_terminal` there. Keep `_error_message`, the retry counter and `build_model` as they are.
- Installed `langchain/agents/middleware/human_in_the_loop.py` -- `interrupt_on`; decisions `{"type": "approve"}` and `{"type": "reject", "message": ...}`. The interrupt value carries `action_requests`, each with `name` and `args`. Resume with `agent.ainvoke(Command(resume={"decisions": [...]}), config)` (`langgraph.types.Command`), and read `state["__interrupt__"]`.
- `tests/test_agent.py` -- the `ScriptedModel` / `scripted()` helpers and the `seeded_db` fixture. Extend `scripted()` so a script can include the escalation call. T-1044 belongs to customer C-91 (Globex, Enterprise, 3 open tickets).
- `run_agent.py` -- already calls `triage(ticket_id)`, so the default approver applies with no change.
- Epic 3 relies on the `approve` callback to auto-approve and count escalations, and on the four-field output.

## Tasks & Acceptance

**Execution:**
- [x] `agent.py` -- add `escalate_to_human`, the middleware, the checkpointer, the `approve` parameter with the resume loop, `ask_at_terminal`, and the escalation working step. -- CAP-5.
- [x] `tests/test_agent.py` -- one test per non-live matrix row, using scripted models with the real MCP tools and no API keys. Monkeypatch `input` for the terminal answers, including raising `EOFError`. -- Keeps CAP-5 verifiable offline.

**Acceptance Criteria:**
- Given no API keys, when `uv run pytest` runs, then every test passes, including story 1's agent tests.
- Given a real Groq key, when the two live rows run, then they behave as the matrix says.

## Implementation Notes

- Implemented by a subagent from this spec. Files: `agent.py` (the `escalate_to_human` tool, `HumanInTheLoopMiddleware`, `InMemorySaver` with a uuid `thread_id`, the `approve` resume loop, `ask_at_terminal`, working step 4) and `tests/test_agent.py`.
- Review pass 1 patches: all terminal text goes to stderr, so stdout carries only the decision JSON. Added a test for two escalation calls in one turn (each is asked separately, and only the approved one runs). The `interrupt.id`-keyed branch for several interrupts in one step stays untested, because this agent never produces that.
- Deferred: whether a live model re-calls the tool after a decline (see `deferred-work.md`).
- `uv run pytest -q` with no API keys: 82 passed. Live on Groq, T-1044 with `yes`: `Escalated.` and access/P1/access-team. With `no`: `Not escalated.` and the same decision. Stdout parses as JSON in both. Groq's limit of 8,000 tokens a minute failed some back-to-back runs, and Gemini's daily quota was spent.
- The MLflow LangChain tracer logs `AttributeError` on `on_interrupt` and `on_resume`. That is a dependency limitation; the traces still record.

## Spec Change Log

## Review Triage Log

Review pass 1 (2026-09-26). Layers: Blind Hunter (BH), Edge Case Hunter (ECH), Verification Gap (VG). Findings raised by more than one layer are logged once.

| # | Finding | Layers | Verdict | Evidence | Route |
|---|---------|--------|---------|----------|-------|
| 1 | Several escalation requests in one turn are untested, as is the multi-interrupt branch | VG, BH | medium | Pre-verified by VG. Every test has exactly one action request, so reusing one answer for both, or dropping one, would stay green | patch |
| 2 | The approval prompt and `Escalated.` share stdout with the decision JSON | BH | low | `echo yes \| run_agent.py T-1044 \| jq` breaks. Sending the interactive text to stderr is a direct fix that leaves the terminal view unchanged | patch |
| 3 | After a decline, a live model may call `escalate_to_human` again and again, with no cap | BH, ECH | maybe-false | The loop has no bound, and each resume gets a fresh recursion budget. In the one live "no" run, Groq did not repeat the call. Settle it with several live `echo no` runs on both providers. Would be medium if true | defer |
| 4 | The escalation `ticket_id` is not checked against the ticket being triaged | BH, ECH | low | The person sees the ticket ID before answering, and the tool has no side effect; the fix adds a guard | reject |
| 5 | The model-written reason is printed without cleaning ANSI codes or newlines | BH, ECH | low | Needs an injected reason that fakes the prompt, and the real `Escalate? [y/N]` still follows; the fix adds cleaning | reject |
| 6 | `approve(...) is True` treats truthy non-bool answers as a decline | BH, VG | low | It fails safe, and the docstring says to return True or False | reject |
| 7 | An approver that raises propagates out of `triage()` | BH, ECH | false | A loud failure on Ctrl-C or a broken callback is correct behaviour, not a silent escalation | reject |
| 8 | The human's decision is not a dedicated trace attribute | BH | low | The tool span appears only when approved, and Epic 3 counts escalations through the callback | reject |
| 9 | A P1 decision for an Enterprise customer without an escalation call is not flagged | BH, ECH | false | The intent says escalation is the model's decision, never code's | reject |
| 10 | The test class `Approver` shadows `agent.Approver` | BH | low | Cosmetic, test-only | reject |
| 11 | The test's `prompt.index("Return the decision")` is fragile | BH | low | The policy is read-only and does not contain that phrase | reject |
| 12 | The live matrix rows have no recorded evidence | BH, VG | false | Run live on Groq during step 3: `yes` gave `Escalated.` and P1 (three of five runs; the other two hit Groq's rate limit of 8,000 tokens a minute), and `no` gave `Not escalated.` and P1 | reject |
| 13 | The MLflow tracer raises `AttributeError` on `on_interrupt` and `on_resume` | implementer note | low | A dependency limitation; the traces still record, and fixing it means touching protected files | reject |

## Verification

**Commands:**
- `uv run pytest -q` (with no API keys set) -- expected: all tests pass.
- `echo yes | PROVIDER=groq uv run python run_agent.py T-1044` -- expected: prompt, `Escalated.`, a P1 decision.
- `echo no | PROVIDER=groq uv run python run_agent.py T-1044` -- expected: prompt, `Not escalated.`, a P1 decision.
