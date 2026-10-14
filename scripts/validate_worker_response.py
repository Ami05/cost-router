#!/usr/bin/env python3
"""Return-side schema validation for shell and read worker responses.

Fires on SubagentStop. Advisory-only (warn, never hard-block) in v1.
Violations logged to data/worker_response_violations.jsonl.

Usage (hooks):
    bash run.sh validate_worker_response.py --event subagent_stop
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    DATA_DIR,
    _warn,
    gate_allow,
    now_iso,
    read_stdin_json,
    session_id_of,
)
from _model_utils import is_claude_payload  # noqa: E402

# ---------------------------------------------------------------------------
# Response schemas (kept in sync with dispatch-schemas.json response_schemas)
# ---------------------------------------------------------------------------

_SHELL_REQUIRED = frozenset({"exit_code", "stdout", "stderr"})
_READ_REQUIRED = frozenset({"findings"})
_READ_FINDING_REQUIRED = frozenset({"file", "line", "summary"})

VIOLATIONS_LOG = DATA_DIR / "worker_response_violations.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    ti = payload.get("tool_input")
    if isinstance(ti, dict):
        return ti
    ti = payload.get("toolInput")
    return ti if isinstance(ti, dict) else {}


def _dispatch_prompt(tool_input: dict[str, Any]) -> str:
    for key in ("prompt", "description", "task", "input"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _detect_worker_type(prompt: str) -> str | None:
    """Detect worker type from the original dispatch prompt JSON."""
    if not prompt:
        return None
    try:
        parsed = json.loads(prompt)
        if isinstance(parsed, dict):
            return parsed.get("worker")
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: keyword scan for thin dispatches
    lower = prompt.lower()
    if '"worker":"shell"' in lower.replace(" ", "") or '"worker": "shell"' in lower:
        return "shell"
    if '"worker":"read"' in lower.replace(" ", "") or '"worker": "read"' in lower:
        return "read"
    return None


def _last_assistant_text(transcript_path: str) -> str | None:
    """Extract the last assistant message text from a subagent transcript JSONL."""
    if not transcript_path:
        return None
    path = Path(transcript_path)
    if not path.is_file():
        return None
    last_text: str | None = None
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            msg = entry.get("message", {})
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, str) and content:
                last_text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            last_text = text
    except OSError:
        return None
    return last_text


def _validate_shell_response(data: dict[str, Any]) -> list[str]:
    """Return list of missing required fields for shell worker response."""
    missing = []
    for field in sorted(_SHELL_REQUIRED):
        if field not in data:
            missing.append(field)
        elif field == "exit_code" and not isinstance(data[field], int):
            missing.append(f"{field}:not_integer")
    return missing


def _validate_read_response(data: dict[str, Any]) -> list[str]:
    """Return list of schema violations for read worker response."""
    violations = []
    if "findings" not in data:
        violations.append("findings:missing")
        return violations
    findings = data["findings"]
    if not isinstance(findings, list):
        violations.append("findings:not_array")
        return violations
    for i, item in enumerate(findings):
        if not isinstance(item, dict):
            violations.append(f"findings[{i}]:not_object")
            continue
        for field in sorted(_READ_FINDING_REQUIRED):
            if field not in item:
                violations.append(f"findings[{i}].{field}:missing")
            elif field == "line" and not isinstance(item[field], int):
                violations.append(f"findings[{i}].{field}:not_integer")
    return violations


def _log_violation(
    session_id: str,
    worker_type: str,
    violations: list[str],
    subagent_id: str | None,
) -> None:
    """Append one violation record to data/worker_response_violations.jsonl."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": now_iso(),
            "session_id": session_id,
            "worker_type": worker_type,
            "subagent_id": subagent_id,
            "violations": violations,
        }
        with VIOLATIONS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        _warn("worker_response_violation_log_failed", {"error": str(e)})


# ---------------------------------------------------------------------------
# Main gate function
# ---------------------------------------------------------------------------

def event_subagent_stop(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Validate the subagent return value against the response schema.

    Advisory-only (v1): always returns gate_allow; violations are logged and
    an advisory nudge is attached to the response.
    """
    try:
        tool_input = _tool_input(payload)
        prompt = _dispatch_prompt(tool_input)
        worker_type = _detect_worker_type(prompt)

        # Only validate shell and read workers
        if worker_type not in ("shell", "read"):
            return {"continue": True}

        session_id = session_id_of(payload)
        subagent_id: str | None = None
        for key in ("agent_id", "subagent_id", "agentId", "subagentId"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                subagent_id = v
                break

        # Extract return text from transcript
        return_text = _last_assistant_text(payload.get("agent_transcript_path", ""))

        if not return_text:
            # No return text to validate — skip silently
            return {"continue": True}

        # Try to parse as JSON
        try:
            return_data = json.loads(return_text)
        except (json.JSONDecodeError, ValueError):
            # Free text response — advisory violation
            violations = ["return_value:not_json"]
            _log_violation(session_id, worker_type, violations, subagent_id)
            nudge = (
                f"[advisory] {worker_type} worker returned free text instead of structured JSON. "
                f"Expected schema with required fields: "
                + (", ".join(sorted(_SHELL_REQUIRED)) if worker_type == "shell"
                   else "findings[{file,line,summary}]")
            )
            result = gate_allow(nudge)
            if is_claude:
                return {
                    "continue": True,
                    "hookSpecificOutput": {
                        "additionalContext": nudge,
                    },
                }
            return {"continue": True, "agent_message": nudge}

        if not isinstance(return_data, dict):
            violations = ["return_value:not_object"]
            _log_violation(session_id, worker_type, violations, subagent_id)
            return {"continue": True}

        # Validate against schema
        if worker_type == "shell":
            violations = _validate_shell_response(return_data)
        else:  # read
            violations = _validate_read_response(return_data)

        if not violations:
            return {"continue": True}

        # Advisory nudge
        _log_violation(session_id, worker_type, violations, subagent_id)
        nudge = (
            f"[advisory] {worker_type} worker response missing required fields: "
            + ", ".join(violations)
            + ". Workers should return structured JSON matching the response schema."
        )
        if is_claude:
            return {
                "continue": True,
                "hookSpecificOutput": {
                    "additionalContext": nudge,
                },
            }
        return {"continue": True, "agent_message": nudge}

    except Exception as e:  # noqa: BLE001 — fail open
        _warn("validate_worker_response_failed", {"error": str(e)})
        return {"continue": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default="subagent_stop")
    args = parser.parse_args()

    payload = read_stdin_json()
    is_claude = is_claude_payload(payload)

    if args.event == "subagent_stop":
        result = event_subagent_stop(payload, is_claude)
        print(json.dumps(result))
        return 0

    print(json.dumps({"continue": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
