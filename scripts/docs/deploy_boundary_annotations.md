# Deploy Boundary Annotations

**Deploy date:** 2026-09-19T00:00:00Z
**Owner:** Data Analyst
**Linked tickets:** TICKET-068 (main deploy), TICKET-075 (double-emit fix)

## Required Annotations — Main Deploy Boundary

All panels below must carry a vertical annotation at `2026-09-19T00:00:00Z`.

**Annotation text:**
> _"Anti-pattern fix deployed 2026-09-19. model_key attribution corrected. Backfill applied to 1,112 pre-fix entries. Pre-fix values are not comparable to post-fix values."_

### Panels Requiring This Annotation

- [ ] model_key null rate
- [ ] model attribution accuracy
- [ ] cost_per_model trend (all models)
- [ ] extraction_fallback rate
- [ ] routing decision distribution

## Required Annotations — Double-Emit Fix Boundary (TICKET-075)

The `tier_unknown` double-emit bug (TICKET-040) was fixed as part of newReq2. This boundary requires a **separate** annotation because it may differ from the main deploy date.

**Action required:** Confirm exact double-emit fix deploy timestamp, then add:

**Annotation text:**
> _"Double-emit fix deployed (TICKET-040) — tier_unknown rate expected to drop ~50% at this boundary. Pre-fix values include duplicate counts."_

### Panels Requiring This Annotation

- [ ] tier_unknown frequency chart

## Confirmation Checklist

- [ ] model_key null rate — annotated
- [ ] model attribution accuracy — annotated
- [ ] cost_per_model trend — annotated
- [ ] extraction_fallback rate — annotated
- [ ] routing decision distribution — annotated
- [ ] tier_unknown frequency — annotated with double-emit fix timestamp
- [ ] Double-emit fix exact timestamp confirmed: _______________
