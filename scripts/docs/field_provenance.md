# Field Provenance: data_origin vs backfill_run_id

**Owner:** Chief Data Architect
**Linked ticket:** TICKET-085

## data_origin

- **Type:** `string | null`
- **Format:** `"backfilled:YYYY-MM-DD-run-name"` for backfill entries
- **Purpose:** Human-readable provenance label for display in dashboards and audit logs
- **Set at:** Write time by backfill script
- **Null when:** Entry was written by normal `append_event()` operation, or entry predates the field

## backfill_run_id

- **Type:** `string | null`
- **Format:** `"YYYY-MM-DD-run-name"` (e.g., `"2026-09-19-remediation"`)
- **Purpose:** Machine-readable identifier for filtering, rollback, and audit operations
- **Set at:** Write time by backfill script only
- **Null when:** Entry was written by normal operation

## Relationship

Both fields are set on the same backfill entries in different formats. They are not redundant:

- Use `backfill_run_id` for **filtering** (WHERE clauses, rollback)
- Use `data_origin` for **display** (GROUP BY in dashboards, reports)

## Canonical Query Patterns

```sql
-- Filter backfill entries for rollback
WHERE backfill_run_id = '2026-09-19-remediation'

-- Display provenance breakdown
SELECT data_origin, COUNT(*) FROM reward_log GROUP BY data_origin ORDER BY 2 DESC

-- Find entries lacking provenance (pre-field entries)
WHERE backfill_run_id IS NULL AND data_origin IS NULL

-- Exclude all backfill entries from cost reports
WHERE backfill_run_id IS NULL
```

## Future Schema Evolution

Planned: consolidate into a single `provenance` JSON object field in schema_version 2+. Until then, always set both fields together in any new backfill script.
