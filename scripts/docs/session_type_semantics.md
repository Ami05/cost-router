# Session Type Semantics

## Overview

Session types are derived from the `prompt_intent` field in routing state and indicate the nature of the session work. This document describes the valid values, their meanings, and triggering conditions.

**Source:** `_derive_session_type()` in `session_hook.py` (lines 37–61)

## Value Schema

| Value | Meaning | Trigger Condition |
|---|---|---|
| `qa` | Q&A or information-seeking session | `prompt_intent` starts with `qa_` (e.g., `qa_code_review`, `qa_architecture`) |
| `agentic` | Agent-driven task with tool use or dispatch | `prompt_intent` equals exactly `agentic_action` |
| `unrecognized` | Extraction or classification failed; signal-quality gap | `prompt_intent` is absent, empty, or has an unrecognized prefix (e.g., `unknown_prefix_`) — emits `extraction_fallback` event with reason |
| `unknown` | Reserved for future use; should never be emitted by current logic | N/A (reserved state, not currently returned by `_derive_session_type()`) |

## Consumer Notes

### Treat `unrecognized` as a Signal-Quality Gap
Sessions with `session_type: "unrecognized"` indicate that `prompt_intent` extraction or classification failed. These should be flagged during analysis and filtered before aggregating session statistics or dashboards. Common causes:
- `prompt_intent` field missing or empty in routing state
- Intent prefix does not match known patterns (qa_*, agentic_action)

### Filter Before Aggregates
When computing metrics by session type (e.g., cost per session type, model distribution), exclude `unrecognized` sessions or mark them separately to avoid skewing totals.

### `unknown` Should Never Appear
The `unknown` value is reserved but not currently emitted by `_derive_session_type()`. If encountered in event data, it indicates:
1. Data corruption or manual modification, or
2. Future expansion of the function (do not assume current behavior)

Report any `unknown` values to the routing team for investigation.

## Subagent ID Extraction

**Note:** The dispatch protocol mentions `subagent_id_of()` function behavior:
- Returns `"n/a"` (string) when no subagent ID keys are present in the payload
- Returns `None` (null) when subagent ID keys are present but invalid
- Caller emits `extraction_fallback` event with `reason=subagent_id_missing` in the second case

## Consumer Query

To analyze session type distribution in production data, use:

```sql
SELECT session_type, COUNT(*) cnt FROM routing_events GROUP BY 1 ORDER BY 2 DESC;
```

**Expected result:** `qa` and `agentic` should dominate; `unrecognized` should be minimal (indicates signal-quality gaps). `unknown` should never appear in current data.
