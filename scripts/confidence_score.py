#!/usr/bin/env python3
"""cost-router hook: confidence score accumulation (PB-B1) + blast radius classification (PB-B2).

Runs at Stop. Computes confidence scores for routing rules based on observed reward signals.

PB-B1 (Evidence Accumulation):
  Reads all events, groups by session_id, finds sessions with reward_signal events,
  determines dominant intent per session, computes (intent, verdict_role) pairs,
  aggregates signal consistency, and derives confidence scores.

PB-B2 (Blast Radius):
  Classifies each (intent, verdict_role) -> suggested_role pair's risk level (low/medium/high)
  based on tier transitions, intent category, and pinned status.

Writes to data/confidence_scores.json with rolling window of latest CONFIDENCE_WINDOW sessions.
"""

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (
    DATA_DIR,
    append_event,
    load_json_state,
    now_iso,
    read_events,
    read_stdin_json,
    save_json_state,
    session_id_of,
    _warn,
)

# ── Pure scoring and signal-weighting functions (delegated to confidence_algorithm) ──
# All raw computation lives there; confidence_score.py handles I/O and hooks only.
from confidence_algorithm import (  # noqa: E402
    _tier,
    classify_blast_radius,
    _get_int_env,
    _get_float_env,
)


# ── Confidence score computation (PB-B1) ──────────────────────────────────────


def load_pinned_map() -> dict[str, bool]:
    """Load pinned status for each intent from pinned_rules.json.

    Returns:
        dict mapping intent -> pinned (bool), defaults to False.
    """
    candidate_path = DATA_DIR / "pinned_rules.json"
    try:
        data = json.loads(candidate_path.read_text(encoding="utf-8"))
        rules = data.get("rules", [])
        # An empty rules list is the normal cold-start state before tune-routing
        # promotes any candidate — not a failure. Warning here on every read
        # buried real entries under ~350 duplicate warnings.
        pinned_map = {}
        for rule in rules:
            intent = rule.get("intent")
            if intent:
                pinned_map[intent] = True
        return pinned_map
    except (OSError, json.JSONDecodeError):
        _warn("pinned_rules_unreadable", {"file": "pinned_rules.json", "message": "pinned_rules.json missing or unreadable"})
        return {}


def compute_confidence_scores() -> dict[str, any]:
    """Compute confidence scores for routing rules from reward signal events.

    Returns:
        dict with keys:
          - updated_at: ISO timestamp
          - window: rolling window size
          - entries: list of {intent, verdict_role, suggested_role, n, consistent, confidence, blast_radius, ready}
    """
    # Read configuration — config.json defaults, env vars override
    from _common import config_get  # noqa: PLC0415
    confidence_window = _get_int_env("CONFIDENCE_WINDOW", config_get("feedback_loop.confidence_window", 50))
    confidence_threshold = _get_float_env("CONFIDENCE_THRESHOLD", config_get("feedback_loop.confidence_threshold_low", 0.65))
    confidence_min_n = _get_int_env("CONFIDENCE_MIN_N", config_get("feedback_loop.confidence_min_n_low", 3))
    confidence_threshold_medium = _get_float_env("CONFIDENCE_THRESHOLD_MEDIUM", config_get("feedback_loop.confidence_threshold_medium", 0.80))
    confidence_min_n_medium = _get_int_env("CONFIDENCE_MIN_N_MEDIUM", config_get("feedback_loop.confidence_min_n_medium", 5))

    # Load pinned statuses
    pinned_map = load_pinned_map()

    # Read all events
    events = read_events()

    # Step 1: Group events by session_id
    sessions_by_id: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        session_id = event.get("session_id")
        if session_id:
            sessions_by_id[session_id].append(event)

    # Step 2: Find sessions with at least one routing_declared event
    signal_sessions: list[tuple[str, list[dict]]] = []
    for session_id, session_events in sessions_by_id.items():
        if any(e.get("type") == "routing_declared" for e in session_events):
            signal_sessions.append((session_id, session_events))

    # Step 3: Determine dominant intent for each signal session from intent events,
    # falling back to routing_state if no intent event is present.
    routing_state = load_json_state(DATA_DIR / "routing_state.json")
    sessions_entry = routing_state.get("sessions", {})

    sessions_with_intent: list[tuple[str, list[dict], str]] = []
    for session_id, session_events in signal_sessions:
        # Prefer intent from an explicit intent event
        intent_event = next(
            (e for e in session_events if e.get("type") == "intent"), None
        )
        if intent_event:
            prompt_intent = intent_event.get("intent", "")
        else:
            session_entry = sessions_entry.get(session_id, {})
            prompt_intent = session_entry.get("prompt_intent", "")
        if not prompt_intent:
            # Skip sessions with no resolvable intent
            continue
        sessions_with_intent.append((session_id, session_events, prompt_intent))

    # Step 4: Rolling window - sort by latest event ts, take last CONFIDENCE_WINDOW
    sessions_with_intent.sort(
        key=lambda x: max(
            (e.get("ts", "") for e in x[1]),
            default="",
        )
    )
    windowed_sessions = sessions_with_intent[-confidence_window:]

    # Step 5: Collect observations: (intent, verdict_role) per windowed session
    # Uses routing_declared events; verdict_role comes from the "role" field.
    observations: list[tuple[str, str, str]] = []  # (intent, verdict_role, ts)
    for session_id, session_events, dominant_intent in windowed_sessions:
        # Take first routing_declared event for verdict_role
        signal_event = next(
            (e for e in session_events if e.get("type") == "routing_declared"), None
        )
        if signal_event:
            verdict_role = signal_event.get("role", "")
            ts = signal_event.get("ts", "")
            observations.append((dominant_intent, verdict_role, ts))

    # Step 6: Group by intent, collecting verdict_roles
    grouped: dict[str, list[str]] = defaultdict(list)
    for intent, verdict_role, ts in observations:
        grouped[intent].append(verdict_role)

    # Step 7: Compute role-consistency confidence per intent
    entries = []
    for intent, verdict_roles in grouped.items():
        n = len(verdict_roles)
        # Find most common verdict_role
        role_counts: Counter[str] = Counter(verdict_roles)
        verdict_role, consistent = role_counts.most_common(1)[0]
        confidence = round(consistent / n, 3) if n > 0 else 0.0

        import sys
        print(
            f"CONFIDENCE_DEBUG intent={intent!r} n={n} verdict_role={verdict_role!r} confidence={confidence}",
            file=sys.stderr,
        )

        # Step 9: suggested_role is None (no signal-based suggestion with routing_declared)
        suggested_role = None

        # Step 10: Classify blast_radius
        pinned = pinned_map.get(intent, False)
        blast_radius = classify_blast_radius(intent, verdict_role, suggested_role, pinned)

        # Step 11: Determine ready flag
        ready = False
        if blast_radius == "low":
            ready = confidence >= confidence_threshold and n >= confidence_min_n
        elif blast_radius == "medium":
            ready = confidence >= confidence_threshold_medium and n >= confidence_min_n_medium

        entries.append(
            {
                "intent": intent,
                "verdict_role": verdict_role,
                "rule_id": f"{verdict_role}:{intent}",
                "suggested_role": suggested_role,
                "n": n,
                "consistent": consistent,
                "confidence": confidence,
                "blast_radius": blast_radius,
                "ready": ready,
            }
        )

    return {
        "updated_at": now_iso(),
        "window": confidence_window,
        "entries": entries,
    }


