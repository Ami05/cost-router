#!/usr/bin/env python3
"""cost-router hook: nudge or deny redundant full-file Read calls in a session.

beforeReadFile / preToolUse (matcher: Read) — nudge on repeat 1; deny on
repeat 2+ of the same full reload (offset/limit reads always allowed).

State: data/read_state.json keyed by session_id. Stdlib-only; fail-open on errors.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any



sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    DENY_AFTER_REPEAT_COUNT,
    READ_REPEAT_MAX_TRACKED_SESSIONS,
    emit_gate_result,
    gate_allow,
    gate_deny,
    load_json_state,
    read_stdin_json,
    save_json_state,
    session_id_of,
)
from _model_utils import is_claude_payload, tool_input_of  # noqa: E402
from graph_store import resolved_hash  # noqa: E402

READ_STATE_FILE = DATA_DIR / "read_state.json"
FILE_GRAPH_INDEX = DATA_DIR / "file-graphs" / "index.json"

_REPEAT_STALE_NUDGE = (
    "[cost-router] File was edited — symbol graph is being rebuilt and fresh line "
    "numbers will appear in additionalContext. Use offset/limit with those line "
    "numbers to verify your change instead of reloading the full file."
)

_REPEAT_NUDGE = (
    "[cost-router] Already read this file this session. "
    "Use offset/limit for a targeted section read. Do NOT re-read the full file. "
    "Next full re-read will be DENIED."
)

_REPEAT_DENY_FILE_GRAPH = (
    "[cost-router] Already read this file — a local symbol graph exists in "
    "data/file-graphs/. Query it instead of reloading the full file."
)

_REPEAT_DENY = (
    "[cost-router] This is repeat #{count} of a full reload of the same file this "
    "session — prior nudges were ignored. Use offset/limit for a targeted section instead."
)


def _read_target(payload: dict[str, Any]) -> tuple[str | None, bool]:
    """Return (target_hash, is_full_read)."""
    tool_input = tool_input_of(payload)
    target = None
    for key in ("path", "file_path", "target_file"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            target = value.strip()
            break

    offset = tool_input.get("offset")
    limit = tool_input.get("limit")
    is_full_read = offset is None and limit is None
    return (resolved_hash(target) if target else None), is_full_read


def _graph_is_stale(target_hash: str | None) -> bool:
    """True if build_file_graph marked this target's graph stale after an edit."""
    if not target_hash:
        return False
    graph_file = DATA_DIR / "file-graphs" / f"{target_hash}.json"
    if not graph_file.is_file():
        return False
    graph = load_json_state(graph_file)
    return bool(graph.get("stale"))


def _file_graph_exists(target_hash: str | None) -> bool:
    """True if build_file_graph.py already built a per-file graph for this target."""
    if not target_hash:
        return False
    index = load_json_state(FILE_GRAPH_INDEX)
    return target_hash in index


def _load_sessions() -> dict[str, Any]:
    state = load_json_state(READ_STATE_FILE)
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    return sessions


def _save_sessions(sessions: dict[str, Any]) -> None:
    if len(sessions) > READ_REPEAT_MAX_TRACKED_SESSIONS:
        ordered = sorted(
            sessions.items(),
            key=lambda item: (item[1].get("turn", 0), item[0]),
        )
        sessions = dict(ordered[-READ_REPEAT_MAX_TRACKED_SESSIONS:])
    save_json_state(READ_STATE_FILE, {"sessions": sessions})


def _get_reads(sessions: dict[str, Any], session_id: str) -> dict[str, int]:
    entry = sessions.get(session_id)
    if not isinstance(entry, dict):
        entry = {"reads": {}}
        sessions[session_id] = entry
    reads = entry.get("reads")
    if not isinstance(reads, dict):
        reads = {}
        entry["reads"] = reads
    return reads


def _is_subagent_context(payload: dict[str, Any]) -> bool:
    """True when this read originates from a dispatched Task worker.

    Subagents share the parent conversation_id, so their reads would
    incorrectly count against the parent's repeat counter.  Exempt them
    so the first read inside a worker is always allowed."""
    for key in ("agent_id", "agent_type", "subagent_id", "subagent_type", "parent_conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return True
    return bool(payload.get("is_parallel_worker"))


def event_read(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    if _is_subagent_context(payload):
        return emit_gate_result(gate_allow(None), is_claude)

    session_id = session_id_of(payload)
    target_hash, is_full_read = _read_target(payload)

    sessions = _load_sessions()
    reads = _get_reads(sessions, session_id)

    nudge: str | None = None
    deny_msg: str | None = None
    if target_hash and is_full_read:
        prior = int(reads.get(target_hash, 0))
        if prior > 0:
            if _graph_is_stale(target_hash):
                prior = 0
                nudge = _REPEAT_STALE_NUDGE
            elif _file_graph_exists(target_hash):
                deny_msg = _REPEAT_DENY_FILE_GRAPH
            elif prior >= DENY_AFTER_REPEAT_COUNT:
                deny_msg = _REPEAT_DENY.format(count=prior + 1)
            else:
                nudge = _REPEAT_NUDGE
        reads[target_hash] = prior + 1
        _save_sessions(sessions)
    elif target_hash:
        reads.setdefault(target_hash, 0)
        _save_sessions(sessions)

    if deny_msg:
        return emit_gate_result(gate_deny(deny_msg), is_claude)
    return emit_gate_result(gate_allow(nudge), is_claude)




def main() -> int:
    try:
        payload = read_stdin_json()
        is_claude = is_claude_payload(payload)
        output = event_read(payload, is_claude)
        print(json.dumps(output))
        if not is_claude and output.get("permission") == "deny":
            return 2
    except Exception:  # noqa: BLE001 — hook must fail open
        print(json.dumps({"permission": "allow"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
