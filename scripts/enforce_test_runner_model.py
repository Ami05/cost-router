#!/usr/bin/env python3
"""Gate: enforce routing_matrix.test_runner model for dispatches that run tests.

GATE-BUG-003 (knowledge/plans/urgent/open-tickets.json): routing_matrix.test_runner
was defined and loaded (see check_routing.get_routing_for_intent, backed by
config.json's routing_matrix) but never compared against a dispatched model.
Across a real session, 7/7 subagent dispatches ran pytest inline inside a
sonnet mutation worker's chained pattern text instead of a separate
test_runner-tier (haiku, except intent="ambiguous") dispatch, because nothing
enforced the matrix.

This gate denies a Task/Agent dispatch whose structured JSON worker-schema
prompt (see CLAUDE.md's Dispatch Protocol -- mutation/create/shell workers'
intent/pattern/why/command fields) implies running a test suite, unless the
declared model matches routing_matrix.test_runner for the session's current
work_unit_intent. The caller is told to split the test run into its own
test_runner-tier shell-worker dispatch instead of chaining it into a
reasoning-tier mutation/design worker.

Registered in gates.py's pre_task chain. Fails open (allow) whenever the
prompt is unstructured, unparsable, missing a model, or when the session's
intent/matrix can't be resolved -- this gate only judges structured worker
dispatches it can confidently classify.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import GateResult, gate_allow, gate_deny, session_id_of  # noqa: E402
from _model_utils import tier_of_model  # noqa: E402

# Text fields checked across the mutation/create/shell worker schemas
# (CLAUDE.md Dispatch Protocol) for verification-command intent.
_PROMPT_TEXT_FIELDS = ("intent", "pattern", "why", "command", "goal")


def _current_test_runner_model(session_id: str) -> str | None:
    """Return routing_matrix.test_runner for this session's current
    work_unit_intent, or None if it can't be resolved (fail-open trigger)."""
    try:
        from agent_call_tracker import _get_work_unit_intent  # noqa: PLC0415
        from check_routing import get_routing_for_intent  # noqa: PLC0415

        intent = _get_work_unit_intent(session_id) or "_default"
        routing = get_routing_for_intent(intent)
        model = routing.get("test_runner") if isinstance(routing, dict) else None
        return str(model) if model else None
    except Exception:  # noqa: BLE001
        return None


def _implies_test_run(parsed_prompt: dict[str, Any]) -> bool:
    from check_routing import _VERIFICATION_COMMAND_RE  # noqa: PLC0415

    blob = " ".join(
        str(parsed_prompt.get(f, ""))
        for f in _PROMPT_TEXT_FIELDS
        if parsed_prompt.get(f)
    )
    if not blob:
        return False
    return bool(_VERIFICATION_COMMAND_RE.search(blob))


def check_test_runner_model(payload: dict[str, Any]) -> GateResult:
    """Deny a structured worker dispatch that implies a test run but declares
    a model other than routing_matrix.test_runner for the session's intent."""
    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Task", "Agent"):
        return gate_allow()

    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return gate_allow()  # unstructured input -- out of scope for this gate

    prompt = tool_input.get("prompt", "")
    if not isinstance(prompt, str):
        return gate_allow()

    try:
        parsed_prompt = json.loads(prompt)
    except json.JSONDecodeError:
        return gate_allow()  # unstructured prompt -- out of scope for this gate
    if not isinstance(parsed_prompt, dict):
        return gate_allow()

    if not _implies_test_run(parsed_prompt):
        return gate_allow()

    model = str(tool_input.get("model", "") or "")
    if not model:
        return gate_allow()  # enforce_explicit_model already denies a missing model

    session_id = session_id_of(payload)
    required_model = _current_test_runner_model(session_id)
    if not required_model:
        return gate_allow()  # can't resolve intent/matrix -- fail open

    # Use tier classification to allow any model in the same tier as required
    required_tier = tier_of_model(required_model)
    declared_tier = tier_of_model(model)
    if required_tier and required_tier == declared_tier:
        return gate_allow()

    worker = parsed_prompt.get("worker", "unknown")
    return gate_deny(
        f"Task dispatch rejected: this dispatch's prompt implies running a test "
        f"suite (worker='{worker}', model='{model}'), but routing_matrix.test_runner "
        f"for this session's intent requires model='{required_model}'. Split the test "
        f"run into its own model='{required_model}' shell-worker dispatch instead of "
        "chaining it into this worker's pattern/intent/command field."
    )


def event_pre_task(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Gate function for pre_task chain. Returns host-specific JSON."""
    from _common import emit_gate_result  # noqa: E402

    result = check_test_runner_model(payload)
    return emit_gate_result(result, is_claude)
