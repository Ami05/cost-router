---
name: tune-routing
description: Review usage log for routing misroutes, redundant re-reads, or undispatched multi-step sessions, and propose Decision Table edits. Use when asked to tune/improve the model-router table, or periodically.
context: fork
---

# Tune Routing

1. Run `python3 scripts/analyze_routing.py` from plugin root and print output verbatim.
2. For each suggestion the user acts on, propose the exact Decision Table row — do not edit until user confirms.
3. After approval, edit `rules/model-router.md` using format: `| <task type> | -> <model role> |`.
4. Tell user: "Added to model-router: `<task type>` -> `<model>`."

**Proactive nudge:** `session_summary.py` calls `analyze_routing.py::check_nudge()` on every `/cost-report` run. Nudge fires when: ≥2 new escalations, ≥3 new redundant re-reads, ≥1 undispatched session (≥12 tool calls + 0 Task calls), or ≥15 sessions since last nudge. Cooldown: 2h default (override: `COST_ROUTER_NUDGE_COOLDOWN_HOURS`). Undispatched-session threshold is 1 (more sensitive) — a session with ≥12 tool calls and no Task dispatch means routing was never engaged at all.

**Note:** Heuristic pattern detection over locally estimated events (no raw prompts/responses/paths stored — only hashes and counts). The manual workflow above (steps 1–4, driven by `analyze_routing.py` suggestions surfaced through `/cost-report`'s nudge) is advisory: nothing from that path is edited without your explicit confirmation.

**The pipeline below is separate.** "Auto-apply and queued proposals" is an independent mechanism. It is off by default and only runs when `policy.auto_apply_enabled` is set. When enabled, it edits only marker-tagged rows in `rules/model-router.md`; human-written rows are never touched. Every change is reversible with an undo token (`--undo TOKEN`) and is rolled back automatically on measured regression.

## Auto-apply and queued proposals

Routing proposals accumulate evidence across sessions. This pipeline is off by default and runs only when `policy.auto_apply_enabled` is set. When enabled, a proposal is marked `auto_apply_eligible` when it has low blast-radius (single row, single tier, no Task routing), shadow validation, and an evidence count at or above `policy.auto_apply_threshold` in `config.json`, and is then applied to `rules/model-router.md` when `compact_log.py` runs. Only marker-tagged rows written by this pipeline are added or removed; human-authored rows are never modified. Every applied change is recorded in `data/routing_changelog.json` with an 8-hex `undo_token`. To review pending proposals run `python3 scripts/policy_manager.py --status`; to list only queued (not-yet-auto-applicable) proposals use `--list`. To reverse any applied change pass the token to `python3 scripts/policy_manager.py --undo TOKEN` — the marker-tagged rule row is removed and the changelog entry is marked `rolled_back`.

## Table maintenance

Run `python3 scripts/policy_gc.py --dry-run` from the plugin root to inspect the Decision Table for dead rows (tools with no scenario-library evidence), near-duplicate entries (SequenceMatcher ratio > 0.7), and size-cap violations (max 80 rows). Use `--apply` to automatically remove all flagged rows from `rules/model-router.md`. The GC check runs automatically after each session via `compact_log.py` and prints `[cost-router GC]` to stderr when action is needed.

## Dispatch context metrics

Query `dispatch_registry.json` to surface composition metrics from the last N dispatches:
- **avg invariant ratio:** average of `invariant_ratio` field over dispatches with composition data; indicates % of prompt tokens consumed by infrastructure patterns (hook chain, API calls, event shapes, gate results). Low ratios suggest prompts are task-spec heavy; high ratios may indicate prompt bloat from inline infrastructure context.
- **invariant token coverage:** total invariant_tokens across recent dispatches vs total prompt_tokens, used to tune the invariant fingerprint patterns.
