"""
learn_patterns.py — Learn routing quality patterns from reward logs.

Reads reward_log.jsonl, groups by rule_id, computes per-rule statistics,
and writes config/learned_routing_patterns.json.

CLI flags:
  --report       Print a markdown table of top patterns with coverage pct
  --data-dir     Override input directory (default: ~/.claude/plugins/local/cost-router/data/)
  --config-dir   Override output directory (default: config/ relative to cwd)
"""

import argparse
import collections
import json
import pathlib
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signal_filter_rules import exclude_legacy_rewards


DEFAULT_DATA_DIR = pathlib.Path.home() / ".claude" / "plugins" / "local" / "cost-router" / "data"
WORKING_DIR = pathlib.Path(__file__).parent.parent
DEFAULT_CONFIG_DIR = WORKING_DIR / "config"
REWARD_LOG_FILENAME = "reward_log.jsonl"
OUTPUT_FILENAME = "learned_routing_patterns.json"

# Thresholds
CHEAP_MIN_REWARD = 0.9
EXPENSIVE_MAX_REWARD = 0.7
EXPENSIVE_MAX_REVERT = 0.1
MIN_COUNT = 3


def parse_rule_id(rule_id: str) -> tuple[str, str]:
    """Parse rule_id of format 'ROLE:intent' into (role, intent)."""
    if ":" in rule_id:
        role, intent = rule_id.split(":", 1)
    else:
        role = rule_id
        intent = ""
    return role, intent


def load_reward_log(data_dir: pathlib.Path) -> list[dict]:
    """Load reward log entries from JSONL file. Returns empty list if missing or empty."""
    log_path = data_dir / REWARD_LOG_FILENAME
    if not log_path.exists():
        return []
    entries = []
    with log_path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entries.append(entry)
            except json.JSONDecodeError:
                # Skip malformed lines silently
                continue
    entries = exclude_legacy_rewards(entries)
    return entries


def compute_patterns(entries: list[dict]) -> dict:
    """Group entries by rule_id and compute aggregate statistics."""
    # Accumulators per rule_id
    reward_sums = collections.defaultdict(float)
    revert_sums = collections.defaultdict(float)
    counts = collections.defaultdict(int)
    session_ids = collections.defaultdict(set)

    for entry in entries:
        rule_id = entry.get("rule_id", "")
        if not rule_id:
            continue
        reward = entry.get("reward_value", 0.0)
        revert = entry.get("revert_rate_at_compute", 0.0)
        session = entry.get("session_id", "")

        reward_sums[rule_id] += reward
        revert_sums[rule_id] += revert
        counts[rule_id] += 1
        if session:
            session_ids[rule_id].add(session)

    cheap_patterns = []
    expensive_patterns = []
    all_rule_ids = set(counts.keys())
    total_sessions = len({
        s
        for entry in entries
        for s in [entry.get("session_id", "")]
        if s
    })

    for rule_id in all_rule_ids:
        count = counts[rule_id]
        avg_reward = reward_sums[rule_id] / count
        avg_revert = revert_sums[rule_id] / count
        role, intent = parse_rule_id(rule_id)

        if count >= MIN_COUNT:
            if avg_reward >= CHEAP_MIN_REWARD:
                cheap_patterns.append({
                    "rule_id": rule_id,
                    "intent": intent,
                    "role": role,
                    "avg_reward": round(avg_reward, 4),
                    "count": count,
                })
            if avg_reward < EXPENSIVE_MAX_REWARD or avg_revert > EXPENSIVE_MAX_REVERT:
                expensive_patterns.append({
                    "rule_id": rule_id,
                    "intent": intent,
                    "role": role,
                    "avg_reward": round(avg_reward, 4),
                    "avg_revert": round(avg_revert, 4),
                    "count": count,
                })

    # Sort by avg_reward descending for cheap, ascending for expensive
    cheap_patterns.sort(key=lambda x: x["avg_reward"], reverse=True)
    expensive_patterns.sort(key=lambda x: x["avg_reward"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_sessions": total_sessions,
        "cheap_patterns": cheap_patterns,
        "expensive_patterns": expensive_patterns,
    }


def print_report(result: dict) -> None:
    """Print a markdown table of top patterns with coverage percentage."""
    total_sessions = result["total_sessions"]
    cheap = result["cheap_patterns"]
    expensive = result["expensive_patterns"]

    print(f"# Routing Pattern Report")
    print(f"\nGenerated: {result['generated_at']}")
    print(f"Total sessions: {total_sessions}")

    print(f"\n## Cheap Patterns (avg_reward >= {CHEAP_MIN_REWARD}, count >= {MIN_COUNT})\n")
    if cheap:
        print(f"| rule_id | role | intent | avg_reward | count | coverage_pct |")
        print(f"|---------|------|--------|-----------|-------|-------------|")
        for p in cheap:
            coverage = round(p["count"] / total_sessions * 100, 1) if total_sessions else 0.0
            print(f"| {p['rule_id']} | {p['role']} | {p['intent']} | {p['avg_reward']} | {p['count']} | {coverage}% |")
    else:
        print("_No cheap patterns found._")

    print(f"\n## Expensive Patterns (avg_reward < {EXPENSIVE_MAX_REWARD} or avg_revert > {EXPENSIVE_MAX_REVERT}, count >= {MIN_COUNT})\n")
    if expensive:
        print(f"| rule_id | role | intent | avg_reward | avg_revert | count | coverage_pct |")
        print(f"|---------|------|--------|-----------|-----------|-------|-------------|")
        for p in expensive:
            coverage = round(p["count"] / total_sessions * 100, 1) if total_sessions else 0.0
            print(f"| {p['rule_id']} | {p['role']} | {p['intent']} | {p['avg_reward']} | {p['avg_revert']} | {p['count']} | {coverage}% |")
    else:
        print("_No expensive patterns found._")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Learn routing quality patterns from reward logs."
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print a markdown table of top patterns with coverage pct",
    )
    parser.add_argument(
        "--data-dir",
        type=pathlib.Path,
        default=DEFAULT_DATA_DIR,
        help=f"Input directory containing {REWARD_LOG_FILENAME} (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--config-dir",
        type=pathlib.Path,
        default=DEFAULT_CONFIG_DIR,
        help=f"Output directory for {OUTPUT_FILENAME} (default: {DEFAULT_CONFIG_DIR})",
    )
    args = parser.parse_args()

    entries = load_reward_log(args.data_dir)
    result = compute_patterns(entries)

    # Ensure output directory exists
    args.config_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.config_dir / OUTPUT_FILENAME
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    if args.report:
        print_report(result)
    else:
        cheap_count = len(result["cheap_patterns"])
        expensive_count = len(result["expensive_patterns"])
        print(
            f"Written {output_path} — "
            f"{cheap_count} cheap pattern(s), {expensive_count} expensive pattern(s), "
            f"{result['total_sessions']} total session(s)."
        )


if __name__ == "__main__":
    main()
