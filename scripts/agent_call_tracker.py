#!/usr/bin/env python3
"""cost-router hook: track Agent/Task subagent spawns and warn when Explore agents
lack model='haiku'. Fires as PreToolUse on Agent|Task tool calls.

DISPATCH-5: on Claude Code Task/Agent, enforce dispatch context budget ladder
(corrected est. vs model window). Fail-open on estimator errors.

This module is a thin orchestration shim. Domain logic lives in:
  - spawn_tracker.py        — event recording, stats integrity, agent spawn history
  - bws_enforcer.py         — BWS wave barrier, file-range disjointness, concurrency cap
  - dispatch_budget_enforcer.py — budget checks, grouping enforcement, model-tier validation
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DISPATCH_REGISTRY_FILE,
    append_event,
    now_iso,
    read_stdin_json,
    session_id_of,
    TASK_TARGET_HASH_CHARS,
    hash_target,
)
from estimate_dispatch_context import extract_paths_from_prompt  # noqa: E402

# Re-exports consumed by other modules (gates.py imports event_preuse;
# enforce_test_runner_model.py imports _get_work_unit_intent;
# check_budget_status.py imports _session_last_activity_ts).
# finalize_dispatch_charge is called directly in main() below (TICKET-014).
from spawn_tracker import (  # noqa: E402
    SESSION_LIMIT_AGENT_CALL_TRACKER,
    STATS_FILE,
    _load_session_stats,
    _save_stats,
    _session_last_activity_ts,
    check_stats_integrity,
    finalize_dispatch_charge,
)
from bws_enforcer import (  # noqa: E402
    _FRONTLOAD_NUDGE_WINDOW_SECONDS,
    _first_resume_of,
    _get_work_unit_intent,
    _seconds_since_worker_finished,
    get_active_file_worker_map,
)
from dispatch_budget_enforcer import (  # noqa: E402
    DISPATCH_BUDGET_OVERRIDE_RE,
    EXPLORE_HAIKU_WARNING,
    _TASK_TOOLS,
    _budget_enforcement_message,
    _has_dispatch_budget_override,
    _orchestrator_turn_cost_usd,
    check_cost_warnings,
    check_grouping,
    commit_cost_warnings,
)
from session_state import ROUTING_STATE_FILE  # noqa: E402


# ---------------------------------------------------------------------------
# Payload parsing helpers (shared across all callers via this shim)
# ---------------------------------------------------------------------------

def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        return tool_input
    tool_input = payload.get("toolInput")
    return tool_input if isinstance(tool_input, dict) else {}


def _tool_name(payload: dict[str, Any]) -> str | None:
    """Return the tool name from the payload, or None if absent.

    None is the sentinel for "no tool in this event" — callers must
    handle None explicitly rather than treating it as an empty string.
    """
    for key in ("tool_name", "tool", "toolName"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None  # absent-tool sentinel


def _task_prompt(tool_input: dict[str, Any]) -> str:
    for key in ("prompt", "task", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _routing_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("agent_message", "text", "last_assistant_message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def _is_claude_payload(payload: dict[str, Any]) -> bool:
    return bool(payload.get("hook_event_name")) and not payload.get("cursor_version")


def _allow(is_claude: bool, message: str | None = None) -> dict[str, Any]:
    if is_claude:
        output: dict[str, Any] = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }
        if message:
            output["hookSpecificOutput"]["additionalContext"] = message
        return output
    payload: dict[str, Any] = {"permission": "allow"}
    if message:
        payload["agent_message"] = message
    return payload


def _deny(is_claude: bool, message: str) -> dict[str, Any]:
    if is_claude:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "additionalContext": message,
            },
            "systemMessage": message,
        }
    return {"permission": "deny", "agent_message": message}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def event_preuse(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    session_id = session_id_of(payload)
    tool_input = _tool_input(payload)
    subagent_type = tool_input.get("subagent_type", "")
    model = tool_input.get("model", None)

    # GATE-BUG-006 Ruling: hard-block on integrity failure, not warn-and-continue --
    # a tampered dispatch_count can silently disable max_round_trips, which is a
    # cost-limit bypass, not a cosmetic issue.
    if not check_stats_integrity():
        try:
            append_event({
                "type": "stats_integrity_violation",
                "session_id": session_id,
                "file": str(STATS_FILE),
            })
        except OSError:
            pass
        return _deny(
            is_claude,
            f"[cost-router] {STATS_FILE.name} failed integrity validation — its stored "
            "checksum does not match its contents, indicating an out-of-band edit (e.g. "
            "a hand-reset dispatch_count). Agent/Task dispatch is blocked to prevent a "
            "silent cost-limit bypass. Edit and Write operations are unaffected. To restore "
            "integrity: revert the file to a known-good state, or remove the _checksum field "
            "via a direct editor.",
        )

    sessions, this_session = _load_session_stats(session_id)
    # TICKET-01: stamp activity unconditionally, before any return path, so
    # eviction reflects the true last-touched time even on deny branches.
    this_session["last_activity"] = now_iso()
    # RT-01: unconditional attempt counter — increments on every dispatch attempt
    # including denied ones, unlike dispatch_count which is weighted and gated.
    this_session["dispatch_attempts_count"] = this_session.get("dispatch_attempts_count", 0) + 1

    messages: list[str] = []
    warned = False

    _is_resume = bool(tool_input.get("resume"))
    _is_task = _tool_name(payload) in _TASK_TOOLS
    _tname = _tool_name(payload)
    _prompt = _task_prompt(tool_input)
    _rtext = _routing_text(payload)

    # DISPATCH-002: cold-start dispatch with no declared type and no file scope,
    # while a warm idle worker already exists this session -- likely could ride
    # along via resume instead of a fresh spawn. Gated tightly to avoid false
    # positives on genuinely independent work: only fires when this dispatch names
    # no subagent_type/file scope AND at least one idle warm worker is available.
    # TICKET-048: If tool_name is absent, emit check_skipped so dispatch-efficiency
    # metrics don't miscount missing checks as clean dispatches.
    if _tname is None:
        try:
            append_event({
                "type": "check_skipped",
                "reason": "tool_name_missing",
                "check_type": "DISPATCH-002",
            })
        except OSError:
            pass
    elif (
        _tname in _TASK_TOOLS
        and not _is_resume
        and str(subagent_type).strip().lower() in ("", "general-purpose")
    ):
        if not extract_paths_from_prompt(_prompt):
            _warm_workers = get_active_file_worker_map(session_id)
            if _warm_workers:
                _warm_ids = sorted(set(_warm_workers.values()))
                messages.append(
                    "[cost-router] No worker type or file scope declared, and a warm idle "
                    f"worker is already available this session ({', '.join(_warm_ids)}). "
                    "If this check needs no specific declared worker type, fold it into that "
                    "worker via Task(resume=<id>) as one more scripted step instead of a new spawn."
                )
                try:
                    append_event({
                        "type": "ride_along_nudge",
                        "session_id": session_id,
                        "warm_worker_ids": _warm_ids,
                    })
                except OSError:
                    pass

    # CF-01/02: Project per-dispatch cost for warnings; persist only on allow.
    dispatch_cost = 0.0
    _pending_cost_warning_pcts: list[int] = []
    _session_model = payload.get("model") or None
    if _is_task:
        dispatch_model = tool_input.get("model", "")
        dispatch_cost, _pending_cost_warning_pcts, cost_messages = check_cost_warnings(
            session_id, _session_model, this_session, dispatch_model
        )
        messages.extend(cost_messages)

    if subagent_type.lower() == "explore" and (
        model is None or "haiku" not in str(model).lower()
    ):
        messages.append(EXPLORE_HAIKU_WARNING)
        warned = True

    # ER-01: Track explore agent file coverage per session
    # ER-02: Nudge when new Explore overlaps with previous Explore's files
    if subagent_type.lower() == "explore":
        new_files = set(extract_paths_from_prompt(_prompt))
        explore_files_covered = this_session.get("explore_files_covered", {})

        # ER-02: Check overlap with all previously covered files
        all_covered: set[str] = set()
        for covered_list in explore_files_covered.values():
            if isinstance(covered_list, list):
                all_covered.update(covered_list)
        if all_covered and new_files:
            overlap = new_files & all_covered
            overlap_count = len(overlap)
            total_count = len(new_files)
            if overlap_count / max(total_count, 1) > 0.5:
                nudge_msg = (
                    f"[cost-router] This Explore covers {overlap_count}/{total_count} files "
                    "already read by a previous Explore agent. Consider resuming the previous "
                    "agent (~$0.38 saved per avoided cold-start)."
                )
                messages.append(nudge_msg)
                try:
                    append_event({
                        "type": "explore_overlap_nudge",
                        "session_id": session_id,
                        "overlap_count": overlap_count,
                        "new_count": total_count,
                        "overlap_pct": round(overlap_count / max(total_count, 1) * 100, 1),
                    })
                except OSError:
                    pass

        # ER-01: Store extracted files under a counter key
        explore_key = f"explore-{len(explore_files_covered) + 1}"
        explore_files_covered[explore_key] = list(new_files)
        this_session["explore_files_covered"] = explore_files_covered

    budget_msg, should_deny, _was_overridden = _budget_enforcement_message(
        payload, tool_input, is_claude, _tname, _prompt, _rtext
    )
    if budget_msg:
        messages.append(budget_msg)
    if should_deny:
        this_session["agents"].append(
            {"type": subagent_type, "model": model, "ts": now_iso(), "warned": warned, "denied": True}
        )
        this_session["total_agents"] = len(this_session["agents"])
        sessions[session_id] = this_session
        _save_stats(sessions)
        return _deny(is_claude, "\n\n".join(messages))

    if is_claude and _tname in _TASK_TOOLS:
        grouping_denied, grouping_messages = check_grouping(
            payload, tool_input, session_id, subagent_type, model,
            _prompt, _rtext, this_session,
        )
        if grouping_denied:
            messages.extend(grouping_messages)
            sessions[session_id] = this_session
            _save_stats(sessions)
            return _deny(is_claude, "\n\n".join(messages))

    _resume_id = tool_input.get("resume")
    _agent_entry: dict[str, Any] = {"type": subagent_type, "model": model, "ts": now_iso(), "warned": warned}
    if _resume_id:
        _agent_entry["resume"] = _resume_id

        # DISPATCH-002: nudge when this is the first-ever resume of this worker
        # and it lands soon after the worker went idle -- likely an unforeseen
        # "one more thing" follow-up that should have been in the original prompt.
        if _first_resume_of(this_session.get("agents", []), _resume_id):
            _elapsed = _seconds_since_worker_finished(session_id, _resume_id)
            if _elapsed is not None and _elapsed <= _FRONTLOAD_NUDGE_WINDOW_SECONDS:
                messages.append(
                    "[cost-router] Resuming this worker "
                    f"{int(_elapsed)}s after it went idle, on its first resume. If this "
                    "follow-up instruction was foreseeable, front-load it into the ORIGINAL "
                    "dispatch prompt next time instead of a separate resume round-trip."
                )
                try:
                    append_event({
                        "type": "frontload_resume_nudge",
                        "session_id": session_id,
                        "resume_id": _resume_id,
                        "elapsed_seconds": _elapsed,
                    })
                except OSError:
                    pass

        try:
            append_event({
                "type": "resume_used",
                "session_id": session_id,
                "resume_id": _resume_id,
                "model": model,
            })
        except OSError:
            pass
        # Clear pending grouping-deny resume hint — the agent acted on the suggestion
        this_session.pop("last_grouping_deny_resume", None)

    if _is_task and dispatch_cost:
        commit_cost_warnings(
            session_id, _session_model, this_session, dispatch_cost, _pending_cost_warning_pcts,
            is_claude=is_claude,
        )

    this_session["agents"].append(_agent_entry)
    this_session["total_agents"] = len(this_session["agents"])
    this_session["explore_without_haiku"] = sum(
        1 for a in this_session["agents"] if a.get("warned")
    )
    sessions[session_id] = this_session
    _save_stats(sessions)

    message = "\n".join(messages) if messages else None
    return _allow(is_claude, message)


def main() -> int:
    try:
        payload = read_stdin_json()
        is_claude = _is_claude_payload(payload)

        # TICKET-014: hooks.json (Claude Code) registers this script under
        # SubagentStop with "--event subagent_stop" -- previously this CLI arg
        # was never parsed, so main() always ran event_preuse (a PreToolUse-
        # shaped Agent/Task allow/deny handler) against a Stop payload that has
        # no tool_input, and finalize_dispatch_charge (imported above, RT-01-B)
        # was never called. hooks-cursor.json's Cursor registration passes no
        # --event flag and fires at preToolUse, so it still hits event_preuse.
        _event = None
        if "--event" in sys.argv:
            _idx = sys.argv.index("--event")
            if _idx + 1 < len(sys.argv):
                _event = sys.argv[_idx + 1]

        if _event == "subagent_stop":
            finalize_dispatch_charge(payload, is_claude)
            print(json.dumps({"continue": True, "permission": "allow"}))
            return 0

        output = event_preuse(payload, is_claude)
        print(json.dumps(output))
    except Exception:  # noqa: BLE001 — hook must fail open
        print(json.dumps({"continue": True, "permission": "allow"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
