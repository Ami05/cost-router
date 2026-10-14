#!/usr/bin/env python3
"""cost-router: unified gate dispatcher.

Hooks call: bash run.sh gates.py --gate <name>
Payload arrives on stdin (JSON). Result emitted on stdout (JSON).
Exit 0 = allow/nudge. Exit 2 = deny.

GATE_REGISTRY maps gate name -> ordered list of gate callables.
First deny short-circuits the chain. Any exception -> fail-open (allow).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable
from datetime import datetime, timezone
import os

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    DATA_DIR,
    GateResult,
    _warn,
    append_session_log,
    emit_gate_result,
    gate_allow,
    gate_deny,
    read_stdin_json,
)
from _model_utils import is_claude_payload  # noqa: E402

# Gate callables: fn(payload: dict) -> GateResult
# Import lazily inside functions to avoid circular imports at module level.

def _check_read_repeat(payload: dict[str, Any]) -> dict[str, Any]:
    from check_read_repeat import event_read
    from _model_utils import is_claude_payload as _icp
    output = event_read(payload, _icp(payload))
    return output


def _build_file_graph(payload: dict[str, Any]) -> dict[str, Any]:
    from build_file_graph import event_read as bg_event_read
    from _model_utils import is_claude_payload as _icp
    output = bg_event_read(payload, _icp(payload))
    return output


def _routing_read(payload: dict[str, Any]) -> dict[str, Any]:
    from check_routing import event_read as cr_event_read
    from _model_utils import is_claude_payload as _icp
    output = cr_event_read(payload, _icp(payload))
    return output


def _routing_gate(payload: dict[str, Any]) -> dict[str, Any]:
    from check_routing import event_gate
    from _model_utils import is_claude_payload as _icp
    output = event_gate(payload, _icp(payload))
    return output


def _file_hash_pre(payload: dict[str, Any]) -> dict[str, Any]:
    from file_hash_guard import event_pre_edit
    from _model_utils import is_claude_payload as _icp
    output = event_pre_edit(payload, _icp(payload))
    return output


def _file_hash_read(payload: dict[str, Any]) -> dict[str, Any]:
    from file_hash_guard import event_read as fh_event_read
    from _model_utils import is_claude_payload as _icp
    output = fh_event_read(payload, _icp(payload))
    return output


def _dispatch_context_gate(payload: dict[str, Any]) -> dict[str, Any]:
    from dispatch_context_gate import event_pre_task
    from _model_utils import is_claude_payload as _icp
    return event_pre_task(payload, _icp(payload))


def _explore_model_tier_gate(payload: dict[str, Any]) -> dict[str, Any]:
    from check_routing import event_explore_model_gate
    from _model_utils import is_claude_payload as _icp
    return event_explore_model_gate(payload, _icp(payload))


def _sequential_explore_gate(payload: dict[str, Any]) -> dict[str, Any]:
    from sequential_explore_gate import event_pre_task
    from _model_utils import is_claude_payload as _icp
    return event_pre_task(payload, _icp(payload))


def _file_graph_gate(payload: dict[str, Any]) -> dict[str, Any]:
    from file_graph_gate import gate_fn
    return gate_fn(payload)


def _enforce_explicit_model(payload: dict[str, Any]) -> dict[str, Any]:
    from enforce_explicit_model import event_pre_task as eem_event
    from _model_utils import is_claude_payload as _icp
    return eem_event(payload, _icp(payload))


def _enforce_test_runner_model(payload: dict[str, Any]) -> dict[str, Any]:
    # GATE-BUG-003: routing_matrix.test_runner was dead config -- this denies
    # a chained test run on a non-test_runner-tier worker.
    from enforce_test_runner_model import event_pre_task as etr_event
    from _model_utils import is_claude_payload as _icp
    return etr_event(payload, _icp(payload))


def _enforce_worker_model(payload: dict[str, Any]) -> dict[str, Any]:
    # GATE-BUG-004: routing_matrix.worker was dead config for the haiku case --
    # this denies a shell/create/mutation dispatch on a non-haiku model when
    # the matrix requires haiku.
    from enforce_worker_model import event_pre_task as ewm_event
    from _model_utils import is_claude_payload as _icp
    return ewm_event(payload, _icp(payload))


def _mixed_action_bundle_nudge(payload: dict[str, Any]) -> dict[str, Any]:
    # Nudge-only (never deny): flags a dispatch prompt that mixes judgment
    # language with a bulk mechanical file scope, the pattern
    # classify_dispatch_work allows silently as "ambiguous" by design.
    from enforce_worker_model import event_pre_task_mixed_action as mab_event
    from _model_utils import is_claude_payload as _icp
    return mab_event(payload, _icp(payload))


def _enforce_role_gate_write(payload: dict[str, Any]) -> dict[str, Any]:
    from enforce_role_gates import event_pre_write
    from _model_utils import is_claude_payload as _icp
    return event_pre_write(payload, _icp(payload))


def _shell_same_type_gate(payload: dict[str, Any]) -> dict[str, Any] | None:
    from check_routing import shell_same_type_gate
    from _model_utils import is_claude_payload as _icp
    return shell_same_type_gate(payload, _icp(payload))


def _enforce_role_gate_read(payload: dict[str, Any]) -> dict[str, Any]:
    from enforce_role_gates import event_pre_read
    from _model_utils import is_claude_payload as _icp
    return event_pre_read(payload, _icp(payload))


def _analytical_qa_gate(payload: dict[str, Any]) -> dict[str, Any] | None:
    from check_routing import analytical_qa_gate
    from _model_utils import is_claude_payload as _icp
    return analytical_qa_gate(payload, _icp(payload))


def _inline_narration_gate(payload: dict[str, Any]) -> dict[str, Any] | None:
    from check_routing import inline_narration_gate
    from _model_utils import is_claude_payload as _icp
    return inline_narration_gate(payload, _icp(payload))


def _verdict_pre_nudge(payload: dict[str, Any]) -> dict[str, Any] | None:
    from check_routing import verdict_pre_nudge
    from _model_utils import is_claude_payload as _icp
    return verdict_pre_nudge(payload, _icp(payload))


def _structured_output_gate(payload: dict[str, Any]) -> dict[str, Any] | None:
    from check_routing import structured_output_gate
    from _model_utils import is_claude_payload as _icp
    return structured_output_gate(payload, _icp(payload))


def _dispatch_justification_gate(payload: dict[str, Any]) -> dict[str, Any] | None:
    from check_routing import dispatch_justification_gate
    from _model_utils import is_claude_payload as _icp
    return dispatch_justification_gate(payload, _icp(payload))


def _validate_dispatch_schema(payload: dict[str, Any]) -> dict[str, Any]:
    from validate_dispatch_schema import event_pre_task as vds_event
    from _model_utils import is_claude_payload as _icp
    return vds_event(payload, _icp(payload))


def _effort_injection_gate(payload: dict[str, Any]) -> dict[str, Any]:
    from effort_injection_gate import event_pre_task as eij_event
    from _model_utils import is_claude_payload as _icp
    return eij_event(payload, _icp(payload))


def _agent_call_tracker(payload: dict[str, Any]) -> dict[str, Any]:
    # Must run LAST in the pre_task chain: this is where round-trip count and
    # cumulative cost actually get charged. If it ran before the other pre_task
    # gates (or independently of this chain), dispatches denied by those gates
    # would still be charged even though no agent was ever spawned.
    from agent_call_tracker import event_preuse
    from _model_utils import is_claude_payload as _icp
    return event_preuse(payload, _icp(payload))


# Map (gate_chain, gate_name, action) → event_type for session_log.jsonl.
# Only mapped events are forwarded; unmapped events stay in gate_events.jsonl only.
_GATE_TO_SESSION_EVENT: dict[tuple[str, str, str], str] = {
    ("pre_write", "routing_gate", "deny"): "bulk_write_denied",
    ("pre_write", "routing_gate", "nudge"): "fix_loop_nudge",
    ("pre_write", "enforce_role_gate_write", "deny"): "scope_guard_deny",
    ("pre_write", "direct_new_file_gate", "deny"): "direct_codegen_misroute",
    ("pre_read", "check_read_repeat", "deny"): "similarity_deny",
    # GATE-BUG-005: OC-1 (check_routing.py) sets _gate_name_override="orchestrator_ceiling"
    # on its deny/nudge output so these no longer collide with RD-7/RD-5's entries above.
    ("pre_write", "orchestrator_ceiling", "deny"): "orchestrator_deny",
    ("pre_write", "orchestrator_ceiling", "nudge"): "orchestrator_nudge",
    # GATE-BUG-005: these pre_task gates previously had no entry at all, so their
    # denials never reached session_log.jsonl despite being session-blocking.
    ("pre_task", "enforce_worker_model", "deny"): "worker_model_deny",
    ("pre_task", "enforce_test_runner_model", "deny"): "test_runner_model_deny",
    ("pre_task", "opus_subagent", "deny"): "opus_subagent_deny",
}


def _log_gate_event(
    gate_chain: str,
    gate_name: str,
    action: str,
    result: dict[str, Any],
    payload: dict[str, Any]
) -> None:
    """Log gate event to data/gate_events.jsonl. Never raises; wrapped in try/except."""
    try:
        # Determine data directory relative to this script
        data_dir = DATA_DIR
        events_file = data_dir / "gate_events.jsonl"

        # Extract tool name from payload. Real hook payloads key this as
        # "tool_name" (Claude Code) / "tool" / "toolName" (Cursor) — never
        # a bare "tool" literal, so the old payload.get("tool", "unknown")
        # always fell through to the "unknown" default.
        from routing_state_manager import _tool_name as _real_tool_name
        tool = _real_tool_name(payload) or payload.get("tool") or "unknown"

        # Extract routing_role from payload. payload.get("routing_role") is
        # only a same-turn inline fallback (see verdict_pre_nudge) — the
        # authoritative source is _routing_role_in_current_turn, which checks
        # the declared-routing event log and transcript scan first.
        from routing_resolver import _routing_role_in_current_turn
        routing_role = (
            _routing_role_in_current_turn(payload)
            or payload.get("routing_role")
            or "unknown"
        )

        # Extract session_id
        session_id = payload.get("session_id", "")

        # Determine if result has a message
        has_message = False
        if isinstance(result, dict):
            has_message = bool(
                result.get("agent_message") or
                result.get("hookSpecificOutput", {}).get("additionalContext") or
                result.get("systemMessage")
            )

        # Build event record
        turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "gate_chain": gate_chain,
            "gate_name": gate_name,
            "gate_id": gate_name,
            "action": action,
            "tool": tool,
            "routing_role": routing_role,
            "session_id": session_id,
            "turn_id": turn_id,
            "has_message": has_message
        }

        # Ensure data directory exists
        data_dir.mkdir(parents=True, exist_ok=True)

        # Append to JSONL file
        with open(events_file, "a") as f:
            f.write(json.dumps(event) + "\n")
        # Forward mapped events to session_log.jsonl for Bucket 2 (waste prevention)
        event_type = _GATE_TO_SESSION_EVENT.get((gate_chain, gate_name, action))
        if event_type:
            append_session_log({
                "session_id": session_id,
                "event_type": event_type,
                "gate_chain": gate_chain,
                "gate_name": gate_name,
                "tool": tool,
                "routing_role": routing_role,
            })
    except Exception as e:
        # Never crash the gate chain for logging failures
        print(f"gate_event_log_failed: {e}", file=sys.stderr)
        _warn("gate_event_log_failed", {"file": "gates.py", "error": str(e)})


# Registry: gate name -> ordered chain of callables that return JSON dicts
GATE_REGISTRY: dict[str, list[Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "pre_read": [_enforce_role_gate_read, _analytical_qa_gate, _check_read_repeat, _build_file_graph, _routing_read, _file_hash_read, _file_graph_gate],
    "pre_write": [_enforce_role_gate_write, _structured_output_gate, _verdict_pre_nudge, _inline_narration_gate, _routing_gate, _shell_same_type_gate, _file_hash_pre],
    # GATE-BUG-007: _routing_gate (-> check_routing.event_gate) holds the BWS
    # Tier1/Tier2 concurrency-cap, grouping, serialize_verify, and stall-counter
    # checks for Task/Agent dispatches. It was registered under pre_write only,
    # so pre_task (the chain that actually fires for real Task/Agent events)
    # never reached it — those checks were dead code on the live dispatch path.
    # Placed right after explicit-model/tier checks so a cap/barrier denial
    # preempts the schema/tracker gates that follow.
    "pre_task": [_enforce_explicit_model, _dispatch_justification_gate, _explore_model_tier_gate, _routing_gate, _enforce_test_runner_model, _enforce_worker_model, _mixed_action_bundle_nudge, _dispatch_context_gate, _validate_dispatch_schema, _sequential_explore_gate, _effort_injection_gate, _agent_call_tracker],
}


def run_gate(name: str, payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Run the named gate chain. First deny short-circuits. Fail-open on any error.

    Returns the JSON dict to emit.
    """
    chain = GATE_REGISTRY.get(name)
    if not chain:
        default_result = emit_gate_result(gate_allow(), is_claude)
        _log_gate_event(name, "default", "allow", default_result, payload)
        return default_result

    last_output = emit_gate_result(gate_allow(), is_claude)
    last_gate_name = "default"
    pending_updated_input: dict | None = None
    for gate_fn in chain:
        gate_name = gate_fn.__name__.lstrip("_")
        try:
            output = gate_fn(payload)
            if output is not None:
                # Allow gate to signal its internal sub-gate for accurate event typing.
                # Pop so the override key never leaks to the hook caller.
                if isinstance(output, dict):
                    override = output.pop("_gate_name_override", None)
                    if override:
                        gate_name = override

                # Check if this is a deny response
                is_deny = False
                if isinstance(output, dict):
                    if output.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
                        is_deny = True
                    elif output.get("permission") == "deny":
                        is_deny = True

                if is_deny:
                    # Log deny decision
                    _log_gate_event(name, gate_name, "deny", output, payload)
                    return output

                # Determine action for this output
                action = "allow"
                has_message = False
                if isinstance(output, dict):
                    has_message = bool(
                        output.get("agent_message") or
                        output.get("hookSpecificOutput", {}).get("additionalContext") or
                        output.get("systemMessage")
                    )
                if has_message:
                    action = "nudge"

                # Log allow/nudge decision
                _log_gate_event(name, gate_name, action, output, payload)
                if isinstance(output, dict):
                    _ui = output.get("hookSpecificOutput", {}).get("updatedInput")
                    if _ui:
                        pending_updated_input = _ui
                last_output = output
                last_gate_name = gate_name
        except Exception as exc:  # noqa: BLE001
            # Fail-closed: gate exception → deny (critical gates must not silently pass)
            _log_gate_event(name, gate_fn.__name__ if hasattr(gate_fn, '__name__') else "unknown", "deny", {}, payload)
            return emit_gate_result(gate_deny(f"Gate {gate_fn.__name__} raised {type(exc).__name__}: {exc}"), is_claude)

    # Preserve updatedInput at end of run_gate in case later plain-allow gates
    # overwrote last_output and dropped the earlier effort rewrite.
    if pending_updated_input and isinstance(last_output, dict):
        hso = last_output.get("hookSpecificOutput", {})
        if hso.get("permissionDecision") == "allow":
            hso.setdefault("updatedInput", pending_updated_input)
    return last_output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", required=True)
    args = parser.parse_args()

    try:
        payload = read_stdin_json()
        is_claude = is_claude_payload(payload)
        result = run_gate(args.gate, payload, is_claude)
        print(json.dumps(result))

        # Check if it's a deny decision
        is_deny = False
        if isinstance(result, dict):
            if result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
                is_deny = True
            elif result.get("permission") == "deny":
                is_deny = True

        return 2 if is_deny else 0
    except Exception as exc:  # noqa: BLE001 — fail-closed for critical gates
        print(json.dumps(emit_gate_result(gate_deny(f"Gate chain error: {type(exc).__name__}: {exc}"), True)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
