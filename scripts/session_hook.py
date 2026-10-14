#!/usr/bin/env python3
"""cost-router hook: on session stop, roll up per-session totals into usage.jsonl.

Stop-hook entry point — invoked by hooks/run.sh at SessionStop.
Report rendering lives in cost_report_renderer.py.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    PLUGIN_ROOT,
    SESSION_LIMIT_SESSION_SUMMARY_PROJECTS,
    SIGNAL_FILE,
    USAGE_LOG,
    append_event,
    ensure_log_integrity,
    load_json_state,
    now_iso,
    read_events,
    read_stdin_json,
    save_json_state,
    session_id_of,
)
from _model_utils import load_pricing, price_for, explore_sonnet_waste_usd, tier_of_model, check_model_key_none_threshold  # noqa: E402
from reward import compute_reward  # noqa: E402
from _config import _warn  # noqa: E402


def _derive_session_type(routing_state: dict, session_id: str) -> str:
    """Derive session_type from prompt_intent in routing_state: qa / agentic / mixed.

    Returns:
    - "qa" / "agentic": known session type
    - "unrecognized": extraction or classification failed (emits extraction_fallback)
    - "unknown": genuinely undefined or not applicable (reserved for future use)
    """
    session_entry = (routing_state.get("sessions") or {}).get(session_id, {})
    prompt_intent = session_entry.get("prompt_intent", "")
    if not prompt_intent:
        try:
            _warn("extraction_fallback reason=prompt_intent_absent")
        except Exception:
            pass
        return "unrecognized"
    if str(prompt_intent).startswith("qa_"):
        return "qa"
    if prompt_intent == "agentic_action":
        return "agentic"
    try:
        _warn(f"extraction_fallback reason=intent_prefix_unrecognized prompt_intent={prompt_intent!r}")
    except Exception:
        pass
    return "unrecognized"


def _read_project_usage(session_id: str, cwd: str) -> dict | None:
    """Read real per-model token counts from Claude Code's project transcript.

    Claude Code stores API responses in ~/.claude/projects/<cwd_hash>/<session_id>.jsonl.
    Each assistant-turn entry has message.usage with per-turn token counts.
    We sum across all turns to get session totals — matching what Claude Code displays
    at session end.

    Returns {'per_model': [...], 'total': {...}} or None if unavailable.
    """
    if not session_id or not cwd:
        return None
    cwd_hash = cwd.replace("/", "-")
    transcript = Path.home() / ".claude" / "projects" / cwd_hash / f"{session_id}.jsonl"
    if not transcript.exists():
        return None

    model_sums: dict[str, dict[str, int]] = {}
    try:
        for raw in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message", {})
            usage = msg.get("usage", {})
            if not isinstance(usage, dict) or "output_tokens" not in usage:
                continue  # skip user turns and non-API entries
            model = msg.get("model") or "unknown"
            if model not in model_sums:
                model_sums[model] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
            s = model_sums[model]
            s["input"] += usage.get("input_tokens", 0)
            s["output"] += usage.get("output_tokens", 0)
            s["cache_read"] += usage.get("cache_read_input_tokens", 0)
            s["cache_write"] += usage.get("cache_creation_input_tokens", 0)
    except OSError:
        return None

    if not model_sums:
        return None

    per_model = [{"model": m, **s} for m, s in model_sums.items()]
    total = {k: sum(s[k] for s in model_sums.values()) for k in ("input", "output", "cache_read", "cache_write")}
    return {"per_model": per_model, "total": total}


def session_time_bounds(events: list[dict], session_id: str) -> tuple[str | None, str | None]:
    timestamps = [
        event.get("ts")
        for event in events
        if event.get("session_id") == session_id
        and event.get("type") != "session_summary"
        and event.get("ts")
    ]
    if not timestamps:
        return None, None
    return min(timestamps), max(timestamps)


def summarize_session(events: list[dict], session_id: str, cwd: str = "") -> dict:
    pricing = load_pricing()
    input_tokens = 0
    output_tokens = 0
    context_tokens = 0
    tool_count = 0
    tier_counts: Counter[str] = Counter()
    cost = 0.0
    started_at, ended_at = session_time_bounds(events, session_id)
    has_real_tokens = False

    # Dispatched Task workers (see PROD-15): counted separately from the
    # parent's own prompt/tool/response events since they carry no
    # `direction` and their cost, when known at all, is real API usage from
    # the worker's own turn rather than a heuristic byte estimate.
    subagent_dispatch_count = 0
    subagent_unattributed_count = 0
    subagent_cost = 0.0
    resume_used_count = 0

    for event in events:
        if event.get("session_id") != session_id or event.get("type") == "session_summary":
            continue
        if event.get("type") == "resume_used":
            resume_used_count += 1
            continue
        if event.get("type") == "subagent_dispatch":
            subagent_dispatch_count += 1
            if event.get("token_source") != "real":
                subagent_unattributed_count += 1
                continue
            price = price_for(event.get("model"), pricing)
            subagent_cost += event.get("est_tokens", 0) / 1000 * price.get("output_per_1k", 0)
            subagent_cost += event.get("est_input_tokens_real", 0) / 1000 * price.get("input_per_1k", 0)
            # Priced directly at the cache-read rate here (not via the
            # count-then-discount pass below, which assumes the tokens were
            # already added at full input price in the *first* loop — true
            # for prompt/response events but not for this one).
            cache_price = price.get("cache_read_per_1k", price.get("input_per_1k", 0) * 0.1)
            subagent_cost += event.get("est_cache_read_tokens", 0) / 1000 * cache_price
            continue

        tokens = event.get("est_tokens", 0)
        direction = event.get("direction")
        tier_counts[event.get("tier", "unknown")] += 1
        price = price_for(event.get("model"), pricing)

        if direction == "input":
            input_tokens += tokens
            cost += tokens / 1000 * price.get("input_per_1k", 0)
        elif direction == "output":
            output_tokens += tokens
            cost += tokens / 1000 * price.get("output_per_1k", 0)
        else:
            context_tokens += tokens
            tool_count += 1
            cost += tokens / 1000 * price.get("input_per_1k", 0)

    # Apply cache discount for heuristic events that carry est_cache_read_tokens.
    # subagent_dispatch is excluded — its cache tokens are already priced above.
    for event in events:
        if event.get("session_id") != session_id or event.get("type") in ("session_summary", "subagent_dispatch"):
            continue
        cache_tokens = event.get("est_cache_read_tokens", 0)
        if cache_tokens > 0:
            price = price_for(event.get("model"), pricing)
            cache_price = price.get("cache_read_per_1k", price.get("input_per_1k", 0) * 0.1)
            cost -= cache_tokens / 1000 * price.get("input_per_1k", 0)
            cost += cache_tokens / 1000 * cache_price

    # Override with real token data from Claude Code's project transcript when available.
    # This matches what Claude Code shows at session end (per-model breakdown, cache included).
    project_usage = _read_project_usage(session_id, cwd)
    if project_usage:
        input_tokens = project_usage["total"]["input"]
        output_tokens = project_usage["total"]["output"]
        cost = 0.0
        for m in project_usage["per_model"]:
            p = price_for(m["model"], pricing)
            cache_read_price = p.get("cache_read_per_1k", p.get("input_per_1k", 0) * 0.1)
            cache_write_price = p.get("cache_write_per_1k", p.get("input_per_1k", 0) * 1.25)
            cost += m["input"] / 1000 * p.get("input_per_1k", 0)
            cost += m["output"] / 1000 * p.get("output_per_1k", 0)
            cost += m["cache_read"] / 1000 * cache_read_price
            cost += m["cache_write"] / 1000 * cache_write_price
        has_real_tokens = True
    else:
        has_real_tokens = any(
            e.get("token_source") == "real"
            for e in events
            if e.get("session_id") == session_id and e.get("type") != "session_summary"
        )

    # Added after the project_usage override (which resets `cost` to a
    # from-scratch transcript sum) — see PROD-15: subagent dispatch cost is
    # empirically NOT reflected in that transcript sum, so it must be added
    # on top regardless of which branch above ran, not folded into the first
    # loop's `cost` (which the override branch discards).
    cost += subagent_cost

    reward_val = compute_reward(events, session_id, None, cost)
    session_events = [e for e in events if e.get("session_id") == session_id]
    # Load routing_state to get prompt_intent for session type derivation
    routing_state = load_json_state(DATA_DIR / "routing_state.json")
    session_type = _derive_session_type(routing_state, session_id)

    # RD-2 telemetry rollup
    rd2_nudges = sum(
        1 for e in session_events
        if e.get("type") == "gate_rd2_structural_similarity"
    )
    rd2_outcomes = [
        e for e in session_events
        if e.get("type") == "gate_rd2_compliance"
    ]
    rd2_overrides = sum(1 for e in rd2_outcomes if e.get("outcome") == "overrode")
    rd2_override_rate = round(rd2_overrides / len(rd2_outcomes), 3) if rd2_outcomes else 0.0

    # Agent routing quality (from agent_call_stats.json)
    _agent_stats = load_json_state(DATA_DIR / "agent_call_stats.json")
    _sess_agents = _agent_stats.get("sessions", {}).get(session_id, {})
    explore_without_haiku = _sess_agents.get("explore_without_haiku", 0)
    total_agents_spawned = _sess_agents.get("total_agents", 0)
    agent_model_waste_usd = round(explore_without_haiku * explore_sonnet_waste_usd(), 2)

    return {
        "type": "session_summary",
        "session_id": session_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "est_input_tokens": input_tokens,
        "est_output_tokens": output_tokens,
        "est_context_tokens": context_tokens,
        "est_cost_usd": round(cost, 4),
        "subagent_dispatch_count": subagent_dispatch_count,
        "subagent_unattributed_count": subagent_unattributed_count,
        "tool_call_count": tool_count,
        "tier_mix": dict(tier_counts),
        "reward": round(reward_val, 3),
        "has_real_tokens": has_real_tokens,
        "session_type": session_type,
        "explore_without_haiku": explore_without_haiku,
        "total_agents_spawned": total_agents_spawned,
        "agent_model_waste_usd": agent_model_waste_usd,
        "rd2_nudges": rd2_nudges,
        "rd2_override_rate": rd2_override_rate,
        "resume_used_count": resume_used_count,
    }


def _rollup_daily_signal() -> None:
    """Roll up today's signal events into gate_daily.json (PB-11-5).

    Keyed by YYYY-MM-DD; each entry: {accepted, overridden, reverted, acceptance_rate}.
    Caps at 90 days. Never raises — fail-open.
    """
    _MAX_DAYS = 90
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily: dict = load_json_state(DATA_DIR / "gate_daily.json")

        accepted = overridden = reverted = 0
        try:
            with SIGNAL_FILE.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    ts = rec.get("ts", "")
                    if not isinstance(ts, str) or not ts.startswith(today):
                        continue
                    evt = rec.get("event")
                    if evt == "accepted":
                        accepted += 1
                    elif evt == "overridden":
                        overridden += 1
                    elif evt == "reverted":
                        reverted += 1
        except OSError:
            pass

        denom = accepted + overridden
        rate: float | None = (accepted / denom) if denom > 0 else None
        daily[today] = {
            "accepted": accepted,
            "overridden": overridden,
            "reverted": reverted,
            "acceptance_rate": rate,
        }

        # Cap at 90 days — evict oldest keys.
        if len(daily) > _MAX_DAYS:
            oldest = sorted(daily.keys())[: len(daily) - _MAX_DAYS]
            for k in oldest:
                del daily[k]

        save_json_state(DATA_DIR / "gate_daily.json", daily)
    except Exception:  # noqa: BLE001
        pass


def _upsert_project_registry() -> None:
    """Register current project root and nearby projects in project_registry.json."""
    import os as _os
    registry_path = DATA_DIR / "project_registry.json"
    registry = load_json_state(registry_path)
    projects = registry.get("projects", {})
    if not isinstance(projects, dict):
        projects = {}

    candidates: set[Path] = set()
    for env_key in ("CURSOR_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        val = _os.environ.get(env_key)
        if val:
            candidates.add(Path(val).resolve())

    p = Path.cwd().resolve()
    for _ in range(4):
        candidates.add(p)
        parent = p.parent
        if parent == p:
            break
        p = parent

    ts = now_iso()
    for root in candidates:
        if not root.is_dir():
            continue
        if not str(root).startswith(str(PLUGIN_ROOT)):
            continue
        abs_str = str(root)
        projects[abs_str] = {
            "last_seen_at": ts,
        }

    if len(projects) > SESSION_LIMIT_SESSION_SUMMARY_PROJECTS:
        sorted_projects = sorted(
            projects.items(),
            key=lambda x: x[1].get("last_seen_at", ""),
            reverse=True,
        )
        projects = dict(sorted_projects[:SESSION_LIMIT_SESSION_SUMMARY_PROJECTS])

    save_json_state(registry_path, {"projects": projects})


def main() -> int:
    try:
        payload = read_stdin_json()
        session_id = session_id_of(payload)
        ensure_log_integrity(session_id)
        cwd = payload.get("cwd", "")
        events = read_events()
        if events and events[-1].get("type") == "data_reset":
            print("[cost-router] WARNING: data_reset detected — usage log was absent or empty. Signal quality may be degraded.", file=sys.stderr)
        summary = summarize_session(events, session_id, cwd)
    except Exception:  # noqa: BLE001 — hook must never produce empty stdout
        print(json.dumps({}))
        return 0

    # Enrich summary with guard/graph stats for this session
    try:
        _hash_stats = load_json_state(DATA_DIR / "file_hash_stats.json")
        _guard_warnings = _hash_stats.get(session_id, {}).get("warnings_fired", 0)
        if _guard_warnings > 0:
            summary["file_guard_warnings"] = _guard_warnings

        _refresh_stats = load_json_state(DATA_DIR / "file_graph_refresh_stats.json")
        _rstats = _refresh_stats.get("sessions", {}).get(session_id, {})
        if _rstats:
            summary["graphs_refreshed"] = _rstats.get("graphs_refreshed", 0)
    except Exception:  # noqa: BLE001 — never block session summary
        pass

    try:
        _rollup_daily_signal()
    except Exception:  # noqa: BLE001 — never block session summary
        pass

    try:
        _upsert_project_registry()
    except Exception:  # noqa: BLE001 — never block session summary
        pass

    try:
        if (summary["tool_call_count"] or summary["est_input_tokens"] or
                summary["est_output_tokens"] or summary.get("has_real_tokens")):
            # Dedup: events loaded above pre-dates this write, so if a
            # session_summary already exists (e.g. duplicate Stop event), skip.
            _already_written = any(
                e.get("type") == "session_summary" and e.get("session_id") == session_id
                for e in events
            )
            if not _already_written:
                append_event(summary)
    except Exception:  # noqa: BLE001 — never block session summary on bad dict shape
        pass

    # P12-13/P12-10: load routing state once for role distribution + gap detection.
    _rs_session_entry: dict = {}
    try:
        _routing_state = load_json_state(DATA_DIR / "routing_state.json")
        _rs_session_entry = (_routing_state.get("sessions") or {}).get(session_id, {})
    except (OSError, KeyError, TypeError):
        pass

    # P12-13: role distribution — count routing_declared events for this session.
    _role_counts: dict[str, int] = {"DIRECT": 0, "FW": 0, "FO": 0, "RB": 0}
    _fo_turns_with_no_task: list[str] = []
    for _ev in events:
        if _ev.get("session_id") != session_id:
            continue
        if _ev.get("type") == "routing_declared":
            _role = str(_ev.get("role", "")).upper()
            if _role in _role_counts:
                _role_counts[_role] += 1
    _turns_missing_routing = max(0, int(_rs_session_entry.get("turn", 0)) - sum(_role_counts.values()))
    try:
        append_event({
            "type": "role_distribution",
            "session_id": session_id,
            "ts": now_iso(),
            "counts": _role_counts,
            "turns_total": sum(_role_counts.values()),
            "turns_missing_routing": _turns_missing_routing,
        })
    except OSError:
        pass

    # P12-10: FO-no-dispatch detection — uses routing state already loaded above.
    try:
        _last_role = str(_rs_session_entry.get("last_routing_role", ""))
        _verdict_turn = _rs_session_entry.get("verdict_turn_id")
        _task_turn = _rs_session_entry.get("task_dispatched_turn_id")
        if _last_role == "FO" and _verdict_turn and _verdict_turn != _task_turn:
            append_event({
                "type": "routing_gap",
                "subtype": "fo_no_dispatch",
                "session_id": session_id,
                "ts": now_iso(),
                "fo_turn_id": _verdict_turn,
                "last_task_turn_id": _task_turn,
            })
    except (OSError, KeyError, TypeError):
        pass

    # TICKET-087: Wire check_model_key_none_threshold alert.
    # Count events with model_key=None and total events in this session.
    try:
        _none_count = 0
        _total_count = 0
        for _ev in events:
            if _ev.get("session_id") != session_id:
                continue
            if "model_key" in _ev:
                _total_count += 1
                if _ev.get("model_key") is None:
                    _none_count += 1
        if _total_count > 0:
            _should_alert, _actual_pct = check_model_key_none_threshold(_none_count, _total_count)
            if _should_alert:
                append_event({
                    "type": "alert_model_key_none",
                    "session_id": session_id,
                    "ts": now_iso(),
                    "none_count": _none_count,
                    "total_count": _total_count,
                    "threshold_pct": _actual_pct,
                })
    except (OSError, KeyError, TypeError):
        pass

    # TICKET-081: Wire fallback_slo alert.
    # Count routing_declared events with model_resolution_method=fallback vs. total.
    # If fallback rate > 5%, emit alert_fallback_slo event.
    try:
        _fallback_count = 0
        _routing_declared_count = 0
        for _ev in events:
            if _ev.get("session_id") != session_id:
                continue
            if _ev.get("type") == "routing_declared":
                _routing_declared_count += 1
                if _ev.get("model_resolution_method") == "fallback":
                    _fallback_count += 1
        if _routing_declared_count > 0:
            _fallback_rate_pct = (_fallback_count / _routing_declared_count) * 100.0
            if _fallback_rate_pct > 5.0:
                append_event({
                    "type": "alert_fallback_slo",
                    "session_id": session_id,
                    "ts": now_iso(),
                    "fallback_count": _fallback_count,
                    "total_count": _routing_declared_count,
                    "fallback_rate_pct": _fallback_rate_pct,
                })
    except (OSError, KeyError, TypeError):
        pass

    # Write per-turn records to session_log.jsonl so impact_calculator.py
    # can compute routing savings (Bucket 1). Each output-direction event
    # for this session becomes one entry with the fields impact_calculator
    # expects: tier, model_key, est_output_tokens, turn_id, content_kind.
    _session_log_path = DATA_DIR / "session_log.jsonl"
    _turn_n = 0
    try:
        with _session_log_path.open("a", encoding="utf-8") as _slh:
            for _ev in events:
                if _ev.get("session_id") != session_id:
                    continue
                if _ev.get("direction") != "output":
                    continue
                _turn_n += 1
                _turn_record = {
                    "session_id": session_id,
                    "turn_id": f"{session_id}-t{_turn_n}",
                    "ts": _ev.get("ts", ""),
                    "model_key": _ev.get("model") or "",
                    "est_output_tokens": _ev.get("est_tokens", 0),
                    "tier": tier_of_model(_ev.get("model", "")),
                    "content_kind": "code_output",
                }
                _slh.write(json.dumps(_turn_record) + "\n")
    except OSError:
        pass

    print(json.dumps({}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
