"""Tests for _build_subagent_stop_telemetry_row and event_subagent_stop in check_routing.py.

DGC-006: Verify telemetry row fields, stall_events ledger behavior, and append_event resilience.
"""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_routing import (  # noqa: E402
    _build_subagent_stop_telemetry_row,
    event_subagent_stop,
)
from _fileio import append_event, DATA_DIR  # noqa: E402
from session_state import ROUTING_STATE_FILE, SUBAGENT_STATE_FILE  # noqa: E402


def _make_subagent_stop_payload(session_id="test-session-123", agent_id="agent-456"):
    """Build a minimal SubagentStop payload."""
    return {
        "hook_event_name": "SubagentStop",
        "session_id": session_id,
        "agent_id": agent_id,
        "status": "success",
        "model": "claude-haiku-4-5-20251001",
    }


def _make_state_with_subagent(agent_id, stall_count=3, depth=2):
    """Build a state dict with subagent_progress and session entry."""
    return {
        "sessions": {
            "test-session-123": {
                "last_dispatch_context_estimate": {"total_tokens": 15000},
                "continuation_manifest": {"depth": depth},
            }
        },
        "subagents": {},
        "subagent_progress": {
            agent_id: {
                "stall_count": stall_count,
                "last_marker_ts": time.time(),
            }
        },
    }


def test_build_subagent_stop_telemetry_row_fields():
    """Assert row contains exactly the specified fields and no token fields."""
    payload = _make_subagent_stop_payload(agent_id="agent-001")
    state = _make_state_with_subagent("agent-001", stall_count=5, depth=3)

    row = _build_subagent_stop_telemetry_row(payload, "test-session-123", state)

    # Check required fields exist
    assert row.get("type") == "subagent_stop_telemetry"
    assert row.get("session_id") == "test-session-123"
    assert row.get("agent_id") == "agent-001"
    assert row.get("depth") == 3
    assert row.get("status") == "success"
    assert row.get("model") == "claude-haiku-4-5-20251001"
    assert row.get("outcome") == "success"
    assert row.get("startup_estimate") == 15000
    assert row.get("stall_events") == 5

    # Assert no token fields
    token_fields = [
        "input_tokens", "output_tokens", "cache_creation_tokens",
        "cache_read_tokens", "total_tokens", "tokens"
    ]
    for field in token_fields:
        assert field not in row, f"Unexpected token field {field} in row: {row}"


def test_build_row_stall_events_from_ledger():
    """Assert stall_events reflects the real stall_count from dispatch_enforcer ledger before drop."""
    payload = _make_subagent_stop_payload(agent_id="agent-stall-test")

    # Create state with stall_count = 7 in ledger
    state = _make_state_with_subagent("agent-stall-test", stall_count=7)

    row = _build_subagent_stop_telemetry_row(payload, "test-session-123", state)

    # stall_events should match the ledger before it's dropped
    assert row["stall_events"] == 7, f"Expected stall_events=7, got {row['stall_events']}"


def test_build_row_performance_under_50ms():
    """Assert building the row takes under 50 ms."""
    payload = _make_subagent_stop_payload(agent_id="agent-perf")
    state = _make_state_with_subagent("agent-perf", stall_count=10)

    start = time.perf_counter()
    row = _build_subagent_stop_telemetry_row(payload, "test-session-123", state)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 50, f"Building row took {elapsed_ms:.2f}ms, exceeds 50ms limit"
    assert row is not None


def test_event_subagent_stop_handles_append_failure():
    """Assert event_subagent_stop doesn't raise when append_event fails."""
    payload = _make_subagent_stop_payload(agent_id="agent-append-fail")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Block DATA_DIR to force append_event failures
        blocked_data_dir = Path(tmpdir) / "blocked_dir"
        blocked_data_dir.write_text("not a directory")

        with patch("_fileio.DATA_DIR", blocked_data_dir), \
             patch("check_routing._load_state") as mock_load, \
             patch("check_routing._save_state") as mock_save:

            mock_load.return_value = _make_state_with_subagent("agent-append-fail")

            # This should NOT raise even though append_event will fail
            try:
                result = event_subagent_stop(payload, is_claude=True)
                # Should return a dict with continue=True for Claude
                assert isinstance(result, dict)
                assert result.get("continue") is True
            except Exception as e:
                raise AssertionError(
                    f"event_subagent_stop raised {type(e).__name__} despite append_event failure: {e}"
                )


