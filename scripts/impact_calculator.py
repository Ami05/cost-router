from dataclasses import dataclass
from typing import Optional
from pathlib import Path
import json
import statistics
from datetime import datetime, timedelta

DATA_DIR = Path(__file__).parent.parent / 'data'
CONFIG_PATH = Path(__file__).parent.parent / 'config.json'
PRICING_PATH = Path(__file__).parent / 'pricing.json'

DEFAULT_MIN_SESSIONS = 5
DEFAULT_COLD_START = 3

GATE_DISPLAY_NAMES = {
    'bulk_write_denied': 'Bulk-write denied',
    'dispatch_schema_violation': 'Dispatch Schema Violation',
    'fix_loop_nudge': 'Fix-loop nudged',
    'orchestrator_deny': 'Orchestrator ceiling denied',
    'orchestrator_nudge': 'Orchestrator ceiling nudged',
    'scope_guard_deny': 'Scope-guard denied',
    'similarity_deny': 'Similarity denied',
    'test_runner_model_deny': 'Test runner model denied',
    'worker_model_deny': 'Worker model denied',
}

GATE_EVENT_TYPES = {'bulk_write_denied', 'dispatch_schema_violation', 'fix_loop_nudge', 'orchestrator_deny', 'orchestrator_nudge', 'scope_guard_deny', 'similarity_deny', 'test_runner_model_deny', 'worker_model_deny'}


@dataclass(frozen=True)
class ImpactReport:
    routing_savings: float
    waste_prevented: float
    total_est_saved: float
    routing_rate: float
    prev_week_routing_rate: Optional[float]
    total_spent: float
    total_sessions: int
    total_turns: int
    gate_breakdown: dict  # gate_type -> (count, est_dollars)
    top_moments: list
    baseline_model: str
    has_enough_data: bool


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_pricing() -> dict:
    """Read pricing.json. Return parsed dict or empty dict on missing file."""
    try:
        return json.loads(PRICING_PATH.read_text())
    except Exception:
        return {}


