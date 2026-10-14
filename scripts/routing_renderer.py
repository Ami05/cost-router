#!/usr/bin/env python3
"""Human-readable rendering of routing analysis signals.

All functions return strings for display.  Imports structured data from
routing_detectors.py — never reads event logs directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    ESCALATION_NUDGE_THRESHOLD,
    FLIP_FLOP_THRESHOLD,
    OVER_ROUTE_TOKEN_THRESHOLD,
    QA_FULL_FILE_READ_THRESHOLD,
    QA_LOW_TOOL_CALL_THRESHOLD,
    REPEAT_READ_NUDGE_THRESHOLD,
    SESSION_CADENCE_THRESHOLD,
    SESSION_OVER_ROUTE_MIN_COST_USD,
    SESSION_OVER_ROUTE_TOOL_CALL_CAP,
    TUNE_STATE_FILE,
    UNDISPATCHED_NUDGE_THRESHOLD,
    UNDISPATCHED_TOOL_THRESHOLD,
    load_json_state,
    now_iso,
    read_events,
    save_json_state,
)
from routing_detectors import (  # noqa: E402
    _group_by_session,
    _nudge_cooldown_hours,
    _session_count,
    active_judgment_on_fast_nudge_sessions,
    dispatch_context_quality_stats,
    dispatch_tier_totals,
    find_escalations,
    find_over_routes,
    find_repeat_reads,
    find_undispatched_sessions,
    gate_health_stats,
    session_tier_transitions,
)


def render_dispatch_context_quality(
    stats: dict[str, Any] | None = None,
    events: list[dict] | None = None,
) -> str:
    """Render the Dispatch Context Quality section for /cost-report."""
    if stats is None:
        stats = dispatch_context_quality_stats(events)

    if stats["total_estimates"] == 0 and stats["dispatch_nudge"] == 0 and stats["dispatch_over_budget"] == 0:
        return ""

    lines = ["## Dispatch Context Quality (est.)", ""]
    lines.append(f"- Dispatch context estimates logged: {stats['total_estimates']}")
    lines.append(f"- Over 70% of window (nudge zone): {stats['over_70_pct']}")
    lines.append(f"- Over 100% of window (over budget): {stats['over_100_pct']}")
    if stats["total_estimates"]:
        lines.append(
            f"- Avg prompt tokens vs infra tokens: "
            f"{stats['avg_prompt_tokens']:.0f} vs {stats['avg_infra_tokens']:.0f}"
        )
    if stats["resume_count"] or stats["cold_start_count"]:
        lines.append(
            f"- Resume vs cold-start dispatches: {stats['resume_count']} resume / "
            f"{stats['cold_start_count']} cold-start"
        )
    if stats["dispatch_nudge"] or stats["dispatch_over_budget"]:
        lines.append(
            f"- Gate counters — dispatch_nudge: {stats['dispatch_nudge']}, "
            f"dispatch_over_budget: {stats['dispatch_over_budget']}"
        )
    lines.append("")
    return "\n".join(lines)


def render_gate_health(stats: dict[str, Any] | None = None) -> str:
    """Render the PROD-4 'Gate health' report section from routing_state.json
    counters (see gate_health_stats())."""
    if stats is None:
        stats = gate_health_stats()

    lines = ["## Gate health (routing_state.json counters)"]

    if stats["total_gate_calls"] == 0 and stats["fail_open"] == 0:
        lines.append("- No gate counters recorded yet — routing_state.json has no `counters` data.")
        return "\n".join(lines)

    lines.append(
        f"- Total gate calls: {stats['total_gate_calls']} "
        f"(denies: {stats['total_denies']} — no-verdict: {stats['deny_no_verdict']}, "
        f"dispatch-cap: {stats['deny_dispatch']})"
    )
    lines.append(
        f"- Allow — routed: {stats['allow_routed']}, fastpath: {stats['allow_fastpath']}, "
        f"subagent: {stats['allow_subagent']}"
    )
    if stats["transcript_hit_rate"] is None:
        lines.append("- Transcript hit-rate: n/a (no transcript-detection attempts recorded)")
    else:
        lines.append(
            f"- Transcript hit-rate: {stats['transcript_hit_rate'] * 100:.1f}% "
            f"(hit {stats['transcript_hit']} / absent {stats['transcript_absent']} / "
            f"fallback {stats['transcript_fallback']} / total {stats['transcript_total']})"
        )
    fail_open_marker = " <-- NON-ZERO, INVESTIGATE" if stats["fail_open"] else ""
    lines.append(f"- **fail_open: {stats['fail_open']}**{fail_open_marker}")

    if stats.get("dispatch_nudge") or stats.get("dispatch_over_budget"):
        lines.append(
            f"- Dispatch context gate — nudge (70%): {stats.get('dispatch_nudge', 0)}, "
            f"over budget (>100%): {stats.get('dispatch_over_budget', 0)}"
        )

    if (
        stats.get("dispatch_nudge_judgment_on_fast")
        or stats.get("gate_deny_judgment_on_fast_escalation")
        or stats.get("gate_allow_judgment_on_fast_escalation_rt_exhausted")
    ):
        lines.append(
            f"- Judgment-on-fast — nudged: {stats.get('dispatch_nudge_judgment_on_fast', 0)}, "
            f"escalation denied: {stats.get('gate_deny_judgment_on_fast_escalation', 0)}, "
            f"escalation allowed (rt exhausted): "
            f"{stats.get('gate_allow_judgment_on_fast_escalation_rt_exhausted', 0)}"
        )

    if stats.get('dispatch_nudge_judgment_on_fast', 0) > 0:
        active_nudge_sessions = active_judgment_on_fast_nudge_sessions()
        if active_nudge_sessions:
            lines.append("- Judgment-on-fast — active per-session nudge counts:")
            for short_id, count in active_nudge_sessions:
                lines.append(f"  - `{short_id}`: {count}/5")

    for warning in stats["warnings"]:
        lines.append(f"- ⚠️  WARNING: {warning}")

    return "\n".join(lines)


def gate_health_one_liner(stats: dict[str, Any] | None = None) -> str:
    """A single-line gate-health summary for mirroring into the cost-report
    skill output (PROD-4 requirement 2)."""
    if stats is None:
        stats = gate_health_stats()

    if stats["total_gate_calls"] == 0 and stats["fail_open"] == 0:
        return "Gate health: no gate counters recorded yet."

    hit_rate = (
        f"{stats['transcript_hit_rate'] * 100:.0f}%" if stats["transcript_hit_rate"] is not None else "n/a"
    )
    line = (
        f"Gate health: {stats['total_gate_calls']} gate calls, "
        f"{stats['total_denies']} denies, transcript hit-rate {hit_rate}, "
        f"fail_open={stats['fail_open']}"
    )
    if stats["warnings"]:
        line += " -- WARNING: " + "; ".join(stats["warnings"])
    return line


def dispatch_tier_one_liner(totals: dict[str, Any] | None = None) -> str:
    """A single-line dispatched-Task tier-mix summary, mirroring
    `gate_health_one_liner`'s shape for cost-report (PROD-19)."""
    if totals is None:
        totals = dispatch_tier_totals()

    if totals["total_dispatches"] == 0:
        return "Dispatch tier mix: no Task dispatches recorded yet."

    parts = []
    for tier in ("fast", "reasoning", "unknown"):
        bucket = totals["per_tier"].get(tier)
        if not bucket:
            continue
        parts.append(f"{tier}: {bucket['count']} dispatch(es)/{bucket['est_tokens']} tokens")
    return f"Dispatch tier mix: {', '.join(parts)}"


