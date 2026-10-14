# Worker Dispatch Template

## Report Contract

Every worker MUST return a structured first line:

```
STATUS: <status>
Summary: <one-line summary>
```

### Valid Statuses

| Status | Meaning | Controller Action |
|---|---|---|
| DONE | Complete, all acceptance criteria met | Proceed to next task |
| DONE_WITH_CONCERNS | Complete but flags issues | Review concerns, decide |
| BLOCKED | Hit ambiguity or dependency | Resolve/escalate (see controller_routing.md) |
| NEEDS_CONTEXT | Missing file/interface not in brief | Re-dispatch with requested files |

### Example Report

```
STATUS: DONE
Summary: Implemented auth middleware with JWT validation.
Files changed: src/auth/middleware.py, tests/test_auth.py
Tests: 5/5 passing
```

### Example BLOCKED Report

```
STATUS: BLOCKED
Summary: Cannot determine correct error format.
Reason: AMBIGUITY — API error response schema not specified in brief.
Need: Error response interface from src/api/types.ts
```

## Dispatch Format

When dispatching a worker, include:

1. **Task description** — what to implement
2. **Acceptance criteria** — measurable outcomes
3. **File paths** — files to read/modify
4. **Interface signatures** — relevant function/class signatures
5. **Constraints** — global rules (style, testing, etc.)
6. **Report contract** — "Return STATUS: DONE|DONE_WITH_CONCERNS|BLOCKED|NEEDS_CONTEXT as first line"
