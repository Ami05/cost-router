#!/usr/bin/env python3
"""cost-router: dispatch budget enforcement.

Extracted from agent_call_tracker.py. Owns budget ladder checks, grouping
enforcement, orchestrator-turn cost estimates, model-tier validation, and
the explore-haiku warning.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    TASK_TARGET_HASH_CHARS,
    append_event,
    config_get,
    hash_target,
    load_json_state,
    session_id_of,
)
from _model_utils import est_cold_write_cost_usd, tier_of_model  # noqa: E402
from dispatch_budget_gate import should_deny_by_bands, would_deny_in_warn  # noqa: E402
from estimate_dispatch_context import (  # noqa: E402
    budget_deny_message,
    budget_nudge_message,
    check_grouping_enforcement,
    estimate_dispatch_context,
)

ROUTING_STATE_FILE = DATA_DIR / "routing_state.json"

DISPATCH_BUDGET_OVERRIDE_RE = re.compile(r"dispatch-budget-override", re.IGNORECASE)
_TASK_TOOLS = frozenset({"Task", "Agent"})

EXPLORE_HAIKU_WARNING = (
    "[agent-tracker] Explore agent launched without model='haiku'. "
    "Explore agents read files and return summaries — Sonnet cache write cost is ~$0.56/spawn. "
    "Add model='haiku' to this Agent call to avoid unnecessary spend."
)

# TICKET-04: round-trip cost is not uniform - a haiku read dispatch is far
# cheaper than an opus mutation dispatch. Weight the ceiling accordingly
# instead of charging every dispatch a flat 1.
#
# NOTE: tier_of_model() only has a coarse fast/reasoning split -
# REASONING_MARKERS = ("sonnet", "opus", ...) in _model_utils.py means sonnet
# and opus both return "reasoning". That's fine for cost-tier gating elsewhere,
# but wrong here: sonnet should NOT weight the same as opus. Classify directly
# off the model string instead. The opus=2.0 branch remains for the
# legacy weight path; enforce_explicit_model permanently blocks opus subagents
# opus subagents by default.
_OPUS_RE = re.compile(r"\bopus\b", re.IGNORECASE)
_FAST_RE = re.compile(r"\b(haiku|fast|composer|mini)\b", re.IGNORECASE)


def _has_dispatch_budget_override(*texts: str) -> bool:
    # Check routing context (assistant reasoning) only — not texts[0] (the Task prompt).
    # This prevents agents from injecting the override keyword into Task prompts.
    # To override: include 'dispatch-budget-override' in your Routing line or reasoning.
    return any(DISPATCH_BUDGET_OVERRIDE_RE.search(t) for t in texts[1:] if t)


def _orchestrator_turn_cost_usd(session_id: str, session_model: str | None) -> float:
    """CF-03: estimate cumulative orchestrator context-reload cost for session.

    Reads orchestrator_turns from routing_state.json and multiplies by a
    per-turn flat estimate keyed on model tier (from config).
    Returns 0.0 on any error (fail-open).
    """
    try:
        state = load_json_state(ROUTING_STATE_FILE)
        sessions = state.get("sessions") if isinstance(state, dict) else None
        if not isinstance(sessions, dict):
            return 0.0
        entry = sessions.get(session_id)
        if not isinstance(entry, dict):
            return 0.0
        turns = int(entry.get("orchestrator_turns", 0))
        if turns <= 0:
            return 0.0
        tier = tier_of_model(session_model)
        cost_map: dict[str, float] = dict(
            config_get(
                "session_cost_budget.orchestrator_cost_per_turn",
                {"reasoning": 0.25, "sonnet": 0.05, "fast": 0.01},
            )
        )
        # "reasoning" tier covers opus; "sonnet" covers sonnet models; "fast" covers haiku
        per_turn = cost_map.get(tier, cost_map.get("sonnet", 0.05))
        return turns * per_turn
    except Exception:  # noqa: BLE001
        return 0.0


def _budget_enforcement_message(
    payload: dict[str, Any],
    tool_input: dict[str, Any],
    is_claude: bool,
    tool_name: str,
    task_prompt: str,
    routing_text: str,
) -> tuple[str | None, bool, bool]:
    """Return (message, should_deny, was_overridden) for dispatch budget ladder. Claude only."""
    if not is_claude or tool_name not in _TASK_TOOLS:
        return None, False, False

    prompt = task_prompt
    model = tool_input.get("model")
    if not isinstance(model, str):
        model = None
    est = estimate_dispatch_context(prompt, model=model)
    # Count paths extracted from dispatch (safe import with fallback)
    paths_count = 0
    try:
        from split_planner import count_dispatch_paths  # noqa: E402
        paths_count = count_dispatch_paths(prompt)
    except Exception:
        paths_count = 0
    try:
        append_event({
            "type": "dispatch_context_estimate",
            "session_id": session_id_of(payload),
            "model": model,
            "est_tokens": est.get("total_tokens", 0),
            "infra_tokens": est.get("infra_tokens", 0),
            "prompt_tokens": est.get("prompt_tokens", 0),
            "file_tokens": est.get("file_tokens", 0),
            "pct_of_budget": est.get("pct_of_window", 0),
            "over_budget": bool(est.get("over_budget")),
            "estimate_failed": bool(est.get("estimate_failed")),
            "target_hash": hash_target(prompt[:TASK_TARGET_HASH_CHARS]),
            "paths_extracted_count": paths_count,
        })
    except OSError:
        pass

    if est.get("estimate_failed"):
        return (
            "[cost-router] Dispatch context estimate failed — proceeding unverified; "
            "next dispatch may be denied if over-budget",
            False,
            False,
        )

    override = _has_dispatch_budget_override(prompt, routing_text)
    band_deny = should_deny_by_bands(est)
    if would_deny_in_warn(est):
        try:
            append_event({
                "type": "dispatch_budget_would_deny",
                "session_id": session_id_of(payload),
                "model": model,
                "est_tokens": est.get("total_tokens", 0),
                "overridden": override,
                "target_hash": hash_target(prompt[:TASK_TARGET_HASH_CHARS]),
            })
        except OSError:
            pass
    if est.get("over_budget") or band_deny:
        if override:
            try:
                append_event({
                    "type": "override_used",
                    "override_type": "dispatch_budget",
                    "session_id": session_id_of(payload),
                    "est_tokens": est.get("total_tokens", 0),
                    "pct_of_window": est.get("pct_of_window", 0),
                    "model": model,
                    "target_hash": hash_target(prompt[:TASK_TARGET_HASH_CHARS]),
                })
            except OSError:
                pass
            return None, False, True
        deny_msg = budget_deny_message(est, prompt=task_prompt)
        try:
            append_event({
                "type": "dispatch_budget_deny",
                "session_id": session_id_of(payload),
                "model": model,
                "est_tokens": est.get("total_tokens", 0),
                "band_deny": band_deny,
                "split_plan_present": (band_deny and "split plan not available" not in deny_msg),
                "target_hash": hash_target(prompt[:TASK_TARGET_HASH_CHARS]),
            })
        except OSError:
            pass
        return deny_msg, True, False

    nudge = budget_nudge_message(est, advisory_only=False, prompt=task_prompt)
    if nudge:
        return nudge, False, False
    return None, False, False


def check_cost_warnings(
    session_id: str,
    session_model: str | None,
    this_session: dict[str, Any],
    dispatch_model: str,
) -> tuple[float, list[int], list[str]]:
    """CF-01/02: project per-dispatch cost for warnings.

    Returns (dispatch_cost, pending_warning_pcts, warning_messages).
    """
    dispatch_cost = est_cold_write_cost_usd(dispatch_model if dispatch_model else None)
    projected_cumulative = this_session.get("cumulative_cost_usd", 0.0) + dispatch_cost

    budget_usd: float = float(config_get("session_cost_budget.budget_usd", 5.0))
    warning_pcts: list[int] = list(config_get("session_cost_budget.warning_pcts", [50, 80]))
    orchestrator_cost = _orchestrator_turn_cost_usd(session_id, session_model)
    cumulative = projected_cumulative + orchestrator_cost
    warnings_emitted: list[int] = this_session.get("cost_warnings_emitted", [])

    pending_pcts: list[int] = []
    messages: list[str] = []
    for pct in sorted(warning_pcts):
        if cumulative >= budget_usd * pct / 100 and pct not in warnings_emitted:
            pending_pcts.append(pct)
            if pct < 80:
                msg = (
                    f"[cost-router] Mid-session cost estimate: ${cumulative:.2f} "
                    f"({pct}% of ${budget_usd:.2f} budget). "
                    "Consider chaining remaining work to reduce dispatch overhead."
                )
            else:
                msg = (
                    f"[cost-router] Mid-session cost estimate: ${cumulative:.2f} "
                    f"({pct}% of ${budget_usd:.2f} budget). "
                    "Session is approaching budget limit. Consider downgrading to Sonnet if on Opus, or wrapping up."
                )
            messages.append(msg)

    return dispatch_cost, pending_pcts, messages


def commit_cost_warnings(
    session_id: str,
    session_model: str | None,
    this_session: dict[str, Any],
    dispatch_cost: float,
    pending_pcts: list[int],
    is_claude: bool = False,
) -> None:
    """Emit cost_budget_warning events after an allowed dispatch.

    On Claude Code, spawn_tracker.finalize_dispatch_charge (SubagentStop) is the
    sole writer of cumulative_cost_usd, so the stored total is not incremented
    here. Non-Claude hosts (Cursor) register no finalize hook and keep the
    dispatch-time increment.
    """
    if not is_claude:
        this_session["cumulative_cost_usd"] = (
            this_session.get("cumulative_cost_usd", 0.0) + dispatch_cost
        )
    if pending_pcts:
        warnings_emitted = list(this_session.get("cost_warnings_emitted", []))
        budget_usd = float(config_get("session_cost_budget.budget_usd", 5.0))
        orchestrator_cost = _orchestrator_turn_cost_usd(session_id, session_model)
        cumulative = this_session.get("cumulative_cost_usd", 0.0) + orchestrator_cost
        if is_claude:
            cumulative += dispatch_cost  # matches check_cost_warnings projection
        for pct in pending_pcts:
            if pct not in warnings_emitted:
                warnings_emitted.append(pct)
            try:
                append_event({
                    "type": "cost_budget_warning",
                    "session_id": session_id,
                    "cumulative_cost_usd": cumulative,
                    "threshold_pct": pct,
                    "budget_usd": budget_usd,
                })
            except OSError:
                pass
        this_session["cost_warnings_emitted"] = warnings_emitted


def check_grouping(
    payload: dict[str, Any],
    tool_input: dict[str, Any],
    session_id: str,
    subagent_type: str,
    model: str | None,
    task_prompt: str,
    routing_text: str,
    this_session: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Run grouping enforcement check. Returns (should_deny, extra_messages).

    Mutates this_session to record last_grouping_deny_resume when denied.
    """
    from _common import now_iso  # noqa: PLC0415

    # resume_ignored: if previous grouping deny suggested a resume but this
    # dispatch is a fresh cold-start (no resume param), emit the event.
    _last_deny_resume = this_session.get("last_grouping_deny_resume")
    _resume_param = tool_input.get("resume")
    if (
        isinstance(_last_deny_resume, str)
        and _last_deny_resume
        and not (isinstance(_resume_param, str) and _resume_param.strip())
    ):
        try:
            append_event({
                "type": "resume_ignored",
                "session_id": session_id,
                "suggested_resume_id": _last_deny_resume,
                "target_hash": hash_target(task_prompt[:TASK_TARGET_HASH_CHARS]),
            })
        except OSError:
            pass

    grouping = check_grouping_enforcement(
        tool_input,
        routing_text=routing_text,
        transcript_path=payload.get("transcript_path"),
    )
    if not grouping.get("should_deny"):
        return False, []

    msg = grouping.get("message") or ""
    messages = [msg] if msg else []
    _deny_resume = grouping.get("resume")
    try:
        append_event({
            "type": "dispatch_grouping_violation",
            "action": "blocked",
            "session_id": session_id,
            "resume_suggested": _deny_resume,
            "reason": grouping.get("reason"),
            "path_resolution_fallback": bool(grouping.get("path_resolution_fallback")),
            "target_hash": hash_target(task_prompt[:TASK_TARGET_HASH_CHARS]),
        })
    except OSError:
        pass
    # Track the suggested resume so next Task can emit resume_ignored if skipped
    if isinstance(_deny_resume, str) and _deny_resume:
        this_session["last_grouping_deny_resume"] = _deny_resume
    this_session["agents"].append(
        {"type": subagent_type, "model": model, "ts": now_iso(), "warned": False, "denied": True}
    )
    this_session["total_agents"] = len(this_session["agents"])
    return True, messages
