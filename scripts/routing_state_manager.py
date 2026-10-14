#!/usr/bin/env python3
"""Routing state manager — payload extraction helpers, session key management,
dispatch-debt tracking, and bulk-shell gate messages.

Extracted from check_routing.py. All callers in check_routing.py use
re-export shims; this module can also be imported directly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    SESSION_DISPATCH_DENY_THRESHOLD,
    append_event,
    config_get,
    load_json_state,
    subagent_id_of,
)
from _model_utils import est_cold_write_cost_usd, infra_tokens, price_for, load_pricing  # noqa: E402
from context_estimator import estimate_dispatch_context  # noqa: E402
from session_state import ROUTING_STATE_FILE  # noqa: E402

# ---------------------------------------------------------------------------
# Module-level gate message constants (referenced by event_gate / event_scan)
# ---------------------------------------------------------------------------

_DISPATCH_OVERRIDE_MARKERS = (
    "direct override",
    "no subagent dispatch",
    "single-file direct",
    "no task dispatch",
)

_SIMPLE_MARKERS = (
    "append ",
    "add a line",
    "rename ",
    "tweak ",
    "update the field",
    "update a field",
    "fix typo",
    "one line",
    "single field",
    "config field",
)

_GATE_AGENT_MESSAGE = (
    "[cost-router] Invoke checkpoint required: apply the model-router rule (Role Legend "
    "and Decision Table), then emit one line `Routing: <ROLE> — <reason>` before "
    "Edit/Write/Shell/Task. Example: `Routing: DIRECT — single-file refactor, spec complete.` "
    "After emitting that line, retry the same tool (Write/StrReplace) — do not "
    "substitute Shell to bypass the gate."
)

_PROACTIVE_HINT = (
    "[cost-router] Execution turn detected — before the first Edit/Write/Shell/Task: "
    "apply the model-router rule (Role Legend and Decision Table) and emit `Routing: <ROLE> — <reason>`."
)

# ---------------------------------------------------------------------------
# Payload extraction helpers
# ---------------------------------------------------------------------------


def _is_claude_payload(payload: dict[str, Any]) -> bool:
    return bool(payload.get("hook_event_name")) and not payload.get("cursor_version")


def _text_for_prompt(payload: dict[str, Any]) -> str:
    for key in ("prompt", "text", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    # TICKET-011: emit extraction_fallback when no prompt text found.
    try:
        append_event({"type": "extraction_fallback", "reason": "prompt_text_empty"})
    except Exception:  # noqa: BLE001
        pass
    return ""


def _text_for_assistant(payload: dict[str, Any]) -> str:
    for key in ("text", "last_assistant_message", "thought", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _tool_name(payload: dict[str, Any]) -> str | None:
    for key in ("tool_name", "tool", "toolName"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _turn_id(payload: dict[str, Any]) -> str | None:
    """Host-provided per-turn identifier.

    Cursor: `generation_id` ("changes with every user message").
    Claude Code: `prompt_id` (per submitted prompt).
    """
    for key in ("generation_id", "generationId", "prompt_id", "promptId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _conversation_key(payload: dict[str, Any]) -> str:
    """Stable conversation identifier, used to correlate sub-agent lifecycle
    events with the tool calls that run inside them."""
    for key in (
        "parent_conversation_id",
        "conversation_id",
        "conversationId",
        "session_id",
        "sessionId",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    env_id = os.environ.get("CURSOR_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    return env_id or "unknown-session"


# Local alias — subagent_id_of is the canonical implementation in _common.
_subagent_id = subagent_id_of

# ---------------------------------------------------------------------------
# Dispatch debt tracking
# ---------------------------------------------------------------------------


def _cumulative_dispatch_cost_usd(session_id: str) -> float:
    """RT-02/BWS-T2-004: cumulative estimated dispatch cost for this session.

    Reads agent_call_stats.json's cumulative_cost_usd, which spawn_tracker's
    finalize_dispatch_charge() increments on every successful SubagentStop
    (RT-01-B: success-only, so aborted/denied dispatches are never counted).
    Shared by _dispatch_debt_active (escape-hatch trigger) and
    _session_dispatch_cap_active (fan-out cap) so both read the one signal
    instead of each re-deriving it.
    """
    stats = load_json_state(DATA_DIR / "agent_call_stats.json")
    this_session = stats.get("sessions", {}).get(session_id, {})
    return float(this_session.get("cumulative_cost_usd", 0.0))


def _dispatch_debt_active(session_id: str) -> bool:
    """RT-02: Check if the session has accumulated dispatch debt.

    Returns True if either cost or all-inline trigger fires; fail-open False on error.

    Trigger 1 (cost): cumulative_cost_usd >= budget_usd * min(max(warning_pcts) / 100,
        concurrency.session_budget_pct) — aligned with the BWS-T2-004 dispatch cap so
        relief engages no later than the cap denies new fan-out.
    Trigger 2 (all-inline): no subagents dispatched yet AND gate call count has
        reached SESSION_DISPATCH_DENY_THRESHOLD — prevents deadlock when the
        session is running entirely inline without ever dispatching a Task.
    """
    try:
        budget_usd: float = float(config_get("session_cost_budget.budget_usd") or 5.0)
        warning_pcts: list = list(config_get("session_cost_budget.warning_pcts") or [50, 80])
        session_budget_pct: float = float(config_get("concurrency.session_budget_pct") or 0.6)
        relief_threshold_pct: float = min(max(warning_pcts) / 100.0 if warning_pcts else 0.8, session_budget_pct)

        # Trigger 1: cumulative cost has reached the relief threshold (aligned with deny cap).
        cumulative_cost = _cumulative_dispatch_cost_usd(session_id)
        if cumulative_cost >= budget_usd * relief_threshold_pct:
            return True

        # Trigger 2: all-inline deadlock guard — no dispatches yet but gate calls
        # have hit the session deny threshold.
        rstate = load_json_state(ROUTING_STATE_FILE)
        entry = (rstate.get("sessions", {}).get(session_id, {}) or {})
        task_dispatched = int(entry.get("task_dispatched_count_session", 0))
        total_gate_calls = int(entry.get("total_gate_calls_session", 0))
        if task_dispatched == 0 and total_gate_calls >= SESSION_DISPATCH_DENY_THRESHOLD:
            return True

        return False
    except Exception as e:  # noqa: BLE001
        try:
            append_event({
                "type": "config_error",
                "session_id": session_id,
                "error_type": type(e).__name__,
                "config_key": "_dispatch_debt_active",
            })
        except Exception:  # noqa: BLE001
            pass
        return False


def _session_dispatch_cap_active(session_id: str) -> bool:
    """BWS-T2-004: cumulative fan-out cap — a swarm of individually-compliant
    Task/Agent dispatches can sum to monolith-scale cost with no single
    dispatch ever tripping a per-dispatch gate (unboundedOrchestrator obs-4).

    Unlike _dispatch_debt_active (an escape hatch: debt active -> ALLOW an
    inline bypass of the dispatch requirement elsewhere), this is a cap:
    active -> DENY new Task/Agent fan-out. The two are complementary, not
    contradictory — once cumulative cost is high, both "stop forcing new
    subagent dispatches" (here) and "stop requiring dispatch for mechanical
    work" (the escape hatch) point the same direction: stop spending on the
    dispatch loop. This fires at a lower threshold (session_budget_pct,
    default 0.6) than the escape hatch's high_water_pct (default 0.8) so
    fan-out is capped before the deadlock-breaker even needs to engage.

    Fail-open False on any error — a state-read fault must never block a
    dispatch outright (that would be a denial-of-service on the gate itself).
    """
    try:
        budget_usd: float = float(config_get("session_cost_budget.budget_usd") or 5.0)
        session_budget_pct: float = float(config_get("concurrency.session_budget_pct") or 0.6)
        cumulative_cost = _cumulative_dispatch_cost_usd(session_id)
        return cumulative_cost >= budget_usd * session_budget_pct
    except Exception as e:  # noqa: BLE001
        try:
            append_event({
                "type": "config_error",
                "session_id": session_id,
                "error_type": type(e).__name__,
                "config_key": "_session_dispatch_cap_active",
            })
        except Exception:  # noqa: BLE001
            pass
        return False


def _est_context_input_cost_usd(tokens: int, model: str | None) -> float:
    """DGC-205: estimate input cost for given tokens and model.

    Mirrors the private implementation in context_estimator.py.
    """
    if tokens <= 0:
        return 0.0
    try:
        pricing = load_pricing()
        price = price_for(model, pricing)
        per_1k = float(price.get("input_per_1k", 0.003))
        return (tokens / 1000.0) * per_1k
    except Exception:  # noqa: BLE001
        return 0.0


def _session_budget_cap_deny_message(
    session_id: str, task_text: str | None = None, model: str | None = None
) -> str:
    """Session budget cap deny message with optional token-aware reservation.

    DGC-205: Compute and report the reserve required for the new dispatch's
    estimated tokens via estimate_dispatch_context, in addition to cumulative cost.
    """
    try:
        cumulative_cost = _cumulative_dispatch_cost_usd(session_id)
    except Exception:  # noqa: BLE001
        cumulative_cost = 0.0
    budget_usd = float(config_get("session_cost_budget.budget_usd") or 5.0)
    pct = float(config_get("concurrency.session_budget_pct") or 0.6)
    cap_usd = budget_usd * pct
    remaining_budget = max(0.0, cap_usd - cumulative_cost)

    # DGC-205: estimate tokens for the new dispatch being denied
    reserve_usd = 0.0
    reserve_tokens = 0
    if task_text and model:
        try:
            est = estimate_dispatch_context(task_text, model=model)
            reserve_tokens = est.get("total_tokens", 0)
            reserve_usd = _est_context_input_cost_usd(reserve_tokens, model)
        except Exception:  # noqa: BLE001
            pass

    reserve_note = ""
    if reserve_tokens > 0 and reserve_usd > 0:
        reserve_note = (
            f"\n\nToken-aware reservation: this dispatch would require "
            f"~{reserve_tokens:,} tokens (est. ${reserve_usd:.2f}). "
            f"Remaining budget from cap is ${remaining_budget:.2f}."
        )

    override_note = ""
    try:
        limit = int(config_get("concurrency.session_budget_override_limit") or 0)
        if limit > 0:
            rstate = load_json_state(ROUTING_STATE_FILE)
            used = int(((rstate.get("sessions", {}) or {}).get(session_id, {}) or {}).get("session_budget_override_count", 0) or 0)
            override_note = (
                f" Override: include the token session-budget-override in the dispatch "
                f"(limit {limit} per session, {max(limit - used, 0)} uses remaining; "
                "dispatch-budget-override does not satisfy this check)."
            )
    except Exception:  # noqa: BLE001
        override_note = ""
    return (
        f"[cost-router] Session dispatch budget cap: cumulative subagent cost "
        f"(${cumulative_cost:.2f}) has reached {pct * 100:.0f}% of the session "
        f"budget (${budget_usd:.2f}) — new Task/Agent fan-out is denied for this "
        "session. Finish with the units already dispatched, merge remaining work "
        "into an in-flight worker via resume, or continue inline if the remaining "
        "work is small enough." + reserve_note + override_note
    )


# ---------------------------------------------------------------------------
# Bulk-shell gate messages
# ---------------------------------------------------------------------------


def _bulk_shell_ask_message(command: str, model: str | None = None) -> str:
    infra = infra_tokens(model)
    cold_usd = est_cold_write_cost_usd(model)
    return (
        "[cost-router] Bulk verification blocked inline — DISPATCH a Bash-only shell "
        "subagent now: Agent(model=\"haiku\", subagent_type=\"general-purpose\") with "
        f"payload {json.dumps({'worker': 'shell', 'command': command})} (prompt `Run: <cmd>` "
        "only). Do NOT ask the user to run it and do NOT retry parent Shell. "
        "The worker must report results/failures only and must not edit code. "
        f"Context: a new subagent pays ~{infra:,} infra tokens cold-write "
        f"(~${cold_usd:.2f} est.). Emit `bulk-verify: subagent` in your Routing: line. "
        "Only if the user explicitly asks to run it themselves, use "
        "`bulk-verify: user-local` (print command only)."
    )


def _bulk_shell_user_runs_message(command: str) -> str:
    return (
        "[cost-router] User-local path — print this command for the user; do not "
        f"Shell or dispatch Task:\n\n```\n{command}\n```"
    )


def _bulk_shell_dispatch_message() -> str:
    return (
        "[cost-router] Bulk verification must run in a fast shell subagent, not parent "
        "Shell (any model tier). Dispatch Task(model: haiku, "
        "subagent_type: general-purpose) with the command; let the worker Shell it. Do not "
        "retry parent Shell for this command."
    )


def _light_tier_redirect_message(command: str, touched_file: str) -> str:
    """TICKET-040 (fix-7): the just-dispatched fix classified as LIGHT tier
    (single file, no design ambiguity) but this verification command is
    scoped to the whole/bulk suite instead of the touched file — redirect to
    the cheap scoped command instead of paying for a mandatory full
    independent re-run."""
    return (
        "[cost-router] TICKET-040 light-tier fix — this dispatch touched only "
        f"`{touched_file}` with no schema/API-surface change, so it does not need "
        "a full independent re-verification run. Re-run scoped to that file/its "
        f"test module instead of: `{command}`. A single-file-targeted command is "
        "not treated as bulk verification and does not require the bulk-verify "
        "ask/dispatch flow. If the fix's actual blast radius is broader than one "
        "file, dispatch the full-suite bulk-verify subagent as normal."
    )
