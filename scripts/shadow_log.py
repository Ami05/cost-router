#!/usr/bin/env python3
"""PA-2: Shadow mode — log candidate routes alongside actual routes.

Runs at Stop. For each routing_verdict event in the session, logs what
the alternative (candidate) route would have been under next-best-role
mapping. Output: data/shadow_routes.jsonl (append-only, never read by
signal_filter).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR, read_events, session_id_of, read_stdin_json, now_iso, _warn, load_json_state, save_json_state

# Candidate role mapping — next-best alternative to each role
_CANDIDATE_MAP = {
    "DIRECT": "FW",    # could have dispatched instead of inline
    "FW": "DIRECT",    # could have run inline instead of dispatching
    "FO": "RB",        # could have used reasoning orchestrator
    "RB": "FO",        # could have used fast orchestrator
    "FV": "RB",        # could have used reasoning for review
}


def log_shadow_routes(verdicts: list[dict], session_id: str) -> int:
    """Log candidate routes for routing verdicts.

    For each verdict where verdict["role"] is in _CANDIDATE_MAP:
      - Build entry with actual_role, candidate_role, and other metadata
      - Append to DATA_DIR / "shadow_routes.jsonl" (one JSON line per entry)

    Args:
        verdicts: List of routing_declared events from this session
        session_id: Session identifier

    Returns:
        Count of entries written
    """
    count = 0
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        shadow_file = DATA_DIR / "shadow_routes.jsonl"

        # Create file if missing
        if not shadow_file.exists():
            shadow_file.touch()

        # Write entries for verdicts with known next-best alternatives
        with shadow_file.open("a", encoding="utf-8") as f:
            for verdict in verdicts:
                actual_role = verdict.get("role", "")
                if actual_role not in _CANDIDATE_MAP:
                    continue

                entry = {
                    "session_id": session_id,
                    "timestamp": now_iso(),
                    "rule_id": verdict.get("rule_id") or verdict.get("role", "unknown"),
                    "actual_role": actual_role,
                    "actual_model": verdict.get("model", "unknown"),
                    "candidate_role": _CANDIDATE_MAP[actual_role],
                    "candidate_model": "",  # left empty — B-deep can resolve from role
                    "reason": f"next-best alternative to {actual_role}",
                }
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
                count += 1
    except OSError as e:
        _warn("shadow_routes_write_failed", {"file": "shadow_log.py", "error": str(e)})

    return count


_SHADOW_STATE_FILE = DATA_DIR / "shadow_log_state.json"


def _last_shadow_ts() -> str:
    """Timestamp of the newest shadow_routes.jsonl entry, or "" when none/unreadable."""
    try:
        lines = (DATA_DIR / "shadow_routes.jsonl").read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if line.strip():
                return str(json.loads(line).get("timestamp", ""))
    except (OSError, ValueError):
        pass
    return ""


def main() -> int:
    """Entry point: read verdict events, log shadow routes, emit continue signal.

    Processes ALL routing_declared events not yet shadow-logged (tracked via
    a high-water-mark index in shadow_log_state.json), regardless of session_id.
    The old per-session filter produced 0 entries because Stop-hook payloads
    carry harness session IDs that never matched the IDs check_routing.py wrote.

    Returns:
        0, never raises
    """
    try:
        read_stdin_json()  # consume stdin so hook runner doesn't hang
        events = read_events()

        # Load high-water mark. hwm_ts (newest processed event timestamp) is the
        # authoritative cursor: read_events() concatenates four tail-capped logs, so a
        # positional index drifts past the window and silently skips every new row.
        state = load_json_state(_SHADOW_STATE_FILE)
        hwm = state.get("hwm", 0)
        hwm_ts = state.get("hwm_ts")

        # Migration: enrich_001_reset — skip un-enriched rows on first new run
        if _SHADOW_STATE_FILE.exists() and "enrich_001_reset" not in state:
            # Reset HWM to current event count, so old un-enriched rows are skipped
            hwm = len(events)
            state["enrich_001_reset"] = True
            save_json_state(_SHADOW_STATE_FILE, {**state, "hwm": hwm})

        # Migration: legacy index cursor past the read window (the stuck state) —
        # resume from the newest already-logged shadow route instead of skipping all rows.
        if hwm_ts is None and hwm > len(events):
            hwm_ts = _last_shadow_ts()

        # Collect unprocessed routing_declared events
        new_verdicts: list[tuple[int, dict]] = []
        for i, ev in enumerate(events):
            if hwm_ts is not None:
                if ev.get("ts", "") <= hwm_ts:
                    continue
            elif i < hwm:
                continue
            if ev.get("type") == "routing_declared":
                new_verdicts.append((i, ev))

        # Log shadow routes for each verdict, grouped by session_id
        total = 0
        for _, verdict in new_verdicts:
            sid = verdict.get("session_id", "unknown")
            total += log_shadow_routes([verdict], sid)

        # Update high-water mark to end of current event list
        if events:
            newest = max((ev.get("ts", "") for ev in events), default="")
            save_json_state(
                _SHADOW_STATE_FILE,
                {**state, "hwm": len(events), "hwm_ts": max(newest, hwm_ts or "")},
            )
    except Exception as e:
        # Fail silently — shadow logging is observational only
        _warn("shadow_log_main_failed", {"file": "shadow_log.py", "error": str(e)})

    # Stop hook: emit empty object (valid Stop hook no-op; "continue" is not a Stop hook field)
    print(json.dumps({}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
