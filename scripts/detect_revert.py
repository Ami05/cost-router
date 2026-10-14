#!/usr/bin/env python3
"""cost-router hook: detect git reverts as quality-failure reward signal (PA-1).

Runs at PostToolUse:Bash. If the command is a git checkout/restore/reset targeting
a file that was recently edited in this session, logs reward_signal(quality_failure).
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import _warn, append_event, read_events, session_id_of, read_stdin_json, DATA_DIR, load_json_state

_REVERT_RE = re.compile(
    r"\b(git\s+checkout\s+--|git\s+restore|git\s+reset\s+--hard)\b",
    re.IGNORECASE,
)
_EDIT_TOOLS = frozenset({"Write", "Edit", "StrReplace", "MultiEdit"})


def _recently_edited_files(events: list[dict], session_id: str) -> set[str]:
    return {
        e.get("target", "") or e.get("file_path", "")
        for e in events
        if e.get("session_id") == session_id
        and e.get("tool_name") in _EDIT_TOOLS
        and e.get("type") == "tool"
    }


def main() -> int:
    try:
        payload = read_stdin_json()
        tool_input = payload.get("tool_input") or {}
        command = (
            tool_input.get("command", "")
            if isinstance(tool_input, dict)
            else ""
        )
        if not _REVERT_RE.search(command):
            print(json.dumps({"continue": True}))
            return 0

        session_id = session_id_of(payload)
        events = read_events()
        edited = _recently_edited_files(events, session_id)

        # Heuristic: if ANY revert command fires after edits, flag it
        # (file-level matching is best-effort — command parsing is ambiguous)
        if edited:
            try:
                routing_state = load_json_state(DATA_DIR / "routing_state.json")
                intent = (
                    routing_state.get("sessions", {})
                    .get(session_id, {})
                    .get("prompt_intent", "")
                ) or ""
                event: dict = {
                    "type": "reward_signal",
                    "session_id": session_id,
                    "signal": "quality_failure",
                    "command": command[:200],
                    "note": "git revert detected after edits in same session",
                }
                if intent:
                    event["intent"] = intent
                append_event(event)
            except OSError:
                pass
    except Exception as e:
        _warn("detect_revert_main_failed", {"file": "detect_revert.py", "error": str(e)})
    print(json.dumps({"continue": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
