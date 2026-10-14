#!/usr/bin/env python3
"""Pure-computation routing analysis functions.

All functions accept event lists and return structured data (dicts, Counters,
lists) — no string formatting or I/O.  Imported by routing_renderer.py for
display and by any other module that needs the raw signals.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    DEFAULT_NUDGE_COOLDOWN_HOURS,
    DISPATCH_BUDGET_NUDGE_PCT,
    ESCALATION_WINDOW,
    FLIP_FLOP_THRESHOLD,
    GATE_HEALTH_WARN_PCT,
    OVER_ROUTE_SESSION_TOKEN_CAP,
    OVER_ROUTE_TOKEN_THRESHOLD,
    QA_FULL_FILE_READ_THRESHOLD,
    QA_LOW_TOOL_CALL_THRESHOLD,
    REPEAT_DISPATCH_TOKEN_THRESHOLD,
    SESSION_OVER_ROUTE_MIN_COST_USD,
    SESSION_OVER_ROUTE_TOOL_CALL_CAP,
    UNDISPATCHED_TOOL_THRESHOLD,
    load_json_state,
    read_events,
)
from _config import _FAST_PRICE_PER_1K, _REASONING_PRICE_PER_1K  # noqa: E402

# routing_state.json is check_routing.py's live gate state file (sessions,
# subagents, and the `counters` block bumped on every gate decision — see
# check_routing.py's `_bump()`/`event_gate()`). Defined locally rather than
# imported from check_routing to avoid pulling in its argparse/CLI surface
# for a read-only report.
ROUTING_STATE_FILE = DATA_DIR / "routing_state.json"
GATE_COUNTERS_FILE = DATA_DIR / "gate_counters.json"

_MECHANICAL_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "Read", "Grep", "Bash"})

# check_routing.py's event_gate() always ends in exactly one of these terminal
# bumps per gate call (see event_gate()'s _bump() calls) — their sum is "total
# gate calls" for rate/percentage purposes. transcript_hit/absent/fallback and
# gate_no_transcript are sub-counters recorded *within* the deny/allow decision
# and are not summed again here to avoid double-counting.
_GATE_TERMINAL_COUNTERS = (
    "gate_allow_subagent",
    "gate_deny_no_transcript_repeat",
    "gate_nudge_no_transcript",
    "gate_deny_no_verdict",
    "gate_allow_fastpath",
    "gate_deny_dispatch",
    "gate_nudge_dispatch",
    "gate_allow_routed",
    "gate_nudge_inline_narration",
    "gate_nudge_dispatch_justification",
    "gate_nudge_dj_heeded",
    "gate_nudge_dj_gap",
    "gate_deny_shell_same_type",
    "gate_nudge_no_verdict",
)


def _nudge_cooldown_hours() -> float:
    raw = os.environ.get("COST_ROUTER_NUDGE_COOLDOWN_HOURS")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_NUDGE_COOLDOWN_HOURS


def _group_by_session(events: list[dict]) -> dict[str, list[dict]]:
    by_session: dict[str, list[dict]] = {}
    for event in events:
        if event.get("type") == "session_summary":
            continue
        by_session.setdefault(event.get("session_id", "unknown"), []).append(event)
    return by_session


def session_tier_transitions(by_session: dict[str, list[dict]]) -> dict[str, int]:
    """Count how many times each session's tier flips between fast and
    reasoning (consecutive fast/reasoning events only; `unknown`-tier events
    are ignored since they carry no routing signal either way).

    A single transition is the expected shape of a deliberate one-time
    model switch. Repeated flips (>= FLIP_FLOP_THRESHOLD) are the actual
    churn signal — see FLIP_FLOP_THRESHOLD docstring above.
    """
    counts: dict[str, int] = {}
    for session_id, session_events in by_session.items():
        tiers = [e.get("tier") for e in session_events if e.get("tier") in ("fast", "reasoning")]
        counts[session_id] = sum(1 for i in range(1, len(tiers)) if tiers[i] != tiers[i - 1])
    return counts


def find_escalations(events: list[dict]) -> tuple[Counter[str], Counter[str]]:
    """A fast-tier event followed shortly by a reasoning-tier event on the
    same target suggests the task should have been routed straight to the
    reasoning model — this is the `confirmed` signal.

    `prompt`/`response` events never carry a `target_hash` (there is no
    file/command behind a plain chat turn), so a fast-tier prompt/response
    followed by *any* reasoning-tier event in the window cannot be tied to
    the same task on its own. To avoid mistaking one deliberate, one-time
    manual model switch for a misroute, this no-target case is only counted
    as the `weak` signal for sessions that flip tiers repeatedly
    (>= FLIP_FLOP_THRESHOLD transitions) — a single clean switch is ignored
    entirely, on either side, since no Decision Table row can fix a human
    choosing a different model mid-chat.
    """
    by_session = _group_by_session(events)
    transitions = session_tier_transitions(by_session)
    churn_sessions = {sid for sid, n in transitions.items() if n >= FLIP_FLOP_THRESHOLD}

    confirmed: Counter[str] = Counter()
    weak: Counter[str] = Counter()
    for session_id, session_events in by_session.items():
        for i, event in enumerate(session_events):
            if event.get("tier") != "fast":
                continue
            target = event.get("target_hash")
            key = event.get("tool_name") or event.get("type") or "unknown"
            window = session_events[i + 1 : i + 1 + ESCALATION_WINDOW]
            for follower in window:
                if follower.get("tier") != "reasoning":
                    continue
                if target is not None:
                    if follower.get("target_hash") == target:
                        confirmed[key] += 1
                        break
                elif session_id in churn_sessions:
                    weak[key] += 1
                    break
    return confirmed, weak


def find_repeat_reads(events: list[dict]) -> Counter[str]:
    """Same file (by hash) read more than once in a session — a case for a
    targeted section read instead of a full reload."""
    by_session: dict[str, Counter[str]] = {}
    for event in events:
        if event.get("type") != "tool":
            continue
        if event.get("tool_name") not in ("Read", "read_file"):
            continue
        target = event.get("target_hash")
        if not target:
            continue
        by_session.setdefault(event.get("session_id", "unknown"), Counter())[target] += 1

    repeats: Counter[str] = Counter()
    for counts in by_session.values():
        for target, count in counts.items():
            if count > 1:
                repeats[target] += count - 1
    return repeats


def find_undispatched_sessions(events: list[dict]) -> Counter[str]:
    """Sessions with enough tool activity to be a genuine multi-step task,
    but zero `Task` (subagent) dispatches anywhere in the session — the
    model-router rule's actual mechanism (routing mechanical work to a
    fast subagent) was never exercised at all, not just misrouted.

    This is a distinct blind spot from `find_escalations` (which needs at
    least one Task call to exist before it can classify it as fast or
    reasoning) and `find_repeat_reads` (file-level, not session-level): a
    session that never dispatches a subagent produces zero escalation
    signal and zero repeat-read signal by construction, yet is exactly the
    failure mode the whole plugin exists to catch — all of that session's
    tool calls ran on the (usually more expensive) top-level session model
    instead of being routed to composer-2.5-fast for the mechanical parts.

    Maps session_id -> non-Task tool-call count, for sessions at or above
    UNDISPATCHED_TOOL_THRESHOLD with zero Task calls.
    """
    by_session = _group_by_session(events)
    result: Counter[str] = Counter()
    for session_id, session_events in by_session.items():
        tool_events = [e for e in session_events if e.get("type") == "tool"]
        if not tool_events:
            continue
        task_calls = sum(1 for e in tool_events if e.get("tool_name") == "Task")
        if task_calls > 0:
            continue
        non_task_calls = len(tool_events)
        if non_task_calls >= UNDISPATCHED_TOOL_THRESHOLD:
            result[session_id] = non_task_calls
    return result


def find_over_routes(events: list[dict]) -> tuple[Counter[str], float]:
    """Return (counter, est_savings_usd) for over-routing signals.

    Level 1 — per-event: flag if ALL of:
      * tier == "reasoning"
      * tool_name in {Write, Edit, Read, Grep, Bash}  (mechanical ops)
      * est_tokens < OVER_ROUTE_TOKEN_THRESHOLD        (small output)
      * session-level sum of est_tokens < 1000         (not a large session)
    The Task tool is never flagged (dispatching subagents is intentional).

    Level 2 — whole-session: flag as "session_over_routed" if ALL of:
      * zero fast-tier events in the session
      * tool_call_count < SESSION_OVER_ROUTE_TOOL_CALL_CAP
      * no fast->reasoning escalation
      * session-level sum of est_cost_usd > SESSION_OVER_ROUTE_MIN_COST_USD

    est_savings_usd: for each Level-1-flagged event:
      est_tokens / 1000 * (reasoning_price - fast_price)
    """
    by_session: dict[str, list[dict]] = {}
    for event in events:
        if event.get("type") == "session_summary":
            continue
        sid = event.get("session_id", "unknown")
        by_session.setdefault(sid, []).append(event)

    counter: Counter[str] = Counter()
    est_savings = 0.0

    for session_events in by_session.values():
        session_est_output_tokens = sum(e.get("est_tokens", 0) for e in session_events)

        # Level 1: per-event check
        if session_est_output_tokens < OVER_ROUTE_SESSION_TOKEN_CAP:
            for event in session_events:
                if event.get("tier") != "reasoning":
                    continue
                tool_name = event.get("tool_name", "")
                if tool_name not in _MECHANICAL_TOOLS:
                    continue
                est_tokens = event.get("est_tokens", 0)
                if est_tokens >= OVER_ROUTE_TOKEN_THRESHOLD:
                    continue
                counter[tool_name] += 1
                est_savings += est_tokens / 1000 * (_REASONING_PRICE_PER_1K - _FAST_PRICE_PER_1K)

        # Level 2: whole-session check
        fast_count = sum(1 for e in session_events if e.get("tier") == "fast")
        tool_call_count = sum(1 for e in session_events if e.get("type") == "tool")

        has_escalation = False
        seen_fast = False
        for e in session_events:
            tier = e.get("tier", "unknown")
            if tier == "fast":
                seen_fast = True
            elif tier == "reasoning" and seen_fast:
                has_escalation = True
                break

        session_est_cost = sum(e.get("est_cost_usd", 0.0) for e in session_events)

        if (
            fast_count == 0
            and tool_call_count < SESSION_OVER_ROUTE_TOOL_CALL_CAP
            and not has_escalation
            and session_est_cost > SESSION_OVER_ROUTE_MIN_COST_USD
        ):
            counter["session_over_routed"] += 1

        # Level 3: Q&A session check
        intent_events = [e for e in session_events if e.get("type") == "intent"]
        is_qa_session = False
        if intent_events:
            from collections import Counter as _Counter
            intent_counts = _Counter(e.get("intent", "") for e in intent_events)
            top_intent = intent_counts.most_common(1)[0][0] if intent_counts else ""
            is_qa_session = top_intent.startswith("qa_")

        if is_qa_session:
            full_file_reads = sum(
                1
                for e in session_events
                if e.get("type") == "tool"
                and e.get("tool_name") in ("Read", "read_file")
                and e.get("est_tokens", 0) > REPEAT_DISPATCH_TOKEN_THRESHOLD
            )
            if full_file_reads > QA_FULL_FILE_READ_THRESHOLD:
                counter["qa_over_read"] += 1

            dominant_tier_reasoning = (
                sum(1 for e in session_events if e.get("tier") == "reasoning")
                > sum(1 for e in session_events if e.get("tier") == "fast")
            )
            if dominant_tier_reasoning and tool_call_count < QA_LOW_TOOL_CALL_THRESHOLD:
                counter["qa_over_routed"] += 1

    return counter, est_savings


def _session_count(events: list[dict]) -> int:
    return sum(1 for event in events if event.get("type") == "session_summary")


def dispatch_context_quality_stats(
    events: list[dict] | None = None,
    counters: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Summarize dispatch context estimate events and optional gate counters.

    Reads `type: dispatch_context_estimate` from usage.jsonl and optional
    `dispatch_nudge` / `dispatch_over_budget` counters from routing_state.json.
    """
    if events is None:
        events = read_events()

    estimates = [e for e in events if e.get("type") == "dispatch_context_estimate"]
    total_estimates = len(estimates)

    over_70 = 0
    over_100 = 0
    prompt_total = 0
    infra_total = 0
    resume_count = 0
    cold_start_count = 0

    for event in estimates:
        pct = event.get("pct_of_budget")
        try:
            pct_val = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct_val = None

        if event.get("over_budget"):
            over_100 += 1
        elif pct_val is not None and pct_val > DISPATCH_BUDGET_NUDGE_PCT:
            over_70 += 1
        elif pct_val is None and event.get("over_nudge"):
            over_70 += 1

        prompt_total += int(event.get("prompt_tokens") or 0)
        infra_total += int(event.get("infra_tokens") or 0)

        if event.get("resume"):
            resume_count += 1
        else:
            cold_start_count += 1

    registry_events = [e for e in events if e.get("type") == "dispatch_registry"]
    registry_resume = sum(1 for e in registry_events if e.get("action") == "resume")
    registry_cold = sum(1 for e in registry_events if e.get("action") == "cold_start")

    if registry_resume or registry_cold:
        resume_count = registry_resume
        cold_start_count = registry_cold

    if counters is None:
        _c = load_json_state(GATE_COUNTERS_FILE)
        counters = _c.get("counters") if isinstance(_c, dict) else {}
    counters = counters or {}

    def c(key: str) -> int:
        try:
            return int(counters.get(key, 0))
        except (TypeError, ValueError):
            return 0

    gate_nudge = c("dispatch_nudge")
    gate_over_budget = c("dispatch_over_budget")

    avg_prompt = (prompt_total / total_estimates) if total_estimates else 0.0
    avg_infra = (infra_total / total_estimates) if total_estimates else 0.0

    return {
        "total_estimates": total_estimates,
        "over_70_pct": over_70,
        "over_100_pct": over_100,
        "avg_prompt_tokens": avg_prompt,
        "avg_infra_tokens": avg_infra,
        "resume_count": resume_count,
        "cold_start_count": cold_start_count,
        "dispatch_nudge": gate_nudge,
        "dispatch_over_budget": gate_over_budget,
    }


