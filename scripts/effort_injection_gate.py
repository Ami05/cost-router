#!/usr/bin/env python3
"""Gate: append Effort line to subagent dispatch prompts based on routing_matrix.

For non-Explore subagent dispatches, this gate classifies the dispatch's prompt
text and injects an 'Effort: <level>' line before the first tool invocation
runs, so the worker sees what effort level the routing_matrix prescribes for
this work intent. The effort line is only injected if it's not already present.

Registered in gates.py's pre_task chain as the last entry (after
sequential_explore_gate, before agent_call_tracker).
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import GateResult, gate_allow, append_event  # noqa: E402


def _inject_effort_line(prompt: str, effort: str) -> str:
    """Append 'Effort: <effort>' to the prompt if not already present.

    Returns the modified prompt.
    """
    # Check if effort line already exists
    for line in prompt.splitlines():
        if line.strip().lower().startswith('effort:'):
            return prompt  # Already present, don't inject

    # Inject at the end
    return f"{prompt}\nEffort: {effort}"


def _map_intent_to_internal(intent: str) -> str:
    """Map classify_dispatch_work's intent to internal routing intent.

    Unclassifiable dispatches map to _default (yields high effort, not xhigh).

    mechanical -> mechanical
    bulk-edit -> bulk-edit
    judgment -> design
    ambiguous -> _default (unclassifiable; classifier ambiguous != session ambiguous intent)
    """
    if intent == "judgment":
        return "design"
    if intent == "ambiguous":
        return "_default"
    return intent


def event_pre_task(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Gate function for pre_task chain. Returns host-specific JSON.

    Injects Effort line into dispatch prompt if not already present.
    Fails open (plain allow) on any error.
    """
    from _common import emit_gate_result  # noqa: E402

    try:
        tool_name = payload.get("tool_name", "")
        if tool_name not in ("Task", "Agent"):
            return emit_gate_result(gate_allow(), is_claude)

        tool_input = payload.get("tool_input", {})
        if not isinstance(tool_input, dict):
            return emit_gate_result(gate_allow(), is_claude)

        # Skip Explore subagent_type
        subagent_type = tool_input.get("subagent_type", "")
        if isinstance(subagent_type, str) and subagent_type.lower() == "explore":
            return emit_gate_result(gate_allow(), is_claude)

        prompt = tool_input.get("prompt", "")
        if not isinstance(prompt, str) or not prompt:
            return emit_gate_result(gate_allow(), is_claude)

        # Classify the dispatch
        from routing_resolver import classify_dispatch_work, get_dispatch_output_config  # noqa: E402, PLC0415

        label = classify_dispatch_work(prompt)
        internal_intent = _map_intent_to_internal(label)

        # Get effort from routing config
        config = get_dispatch_output_config(internal_intent)
        effort = config.get("effort", "high")

        # Inject effort line
        updated_prompt = _inject_effort_line(prompt, effort)

        # Log the injection
        try:
            append_event({
                "type": "effort_injected",
                "intent": label,
                "effort": effort,
            })
        except Exception:  # noqa: BLE001 — logging must fail open
            pass

        # If prompt changed, return updated_input; otherwise plain allow
        if updated_prompt != prompt:
            updated_tool_input = copy.deepcopy(tool_input)
            updated_tool_input["prompt"] = updated_prompt
            return emit_gate_result(
                GateResult(
                    decision="allow",
                    updated_input=updated_tool_input,
                ),
                is_claude,
            )

        return emit_gate_result(gate_allow(), is_claude)

    except Exception:  # noqa: BLE001 — fail open on any error
        return emit_gate_result(gate_allow(), is_claude)
