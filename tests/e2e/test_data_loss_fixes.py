"""Tests for data-integrity bug fixes across reward_signal, compact_log, and policy_manager.

Bug 1 — reward_signal.py: Atomic write for reward_log.jsonl filter+rewrite
Bug 2 — compact_log.py:   Rotation instead of deletion for usage.jsonl
Bug 3 — policy_manager.py: Atomic writes for proposals/changelog/rule file
Bug 5 — policy_manager.py: Counterfactual log rotation with fcntl lock
Bug 6 — compact_log.py:   checkpoint_state.json uses save_json_state
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            result.append(json.loads(line))
    return result


# ---------------------------------------------------------------------------
# Bug 1 — reward_signal.py: Atomic write for reward_log.jsonl filter+rewrite
# ---------------------------------------------------------------------------

class TestRewardSignalAtomicWrite:
    """Verifies the filter+rewrite path in reward_signal.py is atomic."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        import _common
        monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
        import reward_signal
        monkeypatch.setattr(reward_signal, "DATA_DIR", tmp_path)
        self.tmp_path = tmp_path
        self.reward_log = tmp_path / "reward_log.jsonl"
        yield

    def _run_filter(self, entries: list[dict], filter_return: tuple):
        """Write entries to reward_log, run the filter+rewrite block from main()."""
        _write_jsonl(self.reward_log, entries)
        import reward_signal
        with patch("reward_signal.apply_all_filters", return_value=filter_return), \
             patch("reward_signal.read_stdin_json", return_value={"session_id": "s1"}), \
             patch("reward_signal.read_events", return_value=[]), \
             patch("reward_signal._write_reward_entries_to_disk"):
            reward_signal.main()

    def test_normal_filter_rewrites_file_and_no_tmp_left(self):
        """After a normal filter run the file has filtered content and no .tmp files."""
        entries = [
            {"rule_id": "R1", "reward_value": 1.0},
            {"rule_id": "R2", "reward_value": -1.0},
        ]
        clean = [entries[0]]  # filter keeps only R1
        self._run_filter(entries, (clean, {}))

        result = _read_jsonl(self.reward_log)
        assert len(result) == 1
        assert result[0]["rule_id"] == "R1"

        # No .tmp files should linger
        tmp_files = list(self.tmp_path.glob("reward_log.jsonl.*.tmp"))
        assert tmp_files == [], f"Stale tmp files found: {tmp_files}"

    def test_empty_filter_result_preserves_original_file(self):
        """When apply_all_filters returns an empty list the original file is untouched."""
        original_entries = [
            {"rule_id": "R1", "reward_value": 1.0},
            {"rule_id": "R2", "reward_value": 0.5},
        ]
        _write_jsonl(self.reward_log, original_entries)
        original_text = self.reward_log.read_text(encoding="utf-8")

        self._run_filter(original_entries, ([], {}))

        assert self.reward_log.exists(), "reward_log.jsonl was deleted"
        assert self.reward_log.read_text(encoding="utf-8") == original_text, \
            "File was modified when filter returned empty list"

    def test_crash_on_os_replace_preserves_original_and_cleans_tmp(self):
        """If os.replace raises the original file is intact and the tmp is cleaned up."""
        original_entries = [{"rule_id": "R1", "reward_value": 1.0}]
        _write_jsonl(self.reward_log, original_entries)
        original_text = self.reward_log.read_text(encoding="utf-8")

        clean = [{"rule_id": "R1", "reward_value": 1.0}]

        with patch("reward_signal.apply_all_filters", return_value=(clean, {})), \
             patch("reward_signal.read_stdin_json", return_value={"session_id": "s1"}), \
             patch("reward_signal.read_events", return_value=[]), \
             patch("reward_signal._write_reward_entries_to_disk"), \
             patch("os.replace", side_effect=OSError("disk full")):
            import reward_signal
            reward_signal.main()

        # Original file must still be intact
        assert self.reward_log.exists(), "reward_log.jsonl was deleted after os.replace failure"
        assert self.reward_log.read_text(encoding="utf-8") == original_text, \
            "Original file was corrupted after os.replace failure"

        # The tmp file should have been cleaned up by the finally block
        tmp_files = list(self.tmp_path.glob("reward_log.jsonl.*.tmp"))
        assert tmp_files == [], f"Tmp file not cleaned up: {tmp_files}"


