#!/usr/bin/env python3
"""FL-103: Shadow data evaluator.

Joins shadow route logs with reward data to evaluate whether candidate
(shadow) routes would have performed better than actual routes. Writes
per-rule evaluation to data/shadow_evaluation.json.

Pure stdlib — no external dependencies.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR, now_iso, save_json_state, _warn
from signal_filter_rules import apply_all_filters, exclude_legacy_rewards


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_shadow_routes() -> list[dict]:
    """Read data/shadow_routes.jsonl, return list of dicts."""
    shadow_file = DATA_DIR / "shadow_routes.jsonl"
    entries: list[dict] = []
    try:
        with shadow_file.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    entries.append(parsed)
    except FileNotFoundError:
        return []  # Normal on first run — no routes collected yet
    except OSError as e:
        _warn("shadow_eval_load_failed", {"file": "shadow_evaluator.py", "error": str(e)})
        return []
    return entries


def load_filtered_rewards() -> list[dict]:
    """Read data/reward_log.jsonl, apply all signal filters, return clean entries."""
    reward_file = DATA_DIR / "reward_log.jsonl"
    entries: list[dict] = []
    try:
        with reward_file.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    entries.append(parsed)
    except FileNotFoundError:
        return []  # Normal on first run — no routes collected yet
    except OSError as e:
        _warn("shadow_eval_load_failed", {"file": "shadow_evaluator.py", "error": str(e)})
        return []
    clean, _stats = apply_all_filters(entries)
    clean = exclude_legacy_rewards(clean)
    return clean


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str | None) -> float | None:
    """Parse ISO-8601 timestamp to epoch seconds. Returns None on failure."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Join logic
# ---------------------------------------------------------------------------

