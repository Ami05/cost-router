#!/usr/bin/env python3
"""Backfill actual_model for reward_log.jsonl entries where actual_model='unknown'.

Usage:
    python3 scripts/backfill_reward_log.py             # dry-run (default)
    python3 scripts/backfill_reward_log.py --dry-run   # explicit dry-run
    python3 scripts/backfill_reward_log.py --live       # write changes

Recovery order per entry:
  1. Transcript: ~/.claude/projects/<cwd_hash>/<session_id>.jsonl
     — reads message.model from assistant records; uses dominant (most frequent) model.
     — tags backfill_source=transcript
  2. routing_events.jsonl: nearest routing_declared event for same session_id.
     — maps model tier string to canonical model name.
     — tags backfill_source=routing_events
  3. Irrecoverable: tags backfill_source=none
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# ── path setup ────────────────────────────────────────────────────────────────

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from _config import DATA_DIR, PLUGIN_ROOT  # noqa: E402

REWARD_LOG = DATA_DIR / "reward_log.jsonl"
ROUTING_EVENTS_LOG = DATA_DIR / "routing_events.jsonl"
CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# cwd_hash for the cost-router project itself (used as fallback when no cwd on entry)
_COST_ROUTER_CWD_HASH = str(PLUGIN_ROOT).replace("/", "-")


# ── transcript helpers ────────────────────────────────────────────────────────

def _cwd_hash_for_entry(entry: dict[str, Any]) -> str:
    """Derive cwd_hash from entry cwd field or fall back to PLUGIN_ROOT."""
    cwd = entry.get("cwd") or str(PLUGIN_ROOT)
    return cwd.replace("/", "-")


def _extract_model_from_transcript(session_id: str, cwd_hash: str) -> str | None:
    """Return the dominant model string seen in a transcript, or None."""
    transcript_path = CLAUDE_PROJECTS_ROOT / cwd_hash / f"{session_id}.jsonl"
    if not transcript_path.is_file():
        # Also try the cost-router project hash as a fallback
        fallback = CLAUDE_PROJECTS_ROOT / _COST_ROUTER_CWD_HASH / f"{session_id}.jsonl"
        if not fallback.is_file():
            return None
        transcript_path = fallback

    model_counts: Counter[str] = Counter()
    try:
        with transcript_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message", {})
                if not isinstance(msg, dict):
                    continue
                model = msg.get("model")
                if isinstance(model, str) and model and model != "unknown":
                    model_counts[model] += 1
    except OSError:
        return None

    if not model_counts:
        return None
    return model_counts.most_common(1)[0][0]


# ── routing_events helpers ────────────────────────────────────────────────────

def _load_routing_declared_index() -> dict[str, list[dict[str, Any]]]:
    """Return {session_id: [routing_declared events sorted by ts]} from routing_events.jsonl."""
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not ROUTING_EVENTS_LOG.is_file():
        return index
    try:
        with ROUTING_EVENTS_LOG.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "routing_declared":
                    continue
                sid = rec.get("session_id")
                model = rec.get("model")
                if sid and isinstance(model, str) and model and model != "unknown":
                    index[sid].append(rec)
    except OSError:
        pass
    # sort by ts
    for sid in index:
        index[sid].sort(key=lambda r: r.get("ts", ""))
    return index


def _nearest_model_from_routing(
    session_id: str,
    timestamp: str,
    index: dict[str, list[dict[str, Any]]],
) -> str | None:
    """Return the model from the routing_declared event nearest to *timestamp* for session."""
    events = index.get(session_id)
    if not events:
        return None
    # Find the closest event by timestamp string comparison (ISO timestamps sort lexicographically)
    best = min(events, key=lambda r: abs(_ts_diff_seconds(r.get("ts", ""), timestamp)))
    model = best.get("model")
    if isinstance(model, str) and model and model != "unknown":
        return model
    return None


def _ts_diff_seconds(a: str, b: str) -> float:
    """Rough diff in seconds between two ISO timestamp strings — no external deps."""
    from datetime import datetime, timezone

    def _parse(ts: str) -> float:
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                dt = datetime.strptime(ts, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
        return 0.0

    return abs(_parse(a) - _parse(b))


# ── main backfill logic ───────────────────────────────────────────────────────

def _load_reward_log() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not REWARD_LOG.is_file():
        return entries
    with REWARD_LOG.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"_raw": line})
    return entries


def _write_reward_log_atomic(entries: list[dict[str, Any]]) -> None:
    """Write backup then atomically replace reward_log.jsonl."""
    backup = REWARD_LOG.with_suffix(".jsonl.bak")
    shutil.copy2(REWARD_LOG, backup)
    print(f"  Backup written: {backup}")

    tmp = REWARD_LOG.with_suffix(f".jsonl.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for entry in entries:
                if "_raw" in entry:
                    f.write(entry["_raw"] + "\n")
                else:
                    f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        os.replace(tmp, REWARD_LOG)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def backfill(dry_run: bool = True) -> None:
    entries = _load_reward_log()
    if not entries:
        print("reward_log.jsonl is empty or missing — nothing to do.")
        return

    total = len(entries)
    unknown_indices = [i for i, e in enumerate(entries) if e.get("actual_model") == "unknown"]

    if not unknown_indices:
        print(f"No unknown entries found in {total} total entries.")
        return

    print(f"reward_log.jsonl: {total} total entries, {len(unknown_indices)} with actual_model='unknown'")
    print(f"Mode: {'DRY-RUN' if dry_run else 'LIVE'}")
    print()

    # Pre-load routing_declared index once
    routing_index = _load_routing_declared_index()

    # Per-session transcript cache to avoid re-reading the same file
    transcript_cache: dict[tuple[str, str], str | None] = {}

    recovered_transcript = 0
    recovered_routing = 0
    irrecoverable = 0

    for idx in unknown_indices:
        entry = entries[idx]
        session_id = entry.get("session_id", "")
        timestamp = entry.get("timestamp", "")
        cwd_hash = _cwd_hash_for_entry(entry)

        recovered_model: str | None = None
        source: str = "none"

        # Strategy 1: transcript
        cache_key = (session_id, cwd_hash)
        if cache_key not in transcript_cache:
            transcript_cache[cache_key] = _extract_model_from_transcript(session_id, cwd_hash)
        recovered_model = transcript_cache[cache_key]
        if recovered_model:
            source = "transcript"
            recovered_transcript += 1
        else:
            # Strategy 2: routing_events
            recovered_model = _nearest_model_from_routing(session_id, timestamp, routing_index)
            if recovered_model:
                source = "routing_events"
                recovered_routing += 1
            else:
                source = "none"
                irrecoverable += 1

        if not dry_run and recovered_model:
            entries[idx]["actual_model"] = recovered_model
            entries[idx]["backfill_source"] = source
            entries[idx]["data_origin"] = "backfilled:2026-09-19-remediation"
            entries[idx]["backfill_run_id"] = "2026-09-19-remediation"
        elif not dry_run:
            entries[idx]["backfill_source"] = source
            entries[idx]["data_origin"] = "backfilled:2026-09-19-remediation"
            entries[idx]["backfill_run_id"] = "2026-09-19-remediation"

    # Summary table
    print("─" * 52)
    print(f"{'Metric':<38} {'Count':>8}")
    print("─" * 52)
    print(f"{'Total entries':<38} {total:>8}")
    print(f"{'Unknown (candidates)':<38} {len(unknown_indices):>8}")
    print(f"{'Recovered via transcript':<38} {recovered_transcript:>8}")
    print(f"{'Recovered via routing_events':<38} {recovered_routing:>8}")
    print(f"{'Irrecoverable (backfill_source=none)':<38} {irrecoverable:>8}")
    print("─" * 52)

    if dry_run:
        print("\nDry-run complete — no files written. Pass --live to apply.")
    else:
        _write_reward_log_atomic(entries)
        print(f"\nLive write complete. {recovered_transcript + recovered_routing} entries updated.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill actual_model in reward_log.jsonl for entries where it is 'unknown'."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print summary only; do not write (default behaviour).",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Write backup then atomically update reward_log.jsonl.",
    )
    args = parser.parse_args()

    # Default is dry-run when neither flag is given
    dry_run = not args.live
    backfill(dry_run=dry_run)


if __name__ == "__main__":
    main()
