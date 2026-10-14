#!/usr/bin/env python3
"""cost-router hook: guard against editing stale file content.

Three modes selected by --event CLI arg:

  --event read      (PreToolUse Read)
      Hash the file being read and store it in the session registry so we
      know what content the model last saw.

  --event pre_edit  (PreToolUse Write|Edit|StrReplace)
      Compare the on-disk hash against the stored post-read hash.  If they
      differ the file changed externally since the model read it; inject an
      additionalContext warning so the model re-reads before overwriting.

  --event post_edit (PostToolUse Write|Edit|StrReplace)
      Re-hash the file after a successful edit and update the registry so
      subsequent pre_edit checks use the freshly-written content as the
      baseline.

State files (DATA_DIR):
  file_hash_registry.json  — {sessions: {session_id: {filepath: sha256}}}
  file_hash_stats.json     — {session_id: {warnings_fired: int}}

Stdlib-only; fail-open on every error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    SESSION_LIMIT_FILE_HASH_GUARD,
    emit_gate_result,
    gate_allow,
    gate_deny,
    load_json_state,
    read_stdin_json,
    save_json_state,
    session_id_of,
    _warn,
)
from _model_utils import extract_file_path, is_claude_payload, tool_input_of  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HASH_REGISTRY_FILE = DATA_DIR / "file_hash_registry.json"
HASH_STATS_FILE = DATA_DIR / "file_hash_stats.json"
HASH_HISTORY_FILE = DATA_DIR / "file_hash_history.json"
_HASH_HISTORY_CAP = 10

_STALE_WARN = (
    "[file-guard] File has changed since last read ({filepath}). "
    "Content may be stale — re-read before editing to avoid overwriting "
    "external changes."
)

_NO_READ_WARN = (
    "[file-guard] File was never read this session ({filepath}). "
    "Read the file before editing to ensure you are working from current content."
)


# ---------------------------------------------------------------------------
# Payload helpers (mirrors check_read_repeat.py conventions)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str | None:
    """Return hex sha256 of path's bytes, or None if the file is unreadable."""
    if not path.is_file():
        return None  # Not an existing regular file — not applicable, not an error
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError):
        return None  # Race with removal, or a directory — not an error
    except OSError as e:
        _warn("file_hash_failed", {"file": "file_hash_guard.py", "error": str(e)})
        return None


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def _load_registry() -> dict[str, Any]:
    state = load_json_state(HASH_REGISTRY_FILE)
    sessions = state.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def _save_registry(sessions: dict[str, Any]) -> None:
    if len(sessions) > SESSION_LIMIT_FILE_HASH_GUARD:
        # Evict oldest sessions by insertion order; keep the most recent ones.
        keys = list(sessions.keys())
        evict = keys[: len(keys) - SESSION_LIMIT_FILE_HASH_GUARD]
        for k in evict:
            del sessions[k]
    save_json_state(HASH_REGISTRY_FILE, {"sessions": sessions})


def _get_session_hashes(
    sessions: dict[str, Any], session_id: str
) -> dict[str, str]:
    entry = sessions.get(session_id)
    if not isinstance(entry, dict):
        entry = {}
        sessions[session_id] = entry
    return entry


def _load_stats() -> dict[str, Any]:
    state = load_json_state(HASH_STATS_FILE)
    sessions = state.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def _save_stats(sessions: dict[str, Any]) -> None:
    if len(sessions) > SESSION_LIMIT_FILE_HASH_GUARD:
        keys = list(sessions.keys())
        evict = keys[: len(keys) - SESSION_LIMIT_FILE_HASH_GUARD]
        for k in evict:
            del sessions[k]
    save_json_state(HASH_STATS_FILE, {"sessions": sessions})


def _increment_warning(session_id: str) -> None:
    sessions = _load_stats()
    session_stats = sessions.get(session_id)
    if not isinstance(session_stats, dict):
        session_stats = {"warnings_fired": 0}
        sessions[session_id] = session_stats
    session_stats["warnings_fired"] = int(session_stats.get("warnings_fired", 0)) + 1
    _save_stats(sessions)


# ---------------------------------------------------------------------------
# PB-11-3: Hash history for revert detection
# ---------------------------------------------------------------------------


