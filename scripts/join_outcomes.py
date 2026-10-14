#!/usr/bin/env python3
"""
Join intent events with reward outcomes to create labeled training data.

Reads:
  - routing_events.jsonl: lines with type=intent containing session_id, turn_id, intent, source, ts
  - reward_log.jsonl: session_id, rule_id, routing_role, reward_value, revert_rate_at_compute, timestamp

Outputs:
  - outcome_labels.jsonl: joined records with computed outcome labels

Outcome logic:
  - avg_reward >= 0.9 -> succeeded
  - avg_reward < 0.7 -> failed
  - else -> unknown
  - no reward entry -> unknown
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signal_filter_rules import exclude_legacy_rewards


def parse_args():
    """Parse CLI arguments."""
    args = {
        "summary": False,
        "since": None,
        "data_dir": None,
    }

    for arg in sys.argv[1:]:
        if arg == "--summary":
            args["summary"] = True
        elif arg.startswith("--since="):
            args["since"] = arg.split("=", 1)[1]
        elif arg.startswith("--data-dir="):
            args["data_dir"] = arg.split("=", 1)[1]

    # Default data directory
    if args["data_dir"] is None:
        args["data_dir"] = str(Path.home() / ".claude" / "plugins" / "local" / "cost-router" / "data")

    return args


def load_routing_events(data_dir, since=None):
    """Load intent events from routing_events.jsonl."""
    events_file = Path(data_dir) / "routing_events.jsonl"
    events = {}

    if not events_file.exists():
        return events

    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    with open(events_file, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if record.get("type") != "intent":
                    continue

                if since_dt and record.get("ts"):
                    try:
                        ts = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
                        if ts < since_dt:
                            continue
                    except (ValueError, AttributeError):
                        pass

                session_id = record.get("session_id")
                if session_id:
                    if session_id not in events:
                        events[session_id] = []
                    events[session_id].append(record)
            except json.JSONDecodeError:
                pass

    return events


def load_reward_log(data_dir):
    """Load reward log and compute aggregates by session_id."""
    reward_file = Path(data_dir) / "reward_log.jsonl"
    aggregates = defaultdict(lambda: {"rewards": [], "revert_rates": [], "routing_roles": set()})

    if not reward_file.exists():
        return aggregates

    # First pass: parse all entries
    entries = []
    with open(reward_file, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    entries.append(record)
            except json.JSONDecodeError:
                pass

    # Apply legacy exclusion filter
    entries = exclude_legacy_rewards(entries)

    # Second pass: aggregate by session_id
    for record in entries:
        session_id = record.get("session_id")
        if session_id:
            reward_val = record.get("reward_value")
            if reward_val is not None:
                aggregates[session_id]["rewards"].append(float(reward_val))

            revert_rate = record.get("revert_rate_at_compute")
            if revert_rate is not None:
                aggregates[session_id]["revert_rates"].append(float(revert_rate))

            routing_role = record.get("routing_role")
            if routing_role:
                aggregates[session_id]["routing_roles"].add(routing_role)

    return aggregates


def compute_outcome(avg_reward):
    """Determine outcome label based on average reward."""
    if avg_reward is None:
        return "unknown"
    if avg_reward >= 0.9:
        return "succeeded"
    if avg_reward < 0.7:
        return "failed"
    return "unknown"


def join_and_write(events, rewards, data_dir):
    """Join events with rewards and write outcome_labels.jsonl."""
    output_file = Path(data_dir) / "outcome_labels.jsonl"

    # Create data directory if it doesn't exist
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    with open(output_file, "w") as out_f:
        for session_id, event_list in events.items():
            reward_data = rewards.get(session_id)

            # Compute aggregates
            avg_reward = None
            avg_revert_rate = None
            routing_role = None

            if reward_data:
                if reward_data["rewards"]:
                    avg_reward = sum(reward_data["rewards"]) / len(reward_data["rewards"])
                if reward_data["revert_rates"]:
                    avg_revert_rate = sum(reward_data["revert_rates"]) / len(reward_data["revert_rates"])
                if reward_data["routing_roles"]:
                    routing_role = list(reward_data["routing_roles"])[0]

            outcome = compute_outcome(avg_reward)

            # Write one output record per intent event
            for event in event_list:
                output_record = {
                    "session_id": session_id,
                    "turn_id": event.get("turn_id"),
                    "intent": event.get("intent"),
                    "source": event.get("source"),
                    "routing_role": routing_role,
                    "reward_value": avg_reward,
                    "revert_rate": avg_revert_rate,
                    "outcome": outcome,
                    "ts": event.get("ts"),
                }
                out_f.write(json.dumps(output_record) + "\n")


def print_summary(events, rewards, data_dir):
    """Print summary statistics by intent class and outcome."""
    intent_outcomes = defaultdict(lambda: defaultdict(int))
    intent_total = defaultdict(int)

    for session_id, event_list in events.items():
        reward_data = rewards.get(session_id)

        avg_reward = None
        if reward_data and reward_data["rewards"]:
            avg_reward = sum(reward_data["rewards"]) / len(reward_data["rewards"])

        outcome = compute_outcome(avg_reward)

        for event in event_list:
            intent = event.get("intent", "unknown")
            intent_outcomes[intent][outcome] += 1
            intent_total[intent] += 1

    print("\n=== Outcome Summary by Intent Class ===\n")
    for intent in sorted(intent_total.keys()):
        total = intent_total[intent]
        print(f"{intent} (n={total}):")
        for outcome in ["succeeded", "failed", "unknown"]:
            count = intent_outcomes[intent][outcome]
            pct = 100 * count / total if total > 0 else 0
            print(f"  {outcome}: {count} ({pct:.1f}%)")
        print()


def main():
    """Main entry point."""
    args = parse_args()

    data_dir = args["data_dir"]
    since = args["since"]

    # Load data
    events = load_routing_events(data_dir, since=since)
    rewards = load_reward_log(data_dir)

    # Join and write
    join_and_write(events, rewards, data_dir)

    print(f"Wrote {sum(len(v) for v in events.values())} outcome labels to {data_dir}/outcome_labels.jsonl")

    # Print summary if requested
    if args["summary"]:
        print_summary(events, rewards, data_dir)


if __name__ == "__main__":
    main()
