#!/usr/bin/env python3
"""FL-104: Proposal writer.

Reads shadow evaluation results and confidence scores, filters by
improvement threshold, and writes policy proposals to
data/policy_proposals.json.

Pure stdlib — no external dependencies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR, get_feedback_loop_config, now_iso, save_json_state
from changelog import write_changelog_entry


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_shadow_evaluation() -> list[dict[str, Any]]:
    """Read data/shadow_evaluation.json, return list of per-rule evaluations.

    Each returned dict includes the ``rule_id`` key (pulled from the dict key
    in the stored ``rules`` mapping).
    """
    eval_path = DATA_DIR / "shadow_evaluation.json"
    try:
        raw = json.loads(eval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(raw, dict):
        return []

    rules = raw.get("rules", {})
    if not isinstance(rules, dict):
        return []

    entries: list[dict[str, Any]] = []
    for rule_id, data in rules.items():
        if not isinstance(data, dict):
            continue
        entry = dict(data)
        entry["rule_id"] = rule_id
        entries.append(entry)

    return entries


def _load_confidence_lookup() -> dict[str, float]:
    """Load data/confidence_scores.json and return a lookup mapping rule_id/intent to confidence.

    Returns an empty dict on any I/O or parse error.
    """
    scores_path = DATA_DIR / "confidence_scores.json"
    try:
        raw = json.loads(scores_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(raw, dict):
        return {}

    lookup: dict[str, float] = {}
    for entry in raw.get("entries", []):
        if not isinstance(entry, dict):
            continue
        # Index by both plain intent and rule_id format (ROLE:intent)
        key = entry.get("intent", "")
        if key:
            conf = entry.get("confidence", 0.0)
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = 0.0
            lookup[key] = conf
            # Also store under rule_id if present, so composite keys match
            rid = entry.get("rule_id", "")
            if rid:
                lookup[rid] = conf

    return lookup


# ---------------------------------------------------------------------------
# Scoring / filtering
# ---------------------------------------------------------------------------

def score_proposals(
    evaluations: list[dict[str, Any]],
    threshold: float,
) -> list[dict[str, Any]]:
    """Filter evaluations that exceed the improvement threshold and build proposals.

    A proposal is created when ``candidate_expected_reward > actual_mean_reward + threshold``.

    Returns proposals sorted by ``expected_reward_delta`` descending.
    """
    confidence_lookup = _load_confidence_lookup()

    proposals: list[dict[str, Any]] = []
    for ev in evaluations:
        actual = ev.get("actual_mean_reward", 0.0)
        candidate = ev.get("candidate_expected_reward", 0.0)

        try:
            actual = float(actual)
            candidate = float(candidate)
        except (TypeError, ValueError):
            continue

        if candidate <= actual + threshold:
            continue

        rule_id = ev.get("rule_id", "unknown")
        improvement_delta = ev.get("improvement_delta", candidate - actual)

        try:
            improvement_delta = float(improvement_delta)
        except (TypeError, ValueError):
            improvement_delta = candidate - actual

        confidence = confidence_lookup.get(rule_id, 0.0)

        proposals.append({
            "rule_id": rule_id,
            "current_role": ev.get("actual_role", ""),
            "proposed_role": ev.get("candidate_role", ""),
            "expected_reward_delta": improvement_delta,
            "sample_count": ev.get("sample_count", 0),
            "confidence": confidence,
            "selection_bias_flag": ev.get("selection_bias_warning", False),
            "created_at": now_iso(),
        })

    proposals.sort(key=lambda p: p["expected_reward_delta"], reverse=True)
    return proposals


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_proposals(proposals: list[dict[str, Any]]) -> None:
    """Write proposals to data/policy_proposals.json."""
    save_json_state(DATA_DIR / "policy_proposals.json", {
        "timestamp": now_iso(),
        "proposals": proposals,
    })
    for proposal in proposals:
        write_changelog_entry(
            "proposed",
            proposal["rule_id"],
            from_role=proposal["current_role"],
            to_role=proposal["proposed_role"],
            confidence=proposal["confidence"],
            sample_count=proposal["sample_count"],
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Load evaluation, score proposals above threshold, write results."""
    evaluations = load_shadow_evaluation()
    threshold = get_feedback_loop_config()["improvement_threshold"]
    proposals = score_proposals(evaluations, threshold)
    write_proposals(proposals)


if __name__ == "__main__":
    main()
