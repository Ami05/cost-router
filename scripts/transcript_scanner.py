#!/usr/bin/env python3
"""Transcript scanning utilities — pure JSONL parsing, no state I/O.

Extracted from check_routing.py. All callers in check_routing.py use
re-export shims; this module can also be imported directly.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import TRANSCRIPT_TAIL_BYTES  # noqa: E402

# Require a real role token after `Routing:` — mirrors check_routing.ROUTING_VERDICT_RE.
ROUTING_VERDICT_RE = re.compile(
    r"(?i:routing:)\s*(?P<role>DIRECT|FO|FW|RB|FV|YIELD)\b", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Transcript path resolution
# ---------------------------------------------------------------------------

def _transcript_path(payload: dict[str, Any]) -> str | None:
    """Locate the current conversation transcript.

    Both hosts expose it directly: Cursor as a common `transcript_path` field
    (null when transcripts are disabled) plus the `CURSOR_TRANSCRIPT_PATH` env
    var; Claude Code as `transcript_path` in the PreToolUse payload. As a last
    resort we reconstruct Claude Code's project path from cwd + session_id.
    """
    for key in ("transcript_path", "transcriptPath", "transcript"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    env_path = os.environ.get("CURSOR_TRANSCRIPT_PATH")
    if env_path:
        return env_path
    cwd = payload.get("cwd")
    if not cwd:
        cwd = os.getcwd()
    session_id = None
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            session_id = value
            break
    if not session_id:
        session_id = os.environ.get("CLAUDE_SESSION_ID")
    if isinstance(cwd, str) and cwd and session_id:
        cwd_hash = cwd.replace("/", "-")
        candidate = Path.home() / ".claude" / "projects" / cwd_hash / f"{session_id}.jsonl"
        return str(candidate)
    return None


def _read_transcript_tail(path: Path) -> list[str]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > TRANSCRIPT_TAIL_BYTES:
            handle.seek(-TRANSCRIPT_TAIL_BYTES, 2)
            data = handle.read()
            newline = data.find(b"\n")  # drop the (likely partial) first line
            if newline != -1:
                data = data[newline + 1:]
        else:
            data = handle.read()
    return data.decode("utf-8", "replace").splitlines()


# ---------------------------------------------------------------------------
# JSONL record field accessors
# ---------------------------------------------------------------------------

def _record_role(record: dict[str, Any]) -> str:
    message = record.get("message")
    if isinstance(message, dict) and isinstance(message.get("role"), str):
        return message["role"]
    if isinstance(record.get("role"), str):
        return record["role"]
    if isinstance(record.get("type"), str):
        return record["type"]
    return ""


def _record_content(record: dict[str, Any]) -> Any:
    message = record.get("message")
    if isinstance(message, dict) and "content" in message:
        return message["content"]
    return record.get("content")


def _record_is_meta(record: dict[str, Any]) -> bool:
    if record.get("isMeta"):
        return True
    message = record.get("message")
    if isinstance(message, dict) and message.get("isMeta"):
        return True
    return False


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type")
                if block_type in ("text", "thinking") or "text" in block:
                    value = block.get("text") or block.get("thinking") or ""
                    if isinstance(value, str):
                        parts.append(value)
                # PROD-11 known limitation: an encrypted/redacted thinking
                # block carries no "text"/"thinking" string field at all, so
                # a `Routing:` verdict placed only there is invisible to the
                # gate — this is why the model-router rule requires the
                # verdict in visible assistant text, never thinking-only.
        return "\n".join(parts)
    return ""


def _is_genuine_user_prompt(record: dict[str, Any]) -> bool:
    """A real human turn boundary — not a tool_result, meta, or command wrapper.

    Distinguishing these is what preserves the re-classify-every-turn guarantee:
    scoping to text *after* the last genuine prompt excludes prior turns without
    excluding the current turn's own assistant messages (which are interleaved
    with tool_result "user" records)."""
    if _record_role(record) != "user":
        return False
    if _record_is_meta(record):
        return False
    content = _record_content(record)
    if isinstance(content, str):
        stripped = content.strip()
        return bool(stripped) and not stripped.startswith(("<command-", "<local-command"))
    if isinstance(content, list):
        has_text = False
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "tool_result":
                    return False
                if block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text and not text.startswith(("<command-", "<local-command")):
                        has_text = True
            elif isinstance(block, str) and block.strip():
                has_text = True
        return has_text
    return False


# ---------------------------------------------------------------------------
# Role extraction
# ---------------------------------------------------------------------------

def _extract_role(text: str) -> str | None:
    """The GOVERNING role in `text`: the LAST `Routing:` match, not the
    first. PROD-20 (break-014): a decoy verdict earlier in the same text
    (e.g. quoted prose reading '"Routing: DIRECT" was the old convention')
    must not shadow the model's real, final verdict later in the same
    message — `re.search` returns the first match, which is backward here."""
    role = None
    for match in ROUTING_VERDICT_RE.finditer(text):
        role = match.group("role").upper()
    return role


def _scan_role_backward(lines: list[str], agent_id: str | None = None) -> str | None:
    """Same traversal as `_scan_lines_backward`, but returns the claimed role
    text instead of a bool — used only by the PROD-18 large-Write-as-DIRECT
    check, which needs to know *which* role was claimed, not just that one
    was. Returns None both when no routing line is found and when the
    current turn's boundary can't be located; callers must treat both the
    same way here (skip the check) since this is an advisory nudge, not a
    correctness gate.

    When *agent_id* is provided, only assistant records whose ``agentId``
    matches are considered — this prevents a parent's ``Routing: FO`` from
    shadowing a child agent's ``Routing: DIRECT``.  Safety cap: at most 20
    JSONL lines are parsed to bound cost."""
    parsed = 0
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        parsed += 1
        if parsed > 20:
            break
        if not isinstance(record, dict):
            continue
        if _is_genuine_user_prompt(record):
            return None
        if _record_role(record) == "assistant":
            if agent_id and record.get("agentId") != agent_id:
                continue
            text = _text_from_content(_record_content(record))
            if text:
                role = _extract_role(text)
                if role:
                    return role
    return None


# ---------------------------------------------------------------------------
# Token usage readers
# ---------------------------------------------------------------------------

def _read_subagent_transcript_usage(agent_transcript_path: str) -> dict[str, int] | None:
    """Sum real token usage from a subagent's own transcript (see PROD-15/PROD-6).

    Confirmed live 2026-08-01: Claude Code's `SubagentStop` payload carries no
    inline usage/total_usage/token_usage field at all, but it does carry
    `agent_transcript_path` — a transcript scoped to the subagent, in the same
    JSONL shape as the parent's own transcript (see
    `session_summary._read_project_usage`). Each assistant-turn entry's
    `message.usage` is per-turn (not cumulative), so summing across all
    entries gives the subagent's total — same logic as the parent-session
    reader, applied to the subagent's own file instead.
    """
    if not agent_transcript_path:
        return None
    path = Path(agent_transcript_path)
    if not path.is_file():
        return None
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    found = False
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            usage = entry.get("message", {}).get("usage")
            if not isinstance(usage, dict) or "output_tokens" not in usage:
                continue
            found = True
            for key in totals:
                totals[key] += usage.get(key, 0)
    except OSError:
        return None
    return totals if found else None


def _read_session_output_tokens(transcript_path: str) -> int:
    """Sum output_tokens from the parent session transcript JSONL.

    Uses the same per-turn accumulation as _read_subagent_transcript_usage.
    Returns 0 if the file is missing or contains no usage entries.
    """
    if not transcript_path:
        return 0
    path = Path(transcript_path)
    if not path.is_file():
        return 0
    total = 0
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            usage = entry.get("message", {}).get("usage")
            if not isinstance(usage, dict):
                continue
            total += usage.get("output_tokens", 0)
    except OSError:
        return 0
    return total


def _read_live_context_tokens(transcript_path: str, max_bytes: int | None = None) -> dict[str, Any] | None:
    """RC-7: live main-session context size from the last main-session assistant record.

    Takes the last record with type == "assistant", falsy isSidechain and no
    agentId key; records sharing a message.id are deduped (last wins).
    tokens = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
    (output_tokens excluded; not summed across records).
    max_bytes: when set, only the last max_bytes of the file are parsed (the
    partial first line is dropped) so per-tool-call callers stay O(1).
    Returns {"tokens": int, "model": str | None} or None on any failure.
    """
    try:
        if not transcript_path:
            return None
        path = Path(transcript_path)
        if not path.is_file():
            return None
        by_id: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        if max_bytes and path.stat().st_size > max_bytes:
            with path.open("rb") as fh:
                fh.seek(-max_bytes, 2)
                lines = fh.read().decode("utf-8", errors="replace").splitlines()[1:]
        else:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for n, raw in enumerate(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            if entry.get("isSidechain") or "agentId" in entry:
                continue
            msg = entry.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            key = msg.get("id") or f"_noid_{n}"
            if key in by_id:
                order.remove(key)
            by_id[key] = msg
            order.append(key)
        if not order:
            return None
        msg = by_id[order[-1]]
        usage = msg["usage"]
        tokens = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        model = msg.get("model")
        return {"tokens": tokens, "model": model if isinstance(model, str) else None}
    except Exception:  # noqa: BLE001
        return None
