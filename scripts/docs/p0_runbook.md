# P0 Incident Runbook

## Two-Track Response Pattern

When routing data loss is detected, execute both tracks in parallel:

### Track 1: Stop Data Loss (Target: 15 min)

1. **Identify fallback state**
   ```bash
   grep extraction_fallback routing_events.jsonl | tail -20
   ```
   Look for spike in fallback frequency or error messages.

2. **Determine root cause**
   - Check permission errors: `grep "permission\|denied\|access" routing_events.jsonl`
   - Check file corruption: `python3 scripts/validate_data_integrity.py --check-corruption`
   - Check network: check `routing_state.json` timestamp freshness

3. **Execute remediation**
   - **Permission issue:** `chmod 644 routing_events.jsonl reward_log.jsonl gate_counters.json`
   - **Rollback needed:** `git -C ~/.claude/plugins/local/cost-router checkout HEAD -- scripts/extract_routes.py`
   - **State file stale:** `python3 scripts/refresh_routing_state.py --force`

4. **Confirm recovery**
   ```bash
   tail -f routing_events.jsonl
   ```
   Verify extraction_fallback stops appearing. Event rate should resume normal baseline.

### Track 2: Scope and Remediate (Parallel with Track 1)

1. **Determine affected date range**
   - Find first fallback: `grep extraction_fallback routing_events.jsonl | head -1 | jq .ts`
   - Find last fallback: `grep extraction_fallback routing_events.jsonl | tail -1 | jq .ts`

2. **Count affected events**
   ```bash
   grep extraction_fallback routing_events.jsonl | wc -l
   ```

3. **Backfill lost events**
   - Reference: [backfill_methodology.md](backfill_methodology.md)
   - Name backfill run: `backfill_<date>_<hour>_<reason>` (e.g. `backfill_2026-09-19_14_permission_fix`)
   - Run: `python3 scripts/backfill_run_id.py --run-id <backfill_id> --start-ts <first_loss> --end-ts <last_loss>`

4. **Validate remediation**
   ```bash
   python3 scripts/validate_data_integrity.py --before-ts <first_loss> --after-ts <last_loss>
   ```

5. **Document incident**
   - Record methodology in ticket (link below)
   - Include: date range, event count, backfill run ID, validation results

## Escalation Table

| Severity | Definition | Action | Target |
|---|---|---|---|
| **SEV-1** | Active data loss in real time (extraction_fallback rate > 5%) | Page on-call immediately | 15 min to stop loss |
| **SEV-2** | Degraded fallback recovery (fallback present but controlled <1%) | File ticket, triage next business day | 24 hour triage |

## References & Tickets

- [fallback_slo.md](fallback_slo.md) — SLO thresholds and alerting
- [backfill_methodology.md](backfill_methodology.md) — backfill procedure detail
- [deploy_boundary_annotations.md](deploy_boundary_annotations.md) — deployment cutoff times
- **TICKET-089:** P0 incident response playbook v1
- **TICKET-090:** Backfill automation hardening
