#!/usr/bin/env python3
"""PreToolUse:Agent gate — blocks sequential single-file Explore/read dispatches.

Detects when the orchestrator is dispatching many single-scope read workers
in a turn instead of batching them into one Explore dispatch. Warns at
threshold-1, denies at threshold.

Turn reset: fires when `turn_id` changes (payload field "turn_id" or
"message_id"). A batched read dispatch (scope len > 1) resets the counter.

Config key: sequential_explore_threshold (gates.json), default 3.
State: data/sequential_explore_state.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402
from _common import (  # noqa: E402
    emit_gate_result,
    gate_allow,
    gate_deny,
    load_json_state,
    save_json_state,
    config_int,
)
from _model_utils import is_claude_payload, tool_input_of  # noqa: E402

_BATCH_TEMPLATE = (
    'Agent(model="haiku", subagent_type="Explore", prompt="Read <file1>, <file2>, ...")'
)

_DENY_MSG_TMPL = (
    "[sequential-explore-gate] {count} sequential single-file Explore dispatches this turn "
    "(threshold: {threshold}). Batch remaining reads into one Explore worker:\n"
    "  {template}"
)

_WARN_MSG_TMPL = (
    "[sequential-explore-gate] {count} sequential single-file Explore dispatches this turn — "
    "next will be blocked. Batch remaining reads:\n"
    "  {template}"
)


def _load_state() -> dict[str, Any]:
    state_file = _common.DATA_DIR / "sequential_explore_state.json"
    state = load_json_state(state_file)
    if "seq_explore_count" not in state:
        state["seq_explore_count"] = 0
    if "last_turn_id" not in state:
        state["last_turn_id"] = None
    return state


def _turn_id_of(payload: dict[str, Any]) -> str | None:
    """Extract best-effort turn/message id from payload."""
    for key in ("turn_id", "message_id", "messageId", "turnId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _classify_dispatch(payload: dict[str, Any]) -> tuple[bool, bool]:
    """Return (is_single, is_batched) based on the Task prompt.

    - worker=="read": is_single=(len(scope)==1), is_batched=(len(scope)>1)
    - subagent_type=="Explore" (non-JSON prompt): is_single=True, is_batched=False
    - Anything else: (False, False)
    """
    tool_input = tool_input_of(payload)
    prompt = tool_input.get("prompt", "") or ""
    if not isinstance(prompt, str):
        prompt = ""

    # Try JSON parse first
    stripped = prompt.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict) and obj.get("worker") == "read":
                scope = obj.get("scope", [])
                if not isinstance(scope, list):
                    scope = []
                if len(scope) > 1:
                    return False, True
                if len(scope) == 1:
                    return True, False
                # scope empty — treat as single (conservative)
                return True, False
            # Other worker types: neither
            return False, False
        except (json.JSONDecodeError, ValueError):
            pass

    # Non-JSON: check subagent_type field in payload
    subagent_type = tool_input.get("subagent_type", "") or payload.get("subagent_type", "") or ""
    if isinstance(subagent_type, str) and subagent_type.strip().lower() == "explore":
        return True, False

    return False, False


def event_pre_task(payload: dict, is_claude: bool) -> dict:
    """Gate entry point for PreToolUse:Task (Agent dispatch)."""
    try:
        is_single, is_batched = _classify_dispatch(payload)

        # Not an Explore/read dispatch — pass through immediately
        if not is_single and not is_batched:
            return emit_gate_result(gate_allow(), is_claude)

        state = _load_state()

        # Turn reset: new turn_id clears the counter
        turn_id = _turn_id_of(payload)
        if not turn_id or turn_id != state.get("last_turn_id", None):
            state["seq_explore_count"] = 0
            state["last_turn_id"] = turn_id

        # Batched dispatch: reset counter and allow
        if is_batched:
            state["seq_explore_count"] = 0
            state_file = _common.DATA_DIR / "sequential_explore_state.json"
            save_json_state(state_file, state)
            return emit_gate_result(gate_allow(), is_claude)

        # Single-file dispatch: increment counter
        threshold = config_int("sequential_explore_gate.threshold", 3)
        count: int = state["seq_explore_count"] + 1
        state["seq_explore_count"] = count
        state_file = _common.DATA_DIR / "sequential_explore_state.json"
        save_json_state(state_file, state)

        if count >= threshold:
            msg = _DENY_MSG_TMPL.format(
                count=count,
                threshold=threshold,
                template=_BATCH_TEMPLATE,
            )
            return emit_gate_result(gate_deny(msg), is_claude)

        if count >= threshold - 1:
            msg = _WARN_MSG_TMPL.format(
                count=count,
                template=_BATCH_TEMPLATE,
            )
            return emit_gate_result(gate_allow(msg), is_claude)

        return emit_gate_result(gate_allow(), is_claude)

    except Exception:  # noqa: BLE001 — fail open
        return emit_gate_result(gate_allow(), is_claude)
