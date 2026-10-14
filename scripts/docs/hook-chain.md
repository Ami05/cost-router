# Hook Chains

Execution order (sequential, async: false unless noted). All chains reflect
`hooks/hooks.json` (Claude Code) as of this writing — cross-checked directly
against the live hook config, not just this doc's own history. Cursor's
equivalent chains in `hooks/hooks-cursor.json` may differ — see Cursor
sections below; don't assume the two are the same.

## Claude Code — `UserPromptSubmit`

1. `estimate_tokens.py --event prompt`  — token budget accounting before routing
2. `check_routing.py --event reset`     — clears per-turn routing state
3. `compact_log.py --checkpoint`        — rollback-check only (no new-proposal apply)
4. `detect_resume.py`                   — detects Task resume patterns, sets `_is_resume`
5. `read_gate.py --event reset`         — clears per-turn read-gate state
6. `classify_intent.py`                 — keyword-heuristic classifier; logs `intent`, `tier`, `model` to `data/usage.jsonl`
7. `context_warn.py`                    — warns at 160k and 180k main-session context tokens

## Claude Code — `PostToolUse`

**Default (no matcher):**
1. `estimate_tokens.py --event tool`    — token budget accounting after tool use
2. `check_routing.py --event scan`      — scans tool outcomes for gate state updates
3. `context_warn.py`                    — warns at 160k and 180k main-session context tokens

**Matcher: `Task`:**
1. `read_gate.py --event reset`         — clears per-tool read-gate state after Task dispatch

**Matcher: `Write|Edit|MultiEdit`:**
1. `mark_graph_stale.py`                — marks symbol-graph cache stale for affected files
2. `file_hash_guard.py --event post_edit` — detects accidental file reverts post-edit

**Matcher: `Bash|Shell`:**
1. `detect_revert.py`                   — detects accidental git reverts in shell output

## Stop Hook Chain

Execution order (sequential, async: false unless noted). Cursor's
`stop` chain in `hooks/hooks-cursor.json` is a **different, shorter** list —
see the Cursor section below; don't assume the two are the same.

## Claude Code — `Stop`

1. `estimate_tokens.py --event response`
2. `check_routing.py --event scan`
3. `reward_signal.py`         — captures per-rule reward entries to `data/reward_log.jsonl` and implicit `under_route` signals to `usage.jsonl`
4. `confidence_score.py`      — aggregates reward-signal events into `data/confidence_scores.json` (confidence + blast-radius per rule)
5. `session_summary.py`      — appends the session-summary row; also rolls up daily signal stats and the project registry
6. `graph_gc.py`              — expires stale per-file symbol-graph caches
7. `compact_log.py`          — full-session merge into `data/scenario_library.json`; then calls `policy_manager.evaluate_proposals()` (the step that actually writes to `rules/model-router.md`) and `policy_manager.check_rollback()`

`file_graph_refresh.py` is **not** in Claude Code's `Stop` chain — it only runs on Cursor's `stop` (see below).

## Cursor — `stop`

1. `session_summary.py`
2. `graph_gc.py`
3. `file_graph_refresh.py` (timeout: 30s)

Notably absent versus Claude Code: `reward_signal.py`, `confidence_score.py`, and the full-session `compact_log.py` call (Cursor only gets `compact_log.py --checkpoint`, wired to `beforeSubmitPrompt`, which does rollback-checking but not new-proposal auto-apply). See the root `README.md`'s "Adaptive routing" section for what this asymmetry means in practice.

## Why the Claude Code order matters

- `session_summary.py` must run before `reward_signal.py` reads session data that assumes a summary already exists for some code paths (rolling registries, etc.) — though `reward_signal.py`/`confidence_score.py` are actually wired to run *before* `session_summary.py` in the live config (steps 3–4 above, step 5 is `session_summary.py`); if you're re-deriving this ordering from first principles rather than reading `hooks.json` directly, don't assume the two must be strictly one order or the other — check the file.
- `confidence_score.py` must run after `reward_signal.py` (reads the reward-signal events `reward_signal.py` just appended to `usage.jsonl` in this same Stop invocation).
- `compact_log.py` runs last so it compacts the fullest possible picture of the session, and because `policy_manager.evaluate_proposals()` — which it calls — is the step that actually mutates `rules/model-router.md`; doing that before the other Stop-hook scripts have finished reading/writing `usage.jsonl` would risk racing them.

## Pipeline 8: Session Stop, Reward & Rollup (Sequential)

**Summary:** Captures routing rewards, computes quality signals, and rolls up session metrics.

**Includes steps 3–7 of the Claude Code Stop chain above** (reward_signal through session_summary). See detailed documentation in [`pipeline_8_reward_capture.md`](./pipeline_8_reward_capture.md).

