#!/usr/bin/env python3
"""cost-router: compute implicit reward signal per session.

Pure stdlib — no external deps.  All functions accept the full event list
and filter to the relevant session / time window internally so callers can
reuse a single read_events() result.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    REWARD_CLEAN_COMPLETION_BONUS,
    REWARD_COST_PENALTY_CAP,
    REWARD_COST_PENALTY_DIVISOR_USD,
    REWARD_ERROR_PENALTY_CAP,
    REWARD_ERROR_PENALTY_MULTIPLIER,
    REWARD_ESCALATION_PENALTY_CAP,
    REWARD_ESCALATION_PENALTY_MULTIPLIER,
    REWARD_OVERRIDE_PENALTY,
    REWARD_OVER_ROUTE_PENALTY_CAP,
    REWARD_OVER_ROUTE_PENALTY_MULTIPLIER,
    REWARD_REVERT_PENALTY_CAP,
    REWARD_REVERT_PENALTY_MULTIPLIER,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _window_events(
    events: list[dict[str, Any]],
    session_id: str,
    window_start: str | None,
) -> list[dict[str, Any]]:
    """Return non-summary events for *session_id* at or after *window_start*.

    When *window_start* is None all session events are included.  Timestamps
    are compared lexicographically; ISO-8601 strings sort correctly without
    parsing.
    """
    result: list[dict[str, Any]] = []
    for e in events:
        if e.get("session_id") != session_id:
            continue
        if e.get("type") == "session_summary":
            continue
        if window_start is not None and (e.get("ts") or "") < window_start:
            continue
        result.append(e)
    return result


# ---------------------------------------------------------------------------
# Detection functions
# ---------------------------------------------------------------------------


def detect_override(
    events: list[dict[str, Any]],
    session_id: str,
    window_start: str | None,
) -> bool:
    """Return True if the model tier flipped fast->reasoning with no Task dispatch in the window.

    A "Task dispatch" is a tool event whose tool_name is exactly "Task" (the
    Claude Code subagent-dispatch tool).  When such a dispatch is absent and
    the tier nonetheless escalates to reasoning, it is treated as a manual
    override rather than a policy-driven escalation.
    """
    window = _window_events(events, session_id, window_start)

    # Scan for at least one fast->reasoning tier flip.
    seen_fast = False
    has_flip = False
    for e in window:
        tier = e.get("tier", "unknown")
        if tier == "fast":
            seen_fast = True
        elif tier == "reasoning" and seen_fast:
            has_flip = True
            break

    if not has_flip:
        return False

    # A proper Task dispatch in the same window would explain the flip.
    has_task_dispatch = any(e.get("tool_name") in ("Task", "Agent") for e in window)
    return not has_task_dispatch


def detect_errors(
    events: list[dict[str, Any]],
    session_id: str,
    window_start: str | None,
) -> int:
    """Return the count of events with error=True in the window."""
    window = _window_events(events, session_id, window_start)
    return sum(1 for e in window if e.get("error") is True)


def detect_reverts(
    events: list[dict[str, Any]],
    session_id: str,
    window_start: str | None,
) -> int:
    """Return the count of unique files that received two or more Write/Edit calls in the window.

    Each distinct target_hash written or edited 2+ times counts as one revert.
    """
    window = _window_events(events, session_id, window_start)
    hashes: Counter[str] = Counter()
    for e in window:
        if e.get("tool_name") in ("Write", "Edit") and e.get("target_hash"):
            hashes[e["target_hash"]] += 1
    return sum(1 for count in hashes.values() if count >= 2)


# ---------------------------------------------------------------------------
# Reward computation
# ---------------------------------------------------------------------------


def compute_reward(
    events: list[dict[str, Any]],
    session_id: str,
    window_start: str | None,
    est_cost: float,
) -> float:
    """Compute a scalar reward in [-1.0, 1.0] for the session window.

    Formula
    -------
    success_score = 1.0
        - 0.5  if override detected
        - min(0.8, 0.2 * escalations)   escalation count in window
        - min(0.3, 0.1 * errors)         error-flagged events
        - min(0.2, 0.1 * reverts)        files written/edited twice
        + 0.2  if ALL of the above are zero (clean completion bonus)

    cost_penalty = min(REWARD_COST_PENALTY_CAP, est_cost / REWARD_COST_PENALTY_DIVISOR_USD)

    return max(-1.0, min(1.0, success_score - cost_penalty))
    """
    window = _window_events(events, session_id, window_start)

    override = detect_override(events, session_id, window_start)
    errors = detect_errors(events, session_id, window_start)
    reverts = detect_reverts(events, session_id, window_start)

    # Escalations: count reasoning-tier events that follow the first fast-tier event.
    escalations = 0
    seen_fast = False
    for e in window:
        if e.get("type") == "subagent_dispatch":
            continue  # sanctioned dispatches are not escalations
        tier = e.get("tier", "unknown")
        if tier == "fast":
            seen_fast = True
        elif tier == "reasoning" and seen_fast:
            escalations += 1

    success_score = 1.0
    if override:
        success_score -= REWARD_OVERRIDE_PENALTY
    success_score -= min(REWARD_ESCALATION_PENALTY_CAP, REWARD_ESCALATION_PENALTY_MULTIPLIER * escalations)
    success_score -= min(REWARD_ERROR_PENALTY_CAP, REWARD_ERROR_PENALTY_MULTIPLIER * errors)
    success_score -= min(REWARD_REVERT_PENALTY_CAP, REWARD_REVERT_PENALTY_MULTIPLIER * reverts)

    # Clean-completion bonus: only when nothing went wrong.
    if not override and escalations == 0 and errors == 0 and reverts == 0:
        success_score += REWARD_CLEAN_COMPLETION_BONUS

    cost_penalty = min(REWARD_COST_PENALTY_CAP, est_cost / REWARD_COST_PENALTY_DIVISOR_USD)

    from routing_detectors import find_over_routes  # noqa: PLC0415
    session_events = [e for e in events if e.get("session_id") == session_id]
    over_counts, _ = find_over_routes(session_events)
    cost_penalty += min(REWARD_OVER_ROUTE_PENALTY_CAP, len(over_counts) * REWARD_OVER_ROUTE_PENALTY_MULTIPLIER)

    return max(-1.0, min(1.0, success_score - cost_penalty))
