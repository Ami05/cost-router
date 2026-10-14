#!/usr/bin/env python3
"""cost-router feedback loop: scan usage.jsonl for misroute signals and suggest
Decision Table updates.

Read-only itself — this script never edits rules/model-router.md directly.
That used to also be true of the whole tune-routing workflow ("proposes
wording and applies edits only after the user approves"), but is no longer
true of the pipeline as a whole: policy_manager.py (invoked from
compact_log.py's Stop hook) and auto_apply.py both apply routing changes
without waiting for approval once their own evidence/confidence thresholds
are met. See skills/tune-routing/SKILL.md and the root README's "Adaptive
routing" section for the current, accurate description of what's automatic
and what isn't. This module's own suggestions (render_suggestions() below)
are still purely advisory.

This module is a thin entry point that wires together:
  - routing_detectors.py  — pure analysis functions returning structured data
  - routing_renderer.py   — formatting functions producing human-readable output

Usage:
    python3 analyze_routing.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Re-export the public API callers expect from this module so existing imports
# (e.g. `from analyze_routing import find_over_routes`) continue to work.
from routing_detectors import (  # noqa: F401, E402
    GATE_COUNTERS_FILE,
    ROUTING_STATE_FILE,
    _GATE_TERMINAL_COUNTERS,
    _MECHANICAL_TOOLS,
    _group_by_session,
    _nudge_cooldown_hours,
    _session_count,
    active_judgment_on_fast_nudge_sessions,
    dispatch_context_quality_stats,
    dispatch_tier_totals,
    find_escalations,
    find_over_routes,
    find_repeat_reads,
    find_undispatched_sessions,
    gate_health_stats,
    session_tier_transitions,
)
from routing_renderer import (  # noqa: F401, E402
    check_nudge,
    dispatch_tier_one_liner,
    gate_health_one_liner,
    render_dispatch_context_quality,
    render_gate_health,
    render_suggestions,
)
from _config import TUNE_STATE_FILE  # noqa: F401


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hint-only",
        action="store_true",
        help="Print a one-line nudge only if defined thresholds are met (used by cost-report); "
        "prints nothing and exits 0 otherwise.",
    )
    parser.add_argument(
        "--gate-health-only",
        action="store_true",
        help="Print only the one-line gate-health summary (PROD-4; used by cost-report) and exit.",
    )
    parser.add_argument(
        "--dispatch-tier-only",
        action="store_true",
        help="Print only the one-line dispatched-Task tier-mix summary (PROD-19; used by "
        "cost-report) and exit.",
    )
    args = parser.parse_args()

    if args.hint_only:
        triggered, message = check_nudge()
        if triggered:
            print(message)
        return 0

    if args.gate_health_only:
        print(gate_health_one_liner())
        return 0

    if args.dispatch_tier_only:
        print(dispatch_tier_one_liner())
        return 0

    print(render_suggestions())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
