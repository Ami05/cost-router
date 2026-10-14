#!/usr/bin/env python3
"""Session and payload utilities: timestamps, stdin, session/subagent IDs, hashing."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from _config import STDIN_SELECT_TIMEOUT_SECONDS, TARGET_HASH_HEX_CHARS


# ── time helpers ──────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_ts(ts: str | None) -> str:
    """Render an ISO timestamp as a fixed-width UTC string for reports."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return str(ts)


# ── stdin ─────────────────────────────────────────────────────────────────────

def read_stdin_json() -> dict[str, Any]:
    """Read and parse the hook's JSON payload from stdin. Never raises."""
    if sys.stdin.isatty():
        return {}
    import select as _select
    try:
        ready, _, _ = _select.select([sys.stdin], [], [], STDIN_SELECT_TIMEOUT_SECONDS)
        if not ready:
            return {}
    except (io.UnsupportedOperation, ValueError):
        pass
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── session / payload utilities ───────────────────────────────────────────────

def session_id_of(payload: dict[str, Any]) -> str:
    """Best-effort session identifier so events can be grouped per session."""
    for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    env_id = os.environ.get("CURSOR_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    return env_id or str(uuid4())


def subagent_id_of(payload: dict[str, Any]) -> str | None:
    """Best-effort subagent identifier, checked in a fixed key-priority order.

    Returns:
    - str: the subagent ID if found in payload
    - None: only for subagent-context events with missing ID (caller must emit extraction_fallback)
    - "n/a": for non-subagent events (handled at call site based on event type)
    """
    _SUBAGENT_KEYS = ("agent_id", "subagent_id", "agentId", "subagentId")
    for key in _SUBAGENT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if not any(k in payload for k in _SUBAGENT_KEYS):
        return "n/a"
    return None


def hash_target(value: str | None) -> str | None:
    """One-way, truncated hash so repeats can be matched without storing raw paths."""
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:TARGET_HASH_HEX_CHARS]
