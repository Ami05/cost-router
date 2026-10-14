"""confidence_algorithm — pure scoring and signal-weighting functions.

Zero I/O, zero serialization. All functions operate on in-memory values only.
Extracted from confidence_score.py so they can be tested and reused without
any filesystem or hook dependencies.

confidence_score.py imports everything it needs from here.
"""
from __future__ import annotations

import os
from typing import Optional

# ── Tier mapping for blast radius classification ──────────────────────────────

_TIER_MAP = {
    "DIRECT": 0,
    "FW": 1,
    "FO": 1,
    "RB": 2,
}


def tier(role: str) -> int:
    """Return numeric tier for a role (lower = faster/cheaper)."""
    return _TIER_MAP.get(role, 0)


# Keep the private alias so confidence_score.py's existing `_tier(role)` calls
# continue to work after it switches to importing from here.
_tier = tier


# ── Blast radius classification ───────────────────────────────────────────────


def classify_blast_radius(
    intent: str, from_role: str, to_role: Optional[str], pinned: bool = False
) -> str:
    """Classify blast radius (low/medium/high) for a routing rule change.

    Args:
        intent:    routing intent category (e.g. "qa_simple", "qa_architecture")
        from_role: current/verdict role (DIRECT, FW, FO, RB)
        to_role:   suggested role from signal analysis (DIRECT, FW, FO, RB, or None)
        pinned:    if True, rule is pinned -> always "high"

    Returns:
        "low", "medium", or "high" based on risk assessment.

    Rules (in order):
      1. pinned=True -> "high" (never auto-apply)
      2. to_role=None -> "high" (no change direction)
      3. from_role=RB and to_role in {FW,DIRECT,FO} -> "high" (reasoning->fast demotion)
      4. tier_shift = abs(tier(to_role) - tier(from_role))
      5. intent in {qa_simple, qa_with_reads} AND tier_shift <= 1 -> "low"
      6. intent in {agentic_action, qa_architecture} OR tier_shift >= 2 -> "medium"
      7. default -> "medium"
    """
    if pinned:
        return "high"
    if to_role is None:
        return "high"
    if from_role == "RB" and to_role in {"FW", "DIRECT", "FO"}:
        return "high"

    tier_shift = abs(tier(to_role) - tier(from_role))
    if intent in {"qa_simple", "qa_with_reads"} and tier_shift <= 1:
        return "low"
    if intent in {"agentic_action", "qa_architecture"} or tier_shift >= 2:
        return "medium"
    return "medium"


# ── Environment variable helpers ──────────────────────────────────────────────


def get_int_env(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to *default*."""
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def get_float_env(name: str, default: float) -> float:
    """Read a float environment variable, falling back to *default*."""
    try:
        return float(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


# Private aliases for backward-compat with confidence_score.py's `_get_*_env` names.
_get_int_env = get_int_env
_get_float_env = get_float_env
