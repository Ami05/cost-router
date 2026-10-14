# Model Attribution Notes

## Survivorship Bias

Sessions that never emit a `routing_declared` event are invisible to model attribution analytics. This means:

- Cost and token usage for those sessions cannot be attributed to a specific model or role.
- Aggregate attribution metrics (e.g., cost-per-model breakdowns) are computed only over the sessions that do declare routing, not the full session population.
- If a class of sessions systematically skips routing declaration (e.g., short direct tasks, pre-gate failures, crash/restart sessions), the resulting attribution data will be skewed toward sessions that complete normally.

## Health Metric: attribution_coverage_rate

Monitor this metric to detect survivorship bias early:

```
attribution_coverage_rate = sessions_with_routing_declared / total_sessions
```

- **Target:** >= 0.90 (90% of sessions should have at least one routing_declared event)
- **Alert threshold:** < 0.80 sustained over a 1-hour window
- **Source fields:** `routing_events.jsonl` — count distinct `session_id` values with `event_type == "routing_declared"` vs. all distinct `session_id` values

## Implications for Reporting

When producing per-model cost or token reports, always include a coverage caveat if `attribution_coverage_rate` is below target. Do not present attributed totals as whole-population totals without noting the uncovered fraction.
