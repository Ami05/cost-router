#!/usr/bin/env python3
"""cost-router: Pure validation helpers for routing proposals.

No disk I/O. Returns structured results only.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Proposal derivation helpers
# ---------------------------------------------------------------------------


def _derive_proposed_row(key: str) -> str:
    """Derive a concise routing rule row from a scenario key (tool_type:tier:outcome).

    Examples:
      "Write:fast:escalated" -> "Write(spec_complete) -> FW"
      "Read:fast:repeat"     -> "Read(repeat) -> same; reread"
      "Bash:reasoning:clean" -> "Bash(clean) -> FW"
    """
    parts = key.split(":")
    if len(parts) != 3:
        return f"# proposal: {key}"
    tool_type, tier, outcome = parts

    tool_cap = tool_type.capitalize()
    if outcome == "escalated":
        # Sessions that escalated from fast->reasoning suggest the task was
        # more complex than initially routed; propose a fast-worker default
        # with a spec_complete guard rather than immediate escalation.
        return f"{tool_cap}(spec_complete) -> FW"
    elif outcome == "repeat":
        # Repeated target reads suggest we should reuse the same model context.
        return f"{tool_cap}(repeat) -> same; reread"
    else:
        # Clean sessions confirm current fast-worker routing is appropriate.
        return f"{tool_cap}(clean) -> FW"


def _detect_blast_radius(key: str, proposed_row: str) -> str:
    """Return 'high' for multi-row, reversal, or Task routing; else 'low'.

    'high' conditions:
    - The tool_type is 'task' or 'dispatch'
    - The proposed row routes to a Task orchestrator (FO) or wraps Task
    - The tier is 'reasoning' but proposed row routes to FW (reversal)
    - The proposed row contains multiple routing instructions (multiple ';')
    """
    parts = key.split(":")
    if len(parts) == 3:
        tool_type, tier, outcome = parts
        if tool_type.lower() in ("task", "dispatch"):
            return "high"
        if "Task" in proposed_row or "FO" in proposed_row:
            return "high"
        # Reversal: was routing to reasoning, now proposing fast
        if tier == "reasoning" and "FW" in proposed_row:
            return "high"
    # Multiple routing instructions indicate broad impact
    if "\n" in proposed_row:
        return "high"
    if proposed_row.count(";") > 2:
        return "high"
    return "low"


def derive_proposal_defaults(key: str) -> dict[str, Any]:
    """Return the default field values for a new proposal from *key*.

    Pure function — no disk access.
    """
    proposed_row = _derive_proposed_row(key)
    return {
        "key": key,
        "proposed_row": proposed_row,
        "evidence_count": 0,
        "shadow_delta_reward": 0.0,
        "shadow_validated": False,
        "blast_radius": _detect_blast_radius(key, proposed_row),
        "status": "accumulating",
        "undo_token": None,
    }
