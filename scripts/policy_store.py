#!/usr/bin/env python3
"""cost-router: Disk I/O layer for routing proposals and changelog.

All file reads/writes, versioning, and config.json mutation live here.
No business logic — callers (policy_applier) own state transitions.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR, MAX_COUNTERFACTUAL_LOG_BYTES, PLUGIN_ROOT, now_iso, save_json_state, _warn  # noqa: E402

PROPOSALS_FILE = DATA_DIR / "policy_proposals.json"
CHANGELOG_FILE = DATA_DIR / "policy_changelog.jsonl"
RULE_FILE = PLUGIN_ROOT / "rules" / "model-router.md"
COUNTERFACTUAL_LOG_FILE = DATA_DIR / "counterfactual_log.jsonl"


# ---------------------------------------------------------------------------
# Internal write helpers
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via temp file + os.replace.

    Using PID + thread-id in the temp name avoids collisions when concurrent
    hook processes (or threads within one process) write the same file
    simultaneously — same strategy as save_json_state in _common.py.
    Cleans up the temp file on any failure so stale .tmp files never linger.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Proposals I/O
# ---------------------------------------------------------------------------


def load_proposals() -> list[dict[str, Any]]:
    """Read policy_proposals.json; return empty list when absent or corrupt."""
    try:
        data = json.loads(PROPOSALS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data.get("proposals", [])
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError) as e:
        _warn("policy_data_load_failed", {"file": "policy_store.py", "error": str(e)})
        return []


def save_proposals(proposals: list[dict[str, Any]]) -> None:
    """Fails open (PROD-8): `--undo`'s CLI path calls into this without any
    caller-side try/except, unlike compact_log.py's wrapped calls — a
    read-only/full DATA_DIR must not crash it uncaught.

    Wraps the list in {"proposals": [...]} so save_json_state (which expects a
    dict) can apply its atomic temp+os.replace pattern. load_proposals already
    handles both the legacy bare-list format and this dict format.
    """
    save_json_state(PROPOSALS_FILE, {"proposals": proposals})


# ---------------------------------------------------------------------------
# Changelog I/O
# ---------------------------------------------------------------------------


def load_changelog() -> list[dict[str, Any]]:
    """Read policy_changelog.jsonl; return empty list when absent or corrupt."""
    try:
        lines = CHANGELOG_FILE.read_text(encoding="utf-8").splitlines()
        result: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict):
                    result.append(entry)
            except json.JSONDecodeError:
                continue
        return result
    except FileNotFoundError:
        return []  # Normal on first run — file created on first write
    except OSError as e:
        _warn("policy_data_load_failed", {"file": "policy_store.py", "error": str(e)})
        return []


def save_changelog(changelog: list[dict[str, Any]]) -> None:
    """Fails open (PROD-8), same reasoning as save_proposals above.
    Rewrites the full JSONL file (one JSON object per line) atomically so
    concurrent readers never see a partially-written file."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(entry) for entry in changelog]
        content = "\n".join(lines) + "\n" if lines else ""
        _atomic_write_text(CHANGELOG_FILE, content)
    except OSError as e:
        _warn("policy_save_failed", {"file": "policy_store.py", "error": str(e)})


# ---------------------------------------------------------------------------
# Rule file I/O
# ---------------------------------------------------------------------------


def read_rule_file() -> str:
    """Read model-router.md; return empty string on missing/unreadable."""
    try:
        return RULE_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_rule_file(content: str) -> None:
    """Write model-router.md atomically. Fails open (PROD-8)."""
    try:
        _atomic_write_text(RULE_FILE, content)
    except OSError as e:
        _warn("policy_apply_write_failed", {"file": "policy_store.py", "error": str(e)})


# ---------------------------------------------------------------------------
# Counterfactual log I/O
# ---------------------------------------------------------------------------


def append_counterfactual_log(
    sessions: list[dict[str, Any]],
    key: str,
    proposed_row: str,
    undo_token: str,
) -> None:
    """Append one JSONL line per session to ``counterfactual_log.jsonl``.

    ``est_counterfactual_reward`` estimates the reward WITHOUT the applied
    change (i.e. reverses the expected improvement).
    """
    # PROD-8: unlike usage.jsonl (_common.MAX_LOG_BYTES / _rotate_log_if_needed),
    # this log had no size cap — it only grows when a proposal is applied
    # (a rare, human/agent-approved event, not a per-turn one), but with no
    # rotation it would still accumulate forever on a long-lived install.
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Hold LOCK_EX across the size-check + rename + append so that two
        # concurrent hook processes cannot both observe the file over the
        # threshold and both attempt to rename it (double-rotation / data loss).
        # Opening for "a" first creates the file if absent, giving fcntl a
        # valid fd to lock before the stat check.
        with COUNTERFACTUAL_LOG_FILE.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                if (
                    COUNTERFACTUAL_LOG_FILE.is_file()
                    and COUNTERFACTUAL_LOG_FILE.stat().st_size > MAX_COUNTERFACTUAL_LOG_BYTES
                ):
                    COUNTERFACTUAL_LOG_FILE.rename(
                        COUNTERFACTUAL_LOG_FILE.with_suffix(".jsonl.1")
                    )
                    # After rename, fh's inode is now the rotated file.  Open a
                    # fresh handle pointing at the new (empty) active log file.
                    # The outer fh stays open (and locked) until this with-block
                    # exits, preventing a third process from sneaking a second
                    # rename before we finish writing.
                    write_fh = COUNTERFACTUAL_LOG_FILE.open("a", encoding="utf-8")
                else:
                    write_fh = fh

                for session in sessions:
                    actual_reward = float(session.get("reward", 0.0))
                    if "escalated" in key and "FW" in proposed_row:
                        est_cf = actual_reward - 0.2
                    elif "over_routed" in key and "RB" in proposed_row:
                        est_cf = actual_reward - 0.1
                    else:
                        est_cf = actual_reward
                    record = {
                        "session_id": session.get("session_id", ""),
                        "key": key,
                        "actual_reward": actual_reward,
                        "est_counterfactual_reward": round(est_cf, 4),
                        "applied_change": undo_token,
                    }
                    write_fh.write(json.dumps(record) + "\n")

                if write_fh is not fh:
                    write_fh.close()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass
