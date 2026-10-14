# Fallback Hit-Rate SLO

## SLO Definition

**Metric:** `fallback_rate` — fraction of routing decisions resolved via fallback (catch-all or default model) rather than a matched routing rule.

**SLO:** `fallback_rate < 5%` over any rolling 1-hour window.

**Justification:** At steady state the classifier and routing matrix should handle the vast majority of tasks. A 5% ceiling tolerates legitimate classifier misses and edge-case inputs without masking systematic degradation (e.g., a broken classifier returning no intent, a bad config deploy wiping routing rules, or a new task type never seen in training data).

---

## Expected Baseline

| Period | Expected fallback_rate |
|---|---|
| Normal operation | 1–3% |
| After a new classifier deploy | Up to 4% (settling period ~30 min) |
| Degraded / broken classifier | > 10% (hard failure signal) |

If baseline in production exceeds 3% for more than 2 consecutive hours without a deploy, treat it as a latent bug and file an incident.

---

## Alert Logic

### Tier 1 — Warning (transient spike)
- **Condition:** `fallback_rate > 5%` for at least **2 consecutive 5-minute buckets**
- **Action:** Page on-call; check for recent deploys or config changes
- **Suppression:** Silence for 30 min after a known classifier or config deploy

### Tier 2 — Critical (sustained elevation)
- **Condition:** `fallback_rate > 5%` sustained over a **15-minute rolling window**
- **Action:** Page on-call immediately; begin rollback evaluation
- **Do not suppress** based on deploys — a deploy that sustains >5% fallback is itself the incident

### Tier 3 — Hard failure
- **Condition:** `fallback_rate > 20%` for any single 5-minute bucket
- **Action:** Treat as SEV-1; classifier or routing matrix is likely non-functional

### Distinguishing transient from sustained
A spike that clears within one 5-minute bucket (single outlier) is noise — do not alert. Use the 2-bucket minimum for Tier 1 to avoid alert fatigue from transient load bursts.

---

## Response Runbook

### Step 1 — Confirm signal
```
# Check last 30 minutes of fallback events in production log
grep '"routed_via":"fallback"' ~/.claude/plugins/local/cost-router/routing_events.jsonl \
  | tail -200 | python3 -c "
import sys, json
lines = [json.loads(l) for l in sys.stdin]
total = len(lines)
fb = sum(1 for l in lines if l.get('routed_via') == 'fallback')
print(f'fallback_rate={fb/total:.1%} ({fb}/{total})')
"
```

### Step 2 — Check for recent config/classifier changes
- Was `config.json` or `routing_matrix` modified in the last hour?
- Was `classify_intent.py` redeployed?
- Was the plugin resynced (`scripts/sync.sh` or equivalent)?

If yes: compare old vs. new `routing_matrix` for missing rules; roll back if fallback_rate does not recover within 15 min.

### Step 3 — Inspect fallback samples
```
grep '"routed_via":"fallback"' ~/.claude/plugins/local/cost-router/routing_events.jsonl \
  | tail -50 | python3 -c "
import sys, json
from collections import Counter
lines = [json.loads(l) for l in sys.stdin]
intents = Counter(l.get('intent','unknown') for l in lines)
print(intents.most_common(10))
"
```
Look for:
- A dominant intent that should be covered but isn't → add/fix routing rule
- `intent=unknown` or `intent=null` → classifier is failing; check `classify_intent.py` logs
- Spread across many intents → routing matrix has a gap; audit against the task taxonomy

### Step 4 — Remediation paths

| Root cause | Fix |
|---|---|
| Missing routing rule | Add rule to `config.json` → resync |
| Classifier returning wrong/null intent | Roll back `classify_intent.py` or check for keyword collisions in scoring logic |
| Bad config deploy | Roll back `config.json` to last known good |
| Genuine new task type | Add to classifier training data + routing matrix; accept elevated fallback_rate during rollout |

### Step 5 — Verify recovery
After applying a fix, confirm `fallback_rate` drops below 5% over the next two 5-minute buckets before closing the incident.

---

## Ownership

- **Metric source:** `routing_events.jsonl` in production plugin dir
- **Config:** `config.json` → `routing_matrix`
- **Classifier:** `scripts/classify_intent.py`
