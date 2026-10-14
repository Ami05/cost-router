"""Tests for continuation_manifest.py: parse_manifest, compact_continuation, no_progress."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from continuation_manifest import parse_manifest, compact_continuation, no_progress


class TestParseManifest:
    """Test parse_manifest validation and JSON fencing."""

    def test_empty_report(self):
        """Empty report returns UNKNOWN."""
        result = parse_manifest("")
        assert result["status"] == "UNKNOWN"
        assert "empty or non-string report" in result["reason"]

    def test_non_string_report(self):
        """Non-string input returns UNKNOWN."""
        result = parse_manifest(None)
        assert result["status"] == "UNKNOWN"

    def test_no_fenced_block(self):
        """Report without fenced block returns UNKNOWN."""
        result = parse_manifest("Some text without manifest")
        assert result["status"] == "UNKNOWN"
        assert "no fenced continuation-manifest block" in result["reason"]

    def test_malformed_json(self):
        """Malformed JSON in fence returns UNKNOWN."""
        report = "```continuation-manifest\n{invalid json}\n```"
        result = parse_manifest(report)
        assert result["status"] == "UNKNOWN"
        assert "invalid JSON" in result["reason"]

    def test_fenced_block_not_object(self):
        """Non-object JSON in fence returns UNKNOWN."""
        report = '```continuation-manifest\n["array"]\n```'
        result = parse_manifest(report)
        assert result["status"] == "UNKNOWN"
        assert "not a JSON object" in result["reason"]

    def test_multiple_blocks_last_wins(self):
        """Multiple fenced blocks: last one is used."""
        report = '''```continuation-manifest
{"status": "COMPLETE", "remaining": []}
```
Some text.
```continuation-manifest
{"status": "PARTIAL", "remaining": [{"id": "task1"}]}
```'''
        result = parse_manifest(report)
        assert result["status"] == "PARTIAL"
        assert len(result.get("remaining", [])) == 1

    def test_missing_status_field(self):
        """Missing status field defaults to UNKNOWN."""
        report = '```continuation-manifest\n{"remaining": []}\n```'
        result = parse_manifest(report)
        assert result["status"] == "UNKNOWN"
        assert "status field missing or invalid" in result["reason"]

    def test_invalid_status_value(self):
        """Invalid status value defaults to UNKNOWN."""
        report = '```continuation-manifest\n{"status": "INVALID"}\n```'
        result = parse_manifest(report)
        assert result["status"] == "UNKNOWN"

    def test_remaining_truncated_to_max_items(self):
        """Remaining list truncated when exceeds max_remaining_items."""
        items = [{"id": f"task{i}", "goal": f"goal{i}"} for i in range(15)]
        manifest_json = json.dumps({"status": "PARTIAL", "remaining": items})
        report = f"```continuation-manifest\n{manifest_json}\n```"
        result = parse_manifest(report)
        assert len(result["remaining"]) == 10  # max_remaining_items = 10
        assert "truncated to 10 items" in result.get("reason", "")

    def test_valid_complete(self):
        """Valid COMPLETE manifest parses correctly."""
        manifest_json = json.dumps({
            "status": "COMPLETE",
            "remaining": [],
            "done": ["t1", "t2"],
            "tokens_used": 5000,
            "phase": "final",
            "depth": 1
        })
        report = f"```continuation-manifest\n{manifest_json}\n```"
        result = parse_manifest(report)
        assert result["status"] == "COMPLETE"
        assert result.get("tokens_used") == 5000
        assert result.get("phase") == "final"

    def test_valid_partial(self):
        """Valid PARTIAL manifest with remaining items."""
        manifest_json = json.dumps({
            "status": "PARTIAL",
            "remaining": [{"id": "task1", "goal": "goal1", "files": ["a.py"]}],
            "depth": 2
        })
        report = f"```continuation-manifest\n{manifest_json}\n```"
        result = parse_manifest(report)
        assert result["status"] == "PARTIAL"
        assert len(result["remaining"]) == 1
        assert result["remaining"][0]["id"] == "task1"


class TestCompactContinuation:
    """Test compact_continuation output bounding and content filtering."""

    def test_empty_remaining_returns_empty_string(self):
        """Empty remaining list returns empty string."""
        manifest = {"status": "PARTIAL", "remaining": []}
        result = compact_continuation(manifest)
        assert result == ""

    def test_missing_remaining_returns_empty_string(self):
        """Missing remaining key returns empty string."""
        manifest = {"status": "PARTIAL"}
        result = compact_continuation(manifest)
        assert result == ""

    def test_compact_excludes_done(self):
        """Compact output includes remaining but excludes done."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [{"id": "task1", "goal": "goal1", "files": ["a.py"]}],
            "done": ["task0"],
            "depth": 1
        }
        result = compact_continuation(manifest)
        compact = json.loads(result)
        assert "done" not in compact
        assert len(compact["remaining"]) == 1
        assert "task1" in result

    def test_compact_includes_only_id_goal_files(self):
        """Compact output strips non-essential fields from items."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [{
                "id": "task1",
                "goal": "goal1",
                "files": ["a.py"],
                "extra_field": "ignored",
                "metadata": "also ignored"
            }],
            "depth": 1
        }
        result = compact_continuation(manifest)
        compact = json.loads(result)
        item = compact["remaining"][0]
        assert "id" in item
        assert "goal" in item
        assert "files" in item
        assert "extra_field" not in item

    def test_compact_respects_max_compact_chars_limit(self):
        """Output bounded by max_compact_chars (2000 by default)."""
        # Create large remaining list
        items = [
            {"id": f"task{i}", "goal": f"goal{i} " + "x" * 80, "files": [f"file{i}_a.py", f"file{i}_b.py"]}
            for i in range(50)
        ]
        manifest = {"status": "PARTIAL", "remaining": items, "depth": 1}
        result = compact_continuation(manifest)
        assert len(result) <= 2000
        # Should have truncated remaining
        compact = json.loads(result)
        assert len(compact["remaining"]) < 50

    def test_compact_truncation_drops_files_first(self):
        """When size exceeds limit, files are dropped before items."""
        # Create items with large file lists
        items = [
            {"id": f"task{i}", "goal": f"goal{i}" * 5, "files": [f"file{i}_{j}.py" for j in range(20)]}
            for i in range(10)
        ]
        manifest = {"status": "PARTIAL", "remaining": items, "depth": 1}
        result = compact_continuation(manifest)
        compact = json.loads(result)
        # After files dropped, should have no "files" keys or they should be empty
        for item in compact["remaining"]:
            assert "files" not in item or item.get("files") == []

    def test_compact_includes_status_and_depth(self):
        """Compact output includes status and depth fields."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [{"id": "task1", "goal": "goal1"}],
            "depth": 2
        }
        result = compact_continuation(manifest)
        compact = json.loads(result)
        assert compact["status"] == "PARTIAL"
        assert compact["depth"] == 2


