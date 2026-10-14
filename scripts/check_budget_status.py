#!/usr/bin/env python3
"""TICKET-02: read-only round-trip budget query.

Prints the current dispatch_count / ceiling / remaining / cumulative cost for
a session without mutating any state. Intended to be run directly:

    python3 scripts/check_budget_status.py --session-id <id>

If --session-id is omitted, reports on the most recently active session
(by last_activity) in agent_call_stats.json.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR, load_json_state, load_plugin_config  # noqa: E402
from agent_call_tracker import _session_last_activity_ts  # noqa: E402
from check_routing import get_routing_for_intent  # noqa: E402
from session_state import ROUTING_STATE_FILE  # noqa: E402

STATS_FILE = DATA_DIR / "agent_call_stats.json"


def _most_recent_session_id(sessions: dict[str, Any]) -> str | None:
    if not sessions:
        return None
    return max(sessions.items(), key=lambda kv: _session_last_activity_ts(kv[1]))[0]


def _work_unit_intent(session_id: str) -> str:
    state = load_json_state(ROUTING_STATE_FILE)
    entry = state.get("sessions", {}).get(session_id) if isinstance(state, dict) else None
    if isinstance(entry, dict):
        return entry.get("work_unit_intent") or "_default"
    return "_default"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", default=None, help="session to report on (default: most recently active)")
    args = parser.parse_args()

    stats = load_json_state(STATS_FILE)
    sessions = stats.get("sessions", {}) if isinstance(stats, dict) else {}
    if not isinstance(sessions, dict) or not sessions:
        print("No round-trip budget data found (agent_call_stats.json empty or missing).")
        return 0

    session_id = args.session_id or _most_recent_session_id(sessions)
    session_data = sessions.get(session_id)
    if not isinstance(session_data, dict):
        print(f"No budget data for session_id={session_id!r}.")
        return 0

    intent = _work_unit_intent(session_id)
    routing = get_routing_for_intent(intent)
    max_round_trips = int(routing.get("max_round_trips", 0))

    dispatch_count = session_data.get("dispatch_count", 0)
    remaining = max_round_trips - dispatch_count if max_round_trips else None
    cumulative_cost = session_data.get("cumulative_cost_usd", 0.0)

    cfg = load_plugin_config()
    dispatch_attempts_abort_ceiling = int(
        cfg.get("routing", {}).get("dispatch_attempts_abort_ceiling", 100)
    )
    dispatch_attempts_count = session_data.get("dispatch_attempts_count", 0)

    print(f"session_id: {session_id}")
    print(f"work_unit_intent: {intent}")
    print(f"dispatch_count: {dispatch_count} / max_round_trips: {max_round_trips}")
    if remaining is not None:
        print(f"remaining: {remaining}")
    print(f"dispatch_attempts_count: {dispatch_attempts_count} / dispatch_attempts_abort_ceiling: {dispatch_attempts_abort_ceiling}")
    budget_usd = cfg.get("routing", {}).get("budget_usd")
    print(f"cumulative_cost_usd: ${float(cumulative_cost):.4f}" + (f" / budget_usd: ${float(budget_usd):.2f}" if budget_usd is not None else ""))
    print(f"last_activity: {session_data.get('last_activity', 'unknown')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
