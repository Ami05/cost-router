#!/usr/bin/env python3
"""cost-router hook: implicit reward signal capture (PA-1).

Runs at Stop. Pure signal-extraction functions are at the top of this module;
all I/O and side effects are isolated in main().

Signal types extracted:
  - under_route: FW/DIRECT verdict followed by reasoning-tier dispatch in same session
  - quality_failure: handled by detect_revert.py at PostToolUse:Bash (not here)
  - manual_override: routing verdict role != actual dispatch tier

Reward-log writing and noise filtering live in main() only — this module's
public API is the signal-extraction functions.
"""
# Required for Python <3.10 compatibility: defers annotation evaluation so
# X | Y union syntax doesn't raise TypeError at parse time on older interpreters.
from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_event, read_events, session_id_of, read_stdin_json, DATA_DIR, save_json_state, _warn
from _model_utils import model_of, tier_of_model
from signal_filter_rules import apply_all_filters

_FAST_ROLES = frozenset({"DIRECT", "FW", "FO"})
_REASONING_TIERS = frozenset({"reasoning"})


# ---------------------------------------------------------------------------
# Pure signal extraction — no I/O, no side effects
# ---------------------------------------------------------------------------


def detect_escalation_signals(events: list[dict], session_id: str) -> list[dict]:
    """Detect FW/DIRECT verdict followed by reasoning-tier subagent in same session.

    Returns a list of reward_signal dicts (empty when no under_route detected).
    Pure function: reads only the provided event list, writes nothing.
    """
    session_events = [e for e in events if e.get("session_id") == session_id]

    verdicts = [e for e in session_events if e.get("type") == "routing_declared"]
    subagent_events = [
        e
        for e in session_events
        if e.get("type") in ("subagent_start", "subagent_dispatch", "subagent_stop")
        and tier_of_model(e.get("model", "")) in _REASONING_TIERS
    ]

    signals = []
    for verdict in verdicts:
        role = verdict.get("role", "")
        if role in _FAST_ROLES and subagent_events:
            signals.append(
                {
                    "type": "reward_signal",
                    "session_id": session_id,
                    "signal": "under_route",
                    "verdict_role": role,
                    "escalated_to": "reasoning",
                    "model": verdict.get("model", ""),
                    "note": "fast-path verdict followed by reasoning-tier dispatch in same session",
                }
            )
            break  # one signal per session
    return signals


def extract_quality_multiplier(intent: str | None, quality_data: dict[str, dict]) -> tuple[float, float]:
    """Return (quality_multiplier, revert_rate) for the given intent.

    quality_multiplier = max(0.1, 1.0 - revert_rate)
    Defaults to (1.0, 0.0) when intent is absent from quality data.
    Pure function: no I/O.
    """
    if not intent or intent not in quality_data:
        return 1.0, 0.0
    entry = quality_data[intent]
    revert_rate = float(entry.get("revert_rate", 0.0))
    multiplier = max(0.1, 1.0 - revert_rate)
    return multiplier, revert_rate


def build_reward_entries(
    verdicts: list[dict],
    session_id: str,
    has_under_route: bool,
    quality_data: dict[str, dict],
    reward_source: str | None = None,
    computed_reward: float | None = None,
) -> list[dict]:
    """Build per-rule reward entry dicts from verdicts and quality data.

    Pure function: accepts quality_data as a parameter rather than loading it.
    Returns a list of entry dicts ready to be written to reward_log.jsonl.

    Base reward precedence: under_route (-1.0) > computed_reward (cost-aware,
    capped at REWARD_NO_ESCALATION) > binary fallback. A negative base is made
    worse, not better, by a low quality multiplier.
    """
    reward_under = float(os.environ.get("REWARD_UNDER_ROUTE", -1.0))
    reward_ok = float(os.environ.get("REWARD_NO_ESCALATION", 1.0))
    cost_included = False
    if has_under_route:
        base_reward_value, derived_source = reward_under, "under_route"
    elif computed_reward is not None:
        base_reward_value, derived_source = min(computed_reward, reward_ok), "computed"
        cost_included = True
    else:
        base_reward_value, derived_source = reward_ok, "binary_fallback"
    reward_source = reward_source or derived_source

    entries = []
    for verdict in verdicts:
        rule_id = (
            verdict.get("rule_id")
            or verdict.get("intent")
            or verdict.get("role", "unknown")
        )
        intent = verdict.get("intent") or verdict.get("rule_id")
        qmult, revert_rate = extract_quality_multiplier(intent, quality_data)
        eff_mult = qmult if base_reward_value >= 0 else 2.0 - qmult
        _actual_model = verdict.get("model", "")
        verdict_ts = verdict.get('ts') or verdict.get('timestamp')
        entries.append({
            "rule_id": rule_id,
            "routing_role": verdict.get("role", ""),
            "actual_model": _actual_model,
            "model_key": _actual_model or None,
            "base_reward_value": base_reward_value,
            "quality_multiplier": qmult,
            "revert_rate_at_compute": revert_rate,
            "reward_value": round(base_reward_value * eff_mult, 6),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "reward_source": reward_source,
            "schema_version": 1,
            "cost_included": cost_included,
            "verdict_ts": verdict_ts,
        })
    return entries


