"""session_impact.py — Stop hook consumer for automatic session-end impact summary.

Called last in the Stop hook chain, after reward_signal.py and compact_log.py.
Prints a formatted cost-savings box to stderr. Never raises — all errors are swallowed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path

# Project root relative to this file
_ROOT = Path(__file__).parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR, USAGE_LOG, WARNINGS_LOG, _warn  # noqa: E402
from impact_calculator import _load_config_impact, _load_window_entries, compute_session  # noqa: E402


# ---------------------------------------------------------------------------
# IMP-201: Pure string formatters
# ---------------------------------------------------------------------------

def format_session_box(report) -> str:
    """Build the bordered session-end summary box string."""
    total_gate_count = sum(v[0] for v in report.gate_breakdown.values())

    lines = [
        f"| Session: ${report.total_spent:.2f} est. spent  .  ${report.total_est_saved:.2f} est. saved",
        f"| +-- Routing:   ${report.routing_savings:.2f} saved ({report.routing_rate:.0%} of output on fast)",
        f"| +-- Gates:     ${report.waste_prevented:.2f} waste prevented ({total_gate_count} denials)",
    ]

    if report.prev_week_routing_rate is not None:
        delta = report.prev_week_routing_rate
        arrow = "^" if delta >= 0 else "v"
        lines.append(f"| Trend: {arrow} {abs(delta):.0%} more fast-routed vs. last week")

    # Calculate box width from the longest line
    inner_width = max(len(ln) for ln in lines)
    padding = max(inner_width - len("cost-router ") - 2, 4)
    border_top = "+-- cost-router " + "-" * padding + "+"
    border_bot = "+" + "-" * (len(border_top) - 2) + "+"

    parts = [border_top] + lines + [border_bot]
    return "\n".join(parts)


def format_cold_start_message(sessions_remaining: int) -> str:
    """Return the cold-start learning message."""
    return (
        f"cost-router is learning your patterns -- "
        f"impact summary starts after {sessions_remaining} more session(s)"
    )


# ---------------------------------------------------------------------------
# IMP-202: Suppression guards
# ---------------------------------------------------------------------------

def _count_historical_sessions() -> int:
    """Count unique session_ids in session_log.jsonl."""
    path = DATA_DIR / 'session_log.jsonl'
    try:
        seen = set()
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                sid = entry.get('session_id', '')
                if sid:
                    seen.add(sid)
            except Exception:
                continue
        return len(seen)
    except Exception:
        return 0


def should_show_summary(report, config: dict, total_historical_sessions: int) -> str:
    """Decide what to display.

    Returns one of: 'show_box' | 'show_cold_start' | 'suppress'
    Order: turn check first, cold start second, savings third.
    """
    min_turns = config.get('min_turns', 1)
    min_savings = config.get('min_savings_usd', 0.01)
    cold_start = config.get('cold_start_sessions', 0)

    if report.total_turns < min_turns:
        return 'suppress'
    if total_historical_sessions < cold_start:
        return 'show_cold_start'
    if report.total_est_saved < min_savings:
        return 'suppress'
    return 'show_box'


# ---------------------------------------------------------------------------
# IMP-203: Sunday auto-trigger + main()
# ---------------------------------------------------------------------------

def _is_sunday_auto_trigger(config: dict) -> bool:
    """Return True if we should auto-generate weekly reports today."""
    if not config.get('auto_weekly', True):
        return False
    if date.today().weekday() != 6:  # 6 = Sunday
        return False
    # Check if card was already generated this week
    card_path = DATA_DIR / 'impact-card.html'
    if card_path.exists():
        mtime = datetime.fromtimestamp(card_path.stat().st_mtime)
        days_ago = (datetime.now() - mtime).days
        if days_ago < 7:
            return False
    # Count sessions this week
    today = date.today()
    week_start = (today - __import__('datetime').timedelta(days=6)).isoformat()
    week_end = today.isoformat()
    try:
        entries = _load_window_entries(week_start, week_end)
        unique = set(e.get('session_id', '') for e in entries)
        if len(unique) < 5:
            return False
    except Exception:
        return False
    return True



# ---------------------------------------------------------------------------
# Warning threshold alert
# ---------------------------------------------------------------------------

_WARN_THRESHOLD = 5
_SENTINEL = DATA_DIR / ".warnings_alerted"


def _check_warning_threshold() -> None:
    """Emit a one-line stderr notice when recent silent-failure count crosses the threshold.

    Counts only warnings from the last 24 hours so historical entries do not
    permanently trigger the banner after root causes are fixed.
    Never raises -- the check itself must not crash the hook chain.
    """
    import datetime as _dt
    try:
        if not WARNINGS_LOG.exists():
            return
        cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24)).isoformat()
        count = 0
        for ln in WARNINGS_LOG.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                ts = json.loads(ln).get("ts", "")
                if ts >= cutoff:
                    count += 1
            except Exception:
                pass
        if count < _WARN_THRESHOLD:
            return
        if _SENTINEL.exists():
            return
        print(
            f"[cost-router] ⚠ {count} silent failures in the last 24h"
            " — run /cost-report for details",
            file=sys.stderr,
        )
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _SENTINEL.touch()
    except Exception:  # noqa: BLE001
        pass

def main() -> None:
    """Entry point called by Stop hook. Never raises — all errors swallowed."""
    config: dict = {}
    try:
        config = _load_config_impact()
        if not config.get('session_summary', True):
            return
        if not USAGE_LOG.exists():
            _warn("session_impact_no_data", {"reason": "usage.jsonl missing"})
            return

        # Get session_id from env or last log entry
        session_id = os.environ.get('COST_ROUTER_SESSION_ID', '')
        if not session_id:
            path = DATA_DIR / 'session_log.jsonl'
            try:
                lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
                if lines:
                    last = json.loads(lines[-1])
                    session_id = last.get('session_id', '')
            except Exception:
                pass

        if not session_id:
            return

        report = compute_session(session_id)
        total = _count_historical_sessions()
        action = should_show_summary(report, config, total)

        if action == 'show_box':
            print(format_session_box(report), file=sys.stderr)
        elif action == 'show_cold_start':
            remaining = max(0, config.get('cold_start_sessions', 3) - total)
            print(format_cold_start_message(remaining), file=sys.stderr)
        # 'suppress' -> do nothing

        if _is_sunday_auto_trigger(config):
            project_root = str(_ROOT)
            result = subprocess.run(
                [sys.executable, 'scripts/generate_reports.py', '--both'],
                cwd=project_root,
                capture_output=True,
            )
            if result.returncode != 0:
                _warn("report_generation_failed", {"file": "session_impact.py", "returncode": result.returncode})
    except Exception as exc:
        print(f"cost-router impact: error (skipped): {exc}", file=sys.stderr)
    finally:
        if config.get('session_summary', True):
            _check_warning_threshold()
        # Stop hook: emit empty object (valid Stop hook no-op; "continue" is not a Stop hook field)
        print(json.dumps({}))
    sys.exit(0)


if __name__ == '__main__':
    main()
