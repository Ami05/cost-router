#!/usr/bin/env python3
"""DGC-105 tests: Optional depth field and manifest validity checks.

Tests cover:
1. Payloads without depth validate for all worker types
2. Integer depth values are accepted
3. Non-integer depth values are rejected
4. is_manifest_valid() behavior for COMPLETE, PARTIAL, UNKNOWN status
5. is_manifest_valid() behavior for missing/invalid status
6. Checkpoint messages mention continuation-manifest block
7. Allowlist contains "continuation" and "manifest"
"""

import json
import sys
from pathlib import Path

import pytest

# Add scripts to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from validate_dispatch_schema import validate_dispatch_schema
from continuation_manifest import is_manifest_valid
from dispatch_enforcer import _stall_partial_report_message, _stall_time_partial_report_message
from _validate_shared import _CONSTRAINT_ALLOWLIST


class TestDepthFieldOptional:
    """Depth field is optional; payloads without it validate for all worker types."""

    @pytest.mark.parametrize("worker_type", ["mutation", "read", "create", "shell", "orchestrator"])
    def test_payload_without_depth_validates(self, worker_type):
        """Payload without depth field should validate for each worker type."""
        payloads = {
            "mutation": {
                "worker": "mutation",
                "intent": "test mutation",
                "files": ["test.py:1-10"],
                "pattern": "change x to y",
                "constraints": [],
                "output": "targeted_edits",
            },
            "read": {
                "worker": "read",
                "goal": "find something",
                "scope": ["src/"],
                "return": ["function signatures"],
            },
            "create": {
                "worker": "create",
                "intent": "create test file",
                "file_path": "test_new.py",
                "fields": {"description": "test"},
            },
            "shell": {
                "worker": "shell",
                "intent": "run command",
                "command": "echo hello",
                "expect": "exit_code",
            },
            "orchestrator": {
                "worker": "orchestrator",
                "intent": "orchestrate work",
                "step": [{"id": "1", "goal": "do something", "files": ["test.py"]}],
            },
        }

        if worker_type in payloads:
            payload = payloads[worker_type]
            result = validate_dispatch_schema(json.dumps(payload), session_id="test")
            assert result.decision == "allow", f"Payload without depth should validate for {worker_type}: {result.message}"


class TestDepthFieldHandling:
    """Depth field: integer accepted, non-integer rejected."""

    def test_integer_depth_accepted(self):
        """Integer depth values should be accepted."""
        for depth_value in [0, 1, 5, 100, -1]:
            # Note: payload must exceed ~200 tokens to bypass thin-dispatch exemption
            payload = {
                "worker": "read",
                "goal": "find something " + "x" * 600,
                "scope": ["src/", "lib/", "tests/", "docs/"] + ["path"] * 30,
                "return": ["function signatures", "class names", "imports"],
                "depth": depth_value,
            }
            result = validate_dispatch_schema(json.dumps(payload), session_id="test")
            assert result.decision == "allow", f"Integer depth={depth_value} should be accepted: {result.message}"

    @pytest.mark.parametrize("invalid_depth", ["1", 1.5, True, None, [], {}])
    def test_non_integer_depth_rejected(self, invalid_depth):
        """Non-integer depth values should be rejected."""
        # Note: payload must exceed ~200 tokens to bypass thin-dispatch exemption
        # and actually trigger depth validation (token ~= len // 4)
        payload = {
            "worker": "read",
            "goal": "find something " + "x" * 600,  # Ensure >800 chars total
            "scope": ["src/", "lib/", "tests/", "docs/"] + ["path"] * 30,
            "return": ["function signatures", "class names", "imports"],
            "depth": invalid_depth,
        }
        result = validate_dispatch_schema(json.dumps(payload), session_id="test")
        assert result.decision == "deny", f"Non-integer depth {invalid_depth!r} ({type(invalid_depth).__name__}) should be rejected"
        assert "depth" in result.message.lower(), "Error message should mention depth field"