def check_nudge(events: list[dict] | None = None, *, persist: bool = True) -> tuple[bool, str]:
    """Decide whether to nudge the user to run tune-routing right now.

    Returns (triggered, message). See the threshold constants above for the
    exact rule. When triggered and persist=True, records the current signal
    counts + timestamp to TUNE_STATE_FILE so the same evidence doesn't
    re-trigger a nudge until either new evidence arrives or the cooldown
    window passes again.
    """
    if events is None:
        events = read_events()

    confirmed, weak = find_escalations(events)
    # Nudge cadence is driven by confirmed (same-target) escalations only —
    # the weak/no-target signal is informational and shown in the report,
    # but it's too noisy on its own to justify prompting the user.
    escalations_total = sum(confirmed.values())
    repeats_total = sum(find_repeat_reads(events).values())
    undispatched_total = len(find_undispatched_sessions(events))
    sessions_total = _session_count(events)

    state = load_json_state(TUNE_STATE_FILE)

    last_nudge_at = state.get("last_nudge_at")
    if last_nudge_at:
        try:
            last_dt = datetime.fromisoformat(last_nudge_at)
            if datetime.now(timezone.utc) - last_dt < timedelta(hours=_nudge_cooldown_hours()):
                return False, ""
        except ValueError:
            pass

    new_escalations = escalations_total - state.get("escalations_at_last_nudge", 0)
    new_repeats = repeats_total - state.get("repeats_at_last_nudge", 0)
    new_undispatched = undispatched_total - state.get("undispatched_at_last_nudge", 0)
    new_sessions = sessions_total - state.get("sessions_at_last_nudge", 0)

    reasons = []
    if new_escalations >= ESCALATION_NUDGE_THRESHOLD:
        reasons.append(f"{new_escalations} new possible misroute(s) detected")
    if new_repeats >= REPEAT_READ_NUDGE_THRESHOLD:
        reasons.append(f"{new_repeats} new redundant file re-read(s) detected")
    if new_undispatched >= UNDISPATCHED_NUDGE_THRESHOLD:
        reasons.append(
            f"{new_undispatched} new session(s) ran real multi-step work with zero subagent dispatch"
        )
    if not reasons and new_sessions >= SESSION_CADENCE_THRESHOLD:
        reasons.append(f"{new_sessions} sessions since the last routing review")

    if not reasons:
        return False, ""

    message = (
        "**Tune-routing suggested:** " + "; ".join(reasons) + ". "
        "Run `tune routing` (or `/tune-routing`) to review data-driven Decision Table updates."
    )

    if persist:
        save_json_state(
            TUNE_STATE_FILE,
            {
                "last_nudge_at": now_iso(),
                "escalations_at_last_nudge": escalations_total,
                "repeats_at_last_nudge": repeats_total,
                "undispatched_at_last_nudge": undispatched_total,
                "sessions_at_last_nudge": sessions_total,
            },
        )

    return True, message


