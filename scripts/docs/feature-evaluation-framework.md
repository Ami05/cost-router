# Cost-Router Feature Evaluation Framework

## Purpose

Data-driven evaluation of cost-router features. Every feature must prove its value from **actual telemetry**, not theoretical reasoning or internet benchmarks. This framework applies to:
- Existing features under review (keep vs clean up)
- Proposed new features (build vs skip)
- Features that have been built but never validated

## Evaluation Protocol

### Phase 1: Data Inventory

Before any analysis, run these queries against the actual data files. Do NOT skip this step. Do NOT substitute general knowledge for missing data.

```bash
# 1. Feature-specific events in usage.jsonl
grep -c '"type":"<feature_event_type>"' data/usage.jsonl

# 2. Total telemetry volume (baseline)
wc -l data/usage.jsonl data/shadow_routes.jsonl data/reward_log.jsonl data/calibration.jsonl

# 3. Feature-specific state files
ls -la data/<feature>_state.json data/<feature>*.jsonl 2>/dev/null

# 4. Gate events (if feature has a gate)
grep -c '<feature_gate_name>' data/gate_events.jsonl

# 5. Session impact records mentioning this feature
grep '<feature_name>' data/session_summaries.jsonl | wc -l
```

Record results in the scorecard below. **Zero counts are valid findings** — they mean the feature has no proven impact.

### Phase 2: Scorecard

Fill every cell with data references or "NO DATA". Never fill with theoretical reasoning.

| Dimension | Question | Data source | Finding |
|---|---|---|---|
| **Activation count** | How many times has this feature fired? | `usage.jsonl` event count for feature type | _count or NO DATA_ |
| **User-facing triggers** | How many times did the user see output from this feature? | `additionalContext` / `hookSpecificOutput` events | _count or NO DATA_ |
| **Downstream consumers** | What other scripts/features read this feature's output? | `grep -r` for data file paths | _list or NONE_ |
| **Cost impact (measured)** | Is there a before/after cost comparison? | `calibration.jsonl`, session cost deltas | _delta or NO DATA_ |
| **Gate denials** | Has this feature ever blocked a wasteful action? | `gate_events.jsonl` deny counts | _count or NO DATA_ |
| **Feedback loop contribution** | Does this feature's data feed shadow/reward/proposal pipeline? | Check if events flow into `shadow_routes.jsonl` or `reward_log.jsonl` | _yes/no with counts_ |
| **Error/failure rate** | Has this feature ever failed or produced incorrect output? | stderr logs, bug tickets, user complaints | _count or UNKNOWN_ |
| **Code surface** | Lines of code, number of files, imports | `wc -l`, dependency graph | _lines, files_ |
| **Maintenance incidents** | How many times has this feature required fixes? | `git log --oneline scripts/<feature>.py | wc -l` | _commit count_ |

### Phase 3: Verdict Matrix

Based on the scorecard, classify the feature:

| Activation count | Downstream consumers | Verdict |
|---|---|---|
| >0, meaningful volume | Yes, active | **KEEP** — proven value, active in pipeline |
| >0, meaningful volume | None | **EVALUATE** — fires but output goes nowhere |
| >0, trivial volume | Any | **EVALUATE** — may not justify maintenance cost |
| 0 | Any | **SUSPECT** — never activated; likely dead or broken |
| 0 | None | **CLEAN UP** — zero value, pure maintenance burden |

### Phase 4: Decision

For each verdict:

**KEEP**: Document the data evidence in a one-line comment at top of script.

**EVALUATE**: Answer these two questions with data:
1. If we delete this feature, what measurable capability do we lose?
2. What is the maintenance cost of keeping it? (commits in last 3 months, bug fixes, test count)

If (1) is "nothing measurable" and (2) is > 0, clean up.

**SUSPECT**: Investigate why it never fired:
- Is the trigger condition too narrow?
- Is the data pipeline broken upstream?
- Was the feature superseded by another?
Set a 2-week observation window after fixing. If still zero activations, clean up.

**CLEAN UP**: Delete script, remove hook wiring, remove tests, remove doc references. One PR, no partial cleanup.

---

## Anti-Patterns (from real experience)

### 1. "Theoretically saves money"
**Example**: context_budget.py (deleted) was claimed to "prevent bloated turns costing $0.20-0.30+" — but zero `compact_trigger` events existed in telemetry. The claim was based on API pricing knowledge, not measured savings.

**Rule**: If you cannot point to a row in `usage.jsonl` or `reward_log.jsonl`, the impact is unproven. Say "NO DATA" — do not fabricate impact narratives from general knowledge.

### 2. "The feedback loop will use this data"
**Example**: `capture_signal.py` was described as feeding the adaptive routing pipeline, but `reward_signal.py` had already replaced it. The script existed, was imported (fail-open), but its output was consumed by nothing.

**Rule**: Trace the full data flow: script → output file → consumer script. If the consumer doesn't exist or never reads the file, the producer is dead code.

### 3. "Low effort to keep"
**Example**: `shadow_router.py` was ~100 lines with one caller in `policy_manager.py`. "Low effort" ignored: the lazy import path, the shadow_validated flag logic, the test file, the README references, and the 6 files that needed cleanup when it was finally removed.

**Rule**: Maintenance cost = code lines + test lines + doc references + import sites + config entries. Count all of them.

### 4. "It might be useful later"
**Rule**: If a feature has zero activations and zero downstream consumers TODAY, delete it. Rebuilding a 100-line script from git history takes 10 minutes. Maintaining dead code across every refactor costs more.

---

## Applying This Framework

### Quick command: Feature health check

```bash
# Run from project root. Replace FEATURE with the event type.
FEATURE="compact_trigger"
echo "=== $FEATURE activation count ==="
grep -c "\"type\":\"$FEATURE\"" data/usage.jsonl 2>/dev/null || echo "0"
echo "=== downstream consumers ==="
grep -rl "$FEATURE" scripts/*.py | grep -v __pycache__
echo "=== code surface ==="
wc -l scripts/*${FEATURE}*.py 2>/dev/null || echo "no dedicated script"
echo "=== git activity (last 3 months) ==="
git log --oneline --since="3 months ago" -- scripts/*${FEATURE}*.py 2>/dev/null | wc -l
```

### Current features pending evaluation

| Feature | Script | Event type | Status |
|---|---|---|---|
| Context budget / compact trigger | `context_budget.py` | `compact_trigger` | **CLEANED UP — deleted 2026-09-01** |
| Shadow routing | `shadow_log.py` | shadow route entries | **SUSPECT** — 0 entries in shadow_routes.jsonl |
| Reward signal | `reward_signal.py` | reward entries | **EVALUATE** — 46 entries exist |
| Token estimation | `estimate_tokens.py` | prompt/tool/response | **KEEP candidate** — 1,193 events |
| Calibration | `estimate_tokens.py` | calibration records | **EVALUATE** — 22 records, unclear if consumed |

---

## Review cadence

Run this evaluation:
- Before building any new feature
- Monthly for all active features (use `/cost-report` + this framework)
- Immediately when a feature is found to have zero activations
