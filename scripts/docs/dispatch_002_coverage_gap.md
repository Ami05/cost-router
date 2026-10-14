# DISPATCH-002: Historical Coverage Gap

**Metric:** DISPATCH-002 — cold-start skip rate
**Owner:** Data Analyst
**Linked ticket:** TICKET-088

## Definition

DISPATCH-002 measures the rate of `check_skipped` events emitted when a routing check is bypassed due to a cold-start condition (no prior session state available).

## Coverage Gap

**Earliest reliable date:** `2026-09-19`

**Reason:** The `check_skipped` event emission was added by TICKET-048. Routing checks bypassed before 2026-09-19 produced no telemetry event and are invisible to this metric.

**Impact:** DISPATCH-002 rates computed over a date range spanning before `2026-09-19` will **understate actual skip coverage** for the pre-fix period.

## Backfill Status

Not planned. Historical skip events cannot be reconstructed from existing data.

## Chart Annotation Requirement

Any chart displaying DISPATCH-002 over time **must** include a vertical annotation at `2026-09-19` with text:

> _"TICKET-048 deployed — check_skipped events begin here. Pre-fix values are understated and should not be compared to post-fix rates."_

## Query Guard

Always apply when querying DISPATCH-002:

```sql
WHERE ts >= '2026-09-19T00:00:00Z'
```

Omitting this filter produces a misleading denominator.
