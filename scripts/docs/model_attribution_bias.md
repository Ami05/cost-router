# Survivorship Bias in Model Attribution

## Overview

Model attribution in the cost-router system measures which Claude model handles each routing decision. However, a critical blind spot exists: **only sessions that emit `routing_declared` events are visible to attribution metrics**. Sessions that fail, time out, or never reach a routing decision are systematically excluded from the data—creating a survivorship bias that skews attribution statistics.

## What Survivorship Bias Means Here

Survivorship bias occurs when analysis focuses only on "successful" units (sessions that routed) while ignoring "failed" units (sessions that didn't). In the cost-router context:

- **Visible:** Sessions where a model was selected and a `routing_declared` event was logged
- **Invisible:** Sessions that crashed, timed out, hit a BLOCKED gate outcome, or never executed a routing decision
- **Result:** Attribution metrics (e.g., "Sonnet handled 60% of routing decisions") only reflect survivors. If the invisibility rate varies by model, failure rate, or routing path, the percentages become misleading.

## Why It Matters

1. **Skewed cost allocation** — If Opus sessions fail at 5x the rate of Haiku, but only successful sessions are counted, Opus appears cheaper and more efficient than it actually is. Real cost-per-session is higher for the "invisible" failures.

2. **Incomplete routing insights** — A model may be underrepresented in attribution data simply because it fails more often, not because it's unused. Strategic routing decisions made on biased data can perpetuate or worsen the problem.

3. **Confidence decay** — Over time, as the attribution dataset grows, it systematically represents only the paths that *worked*, not the distribution of *all attempts*. This compounds inference errors in downstream analysis.

## How to Monitor It

Track **attribution coverage rate** as a key health metric:

```
coverage_rate = (sessions with routing_declared events) / (total sessions attempted)
```

### Implementation

1. **Numerator:** Count unique `session_id` values in `routing_events.jsonl` where `event_type == "routing_declared"`
2. **Denominator:** Count total session starts or total gate decisions (before routing stage)
3. **Report cadence:** Include coverage rate in weekly or monthly model-level cost/usage reports

### Interpretation

- **Coverage > 95%:** Bias is minimal; attribution metrics are reliable
- **Coverage 85–95%:** Moderate bias; note survivorship caveat in reports
- **Coverage < 85%:** High bias; attribution statistics should not be used for cost allocation without adjustment

## Recommendation for Reporting

Any model-level attribution report or cost summary should include:

1. **Coverage rate statement** — e.g., "Attribution metrics represent 92% of total session attempts; 8% did not reach a routing decision"
2. **Bias caveat** — "These percentages reflect successful sessions only. Cost-per-successful-session differs from cost-per-attempt. See model_attribution_bias.md for details"
3. **Reconciliation data** — If possible, report both:
   - Attribution breakdown (model % among routed sessions)
   - Failure rate by model (% of attempted sessions that never routed)

## Related Metrics

- **gate_counters.json** — tracks gate outcomes (DONE, BLOCKED, ERROR) per gate; compare DONE counts to `routing_declared` counts to identify the invisibility magnitude
- **routing_events.jsonl** — the source of truth for visible attribution; filter by `event_type == "routing_declared"` to measure coverage numerator
- **reward_log.jsonl** — may include session-level metadata; cross-check to identify which complete sessions lack routing events

---

*Last updated: 2026-09-20*