class TestNoProgress:
    """Test no_progress stall detection logic."""

    def test_both_manifests_none(self):
        """Both None returns False."""
        result = no_progress(None, None)
        assert result is False

    def test_prev_none_curr_none(self):
        """Previous None, current None returns False."""
        result = no_progress(None, None)
        assert result is False

    def test_prev_with_items_curr_empty_remaining(self):
        """Current empty remaining returns False (work finished)."""
        prev = {"remaining": [{"id": "task1"}, {"id": "task2"}]}
        curr = {"remaining": []}
        result = no_progress(prev, curr)
        assert result is False

    def test_prev_with_items_curr_none_remaining(self):
        """Current missing remaining returns False."""
        prev = {"remaining": [{"id": "task1"}]}
        curr = {"remaining": None}
        result = no_progress(prev, curr)
        assert result is False

    def test_identical_non_empty_remaining_ids_true(self):
        """Identical non-empty remaining ids returns True (no progress)."""
        prev = {"remaining": [{"id": "task1"}, {"id": "task2"}]}
        curr = {"remaining": [{"id": "task2"}, {"id": "task1"}]}  # Different order
        result = no_progress(prev, curr)
        assert result is True

    def test_different_remaining_ids_false(self):
        """Different remaining ids returns False (progress made)."""
        prev = {"remaining": [{"id": "task1"}, {"id": "task2"}]}
        curr = {"remaining": [{"id": "task1"}]}  # task2 removed
        result = no_progress(prev, curr)
        assert result is False

    def test_prev_none_curr_with_items(self):
        """Previous None, current with items returns False."""
        prev = None
        curr = {"remaining": [{"id": "task1"}]}
        result = no_progress(prev, curr)
        assert result is False

    def test_prev_empty_curr_with_items(self):
        """Previous empty remaining, current with items returns False."""
        prev = {"remaining": []}
        curr = {"remaining": [{"id": "task1"}]}
        result = no_progress(prev, curr)
        assert result is False

    def test_malformed_items_ignored(self):
        """Non-dict items in remaining are skipped."""
        prev = {"remaining": [{"id": "task1"}, "string", None]}
        curr = {"remaining": [{"id": "task1"}, 42, None]}
        result = no_progress(prev, curr)
        assert result is True  # Only task1 ids are considered

    def test_items_missing_id_field(self):
        """Items without 'id' field are excluded from id list."""
        prev = {"remaining": [{"id": "task1"}, {"name": "task2"}]}
        curr = {"remaining": [{"id": "task1"}, {"name": "task2"}]}
        result = no_progress(prev, curr)
        assert result is True  # Only task1 id is counted


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
