#!/usr/bin/env python3
"""Gate result type and response builders (ARCH-1)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class GateResult:
    """Returned by every gate function. gates.py converts this to host JSON."""
    decision: Literal["allow", "deny"]
    message: str | None = None
    updated_input: dict | None = None


def gate_allow(message: str | None = None) -> "GateResult":
    return GateResult(decision="allow", message=message)


def gate_deny(message: str) -> "GateResult":
    return GateResult(decision="deny", message=message)


def emit_gate_result(result: "GateResult", is_claude: bool) -> dict[str, Any]:
    """Convert GateResult to the host-specific JSON response."""
    if result.decision == "deny":
        if is_claude:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "additionalContext": result.message or "",
                },
                "systemMessage": result.message or "",
            }
        return {"permission": "deny", "agent_message": result.message or ""}
    # allow (with optional nudge)
    if is_claude:
        out: dict[str, Any] = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }
        if result.message:
            out["hookSpecificOutput"]["additionalContext"] = result.message
        if result.updated_input:
            out["hookSpecificOutput"]["updatedInput"] = result.updated_input
        return out
    out = {"permission": "allow"}
    if result.message:
        out["agent_message"] = result.message
    return out
