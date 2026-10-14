# Turn ID / Cursor Join Break — Mitigation

## Problem Summary

Cursor `turn_id` join logic broke on 2026-09-19 due to schema evolution. Cursor events populated a new `session_type` field; downstream join predicate still expected old behavior, causing missed joins.

## Decision: Option B — Timestamp Filter

**Adopted solution:** Filter out cursor events before 2026-09-19 using timestamp and session_type.

### Implementation

Apply this filter to downstream join queries:

```sql
WHERE ts < '2026-09-19T00:00:00Z' 
   OR session_type != 'cursor'
```

**Effect:** All cursor events before 2026-09-19 are excluded from join; post-2026-09-19 cursor events with `session_type='cursor'` are explicitly filtered out.

### Rejected Alternative

**Option A: cursor_key_fix_applied flag**
- Add flag to events to mark fixed behavior
- Higher operational overhead (requires flag deployment + verification)
- Doesn't solve pre-2026-09-19 data inconsistency
- *Decision: rejected in favor of simpler timestamp boundary*

## Deployment Checklist

- [ ] Confirm deploy timestamp: **2026-09-19 00:00:00 UTC**
- [ ] Join query deployed with filter: `WHERE ts < '2026-09-19T00:00:00Z' OR session_type != 'cursor'`
- [ ] Test join on: cursor events pre-2026-09-19 (should be excluded)
- [ ] Test join on: cursor events post-2026-09-19 (should be excluded)
- [ ] Test join on: non-cursor events (should be included regardless of date)
- [ ] Monitor join_failure rate post-deploy (target: < 0.1% increase)
- [ ] Document in TICKET-066

## Verification Queries

```sql
-- Verify pre-2026-09-19 cursor events are filtered
SELECT COUNT(*) as excluded_count
FROM routing_events
WHERE ts < '2026-09-19T00:00:00Z' 
  AND session_type = 'cursor';

-- Verify post-2026-09-19 cursor events are filtered
SELECT COUNT(*) as excluded_count
FROM routing_events
WHERE ts >= '2026-09-19T00:00:00Z' 
  AND session_type = 'cursor';

-- Verify non-cursor events are included
SELECT COUNT(*) as included_count
FROM routing_events
WHERE session_type != 'cursor';
```

## Related Ticket

- **TICKET-066:** Cursor turn_id join break mitigation
