#!/usr/bin/env python3
"""Layer 1 payload boundary — normalize raw hook payloads into a typed dataclass.

All raw dict access on the incoming hook payload MUST happen here.
Callers receive a NormalizedPayload and never touch the raw dict directly.

TICKET-016: centralize extraction logic previously scattered across
check_routing.py (_shell_command, _is_claude_payload, _mutation_target_file, etc.)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from _config import _warn


@dataclass(frozen=True)
class NormalizedPayload:
    """Typed, normalized view of a raw hook payload dict.

    Fields are extracted once at boundary entry; downstream code uses
    attribute access instead of repeated raw dict lookups.
    """

    session_id: str
    subagent_id: str | None
    model: str | None
    tool_input: dict[str, Any] | None
    file_path: str | None
    hook_event_name: str | None
    cursor_version: str | None
    command: str | None
    # Extended fields — added for V-3 full migration
    prompt_text: str           # from prompt / text / message (UserPromptSubmit)
    assistant_text: str        # from text / last_assistant_message / thought / content
    tool_name: str | None      # from tool_name / tool / toolName
    turn_id: str | None        # host-aware: prompt_id (Claude) or generation_id (Cursor)
    conversation_key: str      # parent_conversation_id > conversation_id > session_id > env
    raw: dict[str, Any]


# Stateless: no caching, fresh extraction per call, thread-safe.
def normalize_payload(raw: dict[str, Any]) -> NormalizedPayload:
    """Extract and normalize all fields from a raw hook payload.

    This is the single point of raw dict access for incoming payloads.
    Failures are warn-logged; callers never see KeyError/TypeError.
    """
    return NormalizedPayload(
        session_id=_extract_session_id(raw),
        subagent_id=_extract_subagent_id(raw),
        model=_extract_model(raw),
        tool_input=_extract_tool_input(raw),
        file_path=_extract_file_path(raw),
        hook_event_name=raw.get("hook_event_name"),
        cursor_version=raw.get("cursor_version"),
        command=_extract_command(raw),
        prompt_text=_extract_prompt_text(raw),
        assistant_text=_extract_assistant_text(raw),
        tool_name=_extract_tool_name(raw),
        turn_id=_extract_turn_id(raw),
        conversation_key=_extract_conversation_key(raw),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Internal extractors
# ---------------------------------------------------------------------------

def _extract_session_id(raw: dict[str, Any]) -> str:
    """Mirrors session_id_of() in _session.py — kept in sync manually.

    Key priority: session_id > sessionId > conversation_id > conversationId
    then env-var fallback: CURSOR_SESSION_ID > CLAUDE_SESSION_ID > unknown-session.
    """
    for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
        try:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        except Exception:  # noqa: BLE001
            _warn("session_id_extraction_error", {"key": key})
    try:
        env_id = os.environ.get("CURSOR_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
        if env_id:
            return env_id
    except Exception:  # noqa: BLE001
        _warn("session_id_env_fallback_error", {})
    return "unknown-session"


def _extract_subagent_id(raw: dict[str, Any]) -> str | None:
    """First non-empty string among agent_id, subagent_id, agentId, subagentId."""
    for key in ("agent_id", "subagent_id", "agentId", "subagentId"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_model(raw: dict[str, Any]) -> str | None:
    """Extract model name from model, model_name, or modelName keys."""
    for key in ("model", "model_name", "modelName"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_tool_input(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Extract tool_input dict; returns None if absent or not a dict."""
    for key in ("tool_input", "toolInput"):
        value = raw.get(key)
        if isinstance(value, dict):
            return value
    return None


def _extract_file_path(raw: dict[str, Any]) -> str | None:
    """Check tool_input.{path,file_path,target_file} then top-level path/file_path."""
    tool_input = _extract_tool_input(raw)
    if tool_input:
        for key in ("path", "file_path", "target_file"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("path", "file_path"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_command(raw: dict[str, Any]) -> str | None:
    """Extract shell command from tool_input.command."""
    tool_input = _extract_tool_input(raw)
    if tool_input:
        value = tool_input.get("command")
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else None
    return None


def _extract_prompt_text(raw: dict[str, Any]) -> str:
    """Extract user prompt text from prompt / text / message."""
    for key in ("prompt", "text", "message"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return ""


def _extract_assistant_text(raw: dict[str, Any]) -> str:
    """Extract assistant response text from text / last_assistant_message / thought / content."""
    for key in ("text", "last_assistant_message", "thought", "content"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return ""


def _extract_tool_name(raw: dict[str, Any]) -> str | None:
    """Extract tool name from tool_name / tool / toolName."""
    for key in ("tool_name", "tool", "toolName"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_turn_id(raw: dict[str, Any]) -> str | None:
    """Extract host-aware per-turn identifier.

    Cursor: generation_id / generationId tried first.
    Claude Code: prompt_id / promptId tried first.
    Platform detected via cursor_version presence (exact field check).
    hook_event_name is NOT used for detection — substring matching on its
    value is fragile and can misidentify Claude Code hooks whose names
    happen to contain 'cursor'.

    TICKET-052: Detects cross-contaminated payloads where both prompt_id and
    generation_id are present and emits a payload_ambiguous event.
    """
    is_cursor = bool(raw.get("cursor_version"))

    # TICKET-052: Detect ambiguous payloads with both key families present
    has_prompt_id = any(raw.get(k) for k in ("prompt_id", "promptId"))
    has_generation_id = any(raw.get(k) for k in ("generation_id", "generationId"))
    if has_prompt_id and has_generation_id:
        try:
            from _common import append_event
            append_event({
                "type": "payload_ambiguous",
                "reason": "both_platform_key_families_present",
                "prompt_id_present": has_prompt_id,
                "generation_id_present": has_generation_id,
            })
        except Exception:  # noqa: BLE001
            _warn("payload_ambiguous_event_failed", {})

    if is_cursor:
        keys = ("generation_id", "generationId", "prompt_id", "promptId")
    else:
        keys = ("prompt_id", "promptId", "generation_id", "generationId")
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_conversation_key(raw: dict[str, Any]) -> str:
    """Extract stable conversation identifier for correlating sub-agent lifecycle events."""
    for key in (
        "parent_conversation_id",
        "conversation_id",
        "conversationId",
        "session_id",
        "sessionId",
    ):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    env_id = os.environ.get("CURSOR_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    return env_id or "unknown-session"
