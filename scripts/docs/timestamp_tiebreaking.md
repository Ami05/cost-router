# Timestamp Tiebreaking

## Problem

Events with identical timestamps (to the precision stored in the data file) create ordering ambiguity when multiple events occur within the same millisecond or second boundary. Current behavior depends on traversal direction:

- **Forward iteration (FIFO):** Events appended in order are returned in append order.
- **Reverse iteration (LIFO):** Events are returned in reverse append order when timestamps are tied.

This inconsistency can cause non-deterministic routing decisions and analysis results if callers rely on implicit ordering assumptions.

## Current Behavior

### Append and Storage (FIFO)
Events are written to `routing_events.jsonl` in chronological order as they arrive. Within a single millisecond, all events preserve their insertion sequence.

### Reverse Walk (LIFO)
When code walks the log file in reverse (e.g., `reward_log.jsonl` reader seeking the most recent state), ties are broken by reverse insertion order, not by timestamp refinement.

### Sub-Second Ordering Ambiguity
If two events have identical timestamps (e.g., both `1696123200000` ms), there is no sub-second field to distinguish them. The tie must be broken by secondary logic:

1. **Append order** (current, implicit) — whichever was written first
2. **Reverse walk order** (current, implicit) — whichever was written last
3. **Sequence number** (proposed, pending schema version bump) — monotonic counter independent of time

> **TICKET-026 status:** Reviewed `_fileio.py` — no `seq` field was added. Records still carry only the `ts` ISO timestamp. Tie-breaking remains implicit (file position).

## Recommended Fix: Monotonic Sequence Number

Add a `seq` field to event schemas:

```json
{
  "timestamp": 1696123200000,
  "seq": 42,
  "route_id": "model-a",
  ...
}
```

**Advantages:**
- Deterministic across all traversal directions.
- Survives log rotations and splits (if seq is globally unique or scoped to a session/plugin instance).
- No schema changes to external formats; purely internal to the plugin.
- Cheap to implement: increment a counter on each event write.

**Implementation cost:** Requires `schema_version` bump in `routing_state.json` or a feature flag to signal seq support. Old logs without seq fall back to timestamp tiebreaking; new logs always populate seq.

## Interim Guidance for Callers

Until the sequence number fix is deployed:

1. **Do not assume tie ordering across traversal directions.** If your code walks the log in reverse and expects FIFO append order, it will fail.

2. **Prefer forward iteration.** Use `append` order (oldest first) rather than reverse walks when possible. This matches the FIFO guarantee at write time.

3. **Add application-level sequence tracking.** If you need deterministic ordering, track sequence in your own state object and use it as a tiebreaker in comparisons.

4. **Test with tied timestamps.** Write test cases that inject two or more events with identical timestamps and verify the expected ordering before going to production.

5. **Document assumptions.** If your routing logic depends on timestamp ordering, explicitly state which direction (forward/reverse) is assumed and test both.

## Schema Version Considerations

When `schema_version` is bumped to support `seq`:

- Readers **must** check `schema_version` in `routing_state.json`.
- If version < seq-required, populate `seq` client-side from append order or external source.
- If version >= seq-required, `seq` is guaranteed in all event records.
- Log migration tools should reconstruct `seq` from line number or external ordering if original logs lack it.

## Related Tickets

- **Ticket 031:** Timestamp tiebreaking ambiguity in reverse walks.
- **CLAUDE.md:** "Hard Constraints" — no Anthropic API key at runtime; schemas must be pre-generated and committed.