def gate_health_stats(counters: dict[str, int] | None = None) -> dict[str, Any]:
    """Summarize the observability counters check_routing.py writes to
    routing_state.json's `counters` block (PROD-4). Returns a dict of derived
    stats; safe to call with missing/empty counters (all-zero result)."""
    if counters is None:
        _c = load_json_state(GATE_COUNTERS_FILE)
        counters = _c.get("counters") if isinstance(_c, dict) else {}
    counters = counters or {}

    def c(key: str) -> int:
        try:
            return int(counters.get(key, 0))
        except (TypeError, ValueError):
            return 0

    deny_no_verdict = c("gate_deny_no_verdict")
    deny_dispatch = c("gate_deny_dispatch")
    total_denies = deny_no_verdict + deny_dispatch

    allow_routed = c("gate_allow_routed")
    allow_fastpath = c("gate_allow_fastpath")
    allow_subagent = c("gate_allow_subagent")

    total_gate_calls = sum(c(key) for key in _GATE_TERMINAL_COUNTERS)

    transcript_hit = c("transcript_hit")
    transcript_absent = c("transcript_absent")
    transcript_fallback = c("transcript_fallback")
    transcript_total = transcript_hit + transcript_absent + transcript_fallback
    transcript_hit_rate = (transcript_hit / transcript_total) if transcript_total else None

    fail_open = c("fail_open")
    fail_open_pct = (fail_open / total_gate_calls * 100) if total_gate_calls else 0.0
    transcript_fallback_pct = (
        (transcript_fallback / total_gate_calls * 100) if total_gate_calls else 0.0
    )

    dispatch_nudge = c("dispatch_nudge")
    dispatch_over_budget = c("dispatch_over_budget")

    # judgment-work-dispatched-on-fast-tier nudge/escalation counters (PROD-19).
    # judgment_on_fast_nudge_count_session is intentionally NOT included here:
    # it is a per-session field on state["sessions"][id], not a key in the
    # global counters dict this function receives, so it can't be read via
    # c() or meaningfully summed across sessions at this call site.
    dispatch_nudge_judgment_on_fast = c("dispatch_nudge_judgment_on_fast")
    gate_deny_judgment_on_fast_escalation = c("gate_deny_judgment_on_fast_escalation")
    gate_allow_judgment_on_fast_escalation_rt_exhausted = c(
        "gate_allow_judgment_on_fast_escalation_rt_exhausted"
    )

    dj_nudge_total = c("gate_nudge_dispatch_justification") + c("gate_nudge_dj_gap")
    dj_heeded = c("gate_nudge_dj_heeded")
    dj_heeded_ratio = (dj_heeded / dj_nudge_total) if dj_nudge_total > 0 else None

    warnings = []
    if fail_open_pct > GATE_HEALTH_WARN_PCT:
        warnings.append(
            f"fail_open is {fail_open_pct:.1f}% of gate calls (>{GATE_HEALTH_WARN_PCT:.0f}%) — "
            "the gate is silently allowing everything on error more than occasionally; "
            "investigate check_routing.py errors before trusting routing enforcement."
        )
    if transcript_fallback_pct > GATE_HEALTH_WARN_PCT:
        warnings.append(
            f"transcript_fallback is {transcript_fallback_pct:.1f}% of gate calls "
            f"(>{GATE_HEALTH_WARN_PCT:.0f}%) — the gate frequently can't confirm a "
            "`Routing:` verdict from the transcript at all; routing may not be enforced."
        )

    return {
        "total_denies": total_denies,
        "deny_no_verdict": deny_no_verdict,
        "deny_dispatch": deny_dispatch,
        "allow_routed": allow_routed,
        "allow_fastpath": allow_fastpath,
        "allow_subagent": allow_subagent,
        "total_gate_calls": total_gate_calls,
        "transcript_hit": transcript_hit,
        "transcript_absent": transcript_absent,
        "transcript_fallback": transcript_fallback,
        "transcript_total": transcript_total,
        "transcript_hit_rate": transcript_hit_rate,
        "fail_open": fail_open,
        "fail_open_pct": fail_open_pct,
        "transcript_fallback_pct": transcript_fallback_pct,
        "dispatch_nudge": dispatch_nudge,
        "dispatch_over_budget": dispatch_over_budget,
        "dispatch_nudge_judgment_on_fast": dispatch_nudge_judgment_on_fast,
        "gate_deny_judgment_on_fast_escalation": gate_deny_judgment_on_fast_escalation,
        "gate_allow_judgment_on_fast_escalation_rt_exhausted": (
            gate_allow_judgment_on_fast_escalation_rt_exhausted
        ),
        "dj_nudge_effectiveness": round(dj_heeded_ratio, 3) if dj_heeded_ratio is not None else None,
        "warnings": warnings,
    }


