#!/usr/bin/env python3
"""DR-2 & DR-3: Dispatch context gate — inline body detection + handoff profile enforcement.

Fires on PreToolUse:Task tool calls to prevent prompt bloat from inline file bodies
and enforce handoff profile constraints (zero-handoff, paths-only, auto, inline).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    DATA_DIR,
    emit_gate_result,
    gate_allow,
    gate_deny,
    load_json_state,
    save_json_state,
    session_id_of,
)

# Code fence pattern: ```...``` with content
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def check_inline_body_violation(prompt: str, session_state: dict) -> tuple[bool, bool, str]:
    """Detect inline file bodies in code fences (>20 lines not under CONTEXT:/FILES: header).

    Returns:
        (is_violation, is_first_violation, message)
    """
    # Escape hatch: dispatch-context-inline-override
    if "dispatch-context-inline-override" in prompt:
        return False, False, ""

    # Zero-handoff exempt: "Run: <cmd>" pattern with short prompt
    stripped = prompt.strip()
    if stripped.startswith("Run:") and len(stripped) < 200:
        return False, False, ""

    # Find code fence blocks
    violations = 0
    for match in _CODE_FENCE_RE.finditer(prompt):
        fence_content = match.group(1)
        line_count = fence_content.count("\n")

        # Only flag if >20 lines
        if line_count <= 20:
            continue

        # Check if preceded by CONTEXT: or FILES: within 3 lines before fence
        fence_start = match.start()
        preceding_text = prompt[:fence_start]
        preceding_lines = preceding_text.split("\n")[-4:]  # Last 3 lines + current
        has_header = any("CONTEXT:" in line or "FILES:" in line for line in preceding_lines)

        if not has_header:
            violations += 1

    if violations == 0:
        return False, False, ""

    # Count violations in session (only if session_state is not empty/fresh)
    current_count = session_state.get("inline_body_violations", 0)
    is_first = current_count == 0
    session_state["inline_body_violations"] = current_count + 1

    return True, is_first, ""


def classify_and_enforce_handoff(prompt: str, session_state: dict) -> tuple[str, str | None]:
    """Classify handoff profile and enforce constraints.

    Returns:
        (profile, error_message_or_None)

    Profiles:
        - "zero": prompt stripped starts with "Run:" AND len(stripped) < 200
        - "paths-only": has FILES: header but no code fences with content
        - "inline": has code fences with content
        - "auto": everything else
    """
    stripped = prompt.strip()

    # Zero-handoff: apply_edits.py deterministic script invocations
    # These are mechanical JSON-spec file mutations — not LLM tasks.
    if re.search(r"python[3]?\s+.*apply_edits\.py\s+--spec\b", stripped):
        profile = "zero"
        session_state["last_handoff_profile"] = profile
        return profile, None

    # Classify profile
    if stripped.startswith("Run:") and len(stripped) < 200:
        profile = "zero"
    elif "FILES:" in prompt and not _CODE_FENCE_RE.search(prompt):
        profile = "paths-only"
    elif _CODE_FENCE_RE.search(prompt):
        profile = "inline"
    else:
        profile = "auto"

    session_state["last_handoff_profile"] = profile

    # Enforce constraints
    error_msg = None

    if profile == "zero":
        # Check that prompt doesn't exceed 50 chars beyond "Run: <cmd>"
        # Extract the command part (case-insensitive search)
        cmd_match = re.match(r"(?i)run:\s*(.+?)(?:\n|$)", prompt)
        if cmd_match:
            cmd_part = f"Run: {cmd_match.group(1)}"
            extra_chars = len(stripped) - len(cmd_part.strip())
            if extra_chars > 50:
                error_msg = (
                    "[cost-router] Zero-handoff dispatch blocked — prompt exceeds 50 chars "
                    "beyond Run: prefix. Shell dispatches must be Run: <command> only."
                )

    elif profile == "paths-only":
        # Apply inline body check for paths-only
        is_violation, _, _ = check_inline_body_violation(prompt, {})
        if is_violation:
            error_msg = (
                "[cost-router] Paths-only dispatch blocked — inline file body detected. "
                "Remove file content from prompt; pass path in FILES: only."
            )

    return profile, error_msg


def event_pre_task(payload: dict, is_claude: bool) -> dict:
    """Gate function for PreToolUse:Task dispatch.

    Enforces DR-2 (inline body detection) and DR-3 (handoff profile enforcement).
    """
    try:
        # Extract prompt
        prompt = payload.get("toolInput", {}).get("prompt", "") or payload.get("tool_input", {}).get("prompt", "")
        if not prompt:
            return emit_gate_result(gate_allow(), is_claude)

        # Load session state
        state_file = DATA_DIR / "dispatch_context_state.json"
        state = load_json_state(state_file)
        if "sessions" not in state:
            state["sessions"] = {}

        session_id = session_id_of(payload)
        session_entry = state["sessions"].setdefault(session_id, {
            "inline_body_violations": 0,
            "last_handoff_profile": "auto",
        })

        # DR-3: Classify and enforce handoff profile
        profile, profile_error = classify_and_enforce_handoff(prompt, session_entry)

        if profile_error:
            save_json_state(state_file, state)
            return emit_gate_result(gate_deny(profile_error), is_claude)

        # DR-2: Check inline body violations (skip if profile == "zero")
        if profile != "zero":
            is_violation, is_first, _ = check_inline_body_violation(prompt, session_entry)

            if is_violation:
                save_json_state(state_file, state)

                if is_first:
                    # Nudge message (allow)
                    nudge_msg = (
                        "[cost-router] Dispatch prompt contains inline file body (>20 lines not under "
                        "CONTEXT:/FILES: header). Pre-read the file once and pass the path in FILES: — "
                        "let the worker read from disk. Override: add `dispatch-context-inline-override` "
                        "to your Routing line."
                    )
                    return emit_gate_result(gate_allow(nudge_msg), is_claude)
                else:
                    # Deny message
                    deny_msg = (
                        "[cost-router] Dispatch prompt blocked — repeated inline file body embedding. "
                        "Use FILES: <path> + CONTEXT: <10-line excerpt> pattern. Workers read full files "
                        "from disk. Override: `dispatch-context-inline-override` in Routing line."
                    )
                    return emit_gate_result(gate_deny(deny_msg), is_claude)
            else:
                # No violation found — reset counter for next dispatch
                session_entry["inline_body_violations"] = 0

        save_json_state(state_file, state)
        return emit_gate_result(gate_allow(), is_claude)

    except Exception:  # noqa: BLE001
        # Fail-open on any exception
        return emit_gate_result(gate_allow(), is_claude)
