# Resolution Source Query

## Consumer Query

Run this query against routing_events to validate resolution_source distribution:

```sql
SELECT 
  resolution_source,
  COUNT(*) as event_count,
  ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) as pct
FROM routing_events
WHERE ts >= '2026-09-19'
GROUP BY resolution_source
ORDER BY event_count DESC
```

## Expected Distribution

| resolution_source | Expected % | Acceptable Range | Notes |
|---|---|---|---|
| routing_matrix | 85–90% | 82–92% | Primary resolver; includes model override |
| inline_lookup | 8–12% | 6–14% | Fallback when routing_matrix has gap |
| fallback_state_file | <5% | 0–5% | Degraded mode; persisted state used |
| unknown | 0% | N/A | **Must be zero for V-3 sign-off** |

## Validation Checklist

- [ ] Query runs without error
- [ ] Event count >= 100,000 (statistical validity)
- [ ] routing_matrix in range (82–92%)
- [ ] inline_lookup in range (6–14%)
- [ ] fallback_state_file in range (0–5%)
- [ ] unknown == 0 (required for V-3)
- [ ] Anomalies documented in ticket

## Related Ticket

- **TICKET-074:** Resolution source distribution validation

## Historical Context

If query results deviate from expected ranges:
1. Check deployment boundary: did a gate rollout happen during the query window?
   - Reference: [deploy_boundary_annotations.md](deploy_boundary_annotations.md)
2. Check for data loss: were fallbacks triggered?
   - Reference: [p0_runbook.md](p0_runbook.md)
3. Document deviation and root cause in TICKET-074
