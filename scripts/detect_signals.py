"""
detect_signals.py — Detect session outcome signals from log files.

Reads:
  - session_events.jsonl: type=session_summary rows (session_id, started_at, ended_at,
    tool_call_count, reward)
  - routing_events.jsonl: type=intent rows (session_id, turn_id, intent, ts)

Signals detected:
  - Signal2: two intent events with same intent label within 120s in same session -> retried (medium)
  - Signal3: tool_call_count <= 2 and session duration < 30s -> abandoned (low)
  - Signal4: reward >= 0.9 and duration > 30s -> clean_success (high)

Combined outcome:
  - clean_success if clean_success and no retried
  - retried if retried signal present
  - abandoned if abandoned signal present
  - unknown otherwise

Writes to data/session_signals.jsonl:
  {session_id, outcome, confidence, signals, duration_seconds, reward, ts}
"""

import json
import os
import sys
import argparse
from datetime import datetime, timezone
from collections import defaultdict


DEFAULT_DATA_DIR = os.path.expanduser(
    "~/.claude/plugins/local/cost-router/data/"
)


def parse_iso(ts_str):
    """Parse ISO 8601 timestamp string; return datetime or None."""
    if not ts_str:
        return None
    try:
        # Python 3.7+ fromisoformat doesn't handle trailing Z
        cleaned = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


def load_session_summaries(data_dir):
    """Load session_events.jsonl, return dict keyed by session_id."""
    path = os.path.join(data_dir, "session_events.jsonl")
    sessions = {}
    if not os.path.exists(path):
        return sessions
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "session_summary":
                continue
            sid = rec.get("session_id")
            if not sid:
                continue
            sessions[sid] = rec
    return sessions


def load_intent_events(data_dir):
    """Load routing_events.jsonl, return dict of session_id -> list of intent events."""
    path = os.path.join(data_dir, "routing_events.jsonl")
    intents = defaultdict(list)
    if not os.path.exists(path):
        return intents
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "intent":
                continue
            sid = rec.get("session_id")
            if not sid:
                continue
            intents[sid].append(rec)
    return intents


def detect_signals(session, intent_events):
    """
    Given a session_summary record and its intent events, return
    (signals: list[str], outcome: str, confidence: str).
    """
    signals = []

    started_at = parse_iso(session.get("started_at"))
    ended_at = parse_iso(session.get("ended_at"))
    if started_at is None or ended_at is None:
        return None  # skip sessions with missing timestamps

    duration_seconds = (ended_at - started_at).total_seconds()
    tool_call_count = session.get("tool_call_count", 0) or 0
    reward = session.get("reward")
    primary_intent = None
    retried_intent = None

    # Signal2: two intent events with same intent label within 120s -> retried (medium)
    has_retried = False
    if intent_events:
        by_intent = defaultdict(list)
        for ev in intent_events:
            intent_label = ev.get("intent")
            ts_val = parse_iso(ev.get("ts"))
            if intent_label and ts_val:
                by_intent[intent_label].append(ts_val)
        for intent_label, timestamps in by_intent.items():
            if len(timestamps) < 2:
                continue
            sorted_ts = sorted(timestamps)
            for i in range(len(sorted_ts) - 1):
                diff = (sorted_ts[i + 1] - sorted_ts[i]).total_seconds()
                if diff <= 120:
                    has_retried = True
                    retried_intent = intent_label
                    break
            if has_retried:
                break
        if by_intent:
            primary_intent = max(by_intent, key=lambda k: len(by_intent[k]))
    if has_retried:
        signals.append("retried")

    # Signal3: tool_call_count <= 2 and duration < 30s -> abandoned (low)
    _fast_class = primary_intent == "inline_narration" or (primary_intent == "qa_simple" and reward is not None)
    has_abandoned = tool_call_count <= 2 and duration_seconds < 30 and not _fast_class
    if has_abandoned:
        signals.append("abandoned")

    # Signal4: reward >= 0.9 and duration > 30s -> clean_success (high)
    has_clean_success = False
    if reward is not None:
        try:
            has_clean_success = float(reward) >= 0.9 and duration_seconds > 30
        except (ValueError, TypeError):
            pass
    if has_clean_success:
        signals.append("clean_success")

    # Signal5: per-class success heuristic for intents with non-standard success signatures
    has_class_success = False
    if primary_intent == "qa_architecture" and duration_seconds > 60 and tool_call_count >= 3:
        has_class_success = True
    elif primary_intent == "inline_narration" and tool_call_count >= 1 and duration_seconds >= 10:
        has_class_success = True
    elif primary_intent == "qa_simple" and reward is not None:
        try:
            has_class_success = float(reward) >= 0.7 and duration_seconds < 30
        except (ValueError, TypeError):
            pass
    if has_class_success:
        signals.append("clean_success")

    # Combine
    if has_retried:
        outcome = "retried"
        confidence = "medium"
    elif has_abandoned:
        outcome = "abandoned"
        confidence = "low"
    elif has_clean_success:
        outcome = "clean_success"
        confidence = "high"
    elif has_class_success:
        outcome = "clean_success"
        confidence = "medium"
    else:
        outcome = "unknown"
        confidence = "low"

    return signals, outcome, confidence, duration_seconds, reward, retried_intent


def run(data_dir, dry_run=False, summary=False):
    sessions = load_session_summaries(data_dir)
    intent_map = load_intent_events(data_dir)

    results = []
    skipped = 0

    for sid, session in sessions.items():
        ev_list = intent_map.get(sid, [])
        detection = detect_signals(session, ev_list)
        if detection is None:
            skipped += 1
            continue
        signals, outcome, confidence, duration_seconds, reward, retried_intent = detection
        ts = session.get("started_at", "")
        entry = {
            "session_id": sid,
            "outcome": outcome,
            "confidence": confidence,
            "signals": signals,
            "duration_seconds": duration_seconds,
            "reward": reward,
            "ts": ts,
            "retried_intent": retried_intent,
        }
        results.append(entry)

    if summary:
        dist = defaultdict(int)
        for r in results:
            dist[r["outcome"]] += 1
        print("Outcome distribution:")
        for outcome, count in sorted(dist.items()):
            print(f"  {outcome}: {count}")
        print(f"  (skipped {skipped} sessions missing timestamps)")
        return results

    if dry_run:
        for entry in results:
            print(json.dumps(entry))
        if skipped:
            print(f"# skipped {skipped} sessions missing started_at/ended_at", file=sys.stderr)
        return results

    # Write output
    out_path = os.path.join(data_dir, "session_signals.jsonl")
    with open(out_path, "w", encoding="utf-8") as fh:
        for entry in results:
            fh.write(json.dumps(entry) + "\n")

    print(f"Wrote {len(results)} records to {out_path}")
    if skipped:
        print(f"Skipped {skipped} sessions missing started_at/ended_at", file=sys.stderr)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Detect session outcome signals from cost-router log files."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results to stdout without writing to file.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print outcome distribution and exit.",
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help=f"Override data directory (default: {DEFAULT_DATA_DIR})",
    )
    args = parser.parse_args()
    run(data_dir=args.data_dir, dry_run=args.dry_run, summary=args.summary)


if __name__ == "__main__":
    main()