def render_suggestions() -> str:
    events = read_events()
    if not events:
        # routing_state.json's gate counters are independent of usage.jsonl
        # (a different log written by check_routing.py, not the hooks that
        # feed read_events()), so gate health can still be worth surfacing
        # even before any usage events exist.
        return (
            "No usage data yet — nothing to analyze. Run a few sessions with the plugin active first.\n\n"
            + render_gate_health()
        )

    by_session = _group_by_session(events)
    transitions = session_tier_transitions(by_session)
    single_switch_sessions = sum(1 for n in transitions.values() if n == 1)
    churn_session_count = sum(1 for n in transitions.values() if n >= FLIP_FLOP_THRESHOLD)

    confirmed, weak = find_escalations(events)
    repeats = find_repeat_reads(events)
    undispatched = find_undispatched_sessions(events)

    lines = ["# Suggested Decision Table Updates (data-driven)", ""]

    lines.append(render_gate_health())
    lines.append("")

    dispatch_quality = render_dispatch_context_quality(events=events)
    if dispatch_quality:
        lines.append(dispatch_quality)

    lines.append("## Confirmed misroutes (fast escalated to reasoning on the SAME file/command target)")
    lines.append(
        "_Escalation detection requires per-event tier visibility. Claude Code sessions report "
        "`tier: unknown` after SessionStart — misroutes from those sessions are underreported here. "
        "Full signal comes from Cursor sessions only._"
    )
    if confirmed:
        for tool, count in confirmed.most_common(10):
            lines.append(
                f"- `{tool}`: {count} escalation(s) observed — consider routing this "
                "task type straight to the Reasoning Brain."
            )
    else:
        lines.append("- None detected in the current log.")

    lines.append("")
    lines.append(
        "## Weak signal: repeated fast→reasoning→fast churn within a session "
        "(no file/command target, so identity can't be confirmed, but flip-flopping "
        f"({FLIP_FLOP_THRESHOLD}+ transitions in one session) suggests the fast model "
        "kept falling short and had to be rescued)"
    )
    if weak:
        for kind, count in weak.most_common(10):
            lines.append(
                f"- `{kind}`: {count} tier-switch(es) in {churn_session_count} churning session(s). "
                "Directional only — review manually before adding a Decision Table row from this alone."
            )
    else:
        lines.append("- None detected in the current log.")
    if single_switch_sessions:
        lines.append(
            f"- (Ignored: {single_switch_sessions} session(s) with exactly one tier switch — "
            "treated as a deliberate one-time manual model change, not a misroute.)"
        )

    lines.append("")
    lines.append("## Repeat large-file reads (targeted-read opportunity)")
    if repeats:
        total_repeats = sum(repeats.values())
        lines.append(
            f"- {total_repeats} redundant re-read(s) across {len(repeats)} distinct file(s) — "
            "use targeted/section reads instead of full-file reads for these files."
        )
    else:
        lines.append("- None detected in the current log.")

    lines.append("")
    lines.append(
        f"## Sessions with real multi-step work but zero subagent dispatch (≥{UNDISPATCHED_TOOL_THRESHOLD} "
        "tool calls, no `Task` call anywhere — the model-router mechanism never engaged)"
    )
    if undispatched:
        total_calls = sum(undispatched.values())
        lines.append(
            f"- {len(undispatched)} session(s), {total_calls} tool call(s) total ran on the session "
            "model with no subagent dispatch at all. This is a stronger signal than a misroute: the "
            "routing skill wasn't followed, not just followed incorrectly. Review whether these "
            "sessions had mechanical, multi-step work (renames, repeated edits, boilerplate scripts) "
            "that should have gone through a Fast Worker `Task` dispatch instead."
        )
        for session_id, count in undispatched.most_common(5):
            short_id = session_id[:8] if session_id != "unknown" else session_id
            lines.append(f"  - session `{short_id}…`: {count} tool call(s), 0 Task dispatches")
    else:
        lines.append("- None detected in the current log.")

    lines.append("")
    over_counts, over_savings = find_over_routes(events)
    lines.append("## Over-routing signals")
    if over_counts:
        for tool_name, count in over_counts.most_common():
            if tool_name == "session_over_routed":
                lines.append(
                    f"- {count} session(s) used a reasoning model exclusively with "
                    f"<{SESSION_OVER_ROUTE_TOOL_CALL_CAP} tool calls "
                    f"at >${SESSION_OVER_ROUTE_MIN_COST_USD} cost — consider routing short "
                    "mechanical sessions to the fast tier."
                )
            elif tool_name == "qa_over_read":
                lines.append(
                    f"- {count} Q&A session(s) read >{QA_FULL_FILE_READ_THRESHOLD} full files — use targeted/section reads "
                    "for Q&A turns to reduce context tokens."
                )
            elif tool_name == "qa_over_routed":
                lines.append(
                    f"- {count} Q&A session(s) used a reasoning model for "
                    f"<{QA_LOW_TOOL_CALL_THRESHOLD} tool calls — "
                    "consider routing simple Q&A to a fast model."
                )
            else:
                lines.append(
                    f"- `{tool_name}`: {count} call(s) sent to a reasoning model for a "
                    f"<{OVER_ROUTE_TOKEN_THRESHOLD}-token mechanical operation."
                )
        lines.append(f"- Estimated savings if re-routed to fast tier: ${over_savings:.4f}")
    else:
        lines.append("- None detected.")

    lines.append("")
    lines.append(
        "_Heuristic analysis over locally estimated events only (no raw prompts, responses, "
        "or file paths are stored) — not a substitute for judgment. Review each suggestion "
        "before editing `rules/model-router.md`._"
    )
    return "\n".join(lines)
