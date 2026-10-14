# Backfill Methodology — 2026-09-19 Remediation

**Run ID:** 2026-09-19-remediation
**Date:** 2026-09-19
**Owner:** Chief Data Architect
**Linked ticket:** TICKET-062

## Source

`reward_log.jsonl` entries where `model_key == "unknown"` (string literal, not SQL NULL). These entries were produced during the synthetic pre-population period when model attribution was broken by the anti-pattern.

## Match Key

`session_id` + `turn_id` pair, cross-referenced against `routing_events.jsonl` to recover the correct model attribution from hook event payloads written at the time of each session.

## Count Change: 1,035 → 1,112

The initial corruption audit (TICKET-013) identified 1,035 entries with `model_key == "unknown"`. The backfill sweep processed 1,112 entries because:

- **77 additional entries** were discovered when the match was expanded to include partial-turn records that shared session context with the initial 1,035 corrupted entries.
- These partial-turn records had `model_key` values that were technically non-null but were derived from the same corrupted synthetic pre-population source.
- Including them ensures the entire affected cohort is remediated, not just the directly flagged rows.

## Recovery Method

Model attribution was reconstructed by:
1. Reading `routing_events.jsonl` for each `session_id` in the affected set
2. Extracting the `model` field from `tool_use` events that fired within the same turn
3. Writing the recovered value back to `reward_log.jsonl` entries with `backfill_run_id` and `data_origin` provenance fields

## Provenance Fields Set

| Field | Value |
|---|---|
| `backfill_run_id` | `"2026-09-19-remediation"` |
| `data_origin` | `"backfilled:2026-09-19-remediation"` |

## Verification

Run `python3 scripts/validate_data_integrity.py --check backfill_run_id` to confirm all 1,112 entries carry the `backfill_run_id` field. The check must return PASS before TICKET-061 can be closed.

## Irrecoverable Entries

0 entries declared irrecoverable. All 1,112 affected entries had recoverable model attribution via the `routing_events.jsonl` cross-reference.
