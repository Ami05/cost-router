# Cost Reporting Exclusion Policy

**Status:** Pending stakeholder confirmation
**Effective date:** 2026-09-19
**Owner:** Product Owner
**Linked ticket:** TICKET-064

## Policy

All `reward_log.jsonl` entries with `ts < 2026-09-19T00:00:00Z` are **excluded from cost reports**.

## Reason

Entries before 2026-09-19 contain corrupted `model_key == "unknown"` values (or entries backfilled from corrupted sources) that produce incorrect per-model cost attribution. Including them would inflate or distort reported costs for all model tiers.

## Affected Metrics

- `cost_per_model` (all models)
- `quality_multiplier` (per-session)
- `revert_rate` (per-session)

## Affected Date Range

All data before `2026-09-19T00:00:00Z`. The exclusion applies to:
- Original corrupted entries (model_key == "unknown")
- Backfilled entries tagged with `backfill_run_id: "2026-09-19-remediation"` — these are corrected but should not be treated as equivalent to live-path entries for cost reporting

## Implementation

Cost report queries must include:

```sql
WHERE ts >= '2026-09-19T00:00:00Z'
-- OR equivalently:
WHERE backfill_run_id IS NULL
```

## Consumer Confirmation Required

This is a material change to reported metrics. Before this policy takes effect in production dashboards, all consumers must confirm receipt:

| Consumer | Role | Confirmed | Date |
|---|---|---|---|
| (to be identified) | Cost-per-model dashboard owner | [ ] | — |
| (to be identified) | Quality metrics report subscriber | [ ] | — |
| (to be identified) | Cost threshold alert owner | [ ] | — |

**Deploy is gated on this table being completed.**
