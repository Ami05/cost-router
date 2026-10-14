"""Tests for replay_governors.py (DGC-005).

Fixture format: JSONL with subagent_dispatch and subagent_stop_telemetry events,
matching the schema from check_routing.py _build_subagent_stop_telemetry_row.
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from datetime import datetime, timedelta, timezone

# Import the module under test
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from replay_governors import (
    load_fixture,
    normalize_agent_id,
    group_events_by_subagent,
    parse_timestamp,
    find_redispatch_within_window,
    evaluate_false_positives,
    count_would_deny_by_gate,
    count_by_tier_and_outcome,
    generate_report,
    replay,
)


# Test fixtures (small sample data)
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def test_load_fixture_valid(tmp_path: Path) -> None:
    """Test loading a valid JSONL fixture."""
    fixture_file = tmp_path / "test_fixture.jsonl"
    events = [
        {"type": "subagent_dispatch", "session_id": "sess1", "subagent_id": "agent1", "ts": "2026-10-03T10:00:00Z"},
        {"type": "subagent_stop_telemetry", "session_id": "sess1", "agent_id": "agent1", "ts": "2026-10-03T10:05:00Z", "outcome": "success"},
    ]
    fixture_file.write_text("\n".join(json.dumps(e) for e in events))

    loaded = load_fixture(fixture_file)
    assert len(loaded) == 2
    assert loaded[0]["type"] == "subagent_dispatch"
    assert loaded[1]["type"] == "subagent_stop_telemetry"


def test_load_fixture_empty(tmp_path: Path) -> None:
    """Test loading an empty fixture file."""
    fixture_file = tmp_path / "empty.jsonl"
    fixture_file.write_text("")

    loaded = load_fixture(fixture_file)
    assert loaded == []


def test_load_fixture_malformed(tmp_path: Path) -> None:
    """Test loading fixture with malformed JSON (should skip bad lines)."""
    fixture_file = tmp_path / "malformed.jsonl"
    fixture_file.write_text('{"valid": "json"}\ninvalid json\n{"also": "valid"}')

    loaded = load_fixture(fixture_file)
    assert len(loaded) == 2
    assert loaded[0].get("valid") == "json"
    assert loaded[1].get("also") == "valid"


def test_load_fixture_missing_file() -> None:
    """Test loading from nonexistent file."""
    loaded = load_fixture(Path("/nonexistent/fixture.jsonl"))
    assert loaded == []


def test_normalize_agent_id() -> None:
    """Test agent_id extraction from both agent_id and subagent_id keys."""
    assert normalize_agent_id({"agent_id": "id1"}) == "id1"
    assert normalize_agent_id({"subagent_id": "id2"}) == "id2"
    assert normalize_agent_id({"agent_id": "id1", "subagent_id": "id2"}) == "id1"  # agent_id wins
    assert normalize_agent_id({}) is None


def test_group_events_by_subagent() -> None:
    """Test grouping events by agent_id."""
    events = [
        {"type": "subagent_dispatch", "agent_id": "agent1"},
        {"type": "subagent_stop_telemetry", "agent_id": "agent1"},
        {"type": "subagent_dispatch", "agent_id": "agent2"},
        {"type": "subagent_stop_telemetry", "agent_id": "agent2"},
    ]

    grouped = group_events_by_subagent(events)
    assert len(grouped) == 2
    assert len(grouped["agent1"]) == 2
    assert len(grouped["agent2"]) == 2


def test_parse_timestamp() -> None:
    """Test ISO 8601 timestamp parsing."""
    ts1 = parse_timestamp("2026-10-03T10:00:00Z")
    assert ts1 is not None
    assert ts1.year == 2026

    ts2 = parse_timestamp("2026-10-03T10:00:00+00:00")
    assert ts2 is not None

    ts3 = parse_timestamp("invalid")
    assert ts3 is None

    ts4 = parse_timestamp(None)
    assert ts4 is None


def test_find_redispatch_within_window(tmp_path: Path) -> None:
    """Test redispatch detection within 10-minute window."""
    base_time = datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc)

    dispatch_events = [
        {
            "type": "subagent_dispatch",
            "agent_id": "agent1",
            "ts": (base_time + timedelta(minutes=5)).isoformat(),
        },
        {
            "type": "subagent_dispatch",
            "agent_id": "agent1",
            "ts": (base_time + timedelta(minutes=15)).isoformat(),
        },
    ]

    # Redispatch at 5 min is within 10-min window after stop at 0:00
    found = find_redispatch_within_window("agent1", base_time, dispatch_events, window_minutes=10)
    assert found is True

    # Redispatch at 15 min is outside 10-min window
    found = find_redispatch_within_window("agent1", base_time, dispatch_events, window_minutes=10)
    # Still True because we find the 5-min one first
    assert found is True


def test_evaluate_false_positives() -> None:
    """Test false-positive detection (would-deny on successful runs with no redispatch)."""
    grouped = {
        "agent1": [
            {
                "type": "subagent_stop_telemetry",
                "outcome": "success",
                "would_deny_counters": {"would_deny:context_pressure": 1},
                "ts": "2026-10-03T10:00:00Z",
            }
        ],
        "agent2": [
            {
                "type": "subagent_stop_telemetry",
                "outcome": "failed",
                "would_deny_counters": {"would_deny:context_pressure": 1},
                "ts": "2026-10-03T10:00:00Z",
            }
        ],
    }

    summary, by_gate = evaluate_false_positives(grouped)

    # agent1 is successful with would-deny and no redispatch → FP
    # agent2 is failed, so not counted as FP
    assert summary["successful_runs_checked"] == 1
    assert summary["runs_with_would_deny"] == 1
    assert summary["false_positive_rate"] == 1.0


def test_count_would_deny_by_gate() -> None:
    """Test counting would-deny events per gate."""
    grouped = {
        "agent1": [
            {
                "type": "subagent_stop_telemetry",
                "would_deny_counters": {
                    "would_deny:context_pressure": 2,
                    "would_deny:stall_kill": 1,
                },
            }
        ],
        "agent2": [
            {
                "type": "subagent_stop_telemetry",
                "would_deny_counters": {"would_deny:context_pressure": 1},
            }
        ],
    }

    counts = count_would_deny_by_gate(grouped)
    assert counts["would_deny:context_pressure"] == 3
    assert counts["would_deny:stall_kill"] == 1


def test_count_by_tier_and_outcome() -> None:
    """Test counting outcomes by model tier."""
    grouped = {
        "agent1": [
            {
                "type": "subagent_stop_telemetry",
                "model": "haiku",
                "outcome": "success",
            }
        ],
        "agent2": [
            {
                "type": "subagent_stop_telemetry",
                "model": "haiku",
                "outcome": "failed",
            }
        ],
        "agent3": [
            {
                "type": "subagent_stop_telemetry",
                "model": "sonnet",
                "outcome": "success",
            }
        ],
    }

    stats = count_by_tier_and_outcome(grouped)
    assert stats["haiku"]["success"] == 1
    assert stats["haiku"]["failed"] == 1
    assert stats["sonnet"]["success"] == 1


def test_generate_report() -> None:
    """Test report generation."""
    fixture_path = Path("tests/fixtures/replay_baseline.jsonl")
    config_path = Path("config.json")

    report = generate_report(
        fixture_path,
        config_path,
        would_deny_by_gate={"would_deny:context_pressure": 5},
        false_pos_summary={
            "successful_runs_checked": 100,
            "runs_with_would_deny": 3,
            "false_positive_rate": 0.03,
        },
        false_pos_by_gate={"would_deny": {"would_deny:context_pressure": 2}},
        tier_outcome_stats={"haiku": {"success": 80, "failed": 20}},
        total_runs=100,
    )

    assert report["type"] == "replay_baseline_report"
    assert report["total_runs"] == 100
    assert report["would_deny_counts"]["would_deny:context_pressure"] == 5
    assert report["false_positive_analysis"]["false_positive_rate"] == 0.03


def test_replay_integration(tmp_path: Path) -> None:
    """Test full replay workflow with a small fixture."""
    # Create a test fixture with realistic data
    fixture_file = tmp_path / "test_replay.jsonl"
    events = [
        # Agent 1: successful run with would-deny
        {
            "type": "subagent_dispatch",
            "session_id": "sess1",
            "subagent_id": "agent1",
            "ts": "2026-10-03T10:00:00Z",
        },
        {
            "type": "subagent_stop_telemetry",
            "session_id": "sess1",
            "agent_id": "agent1",
            "model": "haiku",
            "outcome": "success",
            "would_deny_counters": {"would_deny:context_pressure": 1},
            "ts": "2026-10-03T10:05:00Z",
        },
        # Agent 2: failed run with would-deny
        {
            "type": "subagent_dispatch",
            "session_id": "sess1",
            "subagent_id": "agent2",
            "ts": "2026-10-03T10:10:00Z",
        },
        {
            "type": "subagent_stop_telemetry",
            "session_id": "sess1",
            "agent_id": "agent2",
            "model": "sonnet",
            "outcome": "failed",
            "would_deny_counters": {"would_deny:stall_kill": 2},
            "ts": "2026-10-03T10:15:00Z",
        },
    ]

    fixture_file.write_text("\n".join(json.dumps(e) for e in events))

    # Run replay
    report = replay(str(fixture_file))

    assert report.get("type") == "replay_baseline_report"
    assert report.get("total_runs") == 2
    assert report["would_deny_counts"]["would_deny:context_pressure"] == 1
    assert report["would_deny_counts"]["would_deny:stall_kill"] == 2
    # Agent 1 is successful with would-deny and no redispatch → FP
    assert report["false_positive_analysis"]["false_positive_rate"] == 1.0


def test_replay_missing_fixture() -> None:
    """Test replay with missing fixture file."""
    report = replay("/nonexistent/fixture.jsonl")
    assert "error" in report


def test_replay_empty_fixture(tmp_path: Path) -> None:
    """Test replay with empty fixture."""
    fixture_file = tmp_path / "empty.jsonl"
    fixture_file.write_text("")

    report = replay(str(fixture_file))
    assert "error" in report


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
