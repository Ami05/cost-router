#!/usr/bin/env python3
"""Gate: reject Task() dispatch without an explicit model parameter.

Registered in gates.py under the pre_task chain. Checks the Task() call's
prompt/parameters for an explicit model selection (haiku|sonnet).
No explicit model → DENY.  Explicit model found → ALLOW.

Opus subagent dispatches are permanently denied (no prompt override).

HG-101: Single highest-leverage fix — prevents workers from inheriting
the session model (often the most expensive).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import GateResult, gate_allow, gate_deny  # noqa: E402
from _model_utils import (  # noqa: E402
    is_valid_subagent_model,
    routing_platform,
    subagent_model_hint,
)

_OPUS = re.compile(r"\bopus\b", re.IGNORECASE)

_SCHEMA_HINT = (
    " Note: prompts >= 200 tokens must ALSO be valid JSON dispatch "
    "(worker: mutation|read|create|shell) — fixing the model alone will not "
    "pass the next gate. See scripts/docs/dispatch-schemas.json."
)


def _explicit_model_denial_msg(
    prompt: str = "", payload: dict[str, Any] | None = None
) -> str:
    """Build the missing-model denial message for the current policy."""
    platform = routing_platform(payload)
    return (
        f"Task dispatch rejected: explicit model required. {subagent_model_hint(platform)}"
        + _dispatch_schema_hint(prompt)
    )


def _dispatch_schema_hint(prompt: str) -> str:
    """Preview the SPO-007 JSON-schema requirement so a single fix (not two
    sequential denials/round-trips) resolves both gates at once."""
    if not isinstance(prompt, str) or len(prompt) // 4 < 200:
        return ""
    try:
        parsed = json.loads(prompt)
    except (json.JSONDecodeError, ValueError):
        return _SCHEMA_HINT
    if not isinstance(parsed, dict):
        return _SCHEMA_HINT
    return ""


def _model_reference(text: str) -> str:
    """Return model substring from structured model field or inline reference."""
    return str(text)


def _is_valid_explicit_model(model: str) -> bool:
    """True when model names an allowed subagent tier under current policy.
    
    For unstructured input (strings), also searches for model references like
    'model=haiku' or 'model=composer-2.5-fast' embedded in the text.
    """
    if not model:
        return False
    # For structured model strings, direct tier check
    if is_valid_subagent_model(model):
        return True
    # For unstructured input, search for embedded model references
    # Match patterns like "model=haiku", "model: composer-2.5-fast", etc.
    import re
    model_ref_match = re.search(r"model[=:\s]+([a-zA-Z0-9.\-]+)", model, re.IGNORECASE)
    if model_ref_match:
        extracted = model_ref_match.group(1)
        return is_valid_subagent_model(extracted)
    return False


def _opus_subagent_check(tool_input: dict[str, Any] | str) -> GateResult | None:
    """Deny opus subagent dispatches (permanent policy, no prompt override)."""
    model_text = ""
    if isinstance(tool_input, dict):
        model_text = _model_reference(tool_input.get("model", ""))
        if not _OPUS.search(model_text):
            return None
    elif isinstance(tool_input, str):
        if not _OPUS.search(tool_input):
            return None
    else:
        return None

    return gate_deny(
        "Task dispatch rejected: model='opus' is not permitted for subagent "
        f"dispatches. {subagent_model_hint(routing_platform(None))}"
    )


def event_pre_task(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Gate function for pre_task chain. Returns host-specific JSON."""
    from _common import emit_gate_result  # noqa: E402

    gate_result = check_explicit_model(payload)
    result = emit_gate_result(gate_result, is_claude)
    if gate_result.decision == "deny":
        tool_input = payload.get("tool_input", {})
        model_text = (
            tool_input if isinstance(tool_input, str) else str(tool_input.get("model", ""))
        )
        if _OPUS.search(model_text):
            result["_gate_name_override"] = "opus_subagent"
    return result


def check_explicit_model(payload: dict[str, Any]) -> GateResult:
    """Check that a Task() call includes an explicit model parameter.

    Inspects:
      - tool_input.model (Claude Code structured input)
      - tool_input.prompt for inline model references (fallback)
    """
    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Task", "Agent"):
        return gate_allow()

    tool_input = payload.get("tool_input", {})
    if isinstance(tool_input, str):
        opus_denial = _opus_subagent_check(tool_input)
        if opus_denial is not None:
            return opus_denial
        # For unstructured input, check if it references a valid model
        if _is_valid_explicit_model(tool_input):
            return gate_allow()
        return gate_deny(_explicit_model_denial_msg(tool_input, payload))

    # Structured input — check model field
    model = tool_input.get("model", "")
    if model:
        opus_denial = _opus_subagent_check(tool_input)
        if opus_denial is not None:
            return opus_denial
        if _is_valid_explicit_model(str(model)):
            return gate_allow()

    prompt = tool_input.get("prompt", "")
    return gate_deny(_explicit_model_denial_msg(prompt, payload))
