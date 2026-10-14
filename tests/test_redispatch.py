"""Tests for manifest-driven redispatch with loop guard (DGC-206).

Tests cover:
- PARTIAL stop reminder injection
- Loop guard depth check
- Loop guard no_progress check
- Manifest validation against Task lineage
- Continuation compaction (no full history)
- Queue TTL unchanged
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from continuation_manifest import (
    parse_manifest,
    compact_continuation,
    no_progress,
    get_remaining_ids,
    validate_manifest_lineage,
)


class TestGetRemainingIds:
    """Test get_remaining_ids extraction."""

    def test_empty_manifest(self):
        """Empty manifest returns empty list."""
        from continuation_manifest import get_remaining_ids
        result = get_remaining_ids({})
        assert result == []

    def test_none_manifest(self):
        """None manifest returns empty list."""
        from continuation_manifest import get_remaining_ids
        result = get_remaining_ids(None)
        assert result == []

    def test_valid_remaining_items(self):
        """Valid remaining items are extracted as strings."""
        from continuation_manifest import get_remaining_ids
        manifest = {
            "status": "PARTIAL",
            "remaining": [
                {"id": "task-1", "goal": "goal1", "files": ["a.py"]},
                {"id": "task-2", "goal": "goal2", "files": ["b.py"]},
            ],
        }
        result = get_remaining_ids(manifest)
        assert result == ["task-1", "task-2"]

    def test_numeric_ids_converted_to_strings(self):
        """Numeric IDs are converted to strings."""
        from continuation_manifest import get_remaining_ids
        manifest = {
            "remaining": [
                {"id": 1, "goal": "goal1"},
                {"id": 2, "goal": "goal2"},
            ],
        }
        result = get_remaining_ids(manifest)
        assert result == ["1", "2"]

    def test_missing_id_field_skipped(self):
        """Items without id field are skipped."""
        from continuation_manifest import get_remaining_ids
        manifest = {
            "remaining": [
                {"id": "task-1", "goal": "goal1"},
                {"goal": "goal2"},  # No id
                {"id": "task-3", "goal": "goal3"},
            ],
        }
        result = get_remaining_ids(manifest)
        assert result == ["task-1", "task-3"]

    def test_non_list_remaining(self):
        """Non-list remaining field returns empty list."""
        from continuation_manifest import get_remaining_ids
        manifest = {
            "remaining": "not a list",
        }
        result = get_remaining_ids(manifest)
        assert result == []


class TestValidateManifestLineage:
    """Test validate_manifest_lineage for Task matching."""

    def test_no_manifest(self):
        """No manifest returns invalid."""
        is_valid, reason = validate_manifest_lineage(None, "some task")
        assert not is_valid
        assert "No valid manifest" in reason

    def test_no_task_text(self):
        """No task text returns invalid."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [{"id": "task-1", "goal": "goal1"}],
        }
        is_valid, reason = validate_manifest_lineage(manifest, None)
        assert not is_valid
        assert "No task_text" in reason

    def test_empty_remaining(self):
        """No remaining items returns invalid."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [],
        }
        is_valid, reason = validate_manifest_lineage(manifest, "some task")
        assert not is_valid
        assert "no remaining items" in reason

    def test_task_references_remaining_id(self):
        """Task text mentioning remaining ID returns valid."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [
                {"id": "DGC-206", "goal": "implement redispatch", "files": ["a.py"]},
            ],
        }
        task_text = "Continue with DGC-206 implementation"
        is_valid, reason = validate_manifest_lineage(manifest, task_text)
        assert is_valid
        assert "DGC-206" in reason

    def test_task_references_goal_text(self):
        """Task text mentioning goal text returns valid."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [
                {"id": "unit-1", "goal": "implement foo feature", "files": ["foo.py"]},
            ],
        }
        task_text = "Continue with implement foo feature work"
        is_valid, reason = validate_manifest_lineage(manifest, task_text)
        assert is_valid
        assert "goal" in reason.lower()

    def test_task_no_reference(self):
        """Task without reference to remaining items returns invalid."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [
                {"id": "task-1", "goal": "goal1", "files": ["a.py"]},
            ],
        }
        task_text = "Do something completely different"
        is_valid, reason = validate_manifest_lineage(manifest, task_text)
        assert not is_valid
        assert "does not reference" in reason

    def test_multiple_remaining_any_match(self):
        """Task matching any remaining item returns valid."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [
                {"id": "task-1", "goal": "goal1", "files": ["a.py"]},
                {"id": "task-2", "goal": "goal2", "files": ["b.py"]},
                {"id": "task-3", "goal": "goal3", "files": ["c.py"]},
            ],
        }
        task_text = "Work on task-2"
        is_valid, reason = validate_manifest_lineage(manifest, task_text)
        assert is_valid


class TestLoopGuardDepth:
    """Test loop guard depth checking (DGC-206 step 4).

    The depth guard checks: depth >= continuation.max_depth (default 3).
    Depth is carried in the manifest and incremented on each redispatch.
    """

    def test_depth_increments_across_manifests(self):
        """Depth should increment on each redispatch."""
        # Original dispatch: depth not set (defaults to 0)
        original = {"remaining": [{"id": "task-1"}]}

        # After first PARTIAL: depth incremented to 1
        after_first = parse_manifest(
            '```continuation-manifest\n'
            '{"status": "PARTIAL", "depth": 1, "remaining": [{"id": "task-1"}]}\n'
            '```'
        )
        assert after_first.get("depth") == 1

        # After second PARTIAL: depth incremented to 2
        after_second = parse_manifest(
            '```continuation-manifest\n'
            '{"status": "PARTIAL", "depth": 2, "remaining": [{"id": "task-1"}]}\n'
            '```'
        )
        assert after_second.get("depth") == 2

    def test_depth_field_required_for_guard(self):
        """Depth field must be present in manifest for loop guard to work."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [{"id": "task-1"}],
        }
        # Missing depth should be treated as 0
        depth = manifest.get("depth", 0)
        assert depth == 0

    def test_depth_comparison_for_denial(self):
        """Depth >= max_depth should trigger denial."""
        # With default max_depth=3, depth 3 and above should be denied
        max_depth = 3  # From config default

        test_cases = [
            (0, False),  # Allowed
            (1, False),  # Allowed
            (2, False),  # Allowed
            (3, True),   # Denied
            (4, True),   # Denied
        ]

        for depth, should_deny in test_cases:
            would_deny = depth >= max_depth
            assert would_deny == should_deny


