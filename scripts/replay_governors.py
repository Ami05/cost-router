#!/usr/bin/env python3
"""DGC-005: Offline replay harness for would-deny and false-positive rates.

Replays recorded subagent runs (from fixtures) against a given config
and reports would-deny counts per gate and tier, with false-positive rates.

False positive definition: a would-deny on a run that completed successfully
with its task accomplished and had no redispatch of the same task within
10 minutes; stall-kill also FP if worker later produced a final report.

Fixture format: per-call timestamps capturing the relevant subagent
transcript subset (subagent_dispatch and subagent_stop_telemetry events).

Usage:
    replay_governors.py --fixture tests/fixtures/replay_baseline.jsonl --config config.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from collections import Counter, defaultdict
from datetime import datetime, timedelta

# Local imports
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import _warn


def load_fixture(fixture_path: Path) -> list[dict[str, Any]]:
    """Load JSONL fixture file with subagent events.

    Expected format: one event per line (subagent_dispatch or subagent_stop_telemetry).
    Each event must have: type, session_id, agent_id (or subagent_id), ts.
    """
    events = []
    try:
        with open(fixture_path) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    events.append(event)
                except json.JSONDecodeError as e:
                    _warn("fixture_parse_error", {"path": str(fixture_path), "line": line_num, "error": str(e)})
                    continue
    except OSError as e:
        _warn("fixture_load_error", {"path": str(fixture_path), "error": str(e)})
        return []

    return events


def normalize_agent_id(event: dict[str, Any]) -> str | None:
    """Extract agent_id from event (handles both agent_id and subagent_id keys)."""
    return event.get("agent_id") or event.get("subagent_id")


def group_events_by_subagent(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group events by agent_id/subagent_id, preserving order."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        agent_id = normalize_agent_id(event)
        if agent_id:
            grouped[agent_id].append(event)
    return grouped


def parse_timestamp(ts_str: str | None) -> datetime | None:
    """Parse ISO 8601 timestamp string to datetime, handling timezones."""
    if not ts_str:
        return None
    try:
        # Handle +00:00 or Z suffix
        ts_str = ts_str.replace("Z", "+00:00")
        # Try with timezone info
        if "+" in ts_str or ts_str.count("-") > 2:  # Has timezone offset
            return datetime.fromisoformat(ts_str)
        else:
            return datetime.fromisoformat(ts_str)
    except (ValueError, AttributeError):
        return None


def find_redispatch_within_window(
    agent_id: str, stop_ts: datetime, all_dispatch_events: list[dict[str, Any]], window_minutes: int = 10
) -> bool:
    """Check if agent was redispatched within window_minutes after stop_ts."""
    window = timedelta(minutes=window_minutes)
    cutoff = stop_ts + window

    for event in all_dispatch_events:
        if normalize_agent_id(event) == agent_id and event.get("type") == "subagent_dispatch":
            ts = parse_timestamp(event.get("ts"))
            if ts and stop_ts < ts <= cutoff:
                return True
    return False


def evaluate_false_positives(
    grouped_events: dict[str, list[dict[str, Any]]]
) -> tuple[dict[str, Any], dict[str, dict[str, int]]]:
    """Evaluate false positives: would-deny on successful runs with no redispatch.

    Returns:
        (summary_stats, false_positives_by_gate)
    """
    false_positives: dict[str, int] = Counter()
    successful_runs_checked = 0
    runs_with_would_deny = 0

    # Collect all dispatch events for redispatch checking
    all_dispatch_events = []
    for events_for_agent in grouped_events.values():
        for event in events_for_agent:
            if event.get("type") == "subagent_dispatch":
                all_dispatch_events.append(event)

    for agent_id, events in grouped_events.items():
        # Find the subagent_stop_telemetry event
        stop_event = None
        for event in events:
            if event.get("type") == "subagent_stop_telemetry":
                stop_event = event
                break

        if not stop_event:
            continue

        outcome = stop_event.get("outcome")
        would_deny_counters = stop_event.get("would_deny_counters", {})

        # Check if this is a successful run
        is_successful = outcome in ("success", "partial")

        if is_successful:
            successful_runs_checked += 1

            # Check if there was a redispatch within 10 minutes
            stop_ts = parse_timestamp(stop_event.get("ts"))
            had_redispatch = stop_ts and find_redispatch_within_window(
                agent_id, stop_ts, all_dispatch_events, window_minutes=10
            )

            # If there were would-deny counters and no redispatch, count as FP
            if would_deny_counters and not had_redispatch:
                runs_with_would_deny += 1
                for gate_key in would_deny_counters.keys():
                    false_positives[gate_key] += 1

    summary_stats = {
        "successful_runs_checked": successful_runs_checked,
        "runs_with_would_deny": runs_with_would_deny,
        "false_positive_rate": (
            runs_with_would_deny / successful_runs_checked if successful_runs_checked > 0 else 0.0
        ),
    }

    return summary_stats, {"would_deny": dict(false_positives)}


def count_would_deny_by_gate(
    grouped_events: dict[str, list[dict[str, Any]]]
) -> dict[str, int]:
    """Count total would-deny events per gate across all subagents."""
    would_deny_counts: Counter[str] = Counter()

    for events in grouped_events.values():
        for event in events:
            if event.get("type") == "subagent_stop_telemetry":
                counters = event.get("would_deny_counters", {})
                for gate_key, count in counters.items():
                    if isinstance(count, int):
                        would_deny_counts[gate_key] += count

    return dict(would_deny_counts)


def count_by_tier_and_outcome(
    grouped_events: dict[str, list[dict[str, Any]]]
) -> dict[str, dict[str, int]]:
    """Count runs by tier and outcome."""
    stats: dict[str, dict[str, int]] = defaultdict(lambda: Counter())

    for events in grouped_events.values():
        for event in events:
            if event.get("type") == "subagent_stop_telemetry":
                outcome = event.get("outcome", "unknown")
                model = event.get("model") or "unknown"
                stats[model][outcome] += 1

    return {model: dict(counts) for model, counts in stats.items()}


def generate_report(
    fixture_path: Path,
    config_path: Path,
    would_deny_by_gate: dict[str, int],
    false_pos_summary: dict[str, Any],
    false_pos_by_gate: dict[str, dict[str, int]],
    tier_outcome_stats: dict[str, dict[str, int]],
    total_runs: int,
) -> dict[str, Any]:
    """Generate a baseline report from replay results."""
    report = {
        "type": "replay_baseline_report",
        "fixture_path": str(fixture_path),
        "config_path": str(config_path),
        "timestamp": datetime.now().isoformat(),
        "total_runs": total_runs,
        "would_deny_counts": would_deny_by_gate,
        "false_positive_analysis": false_pos_summary,
        "false_positives_by_gate": false_pos_by_gate,
        "outcome_distribution_by_model": tier_outcome_stats,
    }
    return report


def replay(fixture_path: str, config_path: str | None = None) -> dict[str, Any]:
    """Main replay function.

    Args:
        fixture_path: path to JSONL fixture with subagent events
        config_path: path to config.json (optional, for context)

    Returns:
        Report dict with would-deny and false-positive statistics.
    """
    fixture_p = Path(fixture_path)
    if not fixture_p.exists():
        _warn("fixture_not_found", {"path": fixture_path})
        return {"error": f"Fixture not found: {fixture_path}"}

    # Load fixture
    events = load_fixture(fixture_p)
    if not events:
        _warn("fixture_empty", {"path": fixture_path})
        return {"error": f"Fixture is empty or unparseable: {fixture_path}"}

    # Group by subagent
    grouped = group_events_by_subagent(events)
    total_runs = len(grouped)

    if total_runs == 0:
        _warn("no_subagent_events", {"path": fixture_path})
        return {"error": "No subagent events found in fixture"}

    # Analyze would-deny counts
    would_deny_by_gate = count_would_deny_by_gate(grouped)

    # Analyze false positives
    false_pos_summary, false_pos_by_gate = evaluate_false_positives(grouped)

    # Analyze outcome distribution
    tier_outcome_stats = count_by_tier_and_outcome(grouped)

    # Generate report
    report = generate_report(
        fixture_p,
        Path(config_path) if config_path else Path("config.json"),
        would_deny_by_gate,
        false_pos_summary,
        false_pos_by_gate,
        tier_outcome_stats,
        total_runs,
    )

    return report


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, help="Path to JSONL fixture file")
    parser.add_argument("--config", default="config.json", help="Path to config.json (for context)")
    parser.add_argument("--output", help="Output report to file (JSON)")
    args = parser.parse_args()

    report = replay(args.fixture, args.config)

    # Print report as JSON
    print(json.dumps(report, indent=2))

    # Save to file if requested
    if args.output:
        try:
            Path(args.output).write_text(json.dumps(report, indent=2))
            print(f"\nReport saved to: {args.output}", file=__import__("sys").stderr)
        except OSError as e:
            _warn("report_write_failed", {"path": args.output, "error": str(e)})
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
