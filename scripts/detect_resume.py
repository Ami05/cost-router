#!/usr/bin/env python3
"""cost-router hook: detect resume-session pattern and force FO routing.

UserPromptSubmit hook. Fires after classify_intent.py.
Detects: early session (few gate calls) + execution intent + N>=3 work units.
On match: writes forced_fo state to data/resume_state.json and injects
additionalContext telling the agent to dispatch via Task first.
Never denies — always allows. Fails open on any error.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    RESUME_GATE_EARLY_CALL_THRESHOLD,
    RESUME_GATE_MAX_TRACKED_SESSIONS,
    RESUME_GATE_SHORT_PROMPT_MAX_CHARS,
    RESUME_GATE_WORK_UNIT_THRESHOLD,
    append_event,
    load_json_state,
    now_iso,
    read_stdin_json,
    save_json_state,
    session_id_of,
    _warn,
)
from classify_intent import classify_intent  # noqa: E402
from _events import read_events  # noqa: E402

RESUME_STATE_FILE = DATA_DIR / "resume_state.json"
ROUTING_STATE_FILE = DATA_DIR / "routing_state.json"

_NUMERIC_WORDS: dict[str, int] = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20,
}

_WORK_NOUNS = frozenset({
    "failures", "fixes", "tests", "issues", "bugs", "errors", "items",
    "tasks", "failure", "fix", "test", "issue", "bug", "error", "item",
    "task", "changes", "files", "functions",
})

_OVERRIDE_PHRASES = frozenset({
    "direct override",
    "no task dispatch",
    "single-file direct",
    "dispatch-budget-override",
})

_ADVISORY_MESSAGE = (
    "[cost-router resume-gate] Short prompt detected at session start. "
    "If you have multiple pending items to implement, prefer dispatching via "
    "Task(model: 'haiku') to batch the work rather than inline mutations."
)

_SIZING_UNSTABLE_MIN_TRIGGERS = 2
_SIZING_UNSTABLE_RATIO_THRESHOLD = 5

_FORCED_FO_MESSAGE = (
    "[cost-router resume-gate] {n} work items detected in an early-session prompt. "
    "Gate enforced: you MUST dispatch via Task(model: 'haiku') before any Edit/Write/Shell. "
    "Emit 'Routing: FO — resume gate' then call Task(...) as your first tool."
)


# ---------------------------------------------------------------------------
# Work unit counting
# ---------------------------------------------------------------------------

def _count_work_units(prompt: str) -> int:
    """Return max detected work unit count across all counting strategies."""
    text = prompt.lower()
    counts: list[int] = []

    # Numbered list: "1. X 2. Y 3. Z"
    numbered = re.findall(r'\b\d+\.\s+\S', prompt)
    counts.append(len(numbered))

    # Bullet list: lines starting with -, *, or •
    bullets = re.findall(r'(?:^|\n)\s*[-*\u2022]\s+\S', prompt)
    counts.append(len(bullets))

    # Numeric word + work noun: "three bugs", "fix all 10 failures"
    for word, val in _NUMERIC_WORDS.items():
        if re.search(r'\b' + word + r'\b', text):
            counts.append(val)
    digit_noun = re.findall(r'\b(\d+)\s+(?:' + '|'.join(_WORK_NOUNS) + r')\b', text)
    for d in digit_noun:
        counts.append(int(d))

    # Fix-label count: "Fix 1:", "fix #3"
    fix_labels = re.findall(r'\bfix\s+(?:#?\d+|[A-Z]\d*)\b', prompt, re.IGNORECASE)
    counts.append(len(fix_labels))

    # Comma-list after verb: "implement X, Y, and Z"
    verb_list = re.findall(
        r'\b(?:implement|add|fix|create|update|refactor|build|write|run)\b[^.!?\n]{0,80}?,',
        text,
    )
    for seg in verb_list:
        commas = seg.count(',')
        if commas >= 1:
            counts.append(commas + 1)

    return max(counts) if counts else 0


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _is_early_session(session_id: str) -> bool:
    """Return True if session has fewer gate calls than the early threshold."""
    state = load_json_state(ROUTING_STATE_FILE)
    sessions = state.get("sessions", {})
    entry = sessions.get(session_id, {})
    return int(entry.get("total_gate_calls_session", 0)) < RESUME_GATE_EARLY_CALL_THRESHOLD


def _has_override(prompt: str) -> bool:
    lower = prompt.lower()
    return any(phrase in lower for phrase in _OVERRIDE_PHRASES)


def _load_resume_sessions() -> dict:
    state = load_json_state(RESUME_STATE_FILE)
    sessions = state.get("sessions", {})
    return sessions if isinstance(sessions, dict) else {}


def _save_resume_sessions(sessions: dict) -> None:
    """Persist sessions, capping at RESUME_GATE_MAX_TRACKED_SESSIONS."""
    if len(sessions) > RESUME_GATE_MAX_TRACKED_SESSIONS:
        overflow = len(sessions) - RESUME_GATE_MAX_TRACKED_SESSIONS
        for key in list(sessions.keys())[:overflow]:
            del sessions[key]
    save_json_state(RESUME_STATE_FILE, {"sessions": sessions})


def _write_forced_fo(session_id: str, turn_id: str | None, work_units: int, reason: str) -> None:
    sessions = _load_resume_sessions()
    sessions[session_id] = {
        "forced_fo_turn_id": turn_id or "unknown",
        "work_units": work_units,
        "reason": reason,
        "cleared": False,
        "ts": now_iso(),
    }
    _save_resume_sessions(sessions)


def _check_sizing_instability(session_id: str, work_units: int) -> None:
    """Flag erratic work-unit sizing across resume_gate_triggered events in this session.

    Looks back at prior resume_gate_triggered events for the same session_id. If there
    have been RESUME_GATE_SIZING (this trigger plus priors) >= _SIZING_UNSTABLE_MIN_TRIGGERS
    triggers and their work_units values vary by more than _SIZING_UNSTABLE_RATIO_THRESHOLD x
    (max/min), emit a resume_gate_sizing_unstable event so the pattern is visible instead of
    buried in raw counts.
    """
    try:
        events = read_events()
    except OSError as e:
        _warn("resume_state_read_failed", {"file": "detect_resume.py", "error": str(e)})
        return

    prior_units = [
        int(e.get("work_units", 0))
        for e in events
        if e.get("type") == "resume_gate_triggered"
        and e.get("session_id") == session_id
        and isinstance(e.get("work_units"), (int, float))
    ]
    all_units = prior_units + [work_units]
    if len(all_units) < _SIZING_UNSTABLE_MIN_TRIGGERS:
        return

    lo, hi = min(all_units), max(all_units)
    if lo <= 0 or (hi / lo) <= _SIZING_UNSTABLE_RATIO_THRESHOLD:
        return

    try:
        append_event({
            "type": "resume_gate_sizing_unstable",
            "session_id": session_id,
            "work_units": work_units,
            "prior_work_units": prior_units,
            "ratio": round(hi / lo, 2),
        })
    except OSError as e:
        _warn("resume_state_read_failed", {"file": "detect_resume.py", "error": str(e)})


def _turn_id_of(payload: dict) -> str | None:
    for key in ("turn_id", "turnId", "message_id", "messageId"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
    return None


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _allow_inject(is_claude: bool, message: str | None) -> dict:
    """Return hook allow output, optionally injecting additionalContext."""
    if is_claude:
        return {"additionalContext": message} if message else {}
    return {"continue": True, "additional_context": message} if message else {"continue": True}


def _is_claude_host(payload: dict) -> bool:
    return bool(payload.get("hook_event_name")) and not payload.get("cursor_version")


# ---------------------------------------------------------------------------
# Main detection logic
# ---------------------------------------------------------------------------

def event_detect_resume(payload: dict, is_claude: bool) -> dict:
    # Subagents are exempt
    if payload.get("agent_id"):
        return _allow_inject(is_claude, None)

    prompt = ""
    for key in ("prompt", "text", "message"):
        v = payload.get(key)
        if isinstance(v, str):
            prompt = v
            break

    session_id = session_id_of(payload)
    turn_id = _turn_id_of(payload)

    # Override keyword present — skip detection entirely
    if prompt and _has_override(prompt):
        try:
            append_event({"type": "resume_gate_overridden", "session_id": session_id})
        except OSError as e:
            _warn("resume_state_read_failed", {"file": "detect_resume.py", "error": str(e)})
        return _allow_inject(is_claude, None)

    # Short prompt — advisory only, no state write
    if len(prompt) < RESUME_GATE_SHORT_PROMPT_MAX_CHARS:
        if not _is_early_session(session_id):
            return _allow_inject(is_claude, None)
        if classify_intent(prompt) != "agentic_action":
            return _allow_inject(is_claude, None)
        try:
            append_event({"type": "resume_gate_short_prompt", "session_id": session_id})
        except OSError as e:
            _warn("resume_state_read_failed", {"file": "detect_resume.py", "error": str(e)})
        return _allow_inject(is_claude, _ADVISORY_MESSAGE)

    # Early session check
    if not _is_early_session(session_id):
        return _allow_inject(is_claude, None)

    # Intent check — must be execution intent
    if classify_intent(prompt) != "agentic_action":
        return _allow_inject(is_claude, None)

    # Work unit count — must meet threshold
    work_units = _count_work_units(prompt)
    if work_units < RESUME_GATE_WORK_UNIT_THRESHOLD:
        return _allow_inject(is_claude, None)

    # Trigger: write forced_fo state and inject advisory
    reason = f"early-session + execute intent + {work_units} work units"
    _write_forced_fo(session_id, turn_id, work_units, reason)
    try:
        append_event({
            "type": "resume_gate_triggered",
            "session_id": session_id,
            "work_units": work_units,
        })
    except OSError as e:
        _warn("resume_state_read_failed", {"file": "detect_resume.py", "error": str(e)})

    _check_sizing_instability(session_id, work_units)

    message = _FORCED_FO_MESSAGE.format(n=work_units)
    return _allow_inject(is_claude, message)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        payload = read_stdin_json()
        is_claude = _is_claude_host(payload)
        result = event_detect_resume(payload, is_claude)
        print(json.dumps(result))
        return 0
    except Exception:  # noqa: BLE001 — hook must fail open
        print(json.dumps({}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
