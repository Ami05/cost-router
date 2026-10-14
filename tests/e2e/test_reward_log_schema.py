"""Tests for reward_log schema, entry building, and legacy filtering.

Tests cover:
- reward_signal.build_reward_entries schema validation
- reward_signal._write_reward_entries_to_disk dedup and idempotency
- signal_filter_rules.exclude_legacy_rewards filtering
- Readers (learn_patterns, join_outcomes, shadow_evaluator) on legacy/tagged mixed data
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _common
import reward_signal
from reward_signal import build_reward_entries, _write_reward_entries_to_disk
from signal_filter_rules import exclude_legacy_rewards
import learn_patterns
import join_outcomes
import shadow_evaluator


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Isolate all data directory operations to tmp_path."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(_common, "DATA_DIR", data_dir)
    monkeypatch.setattr(shadow_evaluator, "DATA_DIR", data_dir)
    monkeypatch.setattr(reward_signal, "DATA_DIR", data_dir)
    return data_dir


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Write list of dicts as JSONL."""
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    """Read JSONL file, return list of dicts."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if l.strip()]


# ===========================================================================
# --- build_reward_entries schema tests ---
# ===========================================================================

class TestBuildRewardEntriesSchema:
    """Test reward_signal.build_reward_entries schema compliance."""

    def test_build_reward_entries_has_required_schema_fields(self):
        """Each entry has reward_source=binary_fallback, schema_version=1, cost_included=False."""
        verdict = {
            "role": "DIRECT",
            "rule_id": "DIRECT:qa_simple",
            "model": "claude-haiku-4-5",
            "ts": "2024-01-15T10:00:00Z",
        }
        entries = build_reward_entries([verdict], "sess-001", False, {})

        assert len(entries) == 1
        entry = entries[0]
        assert entry["reward_source"] == "binary_fallback"
        assert entry["schema_version"] == 1
        assert entry["cost_included"] is False

    def test_build_reward_entries_verdict_ts_handling(self):
        """verdict_ts taken from verdict ts, falling back to timestamp, None when both absent."""
        # Case 1: ts present
        verdict_with_ts = {
            "role": "FW",
            "rule_id": "FW:dispatch",
            "ts": "2024-01-15T10:00:00Z",
        }
        entries = build_reward_entries([verdict_with_ts], "sess-001", False, {})
        assert entries[0]["verdict_ts"] == "2024-01-15T10:00:00Z"

        # Case 2: timestamp fallback (ts absent)
        verdict_with_timestamp = {
            "role": "FW",
            "rule_id": "FW:dispatch",
            "timestamp": "2024-01-15T11:00:00Z",
        }
        entries = build_reward_entries([verdict_with_timestamp], "sess-001", False, {})
        assert entries[0]["verdict_ts"] == "2024-01-15T11:00:00Z"

        # Case 3: both absent -> None
        verdict_no_ts = {
            "role": "FW",
            "rule_id": "FW:dispatch",
        }
        entries = build_reward_entries([verdict_no_ts], "sess-001", False, {})
        assert entries[0]["verdict_ts"] is None

    def test_build_reward_entries_preserves_original_keys(self):
        """Original verdict keys (role, rule_id, model) still present in output."""
        verdict = {
            "role": "RB",
            "rule_id": "RB:reasoning",
            "model": "claude-sonnet-4",
            "intent": "architecture",
        }
        entries = build_reward_entries([verdict], "sess-001", False, {})

        entry = entries[0]
        assert entry["routing_role"] == "RB"
        assert entry["rule_id"] == "RB:reasoning"
        assert entry["actual_model"] == "claude-sonnet-4"


# ===========================================================================
# --- _write_reward_entries_to_disk dedup and idempotency tests ---
# ===========================================================================

class TestWriteRewardEntriesToDisk:
    """Test reward_signal._write_reward_entries_to_disk dedup and idempotency."""

    def test_write_reward_entries_to_disk_creates_file(self, isolated_data):
        """Writing entries creates reward_log.jsonl if absent."""
        entries = [
            {
                "rule_id": "DIRECT:qa",
                "session_id": "sess-001",
                "verdict_ts": "2024-01-15T10:00:00Z",
                "reward_source": "binary_fallback",
                "reward_value": 1.0,
            }
        ]
        log_path = isolated_data / "reward_log.jsonl"
        _write_reward_entries_to_disk(entries, log_path)

        assert log_path.exists()
        lines = _read_jsonl(log_path)
        assert len(lines) == 1

    def test_write_reward_entries_dedup_same_verdict_ts(self, isolated_data):
        """Writing the same entries twice leaves one copy (dedup on verdict_ts)."""
        entry = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "verdict_ts": "2024-01-15T10:00:00Z",
            "reward_source": "binary_fallback",
            "reward_value": 1.0,
        }
        log_path = isolated_data / "reward_log.jsonl"

        # First write
        _write_reward_entries_to_disk([entry], log_path)
        first_read = _read_jsonl(log_path)
        assert len(first_read) == 1

        # Second write with same entry
        _write_reward_entries_to_disk([entry], log_path)
        second_read = _read_jsonl(log_path)
        assert len(second_read) == 1

    def test_write_reward_entries_new_verdict_ts_appends(self, isolated_data):
        """A new verdict_ts appends only the new row."""
        entry1 = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "verdict_ts": "2024-01-15T10:00:00Z",
            "reward_source": "binary_fallback",
            "reward_value": 1.0,
        }
        entry2 = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "verdict_ts": "2024-01-15T11:00:00Z",  # Different timestamp
            "reward_source": "binary_fallback",
            "reward_value": 1.0,
        }
        log_path = isolated_data / "reward_log.jsonl"

        _write_reward_entries_to_disk([entry1], log_path)
        assert len(_read_jsonl(log_path)) == 1

        _write_reward_entries_to_disk([entry2], log_path)
        entries = _read_jsonl(log_path)
        assert len(entries) == 2

    def test_write_reward_entries_verdict_ts_none_always_appends(self, isolated_data):
        """Entries with verdict_ts None are appended every time."""
        entry_no_ts = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "verdict_ts": None,
            "reward_source": "binary_fallback",
            "reward_value": 1.0,
        }
        log_path = isolated_data / "reward_log.jsonl"

        # First write
        _write_reward_entries_to_disk([entry_no_ts], log_path)
        first_read = _read_jsonl(log_path)
        assert len(first_read) == 1

        # Second write with same entry (no verdict_ts)
        _write_reward_entries_to_disk([entry_no_ts], log_path)
        second_read = _read_jsonl(log_path)
        assert len(second_read) == 2

    def test_write_reward_entries_malformed_lines_dont_raise(self, isolated_data):
        """A malformed existing line does not raise and is left in the file."""
        log_path = isolated_data / "reward_log.jsonl"

        # Write a malformed line manually
        log_path.write_text("not valid json\n", encoding="utf-8")

        # Now try to append valid entries
        entry = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "verdict_ts": "2024-01-15T10:00:00Z",
            "reward_source": "binary_fallback",
            "reward_value": 1.0,
        }
        # Should not raise
        _write_reward_entries_to_disk([entry], log_path)

        # Malformed line is still there
        content = log_path.read_text(encoding="utf-8")
        assert "not valid json" in content

        # Valid entry was appended (parse tolerantly: the malformed line stays)
        valid = [json.loads(l) for l in content.splitlines() if l.startswith("{")]
        assert len(valid) == 1
        assert valid[0]["rule_id"] == "DIRECT:qa"


# ===========================================================================
# --- exclude_legacy_rewards filter tests ---
# ===========================================================================

class TestExcludeLegacyRewards:
    """Test signal_filter_rules.exclude_legacy_rewards filtering."""

    def test_exclude_legacy_empty_list_returns_empty(self):
        """Empty list returns empty list."""
        result = exclude_legacy_rewards([])
        assert result == []

    def test_exclude_legacy_all_legacy_rows_returned_unchanged(self):
        """All legacy rows (no reward_source) returned unchanged."""
        legacy_row1 = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "reward_value": 1.0,
        }
        legacy_row2 = {
            "rule_id": "FW:dispatch",
            "session_id": "sess-002",
            "reward_value": -1.0,
        }
        rows = [legacy_row1, legacy_row2]

        result = exclude_legacy_rewards(rows)
        assert len(result) == 2
        assert result == rows

    def test_exclude_legacy_mixed_returns_only_tagged(self):
        """Mixed legacy and tagged rows returns only rows with truthy reward_source."""
        legacy_row = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "reward_value": 1.0,
        }
        tagged_row = {
            "rule_id": "FW:dispatch",
            "session_id": "sess-002",
            "reward_value": -1.0,
            "reward_source": "binary_fallback",
        }
        rows = [legacy_row, tagged_row]

        result = exclude_legacy_rewards(rows)
        assert len(result) == 1
        assert result[0] == tagged_row


# ===========================================================================
# --- Reader integration tests (legacy + tagged rows) ---
# ===========================================================================

class TestReadersWithMixedLegacyTaggedData:
    """Test that readers filter legacy rows appropriately."""

    def test_learn_patterns_load_reward_log_filters_legacy(self, isolated_data):
        """learn_patterns.load_reward_log sees only tagged data."""
        legacy_row = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "reward_value": 1.0,
        }
        tagged_row = {
            "rule_id": "FW:dispatch",
            "session_id": "sess-002",
            "reward_value": -1.0,
            "reward_source": "binary_fallback",
        }

        log_path = isolated_data / "reward_log.jsonl"
        _write_jsonl(log_path, [legacy_row, tagged_row])

        entries = learn_patterns.load_reward_log(isolated_data)
        assert len(entries) == 1
        assert entries[0]["rule_id"] == "FW:dispatch"

    def test_join_outcomes_load_reward_log_filters_legacy(self, isolated_data):
        """join_outcomes.load_reward_log sees only tagged data."""
        legacy_row = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "reward_value": 1.0,
        }
        tagged_row = {
            "rule_id": "FW:dispatch",
            "session_id": "sess-002",
            "reward_value": -1.0,
            "reward_source": "binary_fallback",
        }

        log_path = isolated_data / "reward_log.jsonl"
        _write_jsonl(log_path, [legacy_row, tagged_row])

        aggregates = join_outcomes.load_reward_log(str(isolated_data))
        # Should only have sess-002 (the tagged one)
        assert "sess-002" in aggregates
        assert "sess-001" not in aggregates

    def test_shadow_evaluator_load_filtered_rewards_drops_legacy(self, isolated_data, monkeypatch):
        """shadow_evaluator.load_filtered_rewards drops legacy rows when tagged rows exist."""
        legacy_row = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "reward_value": 1.0,
            "base_reward_value": 1.0,
        }
        tagged_row = {
            "rule_id": "FW:dispatch",
            "session_id": "sess-002",
            "reward_value": -1.0,
            "reward_source": "binary_fallback",
            "base_reward_value": -1.0,
        }

        log_path = isolated_data / "reward_log.jsonl"
        _write_jsonl(log_path, [legacy_row, tagged_row])

        # One-row sessions would be dropped by filter_short_sessions, so bypass apply_all_filters
        monkeypatch.setattr(shadow_evaluator, "apply_all_filters", lambda e: (e, {}))

        rewards = shadow_evaluator.load_filtered_rewards()
        assert len(rewards) == 1
        assert rewards[0]["rule_id"] == "FW:dispatch"

    def test_shadow_evaluator_load_filtered_rewards_only_legacy_returns_unchanged(self, isolated_data, monkeypatch):
        """shadow_evaluator.load_filtered_rewards returns legacy rows unchanged when no tagged exist."""
        legacy_row1 = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "reward_value": 1.0,
            "base_reward_value": 1.0,
        }
        legacy_row2 = {
            "rule_id": "FW:dispatch",
            "session_id": "sess-002",
            "reward_value": -1.0,
            "base_reward_value": -1.0,
        }

        log_path = isolated_data / "reward_log.jsonl"
        _write_jsonl(log_path, [legacy_row1, legacy_row2])

        # One-row sessions would be dropped by filter_short_sessions, so bypass apply_all_filters
        monkeypatch.setattr(shadow_evaluator, "apply_all_filters", lambda e: (e, {}))

        rewards = shadow_evaluator.load_filtered_rewards()
        assert len(rewards) == 2
        assert {r["rule_id"] for r in rewards} == {"DIRECT:qa", "FW:dispatch"}

    def test_shadow_evaluator_real_filters_keep_tagged_session(self, isolated_data):
        """Real apply_all_filters keeps sessions with 3+ rows; sess-002 survives with 3 tagged rows."""
        legacy_row = {
            "rule_id": "DIRECT:qa",
            "session_id": "sess-001",
            "reward_value": 1.0,
            "base_reward_value": 1.0,
        }
        # Three tagged rows for sess-002, >60s apart so retry-storm filter keeps all three
        tagged_row1 = {
            "rule_id": "FW:dispatch",
            "session_id": "sess-002",
            "reward_source": "binary_fallback",
            "routing_role": "FW",
            "reward_value": 1.0,
            "base_reward_value": 1.0,
            "timestamp": "2024-01-15T10:00:00Z",
        }
        tagged_row2 = {
            "rule_id": "FW:dispatch",
            "session_id": "sess-002",
            "reward_source": "binary_fallback",
            "routing_role": "FW",
            "reward_value": 1.0,
            "base_reward_value": 1.0,
            "timestamp": "2024-01-15T10:05:00Z",
        }
        tagged_row3 = {
            "rule_id": "FW:dispatch",
            "session_id": "sess-002",
            "reward_source": "binary_fallback",
            "routing_role": "FW",
            "reward_value": 1.0,
            "base_reward_value": 1.0,
            "timestamp": "2024-01-15T10:10:00Z",
        }

        log_path = isolated_data / "reward_log.jsonl"
        _write_jsonl(log_path, [legacy_row, tagged_row1, tagged_row2, tagged_row3])

        # Real filters: sess-001 (1 row) is dropped, sess-002 (3 rows) survives
        rewards = shadow_evaluator.load_filtered_rewards()
        assert len(rewards) == 3
        assert all(r["session_id"] == "sess-002" for r in rewards)