class TestManifestValidity:
    """is_manifest_valid() behavior for different status values."""

    def test_manifest_valid_complete_status(self):
        """is_manifest_valid returns True for COMPLETE status."""
        manifest = {"status": "COMPLETE", "done": [], "remaining": []}
        assert is_manifest_valid(manifest) is True

    def test_manifest_valid_partial_status(self):
        """is_manifest_valid returns True for PARTIAL status."""
        manifest = {"status": "PARTIAL", "done": ["task1"], "remaining": ["task2"]}
        assert is_manifest_valid(manifest) is True

    def test_manifest_invalid_unknown_status(self):
        """is_manifest_valid returns False for UNKNOWN status."""
        manifest = {"status": "UNKNOWN", "reason": "no progress"}
        assert is_manifest_valid(manifest) is False

    def test_manifest_invalid_missing_status(self):
        """is_manifest_valid returns False when status field is missing."""
        manifest = {"done": [], "remaining": []}
        assert is_manifest_valid(manifest) is False

    def test_manifest_invalid_none(self):
        """is_manifest_valid returns False for None."""
        assert is_manifest_valid(None) is False

    def test_manifest_invalid_non_dict(self):
        """is_manifest_valid returns False for non-dict values."""
        assert is_manifest_valid("not a dict") is False
        assert is_manifest_valid([]) is False
        assert is_manifest_valid(123) is False

    def test_manifest_invalid_status_field(self):
        """is_manifest_valid returns False for invalid status values."""
        manifest = {"status": "INVALID_STATUS"}
        assert is_manifest_valid(manifest) is False


class TestCheckpointMessages:
    """Checkpoint messages mention continuation-manifest block."""

    def test_stall_partial_report_message_mentions_manifest(self):
        """_stall_partial_report_message should mention continuation-manifest."""
        message = _stall_partial_report_message(5)
        assert "continuation-manifest" in message.lower(), \
            "Stall partial report message should mention continuation-manifest"

    def test_stall_time_partial_report_message_mentions_manifest(self):
        """_stall_time_partial_report_message should mention continuation-manifest."""
        message = _stall_time_partial_report_message(60, 45.5)
        assert "continuation-manifest" in message.lower(), \
            "Stall time partial report message should mention continuation-manifest"

    def test_checkpoint_messages_mention_dispatch_protocol(self):
        """Checkpoint messages should reference dispatch-protocol.md."""
        message1 = _stall_partial_report_message(5)
        message2 = _stall_time_partial_report_message(60, 45.5)
        assert "dispatch-protocol" in message1, \
            "Stall partial report message should mention dispatch-protocol"
        assert "dispatch-protocol" in message2, \
            "Stall time partial report message should mention dispatch-protocol"


class TestConstraintAllowlist:
    """Constraint allowlist contains "continuation" and "manifest"."""

    def test_allowlist_contains_continuation(self):
        """Allowlist should contain 'continuation'."""
        tokens = [token.lower() for token in _CONSTRAINT_ALLOWLIST]
        assert "continuation" in tokens, "Constraint allowlist missing 'continuation'"

    def test_allowlist_contains_manifest(self):
        """Allowlist should contain 'manifest'."""
        tokens = [token.lower() for token in _CONSTRAINT_ALLOWLIST]
        assert "manifest" in tokens, "Constraint allowlist missing 'manifest'"

    def test_constraints_with_continuation_and_manifest_accepted(self):
        """Constraints containing continuation and manifest should be accepted."""
        payload = {
            "worker": "mutation",
            "intent": "test mutation",
            "files": ["test.py:1-10"],
            "pattern": "change x to y",
            "constraints": [
                "no continuation or manifest modifications",
                "preserve manifest block",
                "maintain continuation state",
            ],
            "output": "targeted_edits",
        }
        result = validate_dispatch_schema(json.dumps(payload), session_id="test")
        assert result.decision == "allow", f"Constraints with continuation/manifest should be accepted: {result.message}"


class TestManifestFieldTypes:
    """is_manifest_valid handles various manifest field types correctly."""

    def test_manifest_with_done_and_remaining_lists(self):
        """Manifest with properly typed done and remaining fields should validate."""
        manifest = {
            "status": "PARTIAL",
            "done": ["step1", "step2"],
            "remaining": [{"id": "3", "goal": "continue", "files": []}],
            "tokens_used": 1000,
            "phase": 1,
        }
        assert is_manifest_valid(manifest) is True

    def test_manifest_empty_remaining_list_valid(self):
        """Manifest with empty remaining should still be valid if status is COMPLETE/PARTIAL."""
        manifest = {
            "status": "COMPLETE",
            "done": ["task1"],
            "remaining": [],
        }
        assert is_manifest_valid(manifest) is True

    def test_manifest_with_extra_fields(self):
        """Manifest with extra fields should still validate based on status."""
        manifest = {
            "status": "PARTIAL",
            "done": [],
            "remaining": [],
            "custom_field": "custom_value",
            "phase": 2,
            "tokens_used": 500,
            "depth": 1,
        }
        assert is_manifest_valid(manifest) is True