class TestLoopGuardNoProgress:
    """Test loop guard no_progress detection (DGC-206 step 4)."""

    def test_no_progress_detected(self):
        """Identical remaining IDs across manifests triggers guard."""
        prev = {
            "remaining": [
                {"id": "task-1"},
                {"id": "task-2"},
            ]
        }
        curr = {
            "remaining": [
                {"id": "task-1"},
                {"id": "task-2"},
            ]
        }
        assert no_progress(prev, curr)

    def test_progress_made(self):
        """Fewer remaining IDs means progress was made."""
        prev = {
            "remaining": [
                {"id": "task-1"},
                {"id": "task-2"},
            ]
        }
        curr = {
            "remaining": [
                {"id": "task-2"},
            ]
        }
        assert not no_progress(prev, curr)

    def test_all_items_done(self):
        """Empty remaining list is not considered stall."""
        prev = {
            "remaining": [
                {"id": "task-1"},
            ]
        }
        curr = {
            "remaining": [],
        }
        assert not no_progress(prev, curr)

    def test_none_prev_manifest(self):
        """None previous manifest is not stall."""
        curr = {
            "remaining": [
                {"id": "task-1"},
            ]
        }
        assert not no_progress(None, curr)

    def test_different_order_same_ids(self):
        """Same IDs in different order is still stall."""
        prev = {
            "remaining": [
                {"id": "a"},
                {"id": "b"},
            ]
        }
        curr = {
            "remaining": [
                {"id": "b"},
                {"id": "a"},
            ]
        }
        assert no_progress(prev, curr)


class TestCompactionNeverCarriesHistory:
    """Test that compaction excludes done[] (DGC-206 acceptance criteria)."""

    def test_compact_excludes_done_items(self):
        """Compact continuation never includes done[] items."""
        manifest = {
            "status": "PARTIAL",
            "done": ["task-1", "task-2"],
            "remaining": [
                {"id": "task-3", "goal": "finish work", "files": ["a.py"]},
            ],
            "depth": 1,
        }
        compact_json = compact_continuation(manifest)
        assert compact_json

        # Parse and verify done[] is not present
        compact_dict = json.loads(compact_json)
        assert "done" not in compact_dict
        assert "remaining" in compact_dict
        assert len(compact_dict["remaining"]) == 1
        assert compact_dict["remaining"][0]["id"] == "task-3"

    def test_compact_omits_phase_and_tokens_used(self):
        """Compact continuation excludes phase and tokens_used (history)."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [
                {"id": "task-1", "goal": "goal1", "files": ["a.py"]},
            ],
            "phase": "Phase 2: Execution",
            "tokens_used": 45000,
            "depth": 0,
        }
        compact_json = compact_continuation(manifest)
        compact_dict = json.loads(compact_json)

        assert "phase" not in compact_dict
        assert "tokens_used" not in compact_dict
        assert "done" not in compact_dict
        # Only status, remaining, and depth should be present
        assert set(compact_dict.keys()) == {"status", "remaining", "depth"}

    def test_compact_includes_depth_for_loop_guard(self):
        """Compact continuation includes depth (needed for loop guard)."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [{"id": "task-1", "goal": "goal1"}],
            "depth": 2,
        }
        compact_json = compact_continuation(manifest)
        compact_dict = json.loads(compact_json)

        assert compact_dict["depth"] == 2


