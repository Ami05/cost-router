#!/usr/bin/env python3
"""cost-router report renderer: generate markdown cost/usage reports.

CLI entry point for the cost-report skill/command:
    python3 cost_report_renderer.py --report --sessions 10
    python3 cost_report_renderer.py --signal-stats
    python3 cost_report_renderer.py --receipt
    python3 cost_report_renderer.py --warnings
    python3 cost_report_renderer.py --candidates

Hook ingestion (session stop) lives in session_hook.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    SIGNAL_FILE,
    TUNE_SAVINGS_RULE_INJECT_FALLBACK_USD,
    TUNE_SAVINGS_SESSION_SUMMARY_PROPOSAL_MULTIPLIER,
    USAGE_LOG,
    WARNINGS_LOG,
    format_ts,
    load_json_state,
    now_iso,
    read_events,
    save_json_state,
)
from _model_utils import load_pricing, price_for  # noqa: E402
from routing_renderer import check_nudge, render_dispatch_context_quality  # noqa: E402
from session_hook import session_time_bounds  # noqa: E402


def _render_receipt() -> str:
    """Render a policy changelog receipt showing auto-applied changes and pending candidates.

    Loads policy_changelog.json and confidence_scores.json from DATA_DIR.
    Filters changes by last_receipt_ts, computes estimated savings per change,
    and updates last_receipt_ts on successful render.

    Returns markdown receipt string or "No policy changes recorded yet." if empty.
    """
    changelog_path = DATA_DIR / "policy_changelog.json"
    confidence_path = DATA_DIR / "confidence_scores.json"

    changelog = load_json_state(changelog_path)
    confidence = load_json_state(confidence_path)
    pricing = load_pricing()

    # Define role → model mapping for pricing lookups
    role_to_model = {
        "DIRECT": "claude-haiku-4-5-20251001",
        "FW": "claude-haiku-4-5-20251001",
        "FO": "claude-haiku-4-5-20251001",
        "RB": "claude-sonnet-4-6",
    }

    # Filter changes by last_receipt_ts
    last_receipt_ts = changelog.get("last_receipt_ts")
    all_changes = changelog.get("changes", [])

    if last_receipt_ts:
        filtered_changes = [c for c in all_changes if c.get("ts", "") > last_receipt_ts]
    else:
        filtered_changes = all_changes

    # Partition filtered changes
    applied_changes = [c for c in filtered_changes if c.get("status") == "applied"]
    rolled_back_changes = [c for c in filtered_changes if c.get("status") == "rolled_back"]

    # Find pending review candidates (high blast_radius, ready=True, not in current changes)
    entries = confidence.get("entries", [])
    pending_candidates = [
        e for e in entries
        if e.get("blast_radius") == "high" and e.get("ready")
    ]

    # If nothing to show, return early
    if not applied_changes and not rolled_back_changes and not pending_candidates:
        return "No policy changes recorded yet."

    lines = ["## Policy Changelog Receipt", ""]

    # Check if confidence scoring is empty and add warning
    if not entries:
        lines.append("### WARNING: Confidence Scoring Disabled")
        lines.append("No confidence entries found. Run: `python3 scripts/confidence_score.py` to enable policy recommendations.")
        lines.append("")

    # Section 1: Auto-applied changes
    if applied_changes:
        lines.append("### Auto-Applied Changes")
        events = read_events()
        session_summaries = [e for e in events if e.get("type") == "session_summary"]

        for change in applied_changes:
            change_id = change.get("change_id", "unknown")
            intent = change.get("intent", "unknown")
            from_role = change.get("from_role", "?")
            to_role = change.get("to_role", "?")
            confidence_val = change.get("confidence", 0)
            blast = change.get("blast_radius", "?")
            change_ts = change.get("ts", "")

            # Count sessions since apply
            sessions_since = len([
                s for s in session_summaries
                if s.get("ts", "") > change_ts
            ])

            # Compute estimated savings
            old_model = role_to_model.get(from_role, "unknown")
            new_model = role_to_model.get(to_role, "unknown")
            old_price = price_for(old_model, pricing).get("output_per_1k", 0)
            new_price = price_for(new_model, pricing).get("output_per_1k", 0)
            est_savings = sessions_since * (500 / 1000) * (old_price - new_price)

            lines.append(
                f"- [applied] {intent}: {from_role} → {to_role} "
                f"(confidence={confidence_val:.3f}, blast={blast})"
            )
            lines.append(f"  Est. savings: ${est_savings:.4f} | Undo: set REVERT_CHANGE_ID={change_id}")
        lines.append("")

    # Section 2: Rolled-back changes
    if rolled_back_changes:
        lines.append("### Rolled-Back Changes")
        for change in rolled_back_changes:
            intent = change.get("intent", "unknown")
            from_role = change.get("from_role", "?")
            to_role = change.get("to_role", "?")
            reason = change.get("rollback_reason", "unknown")
            lines.append(
                f"- [rolled_back] {intent}: {from_role} → {to_role} — reason: {reason}"
            )
        lines.append("")

    # Section 3: Pending review candidates
    if pending_candidates:
        lines.append("### Pending Your Review (High Blast Radius — not auto-applied)")
        for entry in pending_candidates:
            intent = entry.get("intent", "unknown")
            verdict_role = entry.get("verdict_role", "?")
            suggested_role = entry.get("suggested_role", "?")
            confidence_val = entry.get("confidence", 0)
            n = entry.get("n", 0)
            lines.append(
                f"- {intent}: {verdict_role} → {suggested_role} "
                f"(confidence={confidence_val:.3f}, n={n}) — apply manually via candidate_table.json"
            )
        lines.append("")

    receipt_str = "\n".join(lines)

    # Update last_receipt_ts
    changelog["last_receipt_ts"] = now_iso()
    try:
        save_json_state(changelog_path, changelog)
    except Exception:  # noqa: BLE001
        pass

    return receipt_str


def _render_warnings(limit: int = 20) -> str:
    """Render the Silent Failures section from warnings.jsonl.

    Reads structured warnings written by ``_common._warn`` (fail-open hook
    errors that would otherwise be invisible). Never raises — any failure to
    read/parse the log falls back to the empty-state string.

    Returns markdown string, most recent record last, capped at ``limit``.
    """
    try:
        if not WARNINGS_LOG.is_file():
            return "No silent failures recorded."
        records: list[dict] = []
        for raw in WARNINGS_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                records.append(entry)
        if not records:
            return "No silent failures recorded."

        records = records[-limit:]
        lines = ["## Silent Failures", ""]
        for entry in records:
            ts = format_ts(entry.get("ts"))
            event = entry.get("event", "unknown")
            context = entry.get("context", {})
            lines.append(f"- [{ts}] {event}: {context}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — renderer must never crash the report
        return "No silent failures recorded."


def render_report(limit: int) -> str:
    events = read_events()
    # Collect all summaries then deduplicate: keep the last (most complete) entry
    # per session_id — Stop fires once per turn so a session can have many partials.
    _seen: dict[str, dict] = {}
    for e in events:
        if e.get("type") == "session_summary":
            _seen[e.get("session_id", "")] = e
    summaries = list(_seen.values())
    summaries = summaries[-limit:] if limit > 0 else summaries

    lines = ["# cost-router usage report (est.)", ""]
    lines.append(f"_Report generated: {format_ts(now_iso())}_")
    try:
        # stat() (not a preceding is_file() check) to avoid a TOCTOU race with a
        # concurrent _rotate_log_if_needed() renaming the log out from under us.
        mtime = USAGE_LOG.stat().st_mtime
    except FileNotFoundError:
        lines.append(f"_Log file: {USAGE_LOG.name} (not created yet)_")
    else:
        log_mtime = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds")
        lines.append(
            f"_Log last updated: {format_ts(log_mtime)} ({len(events)} event(s) in "
            f"{USAGE_LOG.name})_"
        )

    lines.append("")

    if not summaries:
        lines.append(
            "No session summaries yet — run a session with the plugin active, "
            "then `/cost-report` again once it ends."
        )
        return "\n".join(lines)

    total_in = sum(s.get("est_input_tokens", 0) for s in summaries)
    total_out = sum(s.get("est_output_tokens", 0) for s in summaries)
    total_ctx = sum(s.get("est_context_tokens", 0) for s in summaries)
    total_cost = sum(s.get("est_cost_usd", 0.0) for s in summaries)

    lines.append(f"Rolling window: last {len(summaries)} session(s)")
    lines.append(f"- Est. input tokens: {total_in:,}")
    lines.append(f"- Est. context/tool tokens: {total_ctx:,}")
    lines.append(f"- Est. output tokens: {total_out:,}")
    lines.append(f"- Est. cost: ${total_cost:.4f}")
    if any(s.get("has_real_tokens") for s in summaries):
        lines.append("_(token_source: real API data used for some sessions)_")
    else:
        lines.append("_(est. — real token data not available on this platform)_")

    # MVP-3: Claude Code tier-detection limitation notice
    _total_tier_events = sum(sum(s.get("tier_mix", {}).values()) for s in summaries)
    _unknown_events = sum(s.get("tier_mix", {}).get("unknown", 0) for s in summaries)
    if _total_tier_events and (_unknown_events / _total_tier_events) > 0.50:
        lines.append("")
        lines.append(
            "> **Platform note (Claude Code):** More than 50% of tool events show "
            "`tier: unknown` — expected on Claude Code, where the `model` field is only "
            "available at `SessionStart`. Escalation detection and tier cost breakdown "
            "are Cursor-sourced only. Token totals and gate enforcement are unaffected."
        )

    # ---- ## This week: auto-tuned changes + queued/accumulating counts ----
    try:
        _cl_path = DATA_DIR / "routing_changelog.json"
        _pr_path = DATA_DIR / "routing_proposals.json"

        _changelog: list = []
        try:
            _raw = json.loads(_cl_path.read_text(encoding="utf-8"))
            _changelog = _raw if isinstance(_raw, list) else []
        except Exception:  # noqa: BLE001
            pass

        _proposals: list = []
        try:
            _raw = json.loads(_pr_path.read_text(encoding="utf-8"))
            _proposals = _raw if isinstance(_raw, list) else []
        except Exception:  # noqa: BLE001
            pass

        _by_token = {
            p.get("undo_token"): p
            for p in _proposals
            if isinstance(p, dict) and p.get("undo_token")
        }
        _by_key = {p.get("key"): p for p in _proposals if isinstance(p, dict)}

        _now = datetime.now(timezone.utc)
        _week_ago = _now - timedelta(days=7)

        _recent: list = []
        for _e in _changelog:
            if not isinstance(_e, dict) or _e.get("status") != "applied":
                continue
            _at = _e.get("applied_at")
            if not _at:
                continue
            try:
                _dt = datetime.fromisoformat(_at.replace("Z", "+00:00"))
                if _dt.tzinfo is None:
                    _dt = _dt.replace(tzinfo=timezone.utc)
                if _dt >= _week_ago:
                    _recent.append(_e)
            except ValueError:
                pass

        _m_queued = sum(
            1 for p in _proposals
            if isinstance(p, dict) and p.get("status") == "queued"
        )
        _k_acc = sum(
            p.get("evidence_count", 0)
            for p in _proposals
            if isinstance(p, dict) and p.get("status") == "accumulating"
        )

        if _recent or _m_queued or _k_acc:
            lines.append("")
            lines.append("## This week")

            if _recent:
                _est_saved = 0.0
                for _e in _recent:
                    _tok = _e.get("undo_token")
                    _key = _e.get("key", "")
                    _prop = _by_token.get(_tok) or _by_key.get(_key)
                    if _prop:
                        _est_saved += (
                            _prop.get("evidence_count", 1)
                            * _prop.get("shadow_delta_reward", 0.1)
                            * TUNE_SAVINGS_SESSION_SUMMARY_PROPOSAL_MULTIPLIER
                        )
                    else:
                        _est_saved += TUNE_SAVINGS_RULE_INJECT_FALLBACK_USD
                lines.append(
                    f"- Auto-tuned {len(_recent)} routing rule(s)"
                    f"  |  Est. saved ~${_est_saved:.2f}"
                )
                for _e in _recent:
                    _rule = _e.get("proposed_row") or _e.get("key", "unknown")
                    _tok = _e.get("undo_token", "")
                    lines.append(
                        f"  * {_rule}"
                        f"  [undo: python3 scripts/policy_manager.py --undo {_tok}]"
                    )

            if _m_queued:
                lines.append(
                    f'- {_m_queued} improvement(s) queued for review'
                    ' -> say "show routing suggestions"'
                )

            if _k_acc:
                lines.append(
                    f"- {_k_acc} evidence point(s) accumulating for next candidate"
                )
    except Exception:  # noqa: BLE001
        pass
    # -----------------------------------------------------------------------

    lines.append("")
    lines.append(
        "| Session | Started (UTC) | Ended (UTC) | Input | Context | Output | "
        "Tool calls | Subagents | Est. cost | Reward |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    total_subagent_dispatches = 0
    total_subagent_unattributed = 0
    for summary in reversed(summaries):
        sid = str(summary.get("session_id", "unknown"))[:12]
        started_at = summary.get("started_at")
        ended_at = summary.get("ended_at")
        if not started_at or not ended_at:
            started_at, ended_at = session_time_bounds(events, summary.get("session_id", ""))
        reward_display = summary.get("reward", "—")
        dispatch_count = summary.get("subagent_dispatch_count", 0)
        unattributed = summary.get("subagent_unattributed_count", 0)
        total_subagent_dispatches += dispatch_count
        total_subagent_unattributed += unattributed
        subagents_display = str(dispatch_count) if dispatch_count else "—"
        if unattributed:
            subagents_display += f" ({unattributed} cost-unknown)"
        # PROOF-3: label each row's cost confidence rather than blending
        # transcript-verified (real) and heuristic (est.) numbers silently.
        # Claude Code sessions where _read_project_usage found a matching
        # transcript are 'real'; everything else (including all Cursor
        # sessions today, and any Claude Code session whose transcript
        # lookup missed) is a labeled heuristic estimate.
        cost_marker = "" if summary.get("has_real_tokens") else "†"
        lines.append(
            f"| {sid} | {format_ts(started_at)} | {format_ts(ended_at)} | "
            f"{summary.get('est_input_tokens', 0):,} | "
            f"{summary.get('est_context_tokens', 0):,} | {summary.get('est_output_tokens', 0):,} | "
            f"{summary.get('tool_call_count', 0)} | {subagents_display} | "
            f"${summary.get('est_cost_usd', 0.0):.4f}{cost_marker} | {reward_display} |"
        )
    lines.append("")
    if any(not s.get("has_real_tokens") for s in summaries):
        lines.append(
            "_† = heuristic estimate, not transcript-verified (no real per-model token counts "
            "found for that session — either the host doesn't expose them, as on Cursor today, "
            "or the Claude Code project transcript wasn't found at its expected path). "
            "Rows without † used real, transcript-derived token counts._"
        )
        lines.append("")
    if total_subagent_unattributed:
        lines.append(
            f"_Note: {total_subagent_unattributed} of {total_subagent_dispatches} dispatched "
            "subagent(s) above have unknown cost (no usage data in the SubagentStop payload) — "
            "totals understate real spend for those. See PROD-15/PROD-2 in knowledge/backlog.md._"
        )
        lines.append("")

    # Subagent routing quality
    _total_explore_warn = sum(s.get("explore_without_haiku", 0) for s in summaries)
    _total_agents = sum(s.get("total_agents_spawned", 0) for s in summaries)
    _total_waste = sum(s.get("agent_model_waste_usd", 0.0) for s in summaries)
    if _total_agents > 0:
        lines.append("## Subagent Routing Quality")
        lines.append(f"- Total agents spawned: {_total_agents}")
        if _total_explore_warn > 0:
            lines.append(f"- Explore agents without haiku: {_total_explore_warn} (est. ${_total_waste:.2f} wasted)")
        else:
            lines.append("- All Explore agents used haiku ✓")
        lines.append("")

    # Session Type Breakdown
    type_stats: dict[str, dict] = {}
    for summary in summaries:
        stype = summary.get("session_type", "unknown")
        if stype not in type_stats:
            type_stats[stype] = {"count": 0, "total_cost": 0.0, "total_tokens": 0}
        type_stats[stype]["count"] += 1
        type_stats[stype]["total_cost"] += summary.get("est_cost_usd", 0.0)
        type_stats[stype]["total_tokens"] += (
            summary.get("est_input_tokens", 0) + summary.get("est_output_tokens", 0)
        )
    # Guard & Graph stats
    _guard_sessions = [s for s in summaries if s.get("file_guard_warnings", 0) > 0]
    _refresh_sessions = [s for s in summaries if s.get("graphs_refreshed", 0) > 0]
    if _guard_sessions or _refresh_sessions:
        lines.append("## Guard & Graph Activity")
        if _guard_sessions:
            total_warnings = sum(s.get("file_guard_warnings", 0) for s in _guard_sessions)
            lines.append(f"- File-guard warnings fired: {total_warnings} across {len(_guard_sessions)} session(s)")
        if _refresh_sessions:
            total_refreshed = sum(s.get("graphs_refreshed", 0) for s in _refresh_sessions)
            lines.append(f"- Native graphs refreshed: {total_refreshed} file(s) across {len(_refresh_sessions)} session(s)")
        lines.append("")

    dispatch_quality = render_dispatch_context_quality(events=events)
    if dispatch_quality:
        lines.append(dispatch_quality.rstrip())

    if type_stats:
        lines.append("## Session Type Breakdown (est.)")
        _type_order = [("qa", "Q&A"), ("agentic", "Agentic"), ("mixed", "Mixed"), ("unknown", "Unknown")]
        for key, label in _type_order:
            st = type_stats.get(key)
            if not st or st["count"] == 0:
                continue
            avg_cost = st["total_cost"] / st["count"]
            avg_tokens = st["total_tokens"] // st["count"]
            lines.append(
                f"  {label} sessions:      {st['count']}   "
                f"avg cost ${avg_cost:.3f}   avg tokens {avg_tokens:,}"
            )
        lines.append("")

    lines.append(
        "_All figures are local heuristic estimates (~4 chars/token), not billing data. "
        "They exclude prompt-cache discounts and may over- or under-count actual usage._"
    )

    triggered, nudge_message = check_nudge(events)
    if triggered:
        lines.append("")
        lines.append(nudge_message)

    # P12-13: Role Distribution section — aggregate role_distribution events across sessions.
    _rd_totals: dict[str, int] = {"DIRECT": 0, "FW": 0, "FO": 0, "RB": 0}
    _rd_session_ids = {s.get("session_id") for s in summaries}
    for _ev in events:
        if _ev.get("type") == "role_distribution" and _ev.get("session_id") in _rd_session_ids:
            for _role in _rd_totals:
                _rd_totals[_role] += _ev.get("counts", {}).get(_role, 0)
    _rd_total_turns = sum(_rd_totals.values())
    if _rd_total_turns > 0:
        lines.append("")
        lines.append("## Role Distribution")
        for _role, _cnt in _rd_totals.items():
            _pct = 100 * _cnt / _rd_total_turns
            lines.append(f"  {_role}: {_cnt} ({_pct:.0f}%)")
        lines.append(f"  Total routed turns: {_rd_total_turns}")

    # Routing Candidates section — show ready entries from confidence_scores.json.
    _conf_data = load_json_state(DATA_DIR / "confidence_scores.json")
    _candidates = [
        entry for entry in (_conf_data if isinstance(_conf_data, list) else [])
        if entry.get("ready")
    ]
    _candidates_sorted = sorted(_candidates, key=lambda e: e.get("confidence", 0), reverse=True)[:10]
    lines.append("")
    lines.append("## Routing Candidates")
    if _candidates_sorted:
        lines.append("| Intent | Verdict Role | Suggested Role | Confidence | N |")
        lines.append("|--------|-------------|----------------|------------|---|")
        for _c in _candidates_sorted:
            _intent = _c.get("intent", "")
            _verdict = _c.get("verdict_role", "")
            _suggested = _c.get("suggested_role", "")
            _conf = _c.get("confidence", 0)
            _n = _c.get("n", "")
            lines.append(f"| {_intent} | {_verdict} | {_suggested} | {_conf:.2f} | {_n} |")
    else:
        lines.append("  No routing candidates")

    # P12-10: Routing Gaps section — list fo_no_dispatch gaps across sessions.
    _gap_events = [
        _ev for _ev in events
        if _ev.get("type") == "routing_gap"
        and _ev.get("subtype") == "fo_no_dispatch"
        and _ev.get("session_id") in _rd_session_ids
    ]
    if _gap_events:
        lines.append("")
        lines.append("## Routing Gaps (FO without Task dispatch)")
        for _gap in _gap_events:
            _sid = _gap.get("session_id", "")[:8]
            _turn = _gap.get("fo_turn_id", "?")
            lines.append(f"  session {_sid}… — FO declared turn {_turn}, no Task dispatched")

    return "\n".join(lines)


def read_signal_stats(n_sessions: int = 10) -> dict:
    """Parse signal.jsonl and compute routing-quality metrics over the last n sessions.

    Returns a dict with keys:
      total_events, accepted_count, overridden_count, reverted_count,
      acceptance_rate (float 0–1 or None if no accept/override events),
      session_count (number of distinct sessions included).
    """
    _empty: dict = {
        "total_events": 0,
        "accepted_count": 0,
        "overridden_count": 0,
        "reverted_count": 0,
        "acceptance_rate": None,
        "session_count": 0,
    }
    events: list[dict] = []
    try:
        with SIGNAL_FILE.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        events.append(parsed)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return _empty

    if not events:
        return _empty

    # Group events by session_id, preserving first-seen order.
    sessions_seen: dict[str, list[dict]] = {}
    for event in events:
        sid = event.get("session_id") or "unknown"
        if sid not in sessions_seen:
            sessions_seen[sid] = []
        sessions_seen[sid].append(event)

    recent_ids = list(sessions_seen.keys())[-n_sessions:]
    accepted = overridden = reverted = 0
    for sid in recent_ids:
        for event in sessions_seen[sid]:
            evt = event.get("event")
            if evt == "accepted":
                accepted += 1
            elif evt == "overridden":
                overridden += 1
            elif evt == "reverted":
                reverted += 1

    total = accepted + overridden + reverted
    denom = accepted + overridden
    acceptance_rate: float | None = (accepted / denom) if denom > 0 else None

    return {
        "total_events": total,
        "accepted_count": accepted,
        "overridden_count": overridden,
        "reverted_count": reverted,
        "acceptance_rate": acceptance_rate,
        "session_count": len(recent_ids),
    }


def render_shadow_eval_stats() -> str:
    """Render a markdown Shadow Evaluation section from data/shadow_evaluation.json.

    Includes per-rule delta summary and a Quality Signal subsection from
    data/quality_signal.json (per-intent revert_rate). Returns a note string
    (not empty) when data files are missing. Flags staleness when
    shadow_evaluation.json is older than exactly 48 hours.
    """
    lines: list[str] = ["## Shadow Evaluation", ""]

    shadow_path = DATA_DIR / "shadow_evaluation.json"
    if not shadow_path.exists():
        lines.append("_No shadow evaluation data yet — run `python3 scripts/shadow_evaluator.py` to generate._")
        lines.append("")
    else:
        try:
            raw = json.loads(shadow_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lines.append("_shadow_evaluation.json could not be read._")
            lines.append("")
            raw = {}

        # Staleness check: flag if file is older than exactly 48 hours.
        try:
            mtime = shadow_path.stat().st_mtime
            age_hours = (datetime.now(timezone.utc).timestamp() - mtime) / 3600
            if age_hours > 48:
                lines.append(
                    f"> **Stale:** shadow_evaluation.json is {age_hours:.0f}h old "
                    "(threshold: 48h). Re-run `python3 scripts/shadow_evaluator.py`."
                )
                lines.append("")
        except OSError:
            pass

        rules: dict = raw.get("rules", {})
        timestamp = raw.get("timestamp", "")
        if timestamp:
            lines.append(f"_Evaluated: {format_ts(timestamp)}_")
            lines.append("")

        if not rules:
            lines.append("_No per-rule entries in shadow_evaluation.json._")
            lines.append("")
        else:
            lines.append("| Rule | Intent | Actual Role | Candidate Role | Actual Reward | Candidate Reward | Delta | N |")
            lines.append("|------|--------|-------------|----------------|---------------|------------------|-------|---|")
            for rule_id, entry in rules.items():
                intent = entry.get("intent", "unknown")
                actual_role = entry.get("actual_role", "?")
                candidate_role = entry.get("candidate_role", "?")
                actual_reward = entry.get("actual_mean_reward", 0.0)
                candidate_reward = entry.get("candidate_expected_reward", 0.0)
                delta = entry.get("improvement_delta", 0.0)
                n = entry.get("sample_count", 0)
                delta_sign = "+" if delta >= 0 else ""
                bias_flag = " ⚠" if entry.get("selection_bias_warning") else ""
                lines.append(
                    f"| {rule_id} | {intent} | {actual_role} | {candidate_role} | "
                    f"{actual_reward:.4f} | {candidate_reward:.4f} | "
                    f"{delta_sign}{delta:.4f}{bias_flag} | {n} |"
                )
            lines.append("")
            n_matched = len(rules)
            lines.append(f"_n_matched = {n_matched} rule(s). ⚠ = selection_bias_warning (candidate history < 10 samples)._")
            lines.append("")

    # Quality Signal subsection from data/quality_signal.json
    lines.append("### Quality Signal")
    lines.append("")
    quality_path = DATA_DIR / "quality_signal.json"
    if not quality_path.exists():
        lines.append("_No quality signal data yet — data/quality_signal.json not found._")
        lines.append("")
    else:
        try:
            q_raw = json.loads(quality_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            q_raw = {}
            lines.append("_quality_signal.json could not be read._")
            lines.append("")

        per_intent: dict = q_raw.get("per_intent", {}) if isinstance(q_raw, dict) else {}
        if not per_intent:
            lines.append("_No per-intent revert data in quality_signal.json._")
            lines.append("")
        else:
            lines.append("| Intent | Revert Rate |")
            lines.append("|--------|-------------|")
            for intent_key, intent_data in per_intent.items():
                revert_rate = intent_data.get("revert_rate", 0.0) if isinstance(intent_data, dict) else intent_data
                lines.append(f"| {intent_key} | {revert_rate:.1%} |")
            lines.append("")

    return "\n".join(lines)


def render_signal_stats(n_sessions: int = 10) -> str:
    """Render a markdown Routing Quality section from signal.jsonl."""
    stats = read_signal_stats(n_sessions)
    if stats["session_count"] == 0:
        return "## Routing Quality\n\n_No signal data yet — signal.jsonl will populate after the first routed session._\n"

    rate = stats["acceptance_rate"]
    rate_str = f"{rate * 100:.0f}%" if rate is not None else "n/a"
    lines = [
        "## Routing Quality",
        "",
        f"Rolling window: last {stats['session_count']} session(s) · "
        f"{stats['total_events']} total signal event(s)",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Acceptance rate | **{rate_str}** |",
        f"| Accepted (routing followed) | {stats['accepted_count']} |",
        f"| Overridden (wrong-tier inline mutation) | {stats['overridden_count']} |",
        f"| Reverted (file written back to prior state) | {stats['reverted_count']} |",
    ]

    # PB-11-5: week-over-week trend from gate_daily.json.
    try:
        daily: dict = load_json_state(DATA_DIR / "gate_daily.json")
        if daily:
            sorted_days = sorted(daily.keys())
            if len(sorted_days) >= 14:
                last7 = sorted_days[-7:]
                prior7 = sorted_days[-14:-7]

                def _week_rate(days: list[str]) -> float | None:
                    a = o = 0
                    for d in days:
                        entry = daily.get(d, {})
                        a += entry.get("accepted", 0)
                        o += entry.get("overridden", 0)
                    return (a / (a + o)) if (a + o) > 0 else None

                cur = _week_rate(last7)
                prev = _week_rate(prior7)
                if cur is not None and prev is not None:
                    delta = cur - prev
                    sign = "+" if delta >= 0 else ""
                    lines.append(
                        f"| Week-over-week trend | Last 7d: {cur*100:.0f}% "
                        f"({sign}{delta*100:.0f}% vs prior week) |"
                    )
                elif cur is not None:
                    lines.append(f"| Last 7 days | {cur*100:.0f}% |")
            elif len(sorted_days) >= 7:
                lines.append("| Week-over-week trend | _Need 14+ days of data_ |")
    except Exception:  # noqa: BLE001
        pass

    lines += [
        "",
        "_Acceptance rate = accepted ÷ (accepted + overridden). "
        "Rising rate with tenure = routing compounding over time._",
    ]
    result = "\n".join(lines) + "\n"
    result += "\n" + render_shadow_eval_stats()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report", action="store_true", help="Print a markdown usage report"
    )
    parser.add_argument(
        "--sessions", type=int, default=10, help="Number of recent sessions to include in --report or --signal-stats"
    )
    parser.add_argument(
        "--signal-stats", action="store_true", help="Print Routing Quality section from signal.jsonl"
    )
    parser.add_argument(
        "--receipt", action="store_true", help="Render policy changelog receipt"
    )
    parser.add_argument(
        "--candidates", action="store_true", help="Print only the Routing Candidates table from confidence_scores.json"
    )
    parser.add_argument(
        "--warnings", action="store_true", help="Print Silent Failures section from warnings.jsonl"
    )
    args = parser.parse_args()

    if args.candidates:
        _conf_data = load_json_state(DATA_DIR / "confidence_scores.json")
        _candidates = [
            entry for entry in (_conf_data if isinstance(_conf_data, list) else [])
            if entry.get("ready")
        ]
        _candidates_sorted = sorted(_candidates, key=lambda e: e.get("confidence", 0), reverse=True)[:10]
        if _candidates_sorted:
            print("| Intent | Verdict Role | Suggested Role | Confidence | N |")
            print("|--------|-------------|----------------|------------|---|")
            for _c in _candidates_sorted:
                _intent = _c.get("intent", "")
                _verdict = _c.get("verdict_role", "")
                _suggested = _c.get("suggested_role", "")
                _conf = _c.get("confidence", 0)
                _n = _c.get("n", "")
                print(f"| {_intent} | {_verdict} | {_suggested} | {_conf:.2f} | {_n} |")
        else:
            print("No routing candidates")
        return 0

    if args.report:
        print(render_report(args.sessions))
        return 0

    if args.signal_stats:
        print(render_signal_stats(args.sessions))
        return 0

    if args.receipt:
        print(_render_receipt())
        return 0

    if args.warnings:
        print(_render_warnings())
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