def write_candidates_to_table(scores: dict) -> None:
    """Merge ready confidence entries into data/candidate_table.json, skipping pinned rules.

    For each entry in scores["entries"] where ready=True:
      - Maps suggested_role -> verdict_role, n -> sample_n
      - Skips any rule with pinned=True in the existing table
      - Writes rationale as: "Tier 1 evidence — <verdict_role> <n> sessions <confidence>%"
      - Atomic write via tmp file + os.replace
    """
    table_path = DATA_DIR / "candidate_table.json"
    table = load_json_state(table_path) or {"version": "1.0", "description": "", "rules": []}
    pinned_data = load_json_state(DATA_DIR / "pinned_rules.json") or {"rules": []}

    # Build a lookup of existing rules keyed by intent
    existing: dict = {r["intent"]: r for r in table.get("rules", [])}
    pinned_intents: set = {r["intent"] for r in pinned_data.get("rules", []) if r.get("intent")}

    for entry in scores.get("entries", []):
        if not entry.get("ready"):
            continue

        intent = entry["intent"]
        verdict_role = entry["verdict_role"]
        suggested_role = entry["suggested_role"]
        sample_n = entry["n"]
        confidence_pct = round(entry["confidence"] * 100, 1)

        # Never overwrite a pinned rule
        if intent in pinned_intents:
            continue

        existing[intent] = {
            "intent": intent,
            "suggested_role": suggested_role,
            "rationale": (
                f"Tier 1 evidence — {verdict_role} {sample_n} sessions {confidence_pct}%"
            ),
            "pinned": False,
        }

    table["rules"] = list(existing.values())

    tmp_path = table_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(table, indent=2))
    os.replace(tmp_path, table_path)


def main() -> int:
    """Hook entry point. Computes confidence scores and writes to data/confidence_scores.json."""
    try:
        payload = read_stdin_json()
        session_id = session_id_of(payload)

        # Compute and save confidence scores
        scores = compute_confidence_scores()
        scores_path = DATA_DIR / "confidence_scores.json"
        save_json_state(scores_path, scores)

        # Merge ready entries into candidate_table.json
        write_candidates_to_table(scores)
    except Exception as e:
        # Fail open - always continue
        _warn("confidence_score_failed", {"error": str(e)})

    # Stop hook: emit empty object (valid Stop hook no-op; "continue" is not a Stop hook field)
    print(json.dumps({}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
