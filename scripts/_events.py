#!/usr/bin/env python3
"""Event log access: read_events, _event_log_for, ensure_log_integrity."""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from typing import Any

from _config import DATA_DIR, INSTALLED_AT_FILE, USAGE_LOG
from _fileio import append_event, _debug_log
from _session import now_iso

_SESSION_EVENT_TYPES = frozenset(
    {"session_summary", "role_distribution", "routing_gap", "data_reset", "session_output_high"}
)
_REWARD_EVENT_TYPES = frozenset({"reward_signal", "explicit_feedback"})
_TOKEN_EVENT_TYPES = frozenset({"prompt", "tool", "response"})

MAX_LOOKBACK_LINES = 1000  # tail-cap per log file to bound read_events() cost


def _event_log_for(record: dict[str, Any]) -> "Any":
    """Route record to appropriate event log file based on event type.

    Builds paths from DATA_DIR at call time so monkeypatching DATA_DIR in
    tests redirects writes without needing to patch each split constant.
    """
    t = record.get("type", "")
    if t in _SESSION_EVENT_TYPES:
        return DATA_DIR / "session_events.jsonl"
    if t in _REWARD_EVENT_TYPES:
        return DATA_DIR / "reward_events.jsonl"
    if t in _TOKEN_EVENT_TYPES:
        return DATA_DIR / "token_events.jsonl"
    return DATA_DIR / "routing_events.jsonl"


def _read_log_lines(log_path: "Any") -> list[str]:
    """Read lines from a log file under a shared lock. Returns [] on missing file."""
    try:
        handle = log_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return []
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH)
            raw_lines = handle.readlines()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    return raw_lines


def read_events(max_lines: int = MAX_LOOKBACK_LINES) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for log_path in (
        DATA_DIR / "routing_events.jsonl",
        DATA_DIR / "session_events.jsonl",
        DATA_DIR / "reward_events.jsonl",
        DATA_DIR / "token_events.jsonl",
    ):
        # Read current file and its .1 rotated counterpart (created at 2MB threshold).
        # Events written before a rotation boundary live exclusively in the .1 file;
        # merging both ensures no silent data loss at rotation edges.
        rotated_path = log_path.parent / (log_path.name + ".1")
        rotated_lines = _read_log_lines(rotated_path)  # [] when absent — handled gracefully
        current_lines = _read_log_lines(log_path)

        if not current_lines and not rotated_lines:
            # Neither file exists; skip this log path silently.
            # (Do not emit synthetic errors for missing side logs; tests expect [] or exact counts.)
            continue

        # Merge: rotated lines are older, current lines are newer.
        # Parse both, sort by "ts" field, then tail to max_lines.
        merged_parsed: list[dict[str, Any]] = []
        for source_lines, source_path in (
            (rotated_lines, rotated_path),
            (current_lines, log_path),
        ):
            safe_lines = source_lines
            for line in safe_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    parsed.setdefault("_source_file", str(source_path))
                    merged_parsed.append(parsed)

        # Sort by timestamp string (ISO-8601 lexicographic sort is chronological).
        merged_parsed.sort(key=lambda r: r.get("ts", ""))

        # Apply lookback cap to the merged result.
        for record in merged_parsed[-max_lines:]:
            record.pop("_source_file", None)
            events.append(record)
    return events


def ensure_log_integrity(session_id: str) -> None:
    """Write a data_reset event if usage.jsonl is absent/empty after first install."""
    min_age_hours = float(os.environ.get("MIN_AGE_HOURS", "1"))

    if not INSTALLED_AT_FILE.exists():
        try:
            INSTALLED_AT_FILE.write_text(now_iso(), encoding="utf-8")
        except OSError:
            pass
        return

    log_missing = not USAGE_LOG.exists() or USAGE_LOG.stat().st_size == 0
    if not log_missing:
        return

    try:
        installed_dt = datetime.fromisoformat(
            INSTALLED_AT_FILE.read_text(encoding="utf-8").strip()
        )
        age_hours = (datetime.now(timezone.utc) - installed_dt).total_seconds() / 3600
    except (OSError, ValueError):
        return

    if age_hours < min_age_hours:
        return

    try:
        append_event({
            "type": "data_reset",
            "reason": "usage_log_absent_or_empty",
            "ts": now_iso(),
            "session_id": session_id,
        })
    except OSError:
        pass
