# Routing Gates — Implementation Reference

**Derived from live source code, not skill labels. Last verified: 2026-10-01**

This table maps gate names, implementation locations, trigger conditions, enforcement actions (nudge/deny), and configuration keys.

## Verified Gates

| Gate ID | File:Line | Trigger Condition | Action | Threshold Key | Threshold Value |
|---------|-----------|-------------------|--------|----------------|-----------------|
| GATE-1 | read_gate.py:313-330 | Verdict-based read count thresholds | Nudge / Deny | DIRECT_NUDGE_THRESHOLD, DIRECT_DENY_THRESHOLD | 3 / 5 (DIRECT); NUDGE_THRESHOLD, DENY_THRESHOLD 4+ (other) |
| GATE-2 | read_gate.py:298-310 | Post-mutation read pressure | Nudge / Deny | POST_MUTATION_NUDGE_THRESHOLD, POST_MUTATION_DENY_THRESHOLD | 3 / 5 |
| GATE-3 | routing_resolver.py:788-799 | Inline Bash/Shell calls without Task dispatch | Nudge / Deny | SHELL_NUDGE_THRESHOLD, SHELL_DENY_THRESHOLD | 2 / 5 |
| inline-mutation | routing_resolver.py:748-784 | Inline Edit/Write/StrReplace count without Task dispatch (tier-agnostic) | Nudge / Deny | INLINE_MUTATION_NUDGE_THRESHOLD, INLINE_MUTATION_DENY_THRESHOLD | 3 / 6 |
| GATE-RD-2 | check_routing.py:1918-1945 | Structural similarity >70% between consecutive Write calls (RD-2 signal) | Nudge (first), Deny on override | (tracked; compliance logged to usage.jsonl) | analyze via rd2_promote_threshold (0.30) |
| GATE-RD-5 | check_routing.py:2078-2084 | Fix-loop detection: 3+ edit-test-fail cycles on same files | Nudge at 3, Deny at 5 | (fix_loop counter in session state) | 3 / 5 |
| GATE-RD-6 | check_routing.py:2060-2071 | FW scope guard: edits span >N distinct files under single FW declaration | Deny | fw_scope_file_limit | 5 |
| DISPATCH-1 | check_routing.py:1411-1424 | Dispatch volume gate: inline mechanical mutations before first Task dispatch | Deny | DISPATCH_GATE_THRESHOLD | 14 |
| DISPATCH-2 | check_routing.py:1721-1737 | Session-level tier-agnostic gate: no Task dispatched yet + accumulated gate calls | Deny | SESSION_DISPATCH_DENY_THRESHOLD | 12 |
| DR-2 | dispatch_context_gate.py:31-73 | Inline file bodies in code fences >20 lines (not under CONTEXT:/FILES: header) | Nudge / Deny | (first violation nudge; repeat deny) | handoff-dependent |
| DR-3 | dispatch_context_gate.py:76-134 | Handoff profile violation: zero-handoff >50 chars OR paths-only with inline body | Deny | (strict enforcement) | N/A |
| explore-model-gate (P12-9) | dispatch_enforcer.py:835-859 | Task dispatch for Explore subagent without fast-tier model declared | Deny | (none; hard constraint) | N/A |
| file-hash-guard | file_hash_guard.py:entire | File hash mismatch on write (pre_edit, post_edit modes) | Block / Warn | SESSION_LIMIT_FILE_HASH_GUARD | (warning limit per session) |
| detect-resume (forced-FO) | detect_resume.py:entire | Resume pattern: early session + execution intent + N≥3 work units | Force FO in additionalContext | RESUME_GATE_EARLY_CALL_THRESHOLD, RESUME_GATE_WORK_UNIT_THRESHOLD | (early call threshold, min work units) |
| check-read-repeat | check_read_repeat.py:139-171 | Re-read same file in session (full read) | Nudge at repeat 1, Deny at repeat 2+ | DENY_AFTER_REPEAT_COUNT | 2 |

## Unverified Gates (In SKILL.md decision table but no code implementation found)

| Gate ID | SKILL.md Row | Status |
|---------|-------------|--------|
| GATE-8 | Row 12 | UNVERIFIED — alias of the verified inline-mutation gate (nudge 3 / deny 6); follow-up: drop the alias label (not in T5 scope) |
| GATE-9 | Row 13 | UNVERIFIED — alias of verified DISPATCH-1/DISPATCH-2; follow-up: drop the alias label (not in T5 scope) |
| GATE-11 | Row 41 | UNVERIFIED — flip-flop nudge (FLIP_FLOP_THRESHOLD in _config.py:232); follow-up: locate or remove the gate (not in T5 scope) |

Resolved by T5 (2026-10-05) — now verified:

| Gate ID | File:Line | Trigger Condition | Action |
|---------|-----------|-------------------|--------|
| GATE-4 | check_routing.py:1012-1047 | Mechanical work dispatched on a reasoning-tier model | Deny |
| GATE-5 | dispatch_enforcer.py:172-173 | Write on existing file over write_existing_max_lines (150) / write_existing_max_kb (5) | Nudge |
| GATE-6 / HG-102 | check_routing.py:2465-2469; enforce_role_gates.py:164 | FO/RB role + Edit/Write/Shell called directly | Deny |
| GATE-12 | check_routing.py:2471-2490 | Routing: DIRECT without a reason after the em-dash | Deny |
| GATE-13 | check_routing.py:2214-2244 (msg dispatch_enforcer.py:395) | Routing: DIRECT with 5+ file targets | Deny |

## Promotion Procedure — RD-2 (Structural Similarity)

RD-2 is currently a **nudge** gate. Use this procedure to promote it to hard deny:

1. Run: `python3 scripts/analyze_rd2_compliance.py`
2. Check output for override rate vs. threshold
3. If `override_rate > routing.rd2_promote_threshold` (default: 30%):
   - Update config: `routing.rd2_promote_threshold` → lower value OR change check_routing.py line 1918 action from nudge→deny
   - Commit with justification tied to override_rate data point

## Configuration Keys

All thresholds are defined in or referenced by:
- `scripts/_common.py` (threshold constants)
- `scripts/config.json` (routing_matrix, gate overrides)
- Gates read config via `config_get()` / `config_int()` from `_config.py`

Load pattern for config inspection:
```python
from _common import config_get, config_int
threshold = config_int("routing.fw_scope_file_limit", 5)
```

## Notes

- **Subagent exemption:** Most gates exempt subagent context (detected via `_is_subagent_context()`)
- **Fast-path exempt:** Some gates respect `entry.get("fast_path_exempt")` to allow early-session shortcuts
- **Session state management:** Gates read/write to `DATA_DIR / "routing_state.json"` and related files
- **Event logging:** Violations logged to `DATA_DIR / "usage.jsonl"` for compliance analysis
- **Debt/RT exhausted:** Several gates allow pass-through when `_dispatch_debt_active()` returns True, indicating round-trip budget exhaustion to avoid mutual denial deadlocks