# ---------------------------------------------------------------------------
# Bug 2 — compact_log.py: Rotation instead of deletion for usage.jsonl
# ---------------------------------------------------------------------------

class TestUsageJsonlRotation:
    """Verifies usage.jsonl is rotated, not deleted, when it grows large."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        import _common
        import _fileio
        monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
        monkeypatch.setattr(_common, "USAGE_LOG", tmp_path / "routing_events.jsonl")
        monkeypatch.setattr(_fileio, "USAGE_LOG", tmp_path / "routing_events.jsonl")
        self.tmp_path = tmp_path
        self.usage_log = tmp_path / "routing_events.jsonl"
        self.rotated = tmp_path / "routing_events.jsonl.1"
        yield

    def test_compact_session_does_not_delete_usage_jsonl(self, monkeypatch):
        """compact_session must never unlink usage.jsonl."""
        import _common
        import log_compactor

        # Write a non-empty usage.jsonl
        entry = {"session_id": "s1", "type": "tool_call", "ts": "2026-09-04T00:00:00+00:00"}
        _write_jsonl(self.usage_log, [entry])
        assert self.usage_log.exists()

        # Patch read_events to return our entry, patch everything that would
        # try to actually compact/save libraries.
        monkeypatch.setattr(log_compactor, "read_events", lambda: [entry])
        monkeypatch.setattr(log_compactor, "save_library", lambda lib: None)
        monkeypatch.setattr(log_compactor, "load_library", lambda: {})

        # Run compact — should not delete the file
        try:
            log_compactor.compact_session("s1")
        except Exception:
            pass  # We only care about file existence, not about full success

        # Either the original exists or the rotated copy does — neither should
        # have been deleted outright.
        assert self.usage_log.exists() or self.rotated.exists(), \
            "usage.jsonl was deleted (neither original nor rotated file exists)"

    def test_rotate_log_if_needed_renames_at_threshold(self, monkeypatch):
        """_rotate_log_if_needed renames usage.jsonl to usage.jsonl.1 at 2MB."""
        import _common
        import _fileio

        # Write a file just over USAGE_LOG_COMPACT_TRIGGER_BYTES (2 MB)
        threshold = _common.USAGE_LOG_COMPACT_TRIGGER_BYTES
        big_content = b"x" * (threshold + 1)
        self.usage_log.write_bytes(big_content)

        _fileio._rotate_log_if_needed()

        assert not self.usage_log.exists(), "usage.jsonl should have been rotated away"
        assert self.rotated.exists(), "usage.jsonl.1 should exist after rotation"

    def test_rotated_file_content_matches_original(self, monkeypatch):
        """After rotation, usage.jsonl.1 contains all original bytes."""
        import _common
        import _fileio

        threshold = _common.USAGE_LOG_COMPACT_TRIGGER_BYTES
        # Write entries as the content, big enough to trigger rotation
        entries = [{"id": i, "data": "x" * 100} for i in range(200)]
        content = "\n".join(json.dumps(e) for e in entries) + "\n"
        # Pad to exceed threshold
        padding = "P" * (threshold + 1 - len(content.encode()))
        full_content = content + padding
        self.usage_log.write_text(full_content, encoding="utf-8")

        _fileio._rotate_log_if_needed()

        assert self.rotated.exists()
        rotated_content = self.rotated.read_text(encoding="utf-8")
        assert rotated_content == full_content, "Rotated file content does not match original"

    def test_rotate_log_does_not_rotate_below_threshold(self, monkeypatch):
        """Files below the threshold are left untouched."""
        import _common
        import _fileio

        threshold = _common.USAGE_LOG_COMPACT_TRIGGER_BYTES
        self.usage_log.write_bytes(b"x" * (threshold - 1))

        _fileio._rotate_log_if_needed()

        assert self.usage_log.exists(), "usage.jsonl was incorrectly rotated below threshold"
        assert not self.rotated.exists(), "usage.jsonl.1 should not exist"


# ---------------------------------------------------------------------------
# Bug 3 — policy_manager.py: Atomic writes for proposals/changelog/rule file
# ---------------------------------------------------------------------------

class TestPolicyManagerAtomicWrites:
    """Verifies proposals, changelog, and RULE_FILE are written atomically."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        import _common
        import policy_manager
        import policy_store

        monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
        monkeypatch.setattr(policy_manager, "DATA_DIR", tmp_path)
        monkeypatch.setattr(policy_manager, "PROPOSALS_FILE", tmp_path / "policy_proposals.json")
        monkeypatch.setattr(policy_manager, "CHANGELOG_FILE", tmp_path / "policy_changelog.jsonl")
        monkeypatch.setattr(policy_store, "DATA_DIR", tmp_path)
        monkeypatch.setattr(policy_store, "PROPOSALS_FILE", tmp_path / "policy_proposals.json")
        monkeypatch.setattr(policy_store, "CHANGELOG_FILE", tmp_path / "policy_changelog.jsonl")
        # Create a fake rules dir + file
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_file = rules_dir / "model-router.md"
        rule_file.write_text("# initial rules\n", encoding="utf-8")
        monkeypatch.setattr(policy_manager, "RULE_FILE", rule_file)
        monkeypatch.setattr(policy_store, "RULE_FILE", rule_file)

        self.tmp_path = tmp_path
        self.proposals_file = tmp_path / "policy_proposals.json"
        self.changelog_file = tmp_path / "policy_changelog.jsonl"
        self.rule_file = rule_file
        yield

    # --- _save_proposals round-trip ---

    def test_save_proposals_round_trip(self):
        """Save then load proposals; verifies both old (list) and new (dict) formats."""
        import policy_manager

        proposals = [
            {"key": "Write:fast:clean", "evidence_count": 3, "status": "accumulating"},
            {"key": "Read:reasoning:escalated", "evidence_count": 6, "status": "queued"},
        ]
        policy_manager._save_proposals(proposals)

        loaded = policy_manager._load_proposals()
        assert loaded == proposals, f"Round-trip mismatch: {loaded}"

    def test_save_proposals_handles_dict_format_on_load(self):
        """_load_proposals handles the new dict-wrapped format produced by save_json_state."""
        import policy_manager

        # Write the new dict format manually
        self.proposals_file.write_text(
            json.dumps({"proposals": [{"key": "k1", "status": "accumulating"}]}),
            encoding="utf-8",
        )
        loaded = policy_manager._load_proposals()
        assert len(loaded) == 1
        assert loaded[0]["key"] == "k1"

    def test_save_proposals_no_tmp_files_left(self):
        """After _save_proposals, no .tmp files linger."""
        import policy_manager

        policy_manager._save_proposals([{"key": "k1", "status": "accumulating"}])
        tmp_files = list(self.tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Stale .tmp files: {tmp_files}"

    # --- _save_changelog atomic write ---

    def test_save_changelog_produces_valid_jsonl(self):
        """_save_changelog writes valid JSONL that can be fully re-parsed."""
        import policy_manager

        entries = [
            {"undo_token": "abc123", "key": "k1", "status": "applied", "applied_at": "2026-09-04T00:00:00+00:00"},
            {"undo_token": "def456", "key": "k2", "status": "rolled_back"},
        ]
        policy_manager._save_changelog(entries)

        assert self.changelog_file.exists()
        loaded = _read_jsonl(self.changelog_file)
        assert loaded == entries, f"Changelog JSONL mismatch: {loaded}"

    def test_save_changelog_no_tmp_files_left(self):
        """After _save_changelog no .tmp files linger."""
        import policy_manager

        policy_manager._save_changelog([{"undo_token": "t1", "key": "k1"}])
        tmp_files = list(self.tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Stale .tmp files: {tmp_files}"

    # --- apply_proposal writes RULE_FILE atomically ---

    def test_apply_proposal_no_tmp_files_left(self):
        """After apply_proposal completes, no .tmp files linger in the rules directory."""
        import policy_manager

        proposal = {
            "key": "Write:fast:clean",
            "proposed_row": "Write(clean) -> FW",
            "evidence_count": 5,
            "shadow_validated": True,
            "blast_radius": "low",
            "status": "auto_apply_eligible",
        }
        policy_manager.apply_proposal(proposal)

        tmp_files = list(self.rule_file.parent.glob("*.tmp"))
        assert tmp_files == [], f"Stale .tmp files in rules dir: {tmp_files}"

    def test_apply_proposal_appends_proposed_row_to_rule_file(self):
        """apply_proposal appends the proposed_row to RULE_FILE."""
        import policy_manager

        proposed_row = "Write(clean) -> FW"
        proposal = {
            "key": "Write:fast:clean",
            "proposed_row": proposed_row,
            "evidence_count": 5,
            "shadow_validated": True,
            "blast_radius": "low",
            "status": "auto_apply_eligible",
        }
        policy_manager.apply_proposal(proposal)

        content = self.rule_file.read_text(encoding="utf-8")
        assert proposed_row in content, f"proposed_row not found in RULE_FILE: {content!r}"

    def test_apply_proposal_idempotent_does_not_duplicate_row(self):
        """Applying the same proposal twice does not duplicate the row in RULE_FILE."""
        import policy_manager

        proposed_row = "Write(clean) -> FW"
        proposal = {
            "key": "Write:fast:clean",
            "proposed_row": proposed_row,
        }
        policy_manager.apply_proposal(proposal)
        policy_manager.apply_proposal(proposal)

        content = self.rule_file.read_text(encoding="utf-8")
        assert content.count(proposed_row) == 1, \
            f"Row duplicated after double apply: {content!r}"

    # --- undo writes RULE_FILE correctly ---

    def test_undo_removes_proposed_row_from_rule_file(self):
        """After undo, the proposed_row is removed from RULE_FILE."""
        import policy_manager

        proposed_row = "Bash(clean) -> FW"
        proposal = {
            "key": "Bash:fast:clean",
            "proposed_row": proposed_row,
        }
        token = policy_manager.apply_proposal(proposal)

        # Verify it was added
        assert proposed_row in self.rule_file.read_text(encoding="utf-8")

        # Undo
        result = policy_manager.undo(token)
        assert result is True

        content = self.rule_file.read_text(encoding="utf-8")
        assert proposed_row not in content, \
            f"Proposed row still present after undo: {content!r}"

    def test_undo_no_tmp_files_left(self):
        """After undo, no .tmp files linger in the rules directory."""
        import policy_manager

        proposal = {"key": "Bash:fast:clean", "proposed_row": "Bash(clean) -> FW"}
        token = policy_manager.apply_proposal(proposal)
        policy_manager.undo(token)

        tmp_files = list(self.rule_file.parent.glob("*.tmp"))
        assert tmp_files == [], f"Stale .tmp files after undo: {tmp_files}"

    def test_undo_unknown_token_returns_false(self):
        """undo with a nonexistent token returns False without crashing."""
        import policy_manager

        result = policy_manager.undo("nonexistent")
        assert result is False


    # --- tuner only edits its own tagged rows ---

    def test_apply_tags_row_with_undo_token(self):
        import policy_manager

        token = policy_manager.apply_proposal(
            {"key": "Bash:fast:clean", "proposed_row": "Bash(clean) -> FW"}
        )
        content = self.rule_file.read_text(encoding="utf-8")
        assert f"<!-- cr-auto:{token} -->" in content

    def test_undo_leaves_human_row_with_same_text(self):
        import policy_manager

        self.rule_file.write_text("| Bash(clean) -> FW |\n", encoding="utf-8")
        token = policy_manager.apply_proposal(
            {"key": "Bash:fast:clean", "proposed_row": "| Bash(clean) -> FW |"}
        )
        assert policy_manager.undo(token) is True
        assert "| Bash(clean) -> FW |" in self.rule_file.read_text(encoding="utf-8")

    def test_evaluate_proposals_gated_by_auto_apply_enabled(self, monkeypatch):
        import policy_applier
        import policy_manager

        proposals = [{
            "key": "Bash:fast:clean",
            "proposed_row": "Bash(clean) -> FW",
            "status": "auto_apply_eligible",
        }]
        policy_manager._save_proposals(proposals)
        monkeypatch.setattr(policy_applier, "policy_auto_apply_enabled", lambda: False)
        policy_manager.evaluate_proposals()
        assert "Bash(clean)" not in self.rule_file.read_text(encoding="utf-8")
        assert policy_manager._load_proposals()[0]["status"] == "auto_apply_eligible"

        monkeypatch.setattr(policy_applier, "policy_auto_apply_enabled", lambda: True)
        policy_manager.evaluate_proposals()
        assert "Bash(clean)" in self.rule_file.read_text(encoding="utf-8")

    def test_gc_size_cap_and_dead_rows_only_touch_tagged(self, monkeypatch):
        import policy_gc

        monkeypatch.setattr(policy_gc, "POLICY_MAX_TABLE_ROWS", 2)
        rows = [
            "| Human1 -> A |",
            "| Human2 -> B |",
            "| Auto1 -> C | <!-- cr-auto:aaaa1111 -->",
            "| Auto2 -> D | <!-- cr-auto:bbbb2222 -->",
        ]
        assert policy_gc.check_size_cap(rows) == rows[2:]
        assert policy_gc.filter_tagged(rows) == rows[2:]
        dead = policy_gc.find_dead_rows(policy_gc.filter_tagged(rows), {})
        assert all("cr-auto" in r for r in dead)


# ---------------------------------------------------------------------------
# Bug 5 — policy_manager.py: Counterfactual log rotation with fcntl lock
# ---------------------------------------------------------------------------

class TestCounterfactualLogRotation:
    """Verifies _append_counterfactual_log rotates correctly and appends new entries."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        import _common
        import policy_manager
        import policy_store

        monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
        monkeypatch.setattr(policy_manager, "DATA_DIR", tmp_path)
        monkeypatch.setattr(
            policy_manager, "COUNTERFACTUAL_LOG_FILE", tmp_path / "counterfactual_log.jsonl"
        )
        monkeypatch.setattr(policy_store, "DATA_DIR", tmp_path)
        monkeypatch.setattr(
            policy_store, "COUNTERFACTUAL_LOG_FILE", tmp_path / "counterfactual_log.jsonl"
        )

        self.tmp_path = tmp_path
        self.cf_log = tmp_path / "counterfactual_log.jsonl"
        self.cf_log_rotated = tmp_path / "counterfactual_log.jsonl.1"
        yield

    def _make_sessions(self, n: int = 3) -> list[dict]:
        return [
            {
                "session_id": f"s{i}",
                "reward": 0.8,
                "summary": {"started_at": f"2026-09-04T0{i}:00:00+00:00"},
            }
            for i in range(n)
        ]

    def test_basic_append_writes_entries(self):
        """Entries are written to the counterfactual log."""
        import policy_manager

        sessions = self._make_sessions(2)
        policy_manager._append_counterfactual_log(sessions, "Write:fast:clean", "Write(clean) -> FW", "tok1")

        assert self.cf_log.exists()
        entries = _read_jsonl(self.cf_log)
        assert len(entries) == 2
        assert all(e["applied_change"] == "tok1" for e in entries)
        assert all(e["key"] == "Write:fast:clean" for e in entries)

    def test_rotation_trigger_creates_rotated_file(self):
        """When the log exceeds MAX_COUNTERFACTUAL_LOG_BYTES, it is rotated to .jsonl.1."""
        import policy_manager
        import _common

        # Write content just over the threshold
        threshold = _common.MAX_COUNTERFACTUAL_LOG_BYTES
        big_content = "x" * (threshold + 1)
        self.cf_log.write_text(big_content, encoding="utf-8")

        sessions = self._make_sessions(1)
        policy_manager._append_counterfactual_log(sessions, "Write:fast:escalated", "Write(spec_complete) -> FW", "tok2")

        assert self.cf_log_rotated.exists(), "counterfactual_log.jsonl.1 should exist after rotation"
        # New entries should be in the fresh log file
        assert self.cf_log.exists(), "new counterfactual_log.jsonl should exist after rotation"
        new_entries = _read_jsonl(self.cf_log)
        assert len(new_entries) == 1
        assert new_entries[0]["applied_change"] == "tok2"

    def test_rotation_old_content_preserved_in_rotated_file(self):
        """After rotation, the old large content is preserved in .jsonl.1."""
        import policy_manager
        import _common

        threshold = _common.MAX_COUNTERFACTUAL_LOG_BYTES
        # Write a real JSONL entry, then pad to exceed threshold
        original_entry = {"session_id": "old", "key": "k", "actual_reward": 0.5,
                          "est_counterfactual_reward": 0.3, "applied_change": "orig"}
        original_line = json.dumps(original_entry) + "\n"
        padding = "P" * (threshold + 1 - len(original_line.encode()))
        self.cf_log.write_text(original_line + padding, encoding="utf-8")
        original_content = self.cf_log.read_text(encoding="utf-8")

        sessions = self._make_sessions(1)
        policy_manager._append_counterfactual_log(sessions, "k", "row", "new_tok")

        assert self.cf_log_rotated.exists()
        rotated_content = self.cf_log_rotated.read_text(encoding="utf-8")
        assert rotated_content == original_content, \
            "Rotated file does not match original content"

    def test_no_rotation_below_threshold(self):
        """Files below MAX_COUNTERFACTUAL_LOG_BYTES are NOT rotated."""
        import policy_manager
        import _common

        threshold = _common.MAX_COUNTERFACTUAL_LOG_BYTES
        small_content = "x" * (threshold - 1)
        self.cf_log.write_text(small_content, encoding="utf-8")

        sessions = self._make_sessions(1)
        policy_manager._append_counterfactual_log(sessions, "k", "row", "tok3")

        assert not self.cf_log_rotated.exists(), \
            "Log was rotated incorrectly below threshold"


# ---------------------------------------------------------------------------
# Bug 6 — compact_log.py: checkpoint_state.json uses save_json_state
# ---------------------------------------------------------------------------

class TestCheckpointStateAtomicWrite:
    """Verifies _save_checkpoint_ts writes atomically and round-trips correctly."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        import _common
        import compact_log

        monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
        monkeypatch.setattr(compact_log, "DATA_DIR", tmp_path)
        monkeypatch.setattr(
            compact_log, "CHECKPOINT_STATE_FILE", tmp_path / "checkpoint_state.json"
        )

        self.tmp_path = tmp_path
        self.checkpoint_file = tmp_path / "checkpoint_state.json"
        yield

    def test_save_and_load_round_trip(self):
        """Save a timestamp then load it back; values must match."""
        import compact_log

        ts = "2026-09-04T12:34:56+00:00"
        compact_log._save_checkpoint_ts(ts)

        loaded = compact_log._load_checkpoint_ts()
        assert loaded == ts, f"Round-trip mismatch: expected {ts!r}, got {loaded!r}"

    def test_checkpoint_file_is_valid_json(self):
        """checkpoint_state.json must be parseable as JSON after save."""
        import compact_log

        compact_log._save_checkpoint_ts("2026-09-04T00:00:00+00:00")

        assert self.checkpoint_file.exists()
        data = json.loads(self.checkpoint_file.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "last_checkpoint_ts" in data

    def test_no_tmp_files_left_after_save(self):
        """No .tmp files should linger after _save_checkpoint_ts."""
        import compact_log

        compact_log._save_checkpoint_ts("2026-09-04T00:00:00+00:00")

        tmp_files = list(self.tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Stale .tmp files after checkpoint save: {tmp_files}"

    def test_save_overwrites_previous_checkpoint(self):
        """Saving a second timestamp replaces the first."""
        import compact_log

        compact_log._save_checkpoint_ts("2026-09-04T10:00:00+00:00")
        compact_log._save_checkpoint_ts("2026-09-04T11:00:00+00:00")

        loaded = compact_log._load_checkpoint_ts()
        assert loaded == "2026-09-04T11:00:00+00:00", \
            f"Expected updated timestamp, got {loaded!r}"

    def test_load_returns_none_when_file_absent(self):
        """_load_checkpoint_ts returns None when the file doesn't exist."""
        import compact_log

        result = compact_log._load_checkpoint_ts()
        assert result is None
