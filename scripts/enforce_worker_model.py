#!/usr/bin/env python3
"""Gate: enforce routing_matrix.worker model for dispatches when the matrix
requires haiku (GATE-BUG-004).

routing_matrix.worker is defined per-intent in config.json (see
check_routing.get_routing_for_intent) but, like routing_matrix.test_runner
before GATE-BUG-003's fix, was never compared against a dispatched model at
Task-dispatch time. Only one intent currently sets routing_matrix.worker to
"haiku" (bulk-edit.worker), and this gate enforces ONLY that case: a
shell/create/mutation worker dispatch must declare model="haiku" (or another
fast-tier alias) when the *dispatch's own prompt text* classifies as
bulk-edit. Every other required value (sonnet, opus, ...) is left
unenforced here — those are judgment calls (e.g. design work) where a false
denial is worse than an occasional mismatch, per CLAUDE.md's Subagent Model
Override table. The "read" worker type is always exempt: read workers are
cheap regardless of which model is used and the matrix's `explorer` (not
`worker`) key governs them.

Classification correction (found while validating GATE-BUG-004 against real
routing_state.json data): the first version of this gate resolved intent via
agent_call_tracker._get_work_unit_intent(session_id), which reads
work_unit_intent -- a field stamped only by check_routing.classify_work_unit_intent()
on DIRECT-role Write/Edit/StrReplace calls, and whose *only* possible return
values are "mechanical"/"reasoning". That function can never produce
"bulk-edit", so the gate's haiku-check branch was unconditionally skipped on
every real dispatch -- a permanent no-op confirmed by 0/157 sessions in
routing_state.json ever showing work_unit_intent="bulk-edit". This version
instead classifies the dispatch's own prompt text via
check_routing.classify_dispatch_work() -- the same function PROD-19
(event_subagent_start) already uses to detect bulk-edit dispatches -- so
"bulk-edit" is actually reachable, and there is no session-state staleness
risk since the classification is computed fresh from the payload being
gated, not from state stamped by an earlier, possibly unrelated tool call.

Registered in gates.py's pre_task chain, immediately after
enforce_test_runner_model. Fails open (allow) whenever the prompt is
unstructured, unparsable, missing a model, or unclassifiable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import GateResult, gate_allow, gate_deny  # noqa: E402
from _model_utils import routing_platform, tier_model_slug, tier_of_model  # noqa: E402

# Worker types this gate enforces against routing_matrix.worker. "read" is
# always exempt (governed by routing_matrix.explorer instead).
_ENFORCED_WORKER_TYPES = ("shell", "create", "mutation")


def _required_worker_model(task_text: str) -> str | None:
    """Return routing_matrix.worker for this dispatch's own classified work,
    or None if it can't be resolved (fail-open trigger)."""
    try:
        from check_routing import classify_dispatch_work, get_routing_for_intent  # noqa: PLC0415

        label = classify_dispatch_work(task_text)
        routing = get_routing_for_intent(label)
        model = routing.get("worker") if isinstance(routing, dict) else None
        return str(model) if model else None
    except Exception:  # noqa: BLE001
        return None


def check_worker_model(payload: dict[str, Any]) -> GateResult:
    """Deny a structured shell/create/mutation worker dispatch that declares
    a non-haiku model when routing_matrix.worker for the dispatch's own
    classified work is haiku. Any other required value is not enforced."""
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

    worker = parsed_prompt.get("worker", "")
    if worker not in _ENFORCED_WORKER_TYPES:
        return gate_allow()  # "read" (or unknown) worker type -- not enforced

    model = str(tool_input.get("model", "") or "")
    if not model:
        return gate_allow()  # enforce_explicit_model already denies a missing model

    # create workers are always mechanical (template fill, no judgment) — require
    # haiku unconditionally without going through classify_dispatch_work.
    if worker == "create":
        required_model = "haiku"
    else:
        required_model = _required_worker_model(prompt)
        if not required_model:
            return gate_allow()  # can't classify the dispatch -- fail open
        if required_model.lower() != "haiku":
            return gate_allow()  # only the haiku case is enforced (GATE-BUG-004)

    # Check if declared model is fast-tier (haiku, composer-2.5-fast, etc.)
    if tier_of_model(model) == "fast":
        return gate_allow()

    platform = routing_platform(payload)
    fast_slug = tier_model_slug("fast", platform)
    return gate_deny(
        f"Task dispatch rejected: this dispatch's prompt classifies as bulk-edit "
        f"work, whose routing_matrix.worker requires a fast-tier model for "
        f"worker='{worker}' dispatches, but this dispatch declares model='{model}'. "
        f"Use model='{fast_slug}' (or another fast-tier slug), or narrow the dispatch "
        "so it no longer classifies as bulk-edit (fewer file references, no bulk-scope phrasing)."
    )


def check_mixed_action_bundle(payload: dict[str, Any]) -> GateResult:
    """Nudge (never deny) when a dispatch's prompt mixes judgment language
    with a bulk mechanical file scope (>= _BULK_EDIT_FILE_THRESHOLD file
    refs). classify_dispatch_work treats this as 'ambiguous' and allows it
    silently by design (a false-positive nudge on a genuinely ambiguous
    dispatch costs more than a missed one) — this only adds a visible
    suggestion to split, per CLAUDE.md's mixed-action routing-table row.
    Telemetry-only for now (see routing_resolver.mixed_action_bundle_signal's
    mixed_action_bundle_unenforced event) until real frequency data justifies
    an actual deny."""
    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Task", "Agent"):
        return gate_allow()

    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return gate_allow()

    prompt = tool_input.get("prompt", "")
    if not isinstance(prompt, str) or not prompt:
        return gate_allow()

    try:
        from routing_resolver import mixed_action_bundle_signal  # noqa: PLC0415
        file_count = mixed_action_bundle_signal(prompt)
    except Exception:  # noqa: BLE001
        return gate_allow()

    if not file_count:
        return gate_allow()

    return gate_allow(
        f"This dispatch's prompt mixes judgment language with {file_count} file "
        "references. If only 1-2 steps need judgment and the rest are mechanical "
        "edits/reads, split it: dispatch the mechanical majority as model='haiku', "
        "and handle the judgment step(s) inline or as a small chained sonnet "
        "dispatch, instead of folding the whole bundle into one sonnet worker. "
        "See CLAUDE.md's Subagent Model Override table, mixed-action row."
    )


def event_pre_task(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Gate function for pre_task chain. Returns host-specific JSON."""
    from _common import emit_gate_result  # noqa: E402

    result = check_worker_model(payload)
    return emit_gate_result(result, is_claude)


def event_pre_task_mixed_action(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Gate function for pre_task chain. Returns host-specific JSON."""
    from _common import emit_gate_result  # noqa: E402

    result = check_mixed_action_bundle(payload)
    return emit_gate_result(result, is_claude)
