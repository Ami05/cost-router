#!/usr/bin/env python3
"""cost-router: log compaction and scenario library I/O.

Extracted from compact_log.py (SOLID-014).  Contains all JSONL rotation,
working-set trimming, scenario-library reads/writes, and the core session
merge logic.  The Stop-hook coordinator (compact_log.py) imports from here.

Public API
----------
compact_logs(session_id, window_start) -> CompactionResult
    Full session merge: update scenario_library.json + compact usage.jsonl.

CompactionResult
    Dataclass-like dict with keys: session_id, key, reward, skipped.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    MAX_SCENARIOS_PER_KEY,
    USAGE_ARCHIVE,
    USAGE_LOG,
    USAGE_LOG_COMPACT_TRIGGER_BYTES,
    now_iso,
    read_events,
    save_json_state,
    _warn,
)
from reward import compute_reward  # noqa: E402

SCENARIO_LIBRARY_FILE = DATA_DIR / "scenario_library.json"
WORKING_SET_BYTES = 614_400        # 600 KB working set budget
ARCHIVE_ROTATE_BYTES = 10_485_760  # 10 MB archive rotation threshold
USAGE_LOG_TRIGGER_BYTES = USAGE_LOG_COMPACT_TRIGGER_BYTES


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class CompactionResult:
    session_id: str
    key: str = ""
    reward: float = 0.0
    skipped: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# Internal session helpers
# ---------------------------------------------------------------------------


def _session_raw_events(all_events: list[dict], session_id: str) -> list[dict]:
    """Return non-summary events that belong to *session_id*."""
    return [
        e
        for e in all_events
        if e.get("session_id") == session_id and e.get("type") != "session_summary"
    ]


def _derive_tier_mix(raw_events: list[dict]) -> dict[str, int]:
    """Compute tier_mix from raw (non-summary) session events."""
    counts: Counter[str] = Counter()
    for e in raw_events:
        counts[e.get("tier", "unknown")] += 1
    return dict(counts)


def derive_key(raw_events: list[dict], tier_mix: dict[str, int]) -> str:
    """Return the scenario-library key for this session: tool_type:tier:outcome.

    * tool_type  — most common ``tool_name`` value among raw events (or "unknown")
    * tier       — dominant tier by event count from *tier_mix* (or "unknown")
    * outcome    —
        "escalated"  if tier_mix contains both ``fast`` and ``reasoning`` counts
        "repeat"     if any ``target_hash`` appears 2 or more times in the session
        "clean"      otherwise
    """
    tool_names = [e.get("tool_name") for e in raw_events if e.get("tool_name")]
    tool_type: str = Counter(tool_names).most_common(1)[0][0] if tool_names else "unknown"

    tier: str = max(tier_mix, key=lambda k: tier_mix[k]) if tier_mix else "unknown"

    has_fast = tier_mix.get("fast", 0) > 0
    has_reasoning = tier_mix.get("reasoning", 0) > 0
    if has_fast and has_reasoning:
        outcome = "escalated"
    else:
        hashes = [e.get("target_hash") for e in raw_events if e.get("target_hash")]
        outcome = "repeat" if any(c >= 2 for c in Counter(hashes).values()) else "clean"

    return f"{tool_type}:{tier}:{outcome}"


def _build_summary(raw_events: list[dict], session_id: str, all_events: list[dict]) -> dict:
    """Return the session_summary event for *session_id*.

    Prefers an already-written session_summary record from *all_events*; falls
    back to computing one from raw events (used during --checkpoint mode).
    """
    for e in all_events:
        if e.get("session_id") == session_id and e.get("type") == "session_summary":
            return e

    # Partial / checkpoint summary
    tier_counts: Counter[str] = Counter()
    tool_count = 0
    input_tokens = 0
    output_tokens = 0
    timestamps: list[str] = []

    for e in raw_events:
        tier_counts[e.get("tier", "unknown")] += 1
        direction = e.get("direction")
        tokens = e.get("est_tokens", 0)
        if direction == "input":
            input_tokens += tokens
        elif direction == "output":
            output_tokens += tokens
        else:
            tool_count += 1
        if e.get("ts"):
            timestamps.append(e["ts"])

    return {
        "type": "session_summary",
        "session_id": session_id,
        "started_at": min(timestamps) if timestamps else None,
        "ended_at": max(timestamps) if timestamps else None,
        "est_input_tokens": input_tokens,
        "est_output_tokens": output_tokens,
        "est_context_tokens": 0,
        "est_cost_usd": 0.0,
        "tool_call_count": tool_count,
        "tier_mix": dict(tier_counts),
        "ts": now_iso(),
    }


# ---------------------------------------------------------------------------
# Scenario library I/O
# ---------------------------------------------------------------------------


def load_library() -> dict:
    """Read scenario_library.json; return empty dict when absent or corrupt."""
    try:
        return json.loads(SCENARIO_LIBRARY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_library(library: dict) -> None:
    """Write *library* to scenario_library.json with an exclusive file lock.

    Fails open (PROD-8): a read-only or full DATA_DIR drops the write rather
    than crashing the Stop-hook path that calls this.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with SCENARIO_LIBRARY_FILE.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(library, indent=2))
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Usage log compaction
# ---------------------------------------------------------------------------