def join_shadow_reward(
    shadows: list[dict],
    rewards: list[dict],
) -> list[dict]:
    """Join shadow routes with reward entries on session_id.

    For each shadow route, find matching reward entries by session_id.
    If no exact match, try fuzzy timestamp match within 60s window.

    Returns list of dicts combining shadow route fields with reward data.
    """
    # Index rewards by session_id for fast lookup
    rewards_by_session: dict[str, list[dict]] = defaultdict(list)
    for r in rewards:
        sid = r.get("session_id", "")
        if sid:
            rewards_by_session[sid].append(r)

    # Also index rewards by timestamp for fuzzy matching
    rewards_by_ts: list[tuple[float, dict]] = []
    for r in rewards:
        epoch = _parse_ts(r.get("timestamp"))
        if epoch is not None:
            rewards_by_ts.append((epoch, r))
    rewards_by_ts.sort(key=lambda x: x[0])

    # Aggregate fallback: mean reward per (rule_id, routing_role) across all sessions.
    # Used when no session-level match exists for a shadow route (e.g. batch-injected
    # routes whose sessions never generated reward_log entries).
    _agg_lists: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rewards:
        _rid = r.get("rule_id", "")
        _role = r.get("routing_role", "")
        _rv = r.get("reward_value")
        if _rid and _role and _rv is not None:
            try:
                _agg_lists[(_rid, _role)].append(float(_rv))
            except (TypeError, ValueError):
                pass
    agg_by_rule_role: dict[tuple[str, str], float] = {
        k: statistics.mean(vs) for k, vs in _agg_lists.items() if vs
    }

    matched: list[dict] = []
    for shadow in shadows:
        sid = shadow.get("session_id", "")
        rule_id = shadow.get("rule_id", "")
        actual_role = shadow.get("actual_role", "")
        candidate_role = shadow.get("candidate_role", "")

        # Try exact session_id match
        session_rewards = rewards_by_session.get(sid, [])

        # If no exact match, try fuzzy timestamp match within 60s
        if not session_rewards:
            shadow_ts = _parse_ts(shadow.get("timestamp"))
            if shadow_ts is not None:
                for rts, r in rewards_by_ts:
                    if abs(rts - shadow_ts) <= 60:
                        session_rewards.append(r)

        if not session_rewards:
            # Aggregate fallback: use historical mean reward for actual_role+rule_id.
            avg = agg_by_rule_role.get((rule_id, actual_role))
            if avg is None:
                continue
            session_rewards = [{"routing_role": actual_role, "reward_value": avg}]

        for reward in session_rewards:
            matched.append({
                "session_id": sid,
                "rule_id": rule_id,
                "actual_role": actual_role,
                "candidate_role": candidate_role,
                "routing_role": reward.get("routing_role", ""),
                "reward_value": reward.get("reward_value", 0),
                "timestamp": shadow.get("timestamp", ""),
            })

    return matched


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def compute_rule_evaluation(
    matched: list[dict],
    all_rewards: list[dict] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute per-rule evaluation of shadow vs actual routes.

    Args:
        matched: Joined shadow+reward entries from join_shadow_reward()
        all_rewards: Full filtered reward log (for candidate historical lookup)

    Returns:
        dict keyed by rule_id with evaluation stats.
    """
    if all_rewards is None:
        all_rewards = []

    # Index all rewards by (rule_id, routing_role) for candidate lookups
    rewards_by_rule_role: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in all_rewards:
        rid = r.get("rule_id", "")
        role = r.get("routing_role", "")
        rv = r.get("reward_value")
        if rv is not None:
            try:
                rewards_by_rule_role[(rid, role)].append(float(rv))
            except (TypeError, ValueError):
                continue

    # Group matched entries by rule_id
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for m in matched:
        rid = m.get("rule_id", "unknown")
        by_rule[rid].append(m)

    result: dict[str, dict[str, Any]] = {}
    for rule_id, entries in by_rule.items():
        # Actual mean reward from matched sessions
        actual_rewards = []
        actual_role = ""
        candidate_role = ""
        for e in entries:
            rv = e.get("reward_value")
            if rv is not None:
                try:
                    actual_rewards.append(float(rv))
                except (TypeError, ValueError):
                    continue
            if not actual_role:
                actual_role = e.get("actual_role", "")
            if not candidate_role:
                candidate_role = e.get("candidate_role", "")

        actual_mean = statistics.mean(actual_rewards) if actual_rewards else 0.0

        # Extract intent from rule_id (format: "ROLE:intent") for per-intent grouping.
        # Convention: role names (DIRECT, FW, FO, RB, FV) must never contain colons.
        # Adding a colon-containing role will break this split. (ENRICH-001)
        _parts = rule_id.split(":", 1)
        intent = _parts[1] if len(_parts) == 2 else "unknown"

        # Candidate expected reward: historical mean for the candidate role on the same
        # intent. Composite rule_ids lead with the actual role, so the candidate's own
        # rule_id is "<candidate_role>:<intent>"; the bare (rule_id, candidate_role) key
        # only matches legacy non-composite rule_ids.
        candidate_history = []
        if len(_parts) == 2:
            candidate_history = rewards_by_rule_role.get(
                (f"{candidate_role}:{intent}", candidate_role), []
            )
        if not candidate_history:
            candidate_history = rewards_by_rule_role.get((rule_id, candidate_role), [])
        candidate_mean = statistics.mean(candidate_history) if candidate_history else 0.0

        entry: dict[str, Any] = {
            "actual_role": actual_role,
            "candidate_role": candidate_role,
            "intent": intent,
            "actual_mean_reward": round(actual_mean, 6),
            "candidate_expected_reward": round(candidate_mean, 6),
            "sample_count": len(entries),
            "improvement_delta": round(candidate_mean - actual_mean, 6),
        }

        if len(candidate_history) < 10:
            entry["selection_bias_warning"] = True

        result[rule_id] = entry

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Load shadow routes + rewards, join, evaluate, write results."""
    shadows = load_shadow_routes()
    rewards = load_filtered_rewards()
    matched = join_shadow_reward(shadows, rewards)
    evaluation = compute_rule_evaluation(matched, all_rewards=rewards)

    output: dict[str, Any] = {
        "timestamp": now_iso(),
        "rules": evaluation,
    }

    save_json_state(DATA_DIR / "shadow_evaluation.json", output)


if __name__ == "__main__":
    main()
