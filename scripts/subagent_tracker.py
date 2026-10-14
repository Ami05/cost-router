#!/usr/bin/env python3
"""Subagent lifecycle tracking and model tier validation.

Extracted from check_routing.py. All callers in check_routing.py use
re-export shims; this module can also be imported directly.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    ACTIVE_SUBAGENT_STALE_TTL_SECONDS,
    subagent_id_of,
)
from _model_utils import tier_of_model  # noqa: E402

# ---------------------------------------------------------------------------
# Subagent context detection
# ---------------------------------------------------------------------------

def _is_subagent_context(payload: dict[str, Any]) -> bool:
    """True when this tool call originates from a dispatched worker (Task).

    Claude Code stamps sub-agent tool events with `agent_id`/`agent_type`;
    Cursor exposes `subagent_id`/`subagent_type`/`parent_conversation_id` /
    `is_parallel_worker` on the sub-agent lifecycle. Dispatched workers are
    downstream of an already-routed parent decision, so their mutations are
    exempt from the routing gate."""
    for key in ("agent_id", "agent_type", "subagent_id", "subagent_type", "parent_conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return True
    if payload.get("is_parallel_worker") is True:
        return True
    return False


# PROD-6: Cursor worker tool payloads lack parent linkage; use a global active counter.
_GLOBAL_SUBAGENT_KEY = "__cursor_global_active_subagents__"


def _effective_subagent_count(info: Any) -> int:
    """Stale-aware active-subagent count: returns 0 if the marker is missing,
    non-positive, or older than ACTIVE_SUBAGENT_STALE_TTL_SECONDS."""
    if not isinstance(info, dict):
        return 0
    count = int(info.get("count", 0))
    if count <= 0:
        return 0
    if (time.time() - float(info.get("ts", 0))) > ACTIVE_SUBAGENT_STALE_TTL_SECONDS:
        return 0
    return count


def _in_active_subagent(payload: dict[str, Any], state: dict[str, Any], is_claude: bool) -> bool:
    """Backstop for hosts whose in-subagent tool payload lacks explicit markers:
    a subagentStart/Stop counter keyed by conversation. A stale marker (missed
    stop) is ignored after SUBAGENT_TTL_SECONDS so the gate is never disabled
    indefinitely.

    On Cursor, ALSO checks a global (non-conversation-keyed) counter — see
    `_GLOBAL_SUBAGENT_KEY` docstring-comment above for why the per-conversation
    key alone is insufficient on this host. This is a deliberate, documented
    trade-off (PROD-6 item (b)): while ANY worker is active anywhere in this
    install, the parent's own mutations are also exempted (a false-allow),
    accepted because the alternative — the worker's own mutations being
    false-denied, which is what happens without this fallback — is worse."""
    from routing_state_manager import _conversation_key  # noqa: PLC0415

    subagents = state.get("subagents", {})

    def _active(info: Any) -> bool:
        if not isinstance(info, dict) or int(info.get("count", 0)) <= 0:
            return False
        return (time.time() - float(info.get("ts", 0))) <= ACTIVE_SUBAGENT_STALE_TTL_SECONDS

    conversation = _conversation_key(payload)
    if _active(subagents.get(conversation)):
        return True
    if not is_claude and _active(subagents.get(_GLOBAL_SUBAGENT_KEY)):
        return True
    return False


# ---------------------------------------------------------------------------
# Model cost ranking
# ---------------------------------------------------------------------------

_MODEL_COST_RANK: dict[str, int] = {"haiku": 0, "sonnet": 1, "opus": 2}


def _model_cost_rank(model: str | None) -> int:
    """Return a cost-rank integer for a model name string (0=cheapest, 2=most expensive).

    Uses segment matching so full model IDs like 'claude-opus-4-5' resolve correctly.
    Falls back to 1 (sonnet-tier) for unrecognised models.
    """
    if not model:
        return 1
    from _model_utils import _model_segments  # noqa: PLC0415
    segs = _model_segments(model)
    for name, rank in sorted(_MODEL_COST_RANK.items(), key=lambda kv: -kv[1]):
        if name in segs:
            return rank
    # Fall back via tier
    t = tier_of_model(model)
    return 0 if t == "fast" else 1


# ---------------------------------------------------------------------------
# Tier transition tracking
# ---------------------------------------------------------------------------

def _tier_transitions(history: list[str]) -> int:
    return sum(1 for i in range(1, len(history)) if history[i] != history[i - 1])


# ---------------------------------------------------------------------------
# Tier mismatch nudge/deny messages
# ---------------------------------------------------------------------------

def _dispatch_tier_mismatch_deny_message(label: str, requested_model: str | None, expected_worker: str) -> str:
    shown = requested_model or "an unspecified model"
    return (
        f"[cost-router] This dispatched Task reads as {label} work, "
        f"but targets {shown} — above the routing_matrix recommendation "
        f"(worker: {expected_worker}). Use Task(model: \"{expected_worker}\") "
        "unless real judgment is genuinely needed here.\n"
        f"Retry with: model=\"{expected_worker}\""
    )


def _dispatch_bulk_edit_on_sonnet_nudge_message(requested_model: str | None) -> str:
    shown = requested_model or "an unspecified model"
    return (
        f"[cost-router] This dispatched Task reads as bulk-edit work, "
        f"but targets {shown} — above the routing_matrix recommendation "
        "(worker: haiku). Bulk-edit work (mechanical changes across many files) "
        "is cheaper on Task(model: \"haiku\"). Proceeding, but consider "
        "downgrading to haiku for cost savings."
    )


def _dispatch_mechanical_on_reasoning_nudge_message(model: str | None) -> str:
    shown = model or "the parent session's reasoning-tier model (no model specified)"
    return (
        f"[cost-router] This dispatched Task reads as mechanical/fully-specified work, "
        f"but targets {shown} — a reasoning-tier model. Mechanical work should go to a "
        "fast-tier model (Task(model: \"haiku\") / Task(model: \"composer-2.5-fast\")) "
        "unless real judgment is genuinely needed here."
    )


def _dispatch_judgment_on_fast_nudge_message(model: str) -> str:
    return (
        f"[cost-router] This dispatched Task reads as judgment/design/ambiguous work, "
        f"but targets {model} — a fast-tier model. Judgment-heavy work (architecture "
        "tradeoffs, ambiguous design decisions, brainstorming) is a quality risk on a "
        "fast-tier model — consider a reasoning-tier model instead if this genuinely "
        "needs judgment, or ignore this if the work really is fully-specified."
    )


def _dispatch_judgment_on_fast_escalation_message(model: str) -> str:
    return (
        f"[cost-router] This dispatched Task reads as judgment/design/ambiguous work "
        f"targeting {model} — a fast-tier model. This nudge has now fired 5 times in "
        "this session without a change in approach, so it is escalating to a block. "
        "Dispatch this Task with a reasoning-tier model, or restructure the work so it "
        "is genuinely mechanical/fully-specified before retrying on a fast-tier model."
    )


def _role_tier_mismatch_nudge_message(declared_role: str, expected_role: str) -> str:
    return (
        f"[cost-router] Routing: {declared_role} was declared, but the dispatched worker's "
        f"model tier doesn't match — this looks like it should have been Routing: {expected_role}. "
        "Role analytics rely on the declared role matching the tier actually dispatched; "
        f"re-emit `Routing: {expected_role}` on future dispatches like this one if it fits better."
    )