### Key outputs:
- `data/reward_log.jsonl` — per-rule reward observations (session_id, rule_id, reward_source, verdict_ts)
- `data/confidence_scores.json` — aggregated confidence metrics per rule
- `data/quality_signal.json` — per-intent quality multiplier for reward adjustment
- `data/session_events.jsonl` — summary events (role_distribution, routing_gap, session_output_high)

### reward_log deduplication:
Dedup is keyed on **(session_id, rule_id, reward_source, verdict_ts)** to prevent duplicate observations from the same session-rule pair at the same decision point. Legacy rows (schema_version < 1) are excluded via signal_filter_rules.

### Reward computation:
- Base reward capped at `REWARD_NO_ESCALATION` constant
- Quality multiplier effect: final_reward = base_reward × quality_multiplier (2 - quality_penalty)
- Cost penalty applied when cost_included=true: penalty = min(cost_usd / cost_penalty_divisor_usd, cost_penalty_cap × base_reward)

Configuration parameters in `config/reward.json`:
- `cost_penalty_cap`: 0.5 (maximum penalty as fraction of reward)
- `cost_penalty_divisor_usd`: 1.0 (cost in USD per penalty unit)

## Pipeline 9: Feedback Loop Orchestration (Async)

**Summary:** Evaluates shadow routes against rewards and generates policy proposals for auto-apply.

**Async execution** on Stop (separate from the sequential chain). See detailed documentation in [`pipeline_9_proposal_trigger.md`](./pipeline_9_proposal_trigger.md).

### Flow:
pipeline_trigger.py (threshold check) → shadow_evaluator → proposal_writer → policy_manager → policy_gc --apply (conditional)

### Key control:
`pipeline_trigger.gc_apply` in `config/reward.json`:
- **false** (default): Proposals written to `data/policy_proposals.json` but rules NOT modified; manual approval required
- **true**: Proposals auto-applied to `rules/model-router.md` via `policy_gc --apply`

### Key outputs:
- `data/shadow_evaluation.json` — per-rule candidate performance metrics
- `data/policy_proposals.json` — candidate rule changes with confidence and improvement_delta
- `data/policy_changelog.jsonl` — audit log of all proposals (applied + rejected)
- `data/routing_changelog.json` — undo tokens for reversal of auto-applied changes
- `rules/model-router.md` — updated rules (only if gc_apply=true)


## Gate Conflict Resolution

Two PreToolUse gates can fire simultaneously and produce contradictory
verdicts. When that happens, follow the resolution order below.

### Session gate vs. round-trip gate

**Scenario:** The session gate (e.g. "12 inline mutations, 0 Task dispatches
→ you must dispatch via Task") fires at the same time as the round-trip
budget gate ("dispatch count ≥ max_round_trips → no new cold-start Tasks").

Both gates are correct individually, but together they leave no valid action
unless the caller knows the escape hatches:

| Resolution (try in order) | When to use |
|---|---|
| `Task(resume=<warm_worker_id>)` | A previously-dispatched worker is still warm/idle. Resuming it does NOT count as a new cold-start, so the round-trip gate allows it. |
| Re-emit `Routing: DIRECT` | No warm worker exists. Re-emit `Routing: DIRECT — <reason>` with a Ruling log line (`Ruling: gate-conflict-override — session+round-trip contradiction, no warm worker — cost: one extra inline edit`) to override the session gate for the specific edit. |
| Ask the user | Neither option fits (e.g. the edit is large enough that inline is wasteful but no worker exists). Surface the contradiction and let the user decide. |

**Key rule:** Never silently ignore one gate. If you override, the Ruling
log line is mandatory so the decision is auditable.

### Bulk-verify gate vs. round-trip gate

**Scenario:** The bulk-verify gate blocks inline shell ("dispatch a subagent
to run tests") but the round-trip gate has exhausted the dispatch budget
("no new cold-start Tasks allowed").

**Resolution:** The bulk-verify gate (`check_routing.py`) automatically
detects round-trip exhaustion via `_round_trip_budget_exhausted()` and falls
through to allow inline shell execution. Counter
`gate_bulk_shell_allow_parent_rt_exhausted` tracks these fallback events.

### Session gate vs. round-trip gate

**Scenario:** The session gate ("N mutations with 0 Task dispatches → must
dispatch") fires but round-trip budget is exhausted ("no new Tasks allowed").

**Resolution:** The session gate (`check_routing.py`) automatically detects
round-trip exhaustion via `_round_trip_budget_exhausted()` and falls through
to allow inline edits. Counter `gate_session_allow_rt_exhausted` tracks
these fallback events. The same fallback is applied to the shell volume gate
(`gate_shell_allow_rt_exhausted`) and the inline dispatch volume gate
(`gate_dispatch_vol_allow_rt_exhausted`).