def compact_usage_log() -> None:
    """Compact usage.jsonl: keep a session-aware working set, evict the rest to archive.

    7-step algorithm:
    1. Early exit if file is under budget.
    2. Parse all events.
    3. Group by session_id; unknown-session events go straight to archive.
    4. Rank sessions newest-first (by max ts of their events).
    5. Fill working set newest-first until WORKING_SET_BYTES; always keep >= 1 session.
    6. Write working set atomically via tempfile + os.replace().
    7. Append evicted sessions to usage_archive.jsonl.
    8. (UC-003) Rotate archive if it exceeds ARCHIVE_ROTATE_BYTES.
    """
    try:
        if not USAGE_LOG.exists():
            return
        if USAGE_LOG.stat().st_size <= WORKING_SET_BYTES:
            return

        evicted_lines: list[str] = []
        with USAGE_LOG.open("r", encoding="utf-8", errors="replace") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                events_by_line: list[tuple[str, dict]] = []
                for line in fh:
                    stripped = line.rstrip("\n")
                    if not stripped.strip():
                        continue
                    try:
                        ev = json.loads(stripped)
                        if isinstance(ev, dict):
                            events_by_line.append((stripped, ev))
                    except json.JSONDecodeError:
                        continue

                if not events_by_line:
                    return

                session_lines: dict[str, list[str]] = {}
                unknown_lines: list[str] = []
                for raw, ev in events_by_line:
                    sid = ev.get("session_id", "")
                    if not sid:
                        unknown_lines.append(raw)
                    else:
                        session_lines.setdefault(sid, []).append(raw)

                def _max_ts(lines: list[str]) -> str:
                    best = ""
                    for ln in lines:
                        try:
                            ts = json.loads(ln).get("ts", "")
                            if ts > best:
                                best = ts
                        except Exception:
                            pass
                    return best

                ranked_sessions: list[tuple[str, list[str]]] = sorted(
                    session_lines.items(),
                    key=lambda kv: _max_ts(kv[1]),
                    reverse=True,
                )

                working_set_lines: list[str] = []
                evicted_lines = list(unknown_lines)
                budget = WORKING_SET_BYTES

                for i, (sid, s_lines) in enumerate(ranked_sessions):
                    chunk = "\n".join(s_lines) + "\n"
                    chunk_bytes = len(chunk.encode("utf-8"))
                    if i == 0 or chunk_bytes <= budget:
                        working_set_lines.extend(s_lines)
                        budget -= chunk_bytes
                    else:
                        evicted_lines.extend(s_lines)

                working_text = "\n".join(working_set_lines) + ("\n" if working_set_lines else "")
                tmp_fd, tmp_path = tempfile.mkstemp(dir=USAGE_LOG.parent, suffix=".tmp")
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_fh:
                        tmp_fh.write(working_text)
                    os.replace(tmp_path, USAGE_LOG)
                    tmp_path = None
                except Exception:
                    if tmp_path is not None:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                    raise
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

        if evicted_lines:
            archive_text = "\n".join(evicted_lines) + "\n"
            with USAGE_ARCHIVE.open("a", encoding="utf-8") as fh:
                fh.write(archive_text)

            try:
                if USAGE_ARCHIVE.stat().st_size > ARCHIVE_ROTATE_BYTES:
                    # Shift oldest-first so a second/third rotation never clobbers
                    # a prior generation: keep usage_archive.{1,2,3}.jsonl.
                    gen3 = USAGE_ARCHIVE.with_name("usage_archive.3.jsonl")
                    gen2 = USAGE_ARCHIVE.with_name("usage_archive.2.jsonl")
                    gen1 = USAGE_ARCHIVE.with_name("usage_archive.1.jsonl")
                    if gen2.exists():
                        if gen3.exists():
                            gen3.unlink()
                        gen2.rename(gen3)
                    if gen1.exists():
                        gen1.rename(gen2)
                    USAGE_ARCHIVE.rename(gen1)
            except OSError:
                pass

    except Exception as e:  # noqa: BLE001
        _warn("compact_usage_log_failed", {"error": str(e)})