def _load_config_impact() -> dict:
    """Return merged impact config and correction factors from config.json."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}
    result = dict(cfg.get('impact', {}))
    result['correction'] = cfg.get('token_estimation', {}).get('correction', {})
    return result


def _load_session_entries(session_id=None) -> list:
    """Read DATA_DIR/session_log.jsonl, optionally filtering by session_id."""
    path = DATA_DIR / 'session_log.jsonl'
    entries = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if session_id is None or entry.get('session_id') == session_id:
                entries.append(entry)
    except Exception:
        pass
    return entries


def _load_window_entries(since: str, until: str) -> list:
    """Read session_log.jsonl filtered by timestamp range (ISO date strings)."""
    path = DATA_DIR / 'session_log.jsonl'
    entries = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            ts = entry.get('ts', '')
            if since <= ts <= until:
                entries.append(entry)
    except Exception:
        pass
    return entries


# ---------------------------------------------------------------------------
# Computation helpers
# ---------------------------------------------------------------------------

def _compute_routing_savings(entries, pricing, baseline_model, corrections):
    """Compute Bucket 1: routing savings.

    Returns: (routing_savings_usd, total_spent_usd, routing_rate, total_turns, routed_turn_ids)
    pricing uses output_per_1k (per 1000 tokens), divide by 1_000.
    """
    baseline_output_price = pricing.get(baseline_model, {}).get('output_per_1k', 0.015)
    routing_savings = 0.0
    total_spent = 0.0
    fast_turns = 0
    total_turns = 0
    routed_turn_ids = set()

    for entry in entries:
        if 'est_output_tokens' not in entry:
            continue
        total_turns += 1
        tier = entry.get('tier', 'unknown')
        model_key = entry.get('model_key', baseline_model)
        raw_tokens = entry.get('est_output_tokens', 0)
        content_kind = entry.get('content_kind', 'code_output')
        ck = 'code' if content_kind == 'code_output' else content_kind
        correction = corrections.get('default', {}).get(ck, 1.0)
        est_tokens = raw_tokens * correction
        actual_price = pricing.get(model_key, {}).get('output_per_1k', baseline_output_price)
        actual_cost = est_tokens * actual_price / 1_000
        total_spent += actual_cost
        if tier == 'fast':
            fast_turns += 1
            counterfactual_cost = est_tokens * baseline_output_price / 1_000
            routing_savings += counterfactual_cost - actual_cost
            routed_turn_ids.add(entry.get('turn_id', ''))

    routing_rate = fast_turns / total_turns if total_turns > 0 else 0.0
    return routing_savings, total_spent, routing_rate, total_turns, routed_turn_ids


def _compute_waste_prevented(entries, pricing, baseline_model, routed_turn_ids, corrections):
    """Compute Bucket 2: waste prevention. NO double-counting with Bucket 1.

    Returns: (waste_prevented_usd, gate_breakdown)
    pricing uses output_per_1k, divide by 1_000.
    """
    baseline_output_price = pricing.get(baseline_model, {}).get('output_per_1k', 0.015)

    output_tokens = [e.get('est_output_tokens', 0) for e in entries if 'est_output_tokens' in e]
    last_20 = output_tokens[-20:] if output_tokens else []
    avg_output_tokens = statistics.median(last_20) if last_20 else 1000

    correction = corrections.get('default', {}).get('code', 1.0)
    waste_prevented = 0.0
    gate_breakdown = {}

    for entry in entries:
        event_type = entry.get('event_type', '')
        turn_id = entry.get('turn_id', '')
        if event_type in GATE_EVENT_TYPES and turn_id not in routed_turn_ids:
            est_cost = avg_output_tokens * correction * baseline_output_price / 1_000
            waste_prevented += est_cost
            if event_type not in gate_breakdown:
                gate_breakdown[event_type] = (0, 0.0)
            count, dollars = gate_breakdown[event_type]
            gate_breakdown[event_type] = (count + 1, dollars + est_cost)

    return waste_prevented, gate_breakdown


def _compute_policy_trend(current_rate: float, since: str):
    """Compute week-over-week routing rate delta. Returns float or None."""
    try:
        since_date = datetime.fromisoformat(since)
    except Exception:
        return None
    prev_week_start = (since_date - timedelta(days=14)).date().isoformat()
    prev_week_end = (since_date - timedelta(days=7)).date().isoformat()
    prev_entries = _load_window_entries(prev_week_start, prev_week_end)
    if not prev_entries:
        return None
    fast_turns = sum(1 for e in prev_entries if e.get('tier') == 'fast' and 'est_output_tokens' in e)
    total_turns = sum(1 for e in prev_entries if 'est_output_tokens' in e)
    prev_rate = fast_turns / total_turns if total_turns > 0 else 0.0
    return current_rate - prev_rate


def _detect_baseline_model(pricing: dict) -> str:
    """Find the model key with the highest output_per_1k price."""
    if not pricing:
        return 'opus'
    candidates = [(k, v.get('output_per_1k', 0)) for k, v in pricing.items() if isinstance(v, dict)]
    if not candidates:
        return 'opus'
    return max(candidates, key=lambda x: x[1])[0]


def _find_top_moments(entries, pricing, baseline_model, corrections, n=3):
    """Find top n sessions by routing savings."""
    sessions = {}
    for entry in entries:
        if 'est_output_tokens' not in entry:
            continue
        sid = entry.get('session_id', '')
        if sid not in sessions:
            sessions[sid] = []
        sessions[sid].append(entry)

    results = []
    for sid, sess_entries in sessions.items():
        savings, _, _, _, _ = _compute_routing_savings(sess_entries, pricing, baseline_model, corrections)
        fast_count = sum(1 for e in sess_entries if e.get('tier') == 'fast')
        ts = min((e.get('ts', '') for e in sess_entries), default='')
        results.append({
            'session_id': sid,
            'ts': ts,
            'description': f'{fast_count} turns routed to fast tier',
            'est_saved_usd': savings,
        })

    results.sort(key=lambda x: x['est_saved_usd'], reverse=True)
    return results[:n]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _empty_report() -> ImpactReport:
    return ImpactReport(
        routing_savings=0.0,
        waste_prevented=0.0,
        total_est_saved=0.0,
        routing_rate=0.0,
        prev_week_routing_rate=None,
        total_spent=0.0,
        total_sessions=0,
        total_turns=0,
        gate_breakdown={},
        top_moments=[],
        baseline_model='opus',
        has_enough_data=False,
    )


def compute_session(session_id: str) -> ImpactReport:
    """Compute impact for a single session."""
    pricing = _load_pricing()
    config = _load_config_impact()
    corrections = config.get('correction', {})
    baseline_model = _detect_baseline_model(pricing)
    entries = _load_session_entries(session_id)
    if not entries:
        return _empty_report()
    routing_savings, total_spent, routing_rate, total_turns, routed_turn_ids = _compute_routing_savings(
        entries, pricing, baseline_model, corrections
    )
    waste_prevented, gate_breakdown = _compute_waste_prevented(
        entries, pricing, baseline_model, routed_turn_ids, corrections
    )
    total_est_saved = routing_savings + waste_prevented
    since = (entries[0].get('ts', '') or '')[:10]
    trend = _compute_policy_trend(routing_rate, since)
    return ImpactReport(
        routing_savings=routing_savings,
        waste_prevented=waste_prevented,
        total_est_saved=total_est_saved,
        routing_rate=routing_rate,
        prev_week_routing_rate=trend,
        total_spent=total_spent,
        total_sessions=1,
        total_turns=total_turns,
        gate_breakdown=gate_breakdown,
        top_moments=[],
        baseline_model=baseline_model,
        has_enough_data=True,
    )


def compute_window(since: str, until: str, min_sessions: int = DEFAULT_MIN_SESSIONS) -> ImpactReport:
    """Compute impact for a date range."""
    pricing = _load_pricing()
    config = _load_config_impact()
    corrections = config.get('correction', {})
    baseline_model = _detect_baseline_model(pricing)
    entries = _load_window_entries(since, until)
    unique_sessions = set(e.get('session_id', '') for e in entries)
    if len(unique_sessions) < min_sessions:
        return _empty_report()
    routing_savings, total_spent, routing_rate, total_turns, routed_turn_ids = _compute_routing_savings(
        entries, pricing, baseline_model, corrections
    )
    waste_prevented, gate_breakdown = _compute_waste_prevented(
        entries, pricing, baseline_model, routed_turn_ids, corrections
    )
    total_est_saved = routing_savings + waste_prevented
    trend = _compute_policy_trend(routing_rate, since)
    top_moments = _find_top_moments(entries, pricing, baseline_model, corrections)
    return ImpactReport(
        routing_savings=routing_savings,
        waste_prevented=waste_prevented,
        total_est_saved=total_est_saved,
        routing_rate=routing_rate,
        prev_week_routing_rate=trend,
        total_spent=total_spent,
        total_sessions=len(unique_sessions),
        total_turns=total_turns,
        gate_breakdown=gate_breakdown,
        top_moments=top_moments,
        baseline_model=baseline_model,
        has_enough_data=True,
    )
