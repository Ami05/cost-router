#!/usr/bin/env python3
"""cost-router hook: main-session context-size warning at 160k and 180k tokens.

UserPromptSubmit hook. Fires after classify_intent.py.
Reads live context tokens from the transcript (RC-7) and injects additionalContext
when context_pressure.hook_mode == 'warn' and live tokens cross thresholds
(160k by default, 180k urgent). Persists warning state per session so the warning
re-arms after /compact. Never denies — always allows. Fails open on any error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR, config_get, config_int  # noqa: E402
from _fileio import load_json_state, save_json_state  # noqa: E402
from _session import read_stdin_json, session_id_of  # noqa: E402
from transcript_scanner import _read_live_context_tokens  # noqa: E402

# ── state file ─────────────────────────────────────────────────────────────────

_STATE_FILE = DATA_DIR / "context_warn_state.json"

# ── subagent detection ─────────────────────────────────────────────────────────

_SUBAGENT_KEYS = ("agent_id", "subagent_id", "agentId", "subagentId")

# ── warning messages ───────────────────────────────────────────────────────────

_WARN_MSG = """CONTEXT WARNING: {k}k tokens

Your session is approaching the context window limit. Consider using /compact to trim transcript history, or dispatch further work to a subagent to reset context on the new worker."""

_URGENT_MSG = """CONTEXT URGENT: {k}k tokens

Your session is very close to the context window limit. Please use /compact immediately to trim transcript history, or dispatch remaining work to a subagent."""


# ── threshold detection ────────────────────────────────────────────────────────

def _level(tokens: int, warn: int, urgent: int) -> int:
    """Return 2 if tokens >= urgent, 1 if >= warn, else 0."""
    if tokens >= urgent:
        return 2
    if tokens >= warn:
        return 1
    return 0


# ── main hook logic ────────────────────────────────────────────────────────────

def check(payload: dict) -> str | None:
    """Emit warning message if live context crossed a threshold (and not in subagent).

    Returns None to suppress the message or a formatted warning string.
    """
    # Subagents: never warn (they get a fresh context each dispatch)
    for key in _SUBAGENT_KEYS:
        if payload.get(key):
            return None

    # Check hook_mode config
    hook_mode = config_get("context_pressure.hook_mode", default="warn")
    if hook_mode != "warn":
        return None

    # Read thresholds from config
    warn_tokens = config_int("context_pressure.hook_warn_tokens", default=160000)
    urgent_tokens = config_int("context_pressure.hook_urgent_tokens", default=180000)
    tail_bytes = config_int("context_pressure.hook_tail_bytes", default=524288)

    # Read live context from transcript
    transcript_path = payload.get("transcript_path") or ""
    live = _read_live_context_tokens(transcript_path, max_bytes=tail_bytes or None)
    if not live:
        return None

    tokens = live.get("tokens", 0)
    level = _level(tokens, warn_tokens, urgent_tokens)

    # Load state and check if we've already warned at this level
    sid = session_id_of(payload)
    state = load_json_state(_STATE_FILE)
    last_level = int(state.get(sid, 0) or 0)

    if level == last_level:
        return None

    # Persist the new level (including 0, which re-arms after /compact)
    state[sid] = level
    save_json_state(_STATE_FILE, state)

    # Only escalations warn; a drop just re-arms
    if level < last_level:
        return None

    # Emit the appropriate message
    k = tokens // 1000
    if level == 2:
        return _URGENT_MSG.format(k=k)
    else:
        return _WARN_MSG.format(k=k)


def main() -> None:
    """Hook entry point: read payload, check, and emit hook response."""
    try:
        payload = read_stdin_json()
        if not isinstance(payload, dict):
            print("{}")
            return

        message = check(payload)
        if not message:
            print("{}")
            return

        # Detect host and format response appropriately
        is_claude = bool(payload.get("hook_event_name")) and not payload.get("cursor_version")

        if is_claude:
            # Claude Code hook format
            response = {
                "hookSpecificOutput": {
                    "hookEventName": payload.get("hook_event_name"),
                    "additionalContext": message,
                }
            }
            print(json.dumps(response))
        else:
            # Cursor hook format
            response = {
                "continue": True,
                "additional_context": message,
            }
            print(json.dumps(response))

    except Exception:  # noqa: BLE001
        # Fail open: always allow the turn to proceed
        print("{}")


if __name__ == "__main__":
    main()