def active_judgment_on_fast_nudge_sessions(state: dict[str, Any] | None = None) -> list[tuple[str, int]]:
    """Return (short_session_id, nudge_count) pairs for every session in
    routing_state.json's `sessions` block whose
    `judgment_on_fast_nudge_count_session` is greater than 0 (PROD-19).

    This is per-session live state, distinct from the aggregate
    `dispatch_nudge_judgment_on_fast` counter in gate_health_stats() — it
    shows which specific sessions are close to the 5-nudge escalation block.
    Session ids are truncated to 8 chars to avoid printing full identifiers.
    """
    if state is None:
        state = load_json_state(ROUTING_STATE_FILE)

    sessions = state.get("sessions") if isinstance(state, dict) else None
    if not isinstance(sessions, dict):
        return []

    results: list[tuple[str, int]] = []
    for session_id, entry in sessions.items():
        if not isinstance(entry, dict):
            continue
        try:
            count = int(entry.get("judgment_on_fast_nudge_count_session", 0))
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            short_id = session_id[:8] if session_id != "unknown" else session_id
            results.append((short_id, count))

    results.sort(key=lambda pair: pair[1], reverse=True)
    return results


def dispatch_tier_totals(events: list[dict] | None = None) -> dict[str, Any]:
    """Aggregate `subagent_dispatch` events by the dispatched model's own
    tier: count and est_tokens per tier, plus a `token_source` breakdown
    (real vs unavailable) so the numbers are legible about which tiers still
    lack confirmed real-token attribution."""
    if events is None:
        events = read_events()

    per_tier: dict[str, dict[str, int]] = {}
    for event in events:
        if event.get("type") != "subagent_dispatch":
            continue
        tier = event.get("tier") or "unknown"
        bucket = per_tier.setdefault(tier, {"count": 0, "est_tokens": 0, "real_count": 0})
        bucket["count"] += 1
        bucket["est_tokens"] += int(event.get("est_tokens", 0) or 0)
        if event.get("token_source") == "real":
            bucket["real_count"] += 1

    total_dispatches = sum(b["count"] for b in per_tier.values())
    return {
        "total_dispatches": total_dispatches,
        "per_tier": per_tier,
    }
