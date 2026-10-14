#!/usr/bin/env python3
"""Signal noise filters for reward_log.jsonl entries (FL-101).

Each filter receives a list of reward-log dicts and returns only the entries
that pass the filter (noise is discarded). The combined pipeline
``apply_all_filters`` runs all filters in order and returns the clean entries
plus a stats dict.

Pure stdlib — no external dependencies.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import now_iso


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str | None) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds. Returns None on failure."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _group_by_session(entries: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        sid = e.get("session_id", "unknown")
        groups[sid].append(e)
    return dict(groups)


# ---------------------------------------------------------------------------
# Individual filters
# ---------------------------------------------------------------------------

def filter_short_sessions(entries: list[dict]) -> list[dict]:
    """Exclude entries from sessions with fewer than 3 turns."""
    groups = _group_by_session(entries)
    short_sessions = {sid for sid, items in groups.items() if len(items) < 3}
    return [e for e in entries if e.get("session_id", "unknown") not in short_sessions]


def filter_retry_storms(entries: list[dict]) -> list[dict]:
    """If 3+ identical tool calls within 60s in a session, keep only the final.

    "Identical" = same session_id + same rule_id + timestamps within 60s window.
    """
    groups = _group_by_session(entries)
    keep_indices: set[int] = set()

    # Build index for fast lookup
    idx_map: dict[int, int] = {}  # id(entry) -> original index
    for i, e in enumerate(entries):
        idx_map[id(e)] = i

    for sid, items in groups.items():
        # Sub-group by rule_id
        by_rule: dict[str, list[dict]] = defaultdict(list)
        for e in items:
            rid = e.get("rule_id", "")
            by_rule[rid].append(e)

        for rid, rule_entries in by_rule.items():
            # Sort by timestamp
            ts_entries = []
            for e in rule_entries:
                epoch = _parse_ts(e.get("timestamp"))
                ts_entries.append((epoch or 0.0, e))
            ts_entries.sort(key=lambda x: x[0])

            # Sliding window: find runs of 3+ within 60s
            i = 0
            while i < len(ts_entries):
                # Find the end of the 60s window starting at i
                j = i
                while j < len(ts_entries) and (ts_entries[j][0] - ts_entries[i][0]) <= 60:
                    j += 1
                window_size = j - i
                if window_size >= 3:
                    # Keep only the last entry in this storm window
                    keep_indices.add(idx_map[id(ts_entries[j - 1][1])])
                    i = j  # skip past the storm
                else:
                    # Keep this entry (not part of a storm)
                    keep_indices.add(idx_map[id(ts_entries[i][1])])
                    i += 1

    return [e for i, e in enumerate(entries) if i in keep_indices]


def filter_accidental_overrides(entries: list[dict]) -> list[dict]:
    """Discard override signals where user changes tier then reverts in same session.

    Looks for entries in same session where routing_role changes and then changes
    back -- the middle entries (the override) are discarded.
    """
    groups = _group_by_session(entries)
    discard_indices: set[int] = set()

    idx_map: dict[int, int] = {}
    for i, e in enumerate(entries):
        idx_map[id(e)] = i

    for sid, items in groups.items():
        if len(items) < 3:
            continue
        # Sort by timestamp for chronological order
        ts_items = []
        for e in items:
            epoch = _parse_ts(e.get("timestamp"))
            ts_items.append((epoch or 0.0, e))
        ts_items.sort(key=lambda x: x[0])

        # Detect A -> B -> A pattern in routing_role
        for i in range(len(ts_items) - 2):
            role_a = ts_items[i][1].get("routing_role", "")
            role_b = ts_items[i + 1][1].get("routing_role", "")
            role_c = ts_items[i + 2][1].get("routing_role", "")
            if role_a and role_b and role_c and role_a == role_c and role_a != role_b:
                # The middle entry is the accidental override -- discard it
                discard_indices.add(idx_map[id(ts_items[i + 1][1])])

    return [e for i, e in enumerate(entries) if i not in discard_indices]


def filter_gate_only(entries: list[dict]) -> list[dict]:
    """Exclude sessions where all entries have reward_value <= 0.

    Uses base_reward_value when present so that quality-dampened entries
    (low quality_multiplier but positive base reward) are NOT dropped here.
    Low quality_multiplier alone is not a reason to discard an entry.

    Safety: never wipe all entries — if every session is negative-only,
    keep them all so the feedback loop has data to learn from.
    """
    groups = _group_by_session(entries)

    def _effective_reward(e: dict) -> float:
        # Prefer base_reward_value when available so quality dampening does not
        # cause false positives in the gate-only filter.
        if "base_reward_value" in e:
            return float(e["base_reward_value"])
        return float(e.get("reward_value", 0))

    gate_only_sessions = {
        sid
        for sid, items in groups.items()
        if all(_effective_reward(e) <= 0 for e in items)
    }
    if gate_only_sessions == set(groups.keys()):
        return entries
    return [e for e in entries if e.get("session_id", "unknown") not in gate_only_sessions]


def exclude_legacy_rewards(rows: list[dict]) -> list[dict]:
    """Exclude reward rows without reward_source field if newer rows exist.

    Legacy rows (written before reward_source was added) lack this field.
    If at least one row has a truthy reward_source, return only those rows.
    If none do, return rows unchanged (never go empty).

    Args:
        rows: List of parsed reward_log entries.

    Returns:
        Filtered list of rows, or original list if no reward_source values exist.
    """
    if not rows:
        return rows
    # Check if at least one row has a truthy reward_source
    has_new_format = any(row.get("reward_source") for row in rows)
    if not has_new_format:
        return rows
    # Return only rows with truthy reward_source
    return [row for row in rows if row.get("reward_source")]


# ---------------------------------------------------------------------------
# Combined pipeline
# ---------------------------------------------------------------------------

def apply_all_filters(
    entries: list[dict],
) -> tuple[list[dict], dict[str, Any]]:
    """Apply all filters in order. Returns (clean_entries, noise_stats)."""
    total = len(entries)
    clean = filter_short_sessions(entries)
    clean = filter_retry_storms(clean)
    clean = filter_accidental_overrides(clean)
    clean = filter_gate_only(clean)
    noise_count = total - len(clean)
    stats: dict[str, Any] = {
        "noise_entries_filtered": noise_count,
        "total_entries": total,
        "noise_rate": round(noise_count / total, 4) if total > 0 else 0.0,
        "timestamp": now_iso(),
    }
    return clean, stats


# ---------------------------------------------------------------------------
# SEM-302: Semantic low-confidence log reader
# ---------------------------------------------------------------------------

_SEMANTIC_LOG_DEFAULT = (
    Path(__file__).resolve().parent.parent / "data" / "semantic_low_confidence.jsonl"
)


def read_semantic_log(path: Path | None = None) -> list[dict]:
    """Read the semantic low-confidence JSONL log.

    Each line is a JSON object with fields:
        timestamp, input_text_hash, top_class, top_score,
        second_class, second_score, fallback_class, was_fallback

    Args:
        path: Path to the JSONL file. Defaults to
              data/semantic_low_confidence.jsonl relative to project root.

    Returns:
        List of parsed log entry dicts. Returns empty list if the file is
        missing or unreadable.
    """
    target = path if path is not None else _SEMANTIC_LOG_DEFAULT
    if not target.exists():
        return []
    entries: list[dict] = []
    try:
        with open(target) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # Skip malformed lines -- continue without crashing
    except OSError:
        return []
    return entries
