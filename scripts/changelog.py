#!/usr/bin/env python3
"""FL-402: Immutable audit trail — policy_changelog.jsonl.

Append-only changelog for feedback-loop lifecycle events (proposed,
canary_started, promoted, rejected, inconclusive, rolled_back, calibrated).

Pure stdlib — no external deps.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR, now_iso  # noqa: E402

_VALID_EVENT_TYPES = frozenset({
    "proposed",
    "promoted",
    "rejected",
    "inconclusive",
    "rolled_back",
    "calibrated",
})

CHANGELOG_FILE = DATA_DIR / "policy_changelog.jsonl"


def write_changelog_entry(event_type: str, rule_id: str, **kwargs: Any) -> None:
    """Append one JSON line to policy_changelog.jsonl.

    Args:
        event_type: One of the valid lifecycle event types.
        rule_id: The rule this event pertains to.
        **kwargs: Optional fields (from_role, to_role, confidence,
                  sample_count, ci_lower, ci_upper, mean_diff, reason).
    """
    if event_type not in _VALID_EVENT_TYPES:
        raise ValueError(
            f"Invalid event_type {event_type!r}; must be one of {sorted(_VALID_EVENT_TYPES)}"
        )

    entry: dict[str, Any] = {
        "timestamp": now_iso(),
        "event_type": event_type,
        "rule_id": rule_id,
    }
    entry.update(kwargs)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CHANGELOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")