# ---------------------------------------------------------------------------
# I/O helpers (side-effect boundary — called only from main())
# ---------------------------------------------------------------------------


def _load_quality_signal() -> dict[str, dict]:
    """Load data/quality_signal.json keyed by intent.

    Returns empty dict if file is absent (emits stderr warning).
    Structure expected: {intent: {revert_rate: float, ...}, ...}
    """
    quality_path = DATA_DIR / "quality_signal.json"
    if not quality_path.exists():
        _warn(
            "quality_signal_missing",
            {
                "file": "reward_signal.py",
                "msg": "data/quality_signal.json not found; quality_multiplier defaults to 1.0",
            },
        )
        return {}
    try:
        with quality_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # quality_signal.py writes {"intents": [{intent, revert_rate, ...}], ...};
            # normalize to the {intent: {revert_rate, ...}} shape consumers expect.
            if isinstance(data.get("intents"), list):
                return {
                    e["intent"]: e
                    for e in data["intents"]
                    if isinstance(e, dict) and e.get("intent")
                }
            return data
        _warn(
            "quality_signal_invalid",
            {"file": "reward_signal.py", "msg": "quality_signal.json root must be a dict"},
        )
        return {}
    except (json.JSONDecodeError, OSError) as e:
        _warn("quality_signal_load_failed", {"file": "reward_signal.py", "error": str(e)})
        return {}