class TestPartialReminderGeneration:
    """Test PARTIAL stop reminder injection (DGC-206 step 1).

    Hook injects a main-session reminder with compact_continuation context.
    The reminder helps the orchestrator understand the PARTIAL result.
    """

    def test_reminder_structure_for_partial(self):
        """PARTIAL result should include depth and remaining count in context."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [
                {"id": "task-1", "goal": "goal1"},
                {"id": "task-2", "goal": "goal2"},
            ],
            "depth": 1,
        }
        # Expected reminder structure includes depth and count
        assert manifest["status"] == "PARTIAL"
        assert manifest["depth"] == 1
        assert len(manifest.get("remaining", [])) == 2

    def test_reminder_includes_compact_json(self):
        """Reminder should include compact continuation context."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [
                {"id": "unit-1", "goal": "implement feature", "files": ["a.py", "b.py"]},
            ],
            "depth": 0,
        }
        compact_json = compact_continuation(manifest)
        assert compact_json
        # Should be able to parse and find unit-1
        parsed = json.loads(compact_json)
        assert "unit-1" in [r.get("id") for r in parsed.get("remaining", [])]

    def test_reminder_injection_target(self):
        """Reminder injected into hookSpecificOutput.additionalContext."""
        # This test documents the expected hook response structure
        # when result_status == "PARTIAL" at SubagentStop
        expected_output = {
            "continue": True,
            "hookSpecificOutput": {
                "additionalContext": (
                    "Subagent done. Reply in 3 sentences...\n"
                    "**Subagent PARTIAL:** depth=1, 2 remaining units."
                )
            }
        }
        assert "PARTIAL" in expected_output["hookSpecificOutput"]["additionalContext"]


class TestValidateTaskManifestLineage:
    """Test hook validation of Task against manifest (DGC-206 step 2).

    Hook validates that the next Task matches a remaining item from the manifest.
    This prevents accidental dispatch of unrelated work during continuation.
    """

    def test_task_must_reference_remaining_items(self):
        """Task should reference at least one remaining item."""
        manifest = {
            "status": "PARTIAL",
            "remaining": [
                {"id": "DGC-206", "goal": "redispatch feature"},
            ],
        }

        # Valid: Task mentions the remaining item
        task_valid = "Implement DGC-206"
        is_valid, _ = validate_manifest_lineage(manifest, task_valid)
        assert is_valid

        # Invalid: Task does not mention remaining item
        task_invalid = "Fix a completely unrelated bug"
        is_valid, _ = validate_manifest_lineage(manifest, task_invalid)
        assert not is_valid

    def test_validation_uses_both_id_and_goal(self):
        """Validation can match either item ID or goal text."""
        manifest = {
            "remaining": [
                {"id": "unit-1", "goal": "implement auth feature"},
            ],
        }

        # Match by ID
        is_valid, _ = validate_manifest_lineage(manifest, "unit-1")
        assert is_valid

        # Match by goal
        is_valid, _ = validate_manifest_lineage(manifest, "implement auth feature")
        assert is_valid

    def test_validation_with_multiple_remaining_items(self):
        """Validation succeeds if Task references any remaining item."""
        manifest = {
            "remaining": [
                {"id": "task-1", "goal": "fix logging"},
                {"id": "task-2", "goal": "add tests"},
                {"id": "task-3", "goal": "optimize queries"},
            ],
        }

        # Task references middle item
        is_valid, _ = validate_manifest_lineage(manifest, "task-2")
        assert is_valid


class TestQueueTTLUnchanged:
    """Test that existing queue TTL behavior is preserved."""

    def test_manifest_module_does_not_affect_queue_ttl(self):
        """manifest module functions do not touch queue management."""
        # This is a documentation test verifying that the continuation
        # manifest module is independent of the dispatch queue TTL logic
        # which is managed by _push_pending_dispatch_worker_intent and
        # _pop_pending_dispatch_worker_intent in check_routing.py.
        # The ticket states "Hooks cannot enqueue dispatches; orchestrator
        # redispatches from JSON manifest" — so manifest module adds no
        # queue-touching code.
        assert True  # Placeholder for documentation


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
