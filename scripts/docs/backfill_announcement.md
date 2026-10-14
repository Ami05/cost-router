# Backfill Announcement — Consumer Alert

## Affected Metrics

Data backfilled on **2026-09-19** affects the following metrics for all events with `ts < 2026-09-19`:

| Metric | Impact | Action Required |
|---|---|---|
| quality_multiplier | Recomputed with corrected routing matrix | Discard cached reports; re-run dashboards |
| revert_rate | Recalculated against restored events | Clear pivot tables; refresh from source |
| cost_per_model | Reallocated across model cohorts | Do not compare pre-backfill snapshots |

## Consumer Actions

1. **Clear cached reports and dashboards**
   - Do not trust snapshot reports generated before 2026-09-19 14:00 UTC
   - Re-run all historical reports from source queries
   - Example: if your dashboard filters on `date < 2026-09-19`, refresh it now

2. **Do not compare pre- vs. post-backfill snapshots**
   - Time-series comparisons spanning 2026-09-19 are invalid
   - Annotate 2026-09-19 14:00 UTC as a structural break in dashboards
   - Use separate cohorts: `before_backfill` and `after_backfill` for analysis

3. **Confirm receipt & understanding**
   - Reply in TICKET-076 with your team name and confirmation that you have (a) cleared cached reports and (b) understand the time-series break

## Backfill Methodology

For detailed methodology, rationale, and validation results, see:
- [backfill_before_after.md](backfill_before_after.md) — side-by-side before/after comparison

## Related Ticket

- **TICKET-076:** Backfill announcement and consumer coordination

## Consumer Confirmation Table

| Team | Report/Dashboard | Cleared | Confirmed | Date |
|---|---|---|---|---|
| [Team A] | [dashboard_name] | Yes / No | [contact_name] | 2026-09-19 |
| [Team B] | [dashboard_name] | Yes / No | [contact_name] | 2026-09-19 |
| [Team C] | [dashboard_name] | Yes / No | [contact_name] | 2026-09-19 |

*Update this table as confirmations arrive.*
