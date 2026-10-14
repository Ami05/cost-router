# Pipeline 8: Session Stop, Reward & Rollup (Sequential)

**Trigger:** Stop hook (sequential portion)

**Flow:** estimate_tokens → check_routing → reward_signal → shadow_log → confidence_score → quality_signal → session_hook → graph_gc → compact_log → session_impact

## reward_log Schema

The `data/reward_log.jsonl` file captures per-rule reward entries and implicit signals. Each row represents a single reward observation.

### Row Fields

| Field | Type | Source | Notes |
|---|---|---|---|
| `session_id` | string | context | Claude Code session identifier |
| `rule_id` | string | context | Routing rule being evaluated (e.g., `DIRECT:qa_simple`) |
| `reward_source` | string (enum) | reward_signal.py | One of: `computed`, `under_route`, `binary_fallback` |
| `schema_version` | integer | reward_signal.py | Current: 1; bumped for backward-incompatible changes |
| `cost_included` | boolean | reward_signal.py | Whether cost penalty was already applied to this reward value |
| `verdict_ts` | float (unix timestamp) | context | Time when the routing verdict was issued; used for ordering and dedup |
| `base_reward_value` | float | reward_signal.py | Raw routing reward; capped at `REWARD_NO_ESCALATION` |
| `quality_multiplier` | float | reward_signal.py | Per-intent quality factor; computed as max(0.1, 1.0 - revert_rate) |
| `reward_value` | float | reward_signal.py | Final computed reward after quality multiplier adjustment |

### Reward Source Types

- **`computed`:** Explicit reward signal from session_hook.summarize_session(); represents actual routing outcome
- **`under_route`:** Implicit signal indicating an overly conservative routing choice (forwarded to usage.jsonl as implicit signal)
- **`binary_fallback`:** Fallback reward (0 or 1) when explicit computation fails; last resort

### Deduplication

Rewards are deduplicated under a flock (distributed lock) keyed on:
```
(session_id, rule_id, reward_source, verdict_ts)
```

**Why:** The same rule evaluated in the same session at the same verdict timestamp should only contribute one reward entry. This prevents duplicate observations from re-runs or retries within the same session.

**Legacy handling:** Rows with `schema_version < 1` are excluded via `signal_filter_rules.exclude_legacy_rewards` in reward_signal.py. Old logs are preserved but not used in feedback loops.

## Reward Computation

### Base Reward Cap

All computed rewards are capped at `REWARD_NO_ESCALATION` constant (defined in reward.json or reward.py). This prevents a single high-impact session from dominating the feedback loop.

### Quality Multiplier Effect

The final reward is adjusted by quality multiplier:
```
final_reward = base_reward_value * effective_multiplier
```

Where quality_multiplier = max(0.1, 1.0 - revert_rate). The effective multiplier applied depends on the base reward sign:
- If base_reward_value >= 0: effective_multiplier = quality_multiplier
- If base_reward_value < 0: effective_multiplier = 2.0 - quality_multiplier

This gives the "2 - qmult rule for negative base" which makes negative rewards worse when quality is low.

### Cost Penalty Configuration

Applied when cost_included=true. Parameters in config/reward.json:

| Config Key | Default | Meaning |
|---|---|---|
| `cost_penalty_cap` | 0.5 | Maximum penalty as fraction of base reward |
| `cost_penalty_divisor_usd` | 1.0 | Cost in USD per penalty unit; scales cost impact |

**Calculation:**
```
actual_penalty = min(cost_incurred / cost_penalty_divisor_usd, cost_penalty_cap * base_reward)
adjusted_reward = base_reward - actual_penalty
```

## Output Files

**Main:** `data/reward_log.jsonl`
- Written by: reward_signal.py, backfill_reward_log.py
- Read by: shadow_evaluator.py, confidence_score.py, quality_signal.py, pipeline_trigger.py

**Supporting:** 
- `data/confidence_scores.json` — aggregated reward metrics per rule
- `data/quality_signal.json` — per-intent quality multiplier for current window

## Configuration Reference

See `config/reward.json`:

```json
{
  "reward": {
    "cost_penalty_cap": 0.5,
    "cost_penalty_divisor_usd": 1.0,
    "REWARD_NO_ESCALATION": 5.0
  }
}
```
