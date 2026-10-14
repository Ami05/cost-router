#!/usr/bin/env python3
"""FL-302: Render policy proposals section for /cost-report.

Reads policy proposals from data/policy_proposals.json and renders
a markdown section showing pending proposals.

Pure stdlib — no external dependencies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR


def load_proposals(limit: int = 3) -> list[dict[str, Any]]:
    """Load top policy proposals (already sorted by expected_reward_delta desc).

    Reads data/policy_proposals.json and returns the first *limit* proposals.
    """
    proposals_path = DATA_DIR / "policy_proposals.json"
    try:
        text = proposals_path.read_text(encoding="utf-8")
    except OSError:
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    if not isinstance(data, dict):
        return []

    proposals = data.get("proposals", [])
    if not isinstance(proposals, list):
        return []

    return proposals[:limit]


def render_proposals() -> str:
    """Render the ## Pending Proposals section for the cost report."""
    proposals = load_proposals()
    lines = ["## Pending Proposals", ""]
    if not proposals:
        lines.append("No proposals \u2014 accumulating shadow data.")
    else:
        for p in proposals:
            rule_id = p.get("rule_id", "unknown")
            current_role = p.get("current_role", "unknown")
            proposed_role = p.get("proposed_role", "unknown")
            expected_delta = p.get("expected_reward_delta", 0.0)
            confidence = p.get("confidence", 0.0)
            sample_count = p.get("sample_count", 0)
            line = (
                f"- **{rule_id}**: {current_role} \u2192 {proposed_role} "
                f"(expected delta: +{expected_delta:.3f}, "
                f"confidence: {confidence:.2f}, "
                f"samples: {sample_count})"
            )
            if p.get("selection_bias_flag"):
                line += " (limited data)"
            lines.append(line)
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(render_proposals())
