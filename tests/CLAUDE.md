## Test Subagent Rules

- **Test and fix are always separate subagent dispatches.** A subagent dispatched to run tests must only report results — it must never edit code or spawn a child subagent to apply a fix. Diagnose the failure and return findings to the orchestrator; the orchestrator dispatches a separate mutation worker to fix it.

## Test Suite Structure
- `tests/e2e/` — primary integration tests (see `tests/readme.md` for conventions)
- `tests/eval/` — routing/classifier eval suite
- No unit tests at `tests/` root — all consolidated into e2e/eval
