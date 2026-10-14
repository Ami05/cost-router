"""Tests for reward_signal.py computed_reward and reward_source logic (T3).

Covers:
  (1) computed_reward given, no under_route: base equals computed, reward_source computed, cost_included True
  (2) computed above REWARD_NO_ESCALATION env is capped via min
  (3) under_route overrides computed: base -1.0, source under_route, cost_included False
  (4) computed_reward None: base 1.0, source binary_fallback, cost_included False
  (5) negative base with quality multiplier below 1 yields MORE negative reward_value;
      positive base yields smaller
  (6) explicit reward_source argument overrides the derived source
  (7) main(): monkeypatch session_hook.summarize_session to test exception and reward flow
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import _common
import _config
import _events
import _fileio
import reward_signal
import session_hook
from reward_signal import (
    build_reward_entries,
    extract_quality_multiplier,
    main,
)

_REAL_DATA_DIR = _config.DATA_DIR


# Only files this suite can write. Live hooks (semantic_*.jsonl, dispatch_*,
# gate_*) append to the same dir while pytest runs and would trip the guard.
_GUARDED_PREFIXES = ("reward_log", "routing_events", "session_log", "warnings")


def _snapshot_real_dir() -> dict:
    """Map file name -> (mtime_ns, size) for guarded files in the real data dir (empty if absent)."""
    if not _REAL_DATA_DIR.exists():
        return {}
    return {
        p.name: (p.stat().st_mtime_ns, p.stat().st_size)
        for p in _REAL_DATA_DIR.iterdir()
        if p.is_file() and p.name.startswith(_GUARDED_PREFIXES)
    }


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Isolate all reward signal I/O to tmp_path.

    DATA_DIR is imported by name into several modules, so each binding must be
    patched; read_events (routing_events.jsonl etc.) uses _events.DATA_DIR,
    append_event uses _fileio.DATA_DIR, reward_signal writes via its own binding.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for mod in (_common, _config, _events, _fileio, reward_signal):
        monkeypatch.setattr(mod, "DATA_DIR", data_dir)
    for mod in (_config, _events, _fileio, _common):
        if hasattr(mod, "USAGE_LOG"):
            monkeypatch.setattr(mod, "USAGE_LOG", data_dir / "routing_events.jsonl")
    monkeypatch.setattr(_config, "WARNINGS_LOG", data_dir / "warnings.jsonl")
    monkeypatch.setattr(_fileio, "SESSION_LOG", data_dir / "session_log.jsonl")
    before = _snapshot_real_dir()
    yield data_dir
    if os.environ.get("PYTEST_XDIST_WORKER"):
        # Other workers' tests share the real data dir; the diff can't be attributed to this test.
        return
    assert _snapshot_real_dir() == before, "test wrote into the real data dir"


@pytest.fixture
def mock_stdin(monkeypatch):
    """Return a function that mocks stdin with a JSON payload."""
    def _set_stdin(payload: dict):
        import io
        json_str = json.dumps(payload)
        monkeypatch.setattr("sys.stdin", io.StringIO(json_str))
    return _set_stdin


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Write records as JSONL."""
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    """Read JSONL records."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if l.strip()]


# ============================================================================
# Test Suite
# ============================================================================


class TestBuildRewardEntriesComputedReward:
    """Tests for build_reward_entries with computed_reward parameter."""

    def test_computed_reward_given_no_under_route(self):
        """(1) computed_reward given, no under_route.

        - base equals computed (0.8)
        - reward_source = "computed"
        - cost_included = True
        """
        verdicts = [
            {
                "rule_id": "FW:dispatch",
                "role": "FW",
                "model": "haiku",
                "intent": "mechanical_edit",
                "ts": "2026-10-04T10:00:00Z",
            }
        ]
        quality_data = {}

        entries = build_reward_entries(
            verdicts=verdicts,
            session_id="sess-test-01",
            has_under_route=False,
            quality_data=quality_data,
            computed_reward=0.8,
        )

        assert len(entries) == 1
        entry = entries[0]
        assert entry["base_reward_value"] == 0.8
        assert entry["reward_source"] == "computed"
        assert entry["cost_included"] is True

    def test_computed_reward_capped_at_env(self):
        """(2) computed above REWARD_NO_ESCALATION env is capped via min.

        computed_reward=3.0, REWARD_NO_ESCALATION=1.5 → base=1.5
        """
        verdicts = [
            {
                "rule_id": "FW:dispatch",
                "role": "FW",
                "model": "haiku",
                "intent": "mechanical_edit",
                "ts": "2026-10-04T10:00:00Z",
            }
        ]
        quality_data = {}

        with patch.dict(os.environ, {"REWARD_NO_ESCALATION": "1.5"}):
            entries = build_reward_entries(
                verdicts=verdicts,
                session_id="sess-test-02",
                has_under_route=False,
                quality_data=quality_data,
                computed_reward=3.0,
            )

        assert len(entries) == 1
        entry = entries[0]
        assert entry["base_reward_value"] == 1.5
        assert entry["reward_source"] == "computed"
        assert entry["cost_included"] is True

    def test_under_route_overrides_computed(self):
        """(3) under_route overrides computed.

        - base = -1.0 (REWARD_UNDER_ROUTE default)
        - source = "under_route"
        - cost_included = False
        - computed_reward arg is ignored
        """
        verdicts = [
            {
                "rule_id": "FW:dispatch",
                "role": "FW",
                "model": "haiku",
                "intent": "mechanical_edit",
                "ts": "2026-10-04T10:00:00Z",
            }
        ]
        quality_data = {}

        entries = build_reward_entries(
            verdicts=verdicts,
            session_id="sess-test-03",
            has_under_route=True,
            quality_data=quality_data,
            computed_reward=0.8,
        )

        assert len(entries) == 1
        entry = entries[0]
        assert entry["base_reward_value"] == -1.0
        assert entry["reward_source"] == "under_route"
        assert entry["cost_included"] is False

    def test_computed_reward_none_fallback_to_binary(self):
        """(4) computed_reward None: base = 1.0, source = binary_fallback.

        When computed_reward is None and no under_route, use binary_fallback.
        """
        verdicts = [
            {
                "rule_id": "FW:dispatch",
                "role": "FW",
                "model": "haiku",
                "intent": "mechanical_edit",
                "ts": "2026-10-04T10:00:00Z",
            }
        ]
        quality_data = {}

        entries = build_reward_entries(
            verdicts=verdicts,
            session_id="sess-test-04",
            has_under_route=False,
            quality_data=quality_data,
            computed_reward=None,
        )

        assert len(entries) == 1
        entry = entries[0]
        assert entry["base_reward_value"] == 1.0
        assert entry["reward_source"] == "binary_fallback"
        assert entry["cost_included"] is False

    def test_negative_base_quality_multiplier_makes_worse(self):
        """(5) negative base with quality multiplier < 1 yields MORE negative.

        With negative base, eff_mult = 2.0 - qmult.
        If qmult = 0.7 (high revert), eff_mult = 1.3, so -1.0 * 1.3 = -1.3 (worse).

        With positive base, eff_mult = qmult.
        If qmult = 0.7, then 0.8 * 0.7 = 0.56 (smaller, better).
        """
        # Case A: negative base with low quality_multiplier
        verdicts_neg = [
            {
                "rule_id": "FW:dispatch",
                "role": "FW",
                "model": "haiku",
                "intent": "failing_dispatch",
                "ts": "2026-10-04T10:00:00Z",
            }
        ]
        quality_data = {
            "failing_dispatch": {"revert_rate": 0.3}  # qmult = 0.7
        }

        entries_neg = build_reward_entries(
            verdicts=verdicts_neg,
            session_id="sess-test-05a",
            has_under_route=True,
            quality_data=quality_data,
            computed_reward=None,
        )

        entry_neg = entries_neg[0]
        # base = -1.0, qmult = 0.7, eff_mult = 2.0 - 0.7 = 1.3
        # reward_value = -1.0 * 1.3 = -1.3 (more negative)
        assert entry_neg["quality_multiplier"] == 0.7
        assert entry_neg["base_reward_value"] == -1.0
        assert entry_neg["reward_value"] == -1.3

        # Case B: positive base with same quality multiplier
        verdicts_pos = [
            {
                "rule_id": "FW:dispatch",
                "role": "FW",
                "model": "haiku",
                "intent": "failing_dispatch",
                "ts": "2026-10-04T10:00:00Z",
            }
        ]

        entries_pos = build_reward_entries(
            verdicts=verdicts_pos,
            session_id="sess-test-05b",
            has_under_route=False,
            quality_data=quality_data,
            computed_reward=0.8,
        )

        entry_pos = entries_pos[0]
        # base = 0.8, qmult = 0.7, eff_mult = 0.7
        # reward_value = 0.8 * 0.7 = 0.56 (smaller, better)
        assert entry_pos["quality_multiplier"] == 0.7
        assert entry_pos["base_reward_value"] == 0.8
        assert entry_pos["reward_value"] == 0.56

    def test_explicit_reward_source_overrides_derived(self):
        """(6) explicit reward_source argument overrides derived source.

        Even if computed_reward is given (derives "computed"), passing
        reward_source="custom" should use "custom".
        """
        verdicts = [
            {
                "rule_id": "FW:dispatch",
                "role": "FW",
                "model": "haiku",
                "intent": "mechanical_edit",
                "ts": "2026-10-04T10:00:00Z",
            }
        ]
        quality_data = {}

        entries = build_reward_entries(
            verdicts=verdicts,
            session_id="sess-test-06",
            has_under_route=False,
            quality_data=quality_data,
            computed_reward=0.8,
            reward_source="custom_override",
        )

        assert len(entries) == 1
        entry = entries[0]
        assert entry["reward_source"] == "custom_override"


class TestMainRewardSignal:
    """Tests for main() function with session_hook integration."""

    def test_main_session_hook_raises_uses_binary_fallback(
        self, isolated_data, mock_stdin, monkeypatch
    ):
        """(7a) main(): session_hook.summarize_session raises.

        When summarize_session raises, computed_reward=None, so written entry has:
        - reward_source = "binary_fallback"
        - cost_included = False
        """
        data_dir = isolated_data

        verdict_event = {
            "type": "routing_declared",
            "session_id": "sess-hook-error",
            "role": "DIRECT",
            "model": "opus",
            "intent": "architecture",
            "rule_id": "DIRECT:architecture",
            "ts": "2026-10-04T10:00:00Z",
        }
        _write_jsonl(data_dir / "routing_events.jsonl", [verdict_event])

        mock_stdin({"session_id": "sess-hook-error", "cwd": "/test"})

        def _boom(*args, **kwargs):
            raise Exception("hook_not_found")

        monkeypatch.setattr(session_hook, "summarize_session", _boom)

        result = main()

        assert result == 0

        entries = _read_jsonl(data_dir / "reward_log.jsonl")
        assert len(entries) >= 1
        fallback = [e for e in entries if e.get("reward_source") == "binary_fallback"]
        assert len(fallback) >= 1
        assert fallback[0]["cost_included"] is False

    def test_main_session_hook_success_uses_computed(
        self, isolated_data, mock_stdin, monkeypatch
    ):
        """(7b) main(): session_hook.summarize_session returns reward.

        When summarize_session returns {"reward": 0.4}, computed_reward=0.4,
        so written entry has reward_source="computed".
        """
        data_dir = isolated_data

        # Setup: create events with a routing_declared verdict
        verdict_event = {
            "type": "routing_declared",
            "session_id": "sess-hook-success",
            "role": "DIRECT",
            "model": "opus",
            "intent": "architecture",
            "rule_id": "DIRECT:architecture",
            "ts": "2026-10-04T10:00:00Z",
        }
        events_file = data_dir / "routing_events.jsonl"
        _write_jsonl(events_file, [verdict_event])

        # Setup: mock stdin
        payload = {"session_id": "sess-hook-success", "cwd": "/test"}
        mock_stdin(payload)

        # Setup: mock session_hook.summarize_session to return reward
        monkeypatch.setattr(
            session_hook,
            "summarize_session",
            lambda *a, **k: {"reward": 0.4, "other": "data"},
        )

        result = main()

        assert result == 0

        # Verify reward_log entry has computed source
        reward_log = data_dir / "reward_log.jsonl"
        entries = _read_jsonl(reward_log)

        assert len(entries) >= 1
        # Find the entry for our verdict (may have filtered noise, so check all)
        computed_entries = [e for e in entries if e.get("reward_source") == "computed"]
        assert len(computed_entries) >= 1
        entry = computed_entries[0]
        assert entry["base_reward_value"] == 0.4
        assert entry["reward_source"] == "computed"
        assert entry["cost_included"] is True


class TestQualityMultiplierHelper:
    """Tests for extract_quality_multiplier function."""

    def test_extract_quality_multiplier_with_revert_rate(self):
        """Quality multiplier = max(0.1, 1.0 - revert_rate)."""
        quality_data = {
            "intent_a": {"revert_rate": 0.2},
            "intent_b": {"revert_rate": 0.95},  # max(0.1, 0.05) = 0.1
        }

        qmult_a, revert_a = extract_quality_multiplier("intent_a", quality_data)
        assert qmult_a == 0.8
        assert revert_a == 0.2

        qmult_b, revert_b = extract_quality_multiplier("intent_b", quality_data)
        assert qmult_b == 0.1
        assert revert_b == 0.95

    def test_extract_quality_multiplier_missing_intent(self):
        """Missing intent defaults to (1.0, 0.0)."""
        quality_data = {}
        qmult, revert = extract_quality_multiplier("unknown_intent", quality_data)
        assert qmult == 1.0
        assert revert == 0.0

    def test_extract_quality_multiplier_none_intent(self):
        """None intent defaults to (1.0, 0.0)."""
        quality_data = {"intent_a": {"revert_rate": 0.5}}
        qmult, revert = extract_quality_multiplier(None, quality_data)
        assert qmult == 1.0
        assert revert == 0.0


class TestRewardLogSidecarLock:
    """Tests for deterministic fcntl.flock behavior on reward_log sidecar lock."""

    def test_filter_and_rewrite_blocks_while_lock_held(self, isolated_data):
        """(1) _filter_and_rewrite_reward_log blocks until sidecar lock is released.

        Acquire the lock manually, start the filter in a daemon thread, assert it's
        still running after join(timeout=0.3), release the lock, then join with
        longer timeout and assert it finishes.
        """
        import threading

        data_dir = isolated_data
        reward_log = data_dir / "reward_log.jsonl"

        # Create a log with 3+ rows with distinct timestamps
        entries = [
            {
                "session_id": "sess-lock-01",
                "rule_id": "FW:dispatch",
                "reward_source": "computed",
                "verdict_ts": "2026-10-04T10:00:00Z",
                "reward_value": 0.5,
                "timestamp": "2026-10-04T10:00:01Z",
                "schema_version": 1,
            },
            {
                "session_id": "sess-lock-02",
                "rule_id": "DIRECT:arch",
                "reward_source": "binary_fallback",
                "verdict_ts": "2026-10-04T10:00:02Z",
                "reward_value": 1.0,
                "timestamp": "2026-10-04T10:00:03Z",
                "schema_version": 1,
            },
            {
                "session_id": "sess-lock-03",
                "rule_id": "FO:decompose",
                "reward_source": "computed",
                "verdict_ts": "2026-10-04T10:00:04Z",
                "reward_value": 0.8,
                "timestamp": "2026-10-04T10:00:05Z",
                "schema_version": 1,
            },
        ]
        _write_jsonl(reward_log, entries)

        # Acquire the lock and hold it
        lock_path = reward_log.with_name(reward_log.name + ".lock")
        lock_file = lock_path.open("a", encoding="utf-8")
        import fcntl
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        # Start filter in daemon thread
        filter_thread = threading.Thread(
            target=reward_signal._filter_and_rewrite_reward_log,
            args=(reward_log,),
            daemon=True,
        )
        filter_thread.start()

        # Assert thread is still alive after short timeout
        filter_thread.join(timeout=0.3)
        assert filter_thread.is_alive(), "filter should be blocked by lock"

        # Release lock
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

        # Wait for thread to finish
        filter_thread.join(timeout=5.0)
        assert not filter_thread.is_alive(), "filter should have finished after lock release"

    def test_write_entries_blocks_while_lock_held(self, isolated_data):
        """(2) _write_reward_entries_to_disk blocks until sidecar lock is released.

        Acquire lock, start write in a daemon thread with one entry, assert it's
        still running after join(timeout=0.3), release lock, join with longer
        timeout and verify thread finished and entry is present exactly once.
        """
        import threading

        data_dir = isolated_data
        reward_log = data_dir / "reward_log.jsonl"
        reward_log.touch()

        # Entry to write
        entry = {
            "session_id": "sess-write-lock",
            "rule_id": "FW:dispatch",
            "reward_source": "computed",
            "verdict_ts": "2026-10-04T11:00:00Z",
            "reward_value": 0.6,
            "timestamp": "2026-10-04T11:00:01Z",
            "schema_version": 1,
        }

        # Acquire the lock and hold it
        lock_path = reward_log.with_name(reward_log.name + ".lock")
        lock_file = lock_path.open("a", encoding="utf-8")
        import fcntl
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        # Start write in daemon thread
        write_thread = threading.Thread(
            target=reward_signal._write_reward_entries_to_disk,
            args=([entry], reward_log),
            daemon=True,
        )
        write_thread.start()

        # Assert thread is still alive after short timeout
        write_thread.join(timeout=0.3)
        assert write_thread.is_alive(), "write should be blocked by lock"

        # Release lock
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

        # Wait for thread to finish
        write_thread.join(timeout=5.0)
        assert not write_thread.is_alive(), "write should have finished after lock release"

        # Verify entry is present exactly once
        entries = _read_jsonl(reward_log)
        matching = [
            e
            for e in entries
            if e.get("session_id") == "sess-write-lock"
            and e.get("rule_id") == "FW:dispatch"
        ]
        assert len(matching) == 1, f"expected 1 matching entry, got {len(matching)}"
        assert matching[0]["reward_value"] == 0.6

    def test_write_lands_in_filtered_log(self, isolated_data):
        """(3) After filter rewrites log, subsequent write lands in the replaced file.

        Create log with entries, call filter, verify file is replaced atomically,
        then call write with a new entry and verify it lands in the filtered log
        and read back all entries.
        """
        data_dir = isolated_data
        reward_log = data_dir / "reward_log.jsonl"

        # Create initial log with entries
        initial_entries = [
            {
                "session_id": "sess-filter-01",
                "rule_id": "FW:dispatch",
                "reward_source": "computed",
                "verdict_ts": "2026-10-04T12:00:00Z",
                "reward_value": 0.7,
                "timestamp": "2026-10-04T12:00:01Z",
                "schema_version": 1,
            },
            {
                "session_id": "sess-filter-02",
                "rule_id": "DIRECT:arch",
                "reward_source": "binary_fallback",
                "verdict_ts": "2026-10-04T12:00:02Z",
                "reward_value": 1.0,
                "timestamp": "2026-10-04T12:00:03Z",
                "schema_version": 1,
            },
        ]
        _write_jsonl(reward_log, initial_entries)

        # Run filter (may reduce log, but should keep at least one entry)
        reward_signal._filter_and_rewrite_reward_log(reward_log)

        # Log file should still exist after filter
        assert reward_log.exists(), "reward_log should exist after filter"

        # Read back to verify filtering happened (or was skipped)
        entries_after_filter = _read_jsonl(reward_log)
        assert len(entries_after_filter) > 0, "log should have entries after filter"

        # Now write a new entry to the filtered log
        new_entry = {
            "session_id": "sess-filter-new",
            "rule_id": "FO:decompose",
            "reward_source": "computed",
            "verdict_ts": "2026-10-04T12:00:10Z",
            "reward_value": 0.9,
            "timestamp": "2026-10-04T12:00:11Z",
            "schema_version": 1,
        }
        reward_signal._write_reward_entries_to_disk([new_entry], reward_log)

        # Read final log and verify new entry is present
        final_entries = _read_jsonl(reward_log)
        new_entry_matches = [
            e
            for e in final_entries
            if e.get("session_id") == "sess-filter-new"
            and e.get("rule_id") == "FO:decompose"
        ]
        assert len(new_entry_matches) == 1, "new entry should be present exactly once"
        assert new_entry_matches[0]["reward_value"] == 0.9

        # Verify at least one original entry survived (or all were filtered, but at least we have the new one)
        assert len(final_entries) >= 1, "final log should have entries"

    def test_append_blocked_during_replace_is_not_lost(self, isolated_data):
        """(4) An append blocked on the lock lands in the replaced file, not an orphaned inode.

        Hold the sidecar lock (a rewrite in progress), start a writer that blocks, swap the
        log via os.replace while still holding the lock, release, and check the blocked
        append is present in the file now at the log path.
        """
        import fcntl
        import threading

        data_dir = isolated_data
        reward_log = data_dir / "reward_log.jsonl"

        def _row(session_id: str, ts: str) -> dict:
            return {
                "session_id": session_id,
                "rule_id": "FW:dispatch",
                "reward_source": "computed",
                "verdict_ts": ts,
                "reward_value": 0.5,
                "timestamp": ts,
                "schema_version": 1,
            }

        _write_jsonl(reward_log, [_row("sess-race-old", "2026-10-04T13:00:00Z")])

        lock_file = reward_log.with_name(reward_log.name + ".lock").open("a", encoding="utf-8")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            writer = threading.Thread(
                target=reward_signal._write_reward_entries_to_disk,
                args=([_row("sess-race-new", "2026-10-04T13:00:10Z")], reward_log),
                daemon=True,
            )
            writer.start()
            writer.join(timeout=0.3)
            assert writer.is_alive(), "writer should be blocked by the sidecar lock"

            # Swap the inode under the blocked writer, as the rewrite does.
            replacement = reward_log.with_name("reward_log.jsonl.replacement")
            _write_jsonl(replacement, [_row("sess-race-replaced", "2026-10-04T13:00:05Z")])
            os.replace(replacement, reward_log)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

        writer.join(timeout=5.0)
        assert not writer.is_alive(), "writer should finish after lock release"

        sessions = [e.get("session_id") for e in _read_jsonl(reward_log)]
        assert sessions.count("sess-race-replaced") == 1
        assert sessions.count("sess-race-new") == 1
        assert "sess-race-old" not in sessions
