#!/usr/bin/env python3
"""cost-router hook: per-intent revert-rate quality signal (OBS-T2-001).

Runs at Stop. Reads quality_failure reward_signal events from usage.jsonl,
groups them by intent over a rolling window of sessions, and writes a
per-intent revert_rate summary to data/quality_signal.json.

Config key: quality_signal.window (default: 30)
"""
# Required for Python <3.10 compatibility: defers annotation evaluation so
# X | Y union syntax doesn't raise TypeError at parse time on older interpreters.
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (
    DATA_DIR,
    _warn,
    config_int,
    now_iso,
    read_events,
    read_stdin_json,
    save_json_state,
    session_id_of,
)

DEFAULT_WINDOW = 30


def _load_window() -> int:
    return config_int("quality_signal.window", DEFAULT_WINDOW)


def compute_quality_signal(events: list[dict] | None = None) -> dict:
    """Compute per-intent revert rates over a rolling session window.

    Args:
        events: pre-loaded event list (for testing); if None, reads from disk.

    Returns:
        dict with keys:
          - updated_at: ISO timestamp
          - window: rolling window size used
          - intents: list of {intent, sessions, reverted, revert_rate}
    """
    window = _load_window()
    if events is None:
        events = read_events()

    # Step 1: collect all sessions that had at least one edit (tool events)
    # and note which sessions had a quality_failure revert signal.
    _EDIT_TOOLS = frozenset({"Write", "Edit", "StrReplace", "MultiEdit"})

    sessions_with_edits: dict[str, str] = {}   # session_id -> intent (may be "")
    sessions_with_revert: set[str] = set()
    # Tool events carry no intent; the classifier's `intent` events do (latest wins).
    session_intent: dict[str, str] = {}

    for e in events:
        sid = e.get("session_id", "")
        if not sid:
            continue

        etype = e.get("type", "")
        ts = e.get("ts", "")

        if etype == "intent" and e.get("intent"):
            session_intent[sid] = e["intent"]

        # Track sessions that have edit activity to build the denominator
        if etype == "tool" and e.get("tool_name") in _EDIT_TOOLS:
            if sid not in sessions_with_edits:
                sessions_with_edits[sid] = e.get("intent", "")
            # Update intent if we find a better one later
            if not sessions_with_edits[sid] and e.get("intent"):
                sessions_with_edits[sid] = e["intent"]

        # Record quality_failure events with their intent
        if etype == "reward_signal" and e.get("signal") == "quality_failure":
            sessions_with_revert.add(sid)
            # If this event has an intent and the session doesn't yet, capture it
            if sid not in sessions_with_edits:
                sessions_with_edits[sid] = e.get("intent", "")
            if not sessions_with_edits.get(sid) and e.get("intent"):
                sessions_with_edits[sid] = e["intent"]

    # Step 2: Build per-session records with (session_id, intent, reverted, ts)
    # Use the latest timestamp per session as the sort key for windowing.
    session_ts: dict[str, str] = {}
    for e in events:
        sid = e.get("session_id", "")
        ts = e.get("ts", "")
        if sid and ts:
            if ts > session_ts.get(sid, ""):
                session_ts[sid] = ts

    # Step 3: Restrict to sessions that had edits, sort by latest ts, window
    candidate_sessions = sorted(
        sessions_with_edits.keys(),
        key=lambda s: session_ts.get(s, ""),
    )
    windowed = candidate_sessions[-window:]

    # Step 4: Group by intent over windowed sessions
    intent_sessions: dict[str, list[str]] = defaultdict(list)
    for sid in windowed:
        intent = sessions_with_edits.get(sid) or session_intent.get(sid) or "unknown"
        intent_sessions[intent].append(sid)

    # Step 5: Compute revert_rate per intent
    intents_output = []
    for intent, sids in sorted(intent_sessions.items()):
        total = len(sids)
        reverted = sum(1 for s in sids if s in sessions_with_revert)
        revert_rate = round(reverted / total, 3) if total > 0 else 0.0
        intents_output.append(
            {
                "intent": intent,
                "sessions": total,
                "reverted": reverted,
                "revert_rate": revert_rate,
            }
        )

    return {
        "updated_at": now_iso(),
        "window": window,
        "intents": intents_output,
    }


def main() -> int:
    """Hook entry point. Computes quality signal and writes data/quality_signal.json."""
    try:
        payload = read_stdin_json()
        result = compute_quality_signal()
        save_json_state(DATA_DIR / "quality_signal.json", result)
    except Exception as e:
        _warn("quality_signal_failed", {"error": str(e)})

    # Stop hook: emit empty object (valid Stop hook no-op)
    print(json.dumps({}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
