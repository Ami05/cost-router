# The New Theorem Workflow

## Principle

No implementation ships without before/after production metrics proving improvement.

## Workflow: 6-Step Process

1. **Capture BEFORE baseline** — `python proof-gate/collect_baseline.py`
2. **Make your code change**
3. **Capture AFTER baseline** — `python proof-gate/collect_baseline.py`
4. **Evaluate delta** — `python proof-gate/evaluate_delta.py --before <before.json> --after <after.json>`
5. **Merge on PASS only** — Review verdicts; merge only if PASS
6. **Automated enforcement** — `python proof-gate/change_gate.py`

## Tracked Metrics & Thresholds

| Metric | Threshold | Direction |
|--------|-----------|-----------|
| classifier_accuracy | +2% | Improvement required |
| fallback_rate | -2% | Reduction required |
| gate_deny_count | Informational | Track only |
| nudge_compliance_rate | +5% | Improvement required |
| cost_estimate_usd | -5% | Reduction required |

## Verdict Definitions

### Per-Metric Verdicts
- **IMPROVED** — Delta exceeds threshold in positive direction
- **UNCHANGED** — Delta within noise; no meaningful change
- **REGRESSED** — Delta exceeds threshold in negative direction

### Workflow Verdicts
- **PASS** — Zero regressions AND at least one improvement across tracked metrics
- **FAIL** — Any regression in tracked metrics

## Tools (NT-001 through NT-004)

- **NT-001** — `collect_baseline.py` — Captures production metrics snapshot
- **NT-002** — `evaluate_delta.py` — Compares BEFORE and AFTER baselines
- **NT-003** — `change_gate.py` — Enforces workflow at merge time
- **NT-004** — Evaluation framework parameters (see `evaluationReport/Evaluation_Parameters.md`)

## Alignment with Evaluation Framework

All metrics tracked align with the evaluation framework parameters:
- Classifier accuracy measured on synthetic datasets (primary metric)
- Fallback rate tracks proportion of queries triggering fallback path
- Gate deny count monitors change gate decisions
- Nudge compliance rate measures adherence to recommendation signals
- Cost estimates validated against production baseline

## Decision Gate

Merge is permitted only after `evaluate_delta.py` returns **PASS** verdict. Automated enforcement via `change_gate.py` ensures no change reaches production without this proof.