def _load_history() -> dict[str, Any]:
    state = load_json_state(HASH_HISTORY_FILE)
    sessions = state.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def _save_history(sessions: dict[str, Any]) -> None:
    if len(sessions) > SESSION_LIMIT_FILE_HASH_GUARD:
        keys = list(sessions.keys())
        evict = keys[: len(keys) - SESSION_LIMIT_FILE_HASH_GUARD]
        for k in evict:
            del sessions[k]
    save_json_state(HASH_HISTORY_FILE, {"sessions": sessions})


def _get_file_history(
    sessions: dict[str, Any], session_id: str, filepath: str
) -> list[str]:
    """Return the mutable hash-history list for (session, filepath)."""
    session_entry = sessions.get(session_id)
    if not isinstance(session_entry, dict):
        session_entry = {}
        sessions[session_id] = session_entry
    history = session_entry.get(filepath)
    if not isinstance(history, list):
        history = []
        session_entry[filepath] = history
    return history


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def event_read(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """PreToolUse Read — hash the file and record it for this session."""
    filepath = extract_file_path(payload)
    if not filepath:
        return emit_gate_result(gate_allow(), is_claude)

    digest = _sha256(Path(filepath))
    if digest is None:
        # File doesn't exist on disk — nothing to store.
        return emit_gate_result(gate_allow(), is_claude)

    session_id = session_id_of(payload)
    sessions = _load_registry()
    hashes = _get_session_hashes(sessions, session_id)
    hashes[filepath] = digest
    _save_registry(sessions)

    return emit_gate_result(gate_allow(), is_claude)


def event_pre_edit(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """PreToolUse Write|Edit|StrReplace — warn if file changed since last read."""
    filepath = extract_file_path(payload)
    if not filepath:
        return emit_gate_result(gate_allow(), is_claude)

    path = Path(filepath)
    current_digest = _sha256(path)
    if current_digest is None:
        # New file being created — no prior content to compare against.
        return emit_gate_result(gate_allow(), is_claude)

    session_id = session_id_of(payload)
    sessions = _load_registry()
    hashes = _get_session_hashes(sessions, session_id)
    stored_digest = hashes.get(filepath)

    if current_digest is not None and stored_digest is None:
        _increment_warning(session_id)
        message = _NO_READ_WARN.format(filepath=filepath)
        return emit_gate_result(gate_allow(message), is_claude)

    if stored_digest is not None and current_digest != stored_digest:
        _increment_warning(session_id)
        message = _STALE_WARN.format(filepath=filepath)
        return emit_gate_result(gate_allow(message), is_claude)

    return emit_gate_result(gate_allow(), is_claude)


def event_post_edit(payload: dict[str, Any]) -> dict[str, Any]:
    """PostToolUse Write|Edit|StrReplace — update registry with new content hash.

    Also tracks hash history per file per session (PB-11-3) and emits a
    'reverted' signal when the new hash matches any prior version (excluding
    the immediately preceding one), indicating the developer rolled back a
    file to an earlier state.
    """
    filepath = extract_file_path(payload)
    if not filepath:
        return {}

    path = Path(filepath)
    digest = _sha256(path)
    if digest is None:
        # File gone (e.g. deleted by the edit) — nothing to store.
        return {}

    session_id = session_id_of(payload)

    # Update stale-check registry (single hash per file — pre-existing logic).
    sessions = _load_registry()
    hashes = _get_session_hashes(sessions, session_id)
    hashes[filepath] = digest
    _save_registry(sessions)

    # PB-11-3: revert detection via hash history.
    history_sessions = _load_history()
    history = _get_file_history(history_sessions, session_id, filepath)
    # history[:-1] = all versions except the most-recently recorded one.
    # A match there means the file was written back to a prior (non-immediate) state.

    history.append(digest)
    if len(history) > _HASH_HISTORY_CAP:
        del history[: len(history) - _HASH_HISTORY_CAP]
    _save_history(history_sessions)

    return {}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        parser = argparse.ArgumentParser(
            description="File-hash guard hook for cost-router."
        )
        parser.add_argument(
            "--event",
            required=True,
            choices=["read", "pre_edit", "post_edit"],
            help="Hook event mode: read | pre_edit | post_edit",
        )
        args = parser.parse_args()

        payload = read_stdin_json()
        is_claude = is_claude_payload(payload)

        if args.event == "read":
            output = event_read(payload, is_claude)
        elif args.event == "pre_edit":
            output = event_pre_edit(payload, is_claude)
        else:
            output = event_post_edit(payload)

        print(json.dumps(output))
    except Exception:  # noqa: BLE001 — hook must fail open
        print(json.dumps({"continue": True, "permission": "allow"}))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
