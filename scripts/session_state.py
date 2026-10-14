#!/usr/bin/env python3
"""Session state I/O — load, save, bump counters, get/init session entries.

Extracted from check_routing.py. All callers in check_routing.py use
re-export shims; this module can also be imported directly.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    ACTIVE_SUBAGENT_STALE_TTL_SECONDS,
    DATA_DIR,
    MAX_TRACKED_SESSIONS,
    load_json_state,
    save_json_state,
)

ROUTING_STATE_FILE = DATA_DIR / "routing_state.json"
GATE_COUNTERS_FILE = DATA_DIR / "gate_counters.json"
SUBAGENT_STATE_FILE = DATA_DIR / "subagent_state.json"
RESUME_STATE_FILE = DATA_DIR / "resume_state.json"


def _default_session_entry() -> dict[str, Any]:
    return {
        "verdict": False,
        "verdict_turn": -1,
        "verdict_turn_id": None,
        "turn_id": None,
        "fast_path_exempt": False,
        "turn": 0,
        "mechanical_since_dispatch": 0,
        "inline_since_dispatch": 0,
        "tier": "unknown",
        "tier_history": [],
        "churn_nudged": False,
        "last_assistant_snippet": "",
        "no_transcript_nudged_turn": None,
        "task_dispatched_turn_id": None,
        "fast_path_calls": 0,
        "prompt_files": [],
        "scan_seen_turn_id": None,
        "bulk_shell_choice": None,
        "bulk_shell_pending_hash": None,
        "total_gate_calls_session": 0,
        "task_dispatched_count_session": 0,
        "inline_mutations_since_dispatch": 0,
        "shell_calls_since_dispatch": 0,
        "bulk_write_file_types": {},
        "bulk_write_last_role": None,
        "orchestrator_turns": 0,
        # Session-scoped counter for escalating repeated judgment-on-fast
        # nudges to a hard block. Distinct from the global observability
        # counter "dispatch_nudge_judgment_on_fast" in state["counters"],
        # which must keep tracking every occurrence for threshold tuning.
        "judgment_on_fast_nudge_count_session": 0,
        # TICKET-042: TTL timestamp for evicting stale verdicts; Unix epoch seconds
        "verdict_created_at": None,
        # Correlation queue: worker/intent parsed at PreToolUse(Task/Agent) time,
        # consumed at SubagentStart time (that payload never carries the JSON).
        "pending_dispatch_worker_intent": [],
    }


def _load_state() -> dict[str, Any]:
    s = load_json_state(ROUTING_STATE_FILE)
    c = load_json_state(GATE_COUNTERS_FILE)
    a = load_json_state(SUBAGENT_STATE_FILE)
    return {
        "sessions": s.get("sessions", {}) if isinstance(s, dict) else {},
        "counters": c.get("counters", {}) if isinstance(c, dict) else {},
        "subagents": a.get("subagents", {}) if isinstance(a, dict) else {},
        "subagent_progress": a.get("subagent_progress", {}) if isinstance(a, dict) else {},
    }


def _bump(state: dict[str, Any], key: str) -> None:
    """Increment a fixed-key observability counter. Counters are a small,
    fixed set of integers, so they can never grow the state file unbounded.

    PROD-7 (decided): concurrent gate/hook processes can lose an update here
    (read-modify-write on routing_state.json, no lock) when a single response
    emits multiple tool_use blocks. Accepted, not fixed: these counters only
    drive soft nudge/deny thresholds, not the routing verdict itself (that's
    re-derived from the transcript each time and is idempotent). A lost
    increment means a nudge fires one call later than intended, never a wrong
    allow/deny. Not worth an flock around this path given the 5s hook timeout
    budget; revisit only if drift is observed to actually skip a threshold
    entirely rather than just delaying it by one call.
    """
    counters = state.setdefault("counters", {})
    counters[key] = int(counters.get(key, 0)) + 1


def _save_state(state: dict[str, Any]) -> None:
    sessions = state.get("sessions", {})
    if len(sessions) > MAX_TRACKED_SESSIONS:
        ordered = sorted(
            sessions.items(),
            key=lambda item: (item[1].get("turn", 0), item[0]),
        )
        sessions = dict(ordered[-MAX_TRACKED_SESSIONS:])
        state["sessions"] = sessions

    # Expire stale / drained sub-agent markers so the bucket stays tiny and a
    # missed stop can't linger.
    subagents = state.get("subagents", {})
    if subagents:
        now = time.time()
        state["subagents"] = {
            key: value
            for key, value in subagents.items()
            if isinstance(value, dict)
            and int(value.get("count", 0)) > 0
            and now - float(value.get("ts", 0)) <= ACTIVE_SUBAGENT_STALE_TTL_SECONDS
        }

    save_json_state(ROUTING_STATE_FILE, {"sessions": state.get("sessions", {})})
    save_json_state(GATE_COUNTERS_FILE, {"counters": state.get("counters", {})})
    save_json_state(SUBAGENT_STATE_FILE, {
        "subagents": state.get("subagents", {}),
        "subagent_progress": state.get("subagent_progress", {}),
    })


def _get_session(sessions: dict[str, Any], session_id: str) -> dict[str, Any]:
    entry = sessions.get(session_id)
    if not isinstance(entry, dict):
        entry = _default_session_entry()
        sessions[session_id] = entry
    for key, default in _default_session_entry().items():
        entry.setdefault(key, default if not isinstance(default, list) else [])
    # migrate old key name
    if "mutating_since_task" in entry and "mechanical_since_dispatch" not in entry:
        entry["mechanical_since_dispatch"] = entry.pop("mutating_since_task")
    entry.pop("mutating_since_task", None)
    return entry
