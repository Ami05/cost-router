# Pipeline 9: Feedback Loop Orchestration (Async)

**Trigger:** Stop hook (async execution)

**Flow:** pipeline_trigger.py (async) → threshold check on shadow_routes → shadow_evaluator → proposal_writer → policy_manager → policy_gc --apply

## Overview

Pipeline 9 is the automated feedback loop that:
1. Monitors routing decisions (via shadow_routes.jsonl)
2. Evaluates proposed rule changes against collected rewards
3. Generates policy proposals when confidence thresholds are met
4. Auto-applies approved changes (if enabled)
5. Maintains audit trail and undo capability

## pipeline_trigger Configuration

The `pipeline_trigger` entry in `config/reward.json` controls async execution:

| Config Key | Default | Meaning |
|---|---|---|
| `gc_apply` | `false` | If true, apply proposals automatically via policy_gc --apply; if false, proposals are generated but require manual approval |

**JSON structure:**
```json
{
  "pipeline_trigger": {
    "gc_apply": false
  }
}
```

When `gc_apply=false` (default), proposals are written to `data/policy_proposals.json` and logged to `data/policy_changelog.jsonl` but do NOT modify `rules/model-router.md`. Manual review is required.

## Execution Flow

### 1. Trigger Check (pipeline_trigger.py)

On session Stop:
- Read `data/shadow_routes.jsonl` (routing decisions that were shadowed for evaluation)
- Check if new route count exceeds threshold since last trigger
- Maintain cooldown state in `data/pipeline_trigger_state.json` to prevent thrashing
- If threshold met, advance to shadow_evaluator

**State file:** `data/pipeline_trigger_state.json`
```json
{
  "last_triggered_at": "<iso-ts>",
  "routes_at_last_trigger": <int>,
  "last_chain_result": "ok"
}
```

### 2. Shadow Evaluation (shadow_evaluator.py)

For each candidate rule in shadow_routes.jsonl:
- Look up observed rewards from `data/reward_log.jsonl` keyed on (rule_id, candidate_role)
- Compare candidate performance vs. current rule baseline
- Compute improvement_delta = actual_mean_reward - expected_baseline
- Flag selection_bias if candidate data is scarce

**Output:** `data/shadow_evaluation.json`
```json
{
  "rule_1": {
    "candidate_expected_reward": <float>,
    "actual_mean_reward": <float>,
    "improvement_delta": <float>,
    "selection_bias_warning": false,
    "confidence": <float>
  }
}
```

### 3. Proposal Writing (proposal_writer.py)

For each rule with improvement_delta > threshold:
- Extract intent from rule_id (dual-key: intent + rule_id)
- Look up confidence_scores from `data/confidence_scores.json` (dual-keyed by intent + rule_id)
- Compose a policy proposal with reasoning and metrics
- Write to `data/policy_proposals.json`

**Dual-key lookup** (ENRICH-001):
```
confidence_scores[intent][rule_id] = { "confidence": X, "blast_radius": Y }
```

### 4. Policy Manager & Apply (policy_manager.py)

If `gc_apply=true` in config:
- Call `policy_manager.evaluate_proposals()` → validates each proposal
- Call `policy_gc.py --apply` → writes approved changes to `rules/model-router.md`
- Create undo token in `data/routing_changelog.json` for manual reversal

If `gc_apply=false` (default):
- Proposals remain in `data/policy_proposals.json`
- No changes to routing rules
- Manual approval required before editing rules/model-router.md

## Output Files

| File | Written by | Meaning |
|---|---|---|
| `data/shadow_evaluation.json` | shadow_evaluator.py | Per-rule candidate performance metrics |
| `data/policy_proposals.json` | proposal_writer.py, policy_manager.py | Candidate rule changes with confidence scores |
| `data/policy_changelog.jsonl` | policy_manager.py | Audit log of all proposals (applied + rejected) |
| `data/routing_changelog.json` | policy_gc.py | Undo tokens for auto-applied changes |
| `rules/model-router.md` | policy_gc.py (if gc_apply=true) | Updated routing rules with applied proposals |

## Input Dependencies

Pipeline 9 reads:
- `data/shadow_routes.jsonl` — routing decisions shadowed for evaluation
- `data/reward_log.jsonl` — per-rule reward observations
- `data/confidence_scores.json` — aggregated confidence per rule
- `data/quality_signal.json` — per-intent quality context
- `data/tune_state.json` — state of tuning decisions (if present)


## Enabling Auto-Apply

To enable automatic policy application:

1. In `config/reward.json`, set:
   ```json
   {
     "pipeline_trigger": {
       "gc_apply": true
     }
   }
   ```

2. Resync to plugin: `resync.sh` (user does this manually; no auto-resync)

3. Proposals will now be auto-applied on each Stop hook that triggers the feedback loop

4. Review undo tokens in `data/routing_changelog.json` if reversal is needed

**Warning:** Auto-apply bypasses manual review. Keep auto-apply disabled until the shadow evaluation is confirmed reliable (watch proposal quality over ≥10 sessions).