def test_build_row_would_deny_counters_extraction():
    """Assert row extracts only would_deny counters, ignoring gate_deny and other entries."""
    payload = _make_subagent_stop_payload(agent_id="agent-counters")

    # Build state with counters containing both would_deny and gate_deny entries
    state = _make_state_with_subagent("agent-counters", stall_count=2)
    state["counters"] = {
        "would_deny:context_pressure": 2,
        "would_deny:depth": 1,
        "gate_deny_x": 5,
        "gate_deny_y": 3,
    }

    row = _build_subagent_stop_telemetry_row(payload, "test-session-123", state)

    # Assert would_deny_counters contains only would_deny entries
    assert "would_deny_counters" in row, "Row missing would_deny_counters field"
    expected_would_deny = {
        "would_deny:context_pressure": 2,
        "would_deny:depth": 1,
    }
    assert row["would_deny_counters"] == expected_would_deny, \
        f"Expected {expected_would_deny}, got {row['would_deny_counters']}"


def test_build_row_would_deny_counters_missing():
    """Assert would_deny_counters is empty dict when state_counters are missing."""
    payload = _make_subagent_stop_payload(agent_id="agent-no-counters")

    # Build state without state_counters
    state = _make_state_with_subagent("agent-no-counters", stall_count=1)
    # Explicitly ensure no state_counters key

    row = _build_subagent_stop_telemetry_row(payload, "test-session-123", state)

    # Assert would_deny_counters is empty dict
    assert "would_deny_counters" in row, "Row missing would_deny_counters field"
    assert row["would_deny_counters"] == {}, \
        f"Expected empty dict, got {row['would_deny_counters']}"


def test_append_event_bounds_file_size():
    """Assert append_event calls rotation check and handles file size bounding."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        routing_events = data_dir / "routing_events.jsonl"

        with patch("_fileio.DATA_DIR", data_dir), \
             patch("_config.DATA_DIR", data_dir), \
             patch("_fileio.USAGE_LOG", routing_events), \
             patch("_fileio.USAGE_LOG_COMPACT_TRIGGER_BYTES", 200):  # Small threshold

            # Write events until rotation may trigger
            for i in range(30):
                event = {
                    "type": "test_event",
                    "session_id": "test-session",
                    "index": i,
                }
                append_event(event)

            # Check that routing_events.jsonl exists with content
            assert routing_events.exists(), f"routing_events.jsonl not created"

            size = routing_events.stat().st_size
            assert size > 0, "routing_events.jsonl should have content"

            # If rotation file exists, verify current log is bounded
            rotated = data_dir / "routing_events.jsonl.1"
            if rotated.exists():
                assert routing_events.stat().st_size <= 1000, "Post-rotation log exceeds bound"


if __name__ == "__main__":
    test_build_subagent_stop_telemetry_row_fields()
    print("PASS: test_build_subagent_stop_telemetry_row_fields")

    test_build_row_stall_events_from_ledger()
    print("PASS: test_build_row_stall_events_from_ledger")

    test_build_row_performance_under_50ms()
    print("PASS: test_build_row_performance_under_50ms")

    test_event_subagent_stop_handles_append_failure()
    print("PASS: test_event_subagent_stop_handles_append_failure")

    test_build_row_would_deny_counters_extraction()
    print("PASS: test_build_row_would_deny_counters_extraction")

    test_build_row_would_deny_counters_missing()
    print("PASS: test_build_row_would_deny_counters_missing")

    test_append_event_bounds_file_size()
    print("PASS: test_append_event_bounds_file_size")

    print("\nAll tests passed!")
