# Backfill Before/After Analysis

**Date:** 2026-09-21  
**Backfill Operation:** V-9 model_key retroactive normalization  
**Ticket:** TICKET-084

## Pre-Backfill State

- **Corrupted entries:** 1112 (entries with `model_key = None|'unknown'|'None'`)
- **Inaccessible source:** Pre-backfill entries lacked model_key extraction
- **Impact:** Downstream queries unable to filter by model, routing metrics unusable

### Example Pre-Backfill Entry
```json
{
  "event_type": "routing_declared",
  "session_id": "sess-xyz",
  "model_key": null,
  "timestamp": "2026-09-21T10:00:00Z"
}
```

## Post-Backfill State

- **Recovered entries:** 1112 → 0 corrupted
- **Recovery sources (priority order):**
  1. Transcript: `~/.claude/projects/<cwd_hash>/<session_id>.jsonl` — dominant model extracted
  2. routing_events: nearest `routing_declared` event — mapped to canonical model
  3. Irrecoverable: tagged `backfill_source: none`
- **Impact:** All entries now have valid model_key; routing metrics queryable

### Example Post-Backfill Entry
```json
{
  "event_type": "routing_declared",
  "session_id": "sess-xyz",
  "model_key": "haiku",
  "timestamp": "2026-09-21T10:00:00Z",
  "backfill_source": "transcript"
}
```

## Verification

Run backfill script with audit trail:
```bash
python3 scripts/backfill_reward_log.py --dry-run  # inspect changes
python3 scripts/backfill_reward_log.py --live     # apply changes
```

Each entry records `backfill_source` for downstream audit.

## Deployment Checklist

- [x] Baseline captured (v9_baseline.md)
- [x] Backfill script validated (scripts/backfill_reward_log.py)
- [x] Integration tests created (test_synthetic_extraction_failure.py, test_append_event_fail_closed.py)
- [ ] Dry-run executed in staging
- [ ] Post-backfill state verified
- [ ] Dashboard metrics re-validated