# ---------------------------------------------------------------------------
# Core session merge
# ---------------------------------------------------------------------------


def compact_session(session_id: str, window_start: str | None = None) -> CompactionResult:
    """Merge the completed (or partial) session into scenario_library.json.

    *window_start* restricts the reward computation to events at or after that
    ISO-8601 timestamp.  Pass None (the default) to include all session events.

    Returns a CompactionResult describing what was done.
    """
    all_events = read_events()
    raw_events = _session_raw_events(all_events, session_id)

    if not raw_events:
        return CompactionResult(session_id=session_id, skipped=True)

    summary_event = next(
        (
            e
            for e in all_events
            if e.get("session_id") == session_id and e.get("type") == "session_summary"
        ),
        None,
    )
    tier_mix = summary_event.get("tier_mix", {}) if summary_event else _derive_tier_mix(raw_events)

    key = derive_key(raw_events, tier_mix)
    summary = _build_summary(raw_events, session_id, all_events)

    est_cost = summary.get("est_cost_usd", 0.0)
    reward_val = compute_reward(all_events, session_id, window_start, est_cost)

    library = load_library()
    entries: list[dict] = library.get(key, [])

    if any(e.get("session_id") == session_id for e in entries):
        return CompactionResult(session_id=session_id, key=key, reward=reward_val, skipped=True)

    entry: dict = {
        "session_id": session_id,
        "reward": round(reward_val, 3),
        "summary": summary,
    }
    entries.append(entry)

    if len(entries) > MAX_SCENARIOS_PER_KEY:
        entries = entries[-MAX_SCENARIOS_PER_KEY:]

    library[key] = entries
    save_library(library)

    return CompactionResult(session_id=session_id, key=key, reward=reward_val, skipped=False)


# ---------------------------------------------------------------------------
# Convenience entry point (run compaction + usage log in one call)
# ---------------------------------------------------------------------------


def compact_logs(session_id: str, window_start: str | None = None) -> CompactionResult:
    """Merge session into scenario_library.json and compact usage.jsonl.

    This is the primary public entry point for the Stop-hook coordinator.
    Usage log compaction is skipped when *window_start* is provided (checkpoint
    mode) — callers should pass window_start=None for full Stop-hook runs.
    """
    result = compact_session(session_id, window_start=window_start)
    if window_start is None:
        compact_usage_log()
    return result
