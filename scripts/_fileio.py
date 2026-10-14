#!/usr/bin/env python3
"""File I/O helpers: log append, state read/write, debug logging."""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from _config import (
    DATA_DIR,
    DEBUG_LOG,
    TAIL_READ_CHUNK_BYTES,
    USAGE_LOG,
    USAGE_LOG_COMPACT_TRIGGER_BYTES,
    _warn,
)
from _session import now_iso

SESSION_LOG = DATA_DIR / "session_log.jsonl"

# ── TTL dedup cache ───────────────────────────────────────────────────────────
# Keyed on (event_type, session_id, reason); value is the monotonic timestamp of
# the last accepted write.  Prevents concurrent duplicate writes from multiple
# processes that all arrive within the TTL window.
_DEDUP_TTL_SECONDS: float = 5.0
_dedup_cache: dict[tuple, float] = {}
_dedup_lock = threading.Lock()

# Event types that are intentionally allowed to repeat with identical content
# within the same session (once per dispatch, not once per session). These
# carry no "reason" field and none of the file-adjacency duplicate_keys below,
# so both the TTL cache and the last-line check would otherwise collapse
# every occurrence in a session into a single write.
_DEDUP_EXEMPT_EVENT_TYPES = frozenset({"role_tier_mismatch"})


def _dedup_key(record: dict) -> tuple:
    return (
        record.get("type", ""),
        record.get("session_id", ""),
        record.get("reason", ""),
    )


def _dedup_check_and_set(record: dict) -> bool:
    """Return True (skip) if an identical record was written within the TTL window."""
    if record.get("type") in _DEDUP_EXEMPT_EVENT_TYPES:
        return False
    key = _dedup_key(record)
    now = time.monotonic()
    with _dedup_lock:
        # evict stale entries to keep the dict bounded
        stale = [k for k, ts in _dedup_cache.items() if now - ts >= _DEDUP_TTL_SECONDS]
        for k in stale:
            del _dedup_cache[k]
        if key in _dedup_cache:
            return True
        _dedup_cache[key] = now
        return False


# ── low-level file helpers ────────────────────────────────────────────────────

def _last_line_from_handle(handle) -> dict[str, Any] | None:
    """Return the last non-empty JSON record from an already-open binary file handle."""
    try:
        handle.seek(0, 2)
        size = handle.tell()
        if size == 0:
            return None
        chunk_size = min(TAIL_READ_CHUNK_BYTES, size)
        handle.seek(-chunk_size, 2)
        chunk = handle.read()
        for line in reversed(chunk.split(b"\n")):
            line = line.strip()
            if line:
                try:
                    return json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return None


def _read_last_line(path: Path) -> dict[str, Any] | None:
    """Return the last non-empty JSON record from *path* without reading the whole file."""
    try:
        with path.open("rb") as f:
            return _last_line_from_handle(f)
    except OSError:
        return None


def _rotate_log_if_needed() -> None:
    """Rename usage.jsonl -> usage.jsonl.1 when it exceeds USAGE_LOG_COMPACT_TRIGGER_BYTES."""
    try:
        if USAGE_LOG.is_file() and USAGE_LOG.stat().st_size > USAGE_LOG_COMPACT_TRIGGER_BYTES:
            USAGE_LOG.rename(USAGE_LOG.with_suffix(".jsonl.1"))
    except OSError as e:
        _warn("log_rotation_failed", {"file": "_fileio.py", "error": str(e)})


def _debug_log(message: str) -> None:
    """Write a one-line diagnostic to debug.log when COST_ROUTER_DEBUG=1."""
    if not os.environ.get("COST_ROUTER_DEBUG"):
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with DEBUG_LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {message}\n")


# ── event log append ──────────────────────────────────────────────────────────

def _event_log_for_io(record: dict[str, Any]) -> Path:
    """Route record to appropriate event log file based on event type (I/O variant)."""
    _SESSION_EVENT_TYPES = frozenset(
        {"session_summary", "role_distribution", "routing_gap", "data_reset", "session_output_high"}
    )
    _REWARD_EVENT_TYPES = frozenset({"reward_signal", "explicit_feedback"})
    _TOKEN_EVENT_TYPES = frozenset({"prompt", "tool", "response"})
    t = record.get("type", "")
    if t in _SESSION_EVENT_TYPES:
        return DATA_DIR / "session_events.jsonl"
    if t in _REWARD_EVENT_TYPES:
        return DATA_DIR / "reward_events.jsonl"
    if t in _TOKEN_EVENT_TYPES:
        return DATA_DIR / "token_events.jsonl"
    if t == "extraction_fallback":
        return DATA_DIR / "extraction_errors.jsonl"
    return DATA_DIR / "routing_events.jsonl"


def append_event(record: dict[str, Any]) -> None:
    """Append one usage-log record. Never raises."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", now_iso())
        record.setdefault("schema_version", 1)
        if _dedup_check_and_set(record):
            _debug_log(
                f"skipped:ttl-dedup type={record.get('type')} "
                f"session={record.get('session_id')} reason={record.get('reason')}"
            )
            return
        _rotate_log_if_needed()

        _log_file = _event_log_for_io(record)
        with _log_file.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                last = _last_line_from_handle(handle)
                if last is not None and record.get("type") not in _DEDUP_EXEMPT_EVENT_TYPES:
                    duplicate_keys = ("session_id", "type", "direction", "bytes", "tool_name", "est_tokens", "target_hash")
                    if all(last.get(key) == record.get(key) for key in duplicate_keys):
                        _debug_log(
                            f"skipped:duplicate type={record.get('type')} "
                            f"tool={record.get('tool_name')} session={record.get('session_id')}"
                        )
                        return
                handle.seek(0, 2)
                # NOTE: records carry only an ISO `ts` field — no monotonic sequence
                # number.  Within the same wall-clock second, order is FIFO (append
                # order).  A reversed walk is therefore LIFO within a second, not
                # strictly chronological.  See scripts/docs/timestamp_tiebreaking.md.
                handle.write((json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        _debug_log(
            f"wrote:{_log_file} type={record.get('type')} "
            f"tool={record.get('tool_name')} session={record.get('session_id')}"
        )
    except OSError:
        _debug_log(f"append_event:failed type={record.get('type')}")


def append_session_log(record: dict[str, Any]) -> None:
    """Append one record to session_log.jsonl. Never raises."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", now_iso())
        with SESSION_LOG.open("ab") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.write((json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        _debug_log(f"append_session_log:failed type={record.get('type')}")


# ── state I/O ─────────────────────────────────────────────────────────────────

def load_json_state(path: Path) -> dict[str, Any]:
    """Read a small local JSON state file. Never raises."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_json_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically write JSON state. Never raises."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    except OSError:
        _debug_log(f"save_json_state:failed path={path}")
