# V-5 Policy: model_key=None Acceptable Writes

**Decision date:** 2026-09-21
**Decision:** Accept None with operational alert (Option A)
**Owner:** Chief Data Architect
**Source:** TICKET-071, TICKET-006, CDA-3

## Decision

`model_key=None` (Python None / JSON null) is **acceptable at ingest** and does not trigger schema rejection.

## Rationale

Schema rejection of `model_key=None` would block valid events where the model cannot be determined at hook call time — for example, during session initialization before any model turn has occurred. Forcing rejection would create spurious `extraction_fallback` events and inflate the fallback rate metric.

## Operational Control

`check_model_key_none_threshold()` (TICKET-046) provides a post-ingest band:
- **Alert threshold:** 2% None rate over any measurement window
- If the None rate exceeds 2%, an alert fires to signal systematic attribution failure

## Accepted Trade-off

| Condition | State |
|---|---|
| `none_rate < 2%` | Normal — no alert |
| `1.9% ≤ none_rate < 2%` | Degraded but below threshold — intentional tolerance |
| `none_rate ≥ 2%` | Alert fires — investigate attribution path |

The original V-5 criterion ("schema rejects None with visible event") was deliberately abandoned per TICKET-006. This policy supersedes it.

## Schema Annotation

`model_key`: `string | null`

Null when model attribution could not be determined at event write time. Consumers must apply `WHERE model_key IS NOT NULL` when computing per-model cost breakdowns.

## What This Does NOT Cover

Silent None writes below the 2% threshold are not acted upon. If a systematic bug causes a 1.5% None rate, it will not alert. Periodic audits of the None rate are recommended even below threshold.
