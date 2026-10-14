#!/usr/bin/env python3
"""cost-router: spawn tracking — event recording, stats integrity, agent spawn history.

Extracted from agent_call_tracker.py. Owns STATS_FILE read/write paths,
the fcntl-guarded merge-write, integrity checksums, and finalize_dispatch_charge.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    DISPATCH_REGISTRY_FILE,
    SESSION_LIMIT_AGENT_CALL_TRACKER,
    append_event,
    config_get,
    config_int,
    load_json_state,
    now_iso,
    session_id_of,
)
from _model_utils import est_cold_write_cost_usd  # noqa: E402

STATS_FILE = DATA_DIR / "agent_call_stats.json"


def _stats_checksum(sessions: dict[str, Any]) -> str:
    """GATE-BUG-006: checksum over STATS_FILE's sessions map, stored alongside
    it as "_checksum" so an out-of-band edit (e.g. a hand-reset dispatch_count)
    is detectable on the next read instead of being silently trusted."""
    return hashlib.sha256(
        json.dumps(sessions, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def check_stats_integrity() -> bool:
    """Return False only if STATS_FILE has a "_checksum" field that does not
    match its "sessions" contents -- i.e. it was edited by something other
    than this module's own write paths. A missing file, empty file, or a file
    with no "_checksum" (written before this check existed) is treated as
    valid: only a checksum that is PRESENT and WRONG indicates tampering.
    Fails open (True) on any read error, matching the rest of this file."""
    try:
        if not STATS_FILE.exists():
            return True
        raw = load_json_state(STATS_FILE)
        if not isinstance(raw, dict) or not raw:
            return True
        checksum = raw.get("_checksum")
        if not checksum:
            return True
        sessions = raw.get("sessions", {})
        if not isinstance(sessions, dict):
            sessions = {}
        return checksum == _stats_checksum(sessions)
    except OSError:
        return True


# fcntl locks are owned per-process, so two threads within the same process
# both acquire the same fcntl lock without contention and can still race the
# read-merge-write. This threading.Lock guards intra-process concurrency;
# it is acquired outermost (before the fcntl lock) and released last (after
# the fcntl lock), so cross-process safety via fcntl is unchanged.
_STATS_THREAD_LOCK = threading.Lock()


def _session_last_activity_ts(session_data: dict[str, Any]) -> float:
    """TICKET-01: epoch timestamp of a session's last recorded activity, or 0.0.

    0.0 (missing/unparseable last_activity) sorts as oldest and is always
    eligible for TTL pruning - correct behavior for legacy entries predating
    this field.
    """
    raw = session_data.get("last_activity") if isinstance(session_data, dict) else None
    if raw:
        try:
            return datetime.fromisoformat(str(raw)).timestamp()
        except (ValueError, TypeError):
            pass
    return 0.0


def _load_session_stats(session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    all_sessions = load_json_state(STATS_FILE).get("sessions", {})
    if not isinstance(all_sessions, dict):
        all_sessions = {}
    this_session = all_sessions.get(session_id)
    if not isinstance(this_session, dict):
        this_session = {
            "agents": [],
            "explore_without_haiku": 0,
            "total_agents": 0,
            "dispatch_count": 0,
            "dispatch_attempts_count": 0,
            "explore_files_covered": {},
        }
        all_sessions[session_id] = this_session
    this_session.setdefault("agents", [])
    this_session.setdefault("explore_without_haiku", 0)
    this_session.setdefault("total_agents", 0)
    this_session.setdefault("dispatch_count", 0)
    this_session.setdefault("dispatch_attempts_count", 0)
    this_session.setdefault("cumulative_cost_usd", 0.0)
    this_session.setdefault("cost_warnings_emitted", [])
    this_session.setdefault("explore_files_covered", {})
    # TICKET-01: recency tracking
    this_session.setdefault("last_activity", now_iso())
    return all_sessions, this_session


def _save_stats(sessions: dict[str, Any]) -> None:
    """Locked read-merge-write of *sessions* into STATS_FILE.

    Re-reads the on-disk sessions map under an exclusive lock and merges the
    caller's sessions over it (caller's entries win per session id) before
    applying TTL prune / count-cap eviction and writing back. This prevents an
    unlocked full-map write from clobbering another concurrent hook
    invocation's counter updates to a *different* session id.

    Fails open (no-op) on OSError, matching the rest of this file.
    """
    try:
        with _STATS_THREAD_LOCK:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with STATS_FILE.open("a+b") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    handle.seek(0)
                    content = handle.read().decode("utf-8", errors="replace")
                    try:
                        on_disk = json.loads(content) if content.strip() else {}
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        on_disk = {}

                    if not isinstance(on_disk, dict):
                        on_disk = {}
                    on_disk_sessions = on_disk.get("sessions", {})
                    if not isinstance(on_disk_sessions, dict):
                        on_disk_sessions = {}

                    merged = dict(on_disk_sessions)
                    merged.update(sessions)

                    # finalize_dispatch_charge is the sole cumulative_cost_usd
                    # writer on Claude Code; a caller holding a stale whole-session
                    # dict (e.g. event_preuse) must not roll those fields back.
                    # Cost only grows, so keep the max; charged ids are a union.
                    for _sid, _caller in sessions.items():
                        _disk = on_disk_sessions.get(_sid)
                        if not isinstance(_disk, dict) or not isinstance(_caller, dict):
                            continue
                        try:
                            _d_cost = float(_disk.get("cumulative_cost_usd", 0.0) or 0.0)
                            _c_cost = float(_caller.get("cumulative_cost_usd", 0.0) or 0.0)
                            if _d_cost > _c_cost:
                                _caller["cumulative_cost_usd"] = _d_cost
                            _d_ids = _disk.get("charged_agent_ids")
                            if isinstance(_d_ids, list) and _d_ids:
                                _c_ids = _caller.get("charged_agent_ids")
                                _c_ids = list(_c_ids) if isinstance(_c_ids, list) else []
                                _union = _c_ids + [i for i in _d_ids if i not in _c_ids]
                                _caller["charged_agent_ids"] = _union[-200:]
                        except (TypeError, ValueError):
                            pass

                    # TICKET-01: idle-TTL prune before the count-based cap, so a stale
                    # session (e.g. an exhausted counter nobody ever revisits) doesn't
                    # linger forever and doesn't crowd out genuinely recent sessions in
                    # the count-based cap.
                    ttl_days = config_int("session_limits.agent_call_tracker_ttl_days", 14)
                    if ttl_days > 0:
                        cutoff = datetime.now(tz=timezone.utc).timestamp() - ttl_days * 86400
                        _ttl_dropped = [
                            sid for sid, data in merged.items()
                            if _session_last_activity_ts(data) < cutoff
                        ]
                        merged = {
                            sid: data for sid, data in merged.items()
                            if _session_last_activity_ts(data) >= cutoff
                        }
                        for _sid in _ttl_dropped:
                            try:
                                append_event({"type": "session_pruned", "session_id": _sid, "reason": "ttl"})
                            except OSError:
                                pass
                    if len(merged) > SESSION_LIMIT_AGENT_CALL_TRACKER:
                        # TICKET-01: recency-based eviction, not alphabetical session_id sort.
                        ordered = sorted(
                            merged.items(),
                            key=lambda item: _session_last_activity_ts(item[1]),
                        )
                        _cap_dropped = [sid for sid, _data in ordered[:-SESSION_LIMIT_AGENT_CALL_TRACKER]]
                        merged = dict(ordered[-SESSION_LIMIT_AGENT_CALL_TRACKER:])
                        for _sid in _cap_dropped:
                            try:
                                append_event({"type": "session_pruned", "session_id": _sid, "reason": "count_cap"})
                            except OSError:
                                pass

                    handle.seek(0)
                    handle.truncate()
                    handle.write(
                        json.dumps(
                            {"sessions": merged, "_checksum": _stats_checksum(merged)},
                            indent=2,
                        ).encode("utf-8")
                    )
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return


def finalize_dispatch_charge(payload: dict[str, Any], is_claude: bool) -> None:  # noqa: ARG001
    """Increment cumulative_cost_usd on SubagentStop for successful dispatches.

    RT-01-B: charges are recorded only on success (SubagentStop) so aborted or
    denied dispatches are not double-counted.  The model is read from the
    dispatch_registry entry written at PreToolUse time — not re-derived from the
    Stop payload, which carries no inline model field on Claude Code
    (confirmed live 2026-07-31; see knowledge/host-payloads.md).

    Fails open on every error path so a registry miss or I/O fault never blocks
    the SubagentStop hook.
    """
    try:
        session_id = session_id_of(payload)
        if not session_id:
            return

        # Resolve agent_id using the same key-priority order as subagent_id_of()
        # in _common.py (L353) — inlined to avoid an import-cycle risk.
        agent_id: str | None = None
        for _key in ("agent_id", "subagent_id", "agentId", "subagentId"):
            _val = payload.get(_key)
            if isinstance(_val, str) and _val:
                agent_id = _val
                break
        if not agent_id:
            return

        # Look up the dispatch_registry entry written at SubagentStart time.
        try:
            registry = load_json_state(DISPATCH_REGISTRY_FILE)
            worker_entry = (
                registry.get("sessions", {})
                .get(session_id, {})
                .get("agents", {})
                .get(agent_id)
            )
        except Exception:  # noqa: BLE001
            worker_entry = None

        # F6: stop-only ids (no dispatch metadata) were never dispatched
        # subagents; charge nothing.
        if not (
            isinstance(worker_entry, dict)
            and (worker_entry.get("started_at") or worker_entry.get("subagent_type"))
        ):
            return
        model: str | None = worker_entry.get("model")
        if not model:
            # F7: registry model should be set from the PreToolUse-queued
            # request; fall back to the default rate but make it visible.
            try:
                append_event({
                    "type": "model_unresolved",
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "source": "finalize_dispatch_charge",
                })
            except OSError:
                pass

        dispatch_cost = est_cold_write_cost_usd(model if model else None)
        if not dispatch_cost:
            return

        # Use _save_stats' locked merge-write path so we don't need to
        # re-implement the fcntl TOCTOU guard here.  Load the current session,
        # bump cumulative_cost_usd, then persist via the existing helper.
        all_sessions = load_json_state(STATS_FILE).get("sessions", {})
        if not isinstance(all_sessions, dict):
            all_sessions = {}
        this_session = all_sessions.get(session_id)
        if not isinstance(this_session, dict):
            this_session = {
                "agents": [],
                "explore_without_haiku": 0,
                "total_agents": 0,
                "dispatch_count": 0,
                "cumulative_cost_usd": 0.0,
                "last_activity": now_iso(),
            }
        # At most once per agent_id (bounded to the last 200 ids).
        charged = this_session.get("charged_agent_ids")
        if not isinstance(charged, list):
            charged = []
        if agent_id in charged:
            return
        charged.append(agent_id)
        this_session["charged_agent_ids"] = charged[-200:]
        this_session.setdefault("cumulative_cost_usd", 0.0)
        this_session["cumulative_cost_usd"] = round(
            this_session["cumulative_cost_usd"] + dispatch_cost, 6
        )
        all_sessions[session_id] = this_session
        _save_stats(all_sessions)
    except Exception:  # noqa: BLE001 — hook must fail open
        pass