def _write_reward_entries_to_disk(entries: list[dict], reward_log_path: Path) -> None:
    """Append *entries* to reward_log.jsonl with deduplication and idempotent locking.

    Fails silently on OSError.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not reward_log_path.exists():
            reward_log_path.touch()

        lock_path = reward_log_path.with_name(reward_log_path.name + '.lock')
        with lock_path.open("a", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                with reward_log_path.open("a+", encoding="utf-8") as f:
                    # Existing rows with a verdict_ts form the dedup key set.
                    f.seek(0)
                    existing_keys = set()
                    malformed_count = 0
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                existing_row = json.loads(line)
                                if isinstance(existing_row, dict):
                                    # Build key: (session_id, rule_id, reward_source, verdict_ts)
                                    session_id = existing_row.get('session_id')
                                    rule_id = existing_row.get('rule_id')
                                    reward_source = existing_row.get('reward_source', 'binary_fallback')
                                    verdict_ts = existing_row.get('verdict_ts')
                                    if verdict_ts is not None:
                                        key = (session_id, rule_id, reward_source, verdict_ts)
                                        existing_keys.add(key)
                            except json.JSONDecodeError:
                                malformed_count += 1

                    # Warn once if any malformed lines found
                    if malformed_count > 0:
                        _warn("reward_log_malformed_lines", {
                            "file": "reward_signal.py",
                            "count": malformed_count,
                            "msg": f"skipped {malformed_count} malformed lines in reward_log.jsonl"
                        })

                    seen_keys = set()
                    entries_to_append = []
                    for entry in entries:
                        session_id = entry.get('session_id')
                        rule_id = entry.get('rule_id')
                        reward_source = entry.get('reward_source', 'binary_fallback')
                        verdict_ts = entry.get('verdict_ts')

                        # Build key for this entry
                        if verdict_ts is not None:
                            # Skip if key already seen in batch or on disk
                            key = (session_id, rule_id, reward_source, verdict_ts)
                            if key not in seen_keys and key not in existing_keys:
                                entries_to_append.append(entry)
                                seen_keys.add(key)
                        else:
                            # Entries with verdict_ts None are always appended
                            entries_to_append.append(entry)

                    f.seek(0, 2)  # Seek to end of file
                    for entry in entries_to_append:
                        f.write(json.dumps(entry, separators=(",", ":")) + "\n")

            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    except OSError as e:
        _warn("reward_log_write_failed", {"file": "reward_signal.py", "error": str(e)})


def _filter_and_rewrite_reward_log(reward_log_path: Path) -> None:
    """Apply noise filters to reward_log.jsonl and atomically rewrite it.

    Acquires exclusive lock for the entire read-filter-replace sequence
    to prevent concurrent appends from losing data.
    """
    try:
        if not reward_log_path.exists():
            return

        lock_path = reward_log_path.with_name(reward_log_path.name + '.lock')
        with lock_path.open("a", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                with reward_log_path.open("a+", encoding="utf-8") as f:
                    raw_entries = []
                    f.seek(0)
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                parsed = json.loads(line)
                                if isinstance(parsed, dict):
                                    raw_entries.append(parsed)
                            except json.JSONDecodeError:
                                continue
                    if not raw_entries:
                        return
                    clean, noise_stats = apply_all_filters(raw_entries)
                    # Guard: if filter removes everything, preserve original data.
                    if clean:
                        tmp = reward_log_path.with_name(
                            f"{reward_log_path.name}.{os.getpid()}.tmp"
                        )
                        try:
                            with tmp.open("w", encoding="utf-8") as tmp_f:
                                for entry in clean:
                                    tmp_f.write(json.dumps(entry, separators=(",", ":")) + "\n")
                            os.replace(tmp, reward_log_path)
                        finally:
                            if tmp.exists():
                                try:
                                    tmp.unlink()
                                except OSError:
                                    pass
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        _warn("reward_log_write_failed", {"file": "reward_signal.py", "error": str(e)})


# ---------------------------------------------------------------------------
# Hook entry point — all side effects are here
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = read_stdin_json()
        session_id = session_id_of(payload)
        events = read_events()

        # 1. Pure signal extraction
        signals = detect_escalation_signals(events, session_id)

        # 2. Build per-rule reward entries (pure)
        session_events = [e for e in events if e.get("session_id") == session_id]
        verdicts = [e for e in session_events if e.get("type") == "routing_declared"]
        has_under_route = any(s.get("signal") == "under_route" for s in signals)
        quality_data = _load_quality_signal()
        try:
            from session_hook import summarize_session
            cr = float(summarize_session(events, session_id, payload.get("cwd", ""))["reward"])
        except Exception as e:
            cr = None
            _warn("reward_compute_failed", {"error": str(e)})
        entries = build_reward_entries(verdicts, session_id, has_under_route, quality_data, computed_reward=cr)

        # 3. Write reward entries to disk
        reward_log_path = DATA_DIR / "reward_log.jsonl"
        _write_reward_entries_to_disk(entries, reward_log_path)

        # 4. Filter noise from reward_log.jsonl
        _filter_and_rewrite_reward_log(reward_log_path)

        # 5. Append reward_signal events to usage.jsonl
        for sig in signals:
            try:
                append_event(sig)
            except OSError as e:
                _warn("reward_log_write_failed", {"file": "reward_signal.py", "error": str(e)})

    except Exception as e:
        print(f"reward_signal_error: {e}", file=sys.stderr)
        _warn("reward_signal_main_failed", {"file": "reward_signal.py", "error": str(e)})
    print(json.dumps({}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# Test compatibility aliases (exported for testing purposes)
_quality_multiplier = extract_quality_multiplier


def _write_reward_entries(verdicts, session_id, has_under_route):
    """Wrapper for backward compatibility with old test API."""
    quality_data = _load_quality_signal()
    entries = build_reward_entries(verdicts, session_id, has_under_route, quality_data)
    _write_reward_entries_to_disk(entries, DATA_DIR / "reward_log.jsonl")
