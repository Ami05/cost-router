"""Regression guard for TICKET-022: check_routing.py's bottom "re-export
shims" block does `from <module> import name` for names that are ALSO
defined by a `def`/module-level assignment earlier in the same file. Because
Python module-level imports rebind the global unconditionally, the earlier
same-named def becomes dead code -- any caller inside check_routing.py
resolves to whichever module the late import pulled from, never the local
def above it. TICKET-020's first fix attempt silently no-opped this way: the
edit landed in the dead local `_check_dispatch_threshold`, while the live
call resolved to routing_resolver.py's copy.

This test computes the current shadowed-name set from the AST (not a
hand-maintained list) and fails only if a name outside the known baseline
below starts colliding -- i.e. it catches new instances of this trap without
demanding the existing ~120-name backlog be cleaned up first. Shrinking the
baseline (as TICKET-022's cleanup proceeds) is expected and should update
KNOWN_SHADOWED_NAMES to match; growing it should not happen and this test is
what catches that.
"""
from __future__ import annotations

import ast
from pathlib import Path

CHECK_ROUTING_PY = Path(__file__).resolve().parent.parent.parent / "scripts" / "check_routing.py"

# Baseline captured 2026-09-27 while investigating TICKET-022. Shrink this
# set as dead local defs get deleted; never grow it to silence a failure --
# a new name appearing here means a new def was added that the re-export
# block already shadows (or vice versa), which is the exact bug class this
# test exists to catch.
KNOWN_SHADOWED_NAMES = frozenset({
    "BULK_VERIFY_RE", "_DISPATCH_OVERRIDE_MARKERS", "_GATE_AGENT_MESSAGE",
    "_GLOBAL_SUBAGENT_KEY", "_MUTATION_KEYWORD_RE", "_PROACTIVE_HINT",
    "_PROGRESS_MARKER_RE", "_PROMPT_FILE_RE", "_ROUTING_REASON_RE",
    "_SIMPLE_MARKERS", "_allow", "_bulk_choice_from_prompt",
    "_bulk_shell_ask_message", "_bulk_shell_dispatch_message",
    "_bulk_shell_user_runs_message", "_candidate_role",
    "_check_dispatch_threshold", "_check_fix_loop",
    "_check_inline_mutation_threshold", "_check_resume_forced",
    "_check_shell_gate", "_churn_nudge_message", "_clear_resume_forced",
    "_concurrency_cap_deny_message", "_context_pressure_check",
    "_conversation_key", "_current_role", "_current_turn_assistant_text",
    "_deny", "_direct_bulk_write_deny_message", "_direct_has_reason",
    "_direct_no_reason_deny_message", "_direct_on_large_write_nudge_message",
    "_direct_write_file_count", "_dispatch_budget_nudge_and_log",
    "_dispatch_bulk_edit_on_sonnet_nudge_message", "_dispatch_debt_active",
    "_dispatch_deny_message", "_dispatch_judgment_on_fast_escalation_message",
    "_dispatch_judgment_on_fast_nudge_message",
    "_dispatch_mechanical_on_reasoning_nudge_message",
    "_dispatch_nudge_message", "_dispatch_requested_model",
    "_dispatch_task_input", "_dispatch_task_text",
    "_dispatch_tier_mismatch_deny_message", "_effective_subagent_count",
    "_expand_scope_names", "_expire_dead_subagents", "_extract_role",
    "_fast_path_limit_deny_message", "_file_name_pattern",
    "_files_in_current_turn_assistant_text", "_files_mentioned_in_prompt",
    "_fix_loop_deny_message", "_fix_loop_nudge_message",
    "_fw_scope_deny_message", "_get_declared_routing",
    "_has_dispatch_budget_override", "_has_dispatch_override",
    "_in_active_subagent", "_increment_resume_denial",
    "_inline_dispatch_volume_deny_message", "_inline_mutation_deny_message",
    "_inline_mutation_nudge_message", "_is_bulk_verification_command",
    "_is_claude_payload", "_is_deploy_command", "_is_genuine_user_prompt",
    "_is_subagent_context", "_is_verification_command",
    "_jaccard_similarity", "_load_candidate_table", "_looks_mutating",
    "_mechanical_intent_deny_message", "_model_cost_rank",
    "_mutation_target_file", "_no_transcript_nudge_message",
    "_out_of_scope_deny_message", "_parse_bulk_verify_choice",
    "_read_session_output_tokens", "_read_subagent_transcript_usage",
    "_read_transcript_tail", "_record_content", "_record_is_meta",
    "_record_role", "_resume_forced_fo_deny_message",
    "_role_tier_mismatch_nudge_message", "_role_tool_mismatch_deny_message",
    "_routing_in_current_turn", "_routing_role_in_current_turn",
    "_scan_lines_backward", "_scan_role_backward",
    "_serialize_verify_deny_message", "_session_no_dispatch_deny_message",
    "_shell_command", "_shell_gate_deny_message", "_shell_gate_nudge_message",
    "_spawn_worthiness_check", "_stall_partial_report_message",
    "_structural_fingerprint", "_structural_similarity_nudge_message",
    "_subagent_progress_gate", "_subagent_recent_assistant_text",
    "_target_is_in_scope", "_text_for_assistant", "_text_for_prompt",
    "_text_from_content", "_tier_transitions", "_tool_name",
    "_transcript_path", "_turn_id", "_verdict_is_current",
    "_wave_barrier_check", "classify_dispatch_work",
    "event_explore_model_gate", "get_routing_for_intent",
    "has_routing_verdict", "is_fast_path_exempt", "score_complexity",
})


def _shadowed_names() -> set[str]:
    """Names defined by a top-level def/assignment that are also pulled in
    by a later top-level `from ... import name` in the same file."""
    src = CHECK_ROUTING_PY.read_text()
    marker_line = next(
        i + 1 for i, line in enumerate(src.splitlines()) if "Re-export shims" in line
    )
    tree = ast.parse(src)
    before_names: set[str] = set()
    after_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno < marker_line:
                before_names.add(node.name)
        elif isinstance(node, ast.Assign):
            if node.lineno < marker_line:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        before_names.add(target.id)
        elif isinstance(node, ast.ImportFrom):
            if node.lineno >= marker_line:
                for alias in node.names:
                    after_names.add(alias.asname or alias.name)
    return before_names & after_names


def test_no_new_shadowed_defs():
    current = _shadowed_names()
    new_collisions = current - KNOWN_SHADOWED_NAMES
    assert not new_collisions, (
        f"New shadowed local def(s) in check_routing.py: {sorted(new_collisions)}. "
        "Each of these names is defined locally AND re-imported from another "
        "module further down the file -- the late import silently wins, making "
        "the local def dead code (see TICKET-022, TICKET-020). Either delete "
        "the dead local def and rely on the import, or remove the shadowing "
        "import if the local def should actually be authoritative."
    )
