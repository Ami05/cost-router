"""E2E tests for sequential_explore_gate PreToolUse gate.

Gate file: scripts/sequential_explore_gate.py
Entry point: event_pre_task(payload, is_claude)
State file: data/sequential_explore_state.json
Config key: sequential_explore_gate.threshold (default: 3)

Tests cover:
1. Single-file read worker (scope len=1): allow with no message
2. Multiple sequential single-file reads in same turn: warn then deny
3. Batched read worker (scope len>1): resets counter, allow
4. Non-read workers (mutation, shell): counter unchanged, allow
5. Explore subagent plain text prompt: counts as single
6. Turn ID reset: new turn resets counter
7. State persistence: counter survives across gate invocations
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Add scripts to path so we can import the gate
PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import _common  # noqa: E402
import sequential_explore_gate  # noqa: E402
from _common import gate_allow, gate_deny, load_json_state, save_json_state  # noqa: E402


class TestSequentialExploreGate:
    """Tests for sequential_explore_gate.event_pre_task()."""

    @pytest.fixture(autouse=True)
    def setup(self, isolated_data_dir):
        """Redirect DATA_DIR (patched across _common/_config/_fileio/_events) to a temp dir."""
        self.test_data_dir = isolated_data_dir
        self.state_file = self.test_data_dir / "sequential_explore_state.json"
        self.event_pre_task = sequential_explore_gate.event_pre_task

    def _payload_single_read(self, turn_id: str = "turn-1") -> dict:
        """Construct a single-file read worker payload."""
        return {
            "turn_id": turn_id,
            "tool_input": {
                "prompt": json.dumps({
                    "worker": "read",
                    "goal": "find function",
                    "scope": ["scripts/"],
                    "return": ["function signatures"],
                }),
            },
        }

    def _payload_batched_read(self, turn_id: str = "turn-1") -> dict:
        """Construct a batched read worker payload (scope len > 1)."""
        return {
            "turn_id": turn_id,
            "tool_input": {
                "prompt": json.dumps({
                    "worker": "read",
                    "goal": "find references",
                    "scope": ["scripts/", "tests/"],
                    "return": ["file:line references"],
                }),
            },
        }

    def _payload_explore_agent(self, turn_id: str = "turn-1") -> dict:
        """Construct an Explore subagent payload (plain text prompt)."""
        return {
            "turn_id": turn_id,
            "tool_input": {
                "subagent_type": "Explore",
                "prompt": "Read scripts/gates.py and scripts/sequential_explore_gate.py to understand the gate logic",
            },
        }

    def _payload_mutation_worker(self, turn_id: str = "turn-1") -> dict:
        """Construct a mutation worker payload (should not increment counter)."""
        return {
            "turn_id": turn_id,
            "tool_input": {
                "prompt": json.dumps({
                    "worker": "mutation",
                    "intent": "fix bug",
                    "files": ["scripts/gates.py:100"],
                    "pattern": "Add bounds check",
                    "output": "targeted_edits",
                }),
            },
        }

    def _payload_shell_worker(self, turn_id: str = "turn-1") -> dict:
        """Construct a shell worker payload (should not increment counter)."""
        return {
            "turn_id": turn_id,
            "tool_input": {
                "prompt": json.dumps({
                    "worker": "shell",
                    "intent": "run tests",
                    "command": "pytest tests/ -x",
                    "expect": "exit_code",
                }),
            },
        }

    def test_first_single_read_allows(self):
        """First single-file read worker should allow with no message."""
        payload = self._payload_single_read(turn_id="turn-1")
        result = self.event_pre_task(payload, is_claude=True)

        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "additionalContext" not in result["hookSpecificOutput"]

        # Verify state was saved
        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 1
        assert state["last_turn_id"] == "turn-1"

    def test_second_single_read_warns(self):
        """Second single-file read in same turn should allow with warning."""
        payload1 = self._payload_single_read(turn_id="turn-1")
        payload2 = self._payload_single_read(turn_id="turn-1")

        # First call
        result1 = self.event_pre_task(payload1, is_claude=True)
        assert result1["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "additionalContext" not in result1["hookSpecificOutput"]

        # Second call (should warn)
        result2 = self.event_pre_task(payload2, is_claude=True)
        assert result2["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "additionalContext" in result2["hookSpecificOutput"]
        assert "[sequential-explore-gate]" in result2["hookSpecificOutput"]["additionalContext"]
        assert "next will be blocked" in result2["hookSpecificOutput"]["additionalContext"]

        # Verify state
        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 2

    def test_third_single_read_denies(self):
        """Third single-file read in same turn should deny (default threshold=3)."""
        turn_id = "turn-1"

        # First call
        result1 = self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)
        assert result1["hookSpecificOutput"]["permissionDecision"] == "allow"

        # Second call
        result2 = self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)
        assert result2["hookSpecificOutput"]["permissionDecision"] == "allow"

        # Third call (should deny)
        result3 = self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)
        assert result3["hookSpecificOutput"]["permissionDecision"] == "deny"
        msg = result3["hookSpecificOutput"]["additionalContext"]
        assert "[sequential-explore-gate]" in msg
        assert "3 sequential single-file Explore dispatches" in msg
        assert "threshold: 3" in msg
        assert "Batch remaining reads" in msg

        # Verify state
        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 3

    def test_batched_read_resets_counter(self):
        """Batched read (scope len > 1) should reset counter and allow."""
        turn_id = "turn-1"

        # Increment counter to 2 with single-file reads
        self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)
        self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)

        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 2

        # Batched read should reset counter
        result = self.event_pre_task(self._payload_batched_read(turn_id=turn_id), is_claude=True)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 0

    def test_mutation_worker_doesnt_increment(self):
        """Mutation worker should not increment counter."""
        turn_id = "turn-1"

        # Set counter to 2
        self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)
        self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)

        # Mutation worker should pass through without incrementing
        result = self.event_pre_task(self._payload_mutation_worker(turn_id=turn_id), is_claude=True)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

        # Counter should still be 2
        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 2

    def test_shell_worker_doesnt_increment(self):
        """Shell worker should not increment counter."""
        turn_id = "turn-1"

        # Set counter to 2
        self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)
        self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)

        # Shell worker should pass through without incrementing
        result = self.event_pre_task(self._payload_shell_worker(turn_id=turn_id), is_claude=True)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

        # Counter should still be 2
        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 2

    def test_explore_agent_counts_as_single(self):
        """Explore subagent (plain text prompt) should count as single."""
        turn_id = "turn-1"

        # Increment counter to 2
        self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)
        self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)

        # Explore agent should count as single and deny (reaching threshold=3)
        result = self.event_pre_task(self._payload_explore_agent(turn_id=turn_id), is_claude=True)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 3

    def test_new_turn_id_resets_counter(self):
        """New turn_id should reset counter."""
        # Increment counter in turn-1
        self.event_pre_task(self._payload_single_read(turn_id="turn-1"), is_claude=True)
        self.event_pre_task(self._payload_single_read(turn_id="turn-1"), is_claude=True)

        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 2
        assert state["last_turn_id"] == "turn-1"

        # New turn should allow the third call (counter resets)
        result = self.event_pre_task(self._payload_single_read(turn_id="turn-2"), is_claude=True)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "additionalContext" not in result["hookSpecificOutput"]

        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 1
        assert state["last_turn_id"] == "turn-2"

    def test_turn_reset_with_different_key_names(self):
        """Turn reset should work with different key names (message_id, etc.)."""
        # First turn with turn_id
        self.event_pre_task(self._payload_single_read(turn_id="turn-1"), is_claude=True)

        # Second payload with messageId key (should still reset on different value)
        payload = {
            "messageId": "msg-2",
            "tool_input": {
                "subagent_type": "Explore",
                "prompt": "Read files",
            },
        }
        result = self.event_pre_task(payload, is_claude=True)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 1

    def test_state_persistence(self):
        """State should persist across multiple gate invocations."""
        # First invocation
        self.event_pre_task(self._payload_single_read(turn_id="turn-1"), is_claude=True)

        # Manually verify state file exists and is readable
        assert self.state_file.exists()
        state1 = load_json_state(self.state_file)
        assert state1["seq_explore_count"] == 1

        # Second invocation (fresh gate call, should read persisted state)
        result = self.event_pre_task(self._payload_single_read(turn_id="turn-1"), is_claude=True)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

        state2 = load_json_state(self.state_file)
        assert state2["seq_explore_count"] == 2

    def test_non_claude_payload(self):
        """Gate should work with is_claude=False (Cursor-style response)."""
        payload = self._payload_single_read(turn_id="turn-1")
        result = self.event_pre_task(payload, is_claude=False)

        # Cursor format uses "permission" not hookSpecificOutput
        assert result["permission"] == "allow"
        assert "agent_message" not in result

        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 1

    def test_non_claude_deny_response(self):
        """Gate should deny in Cursor format on threshold breach."""
        turn_id = "turn-1"

        # Reach threshold
        self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=False)
        self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=False)
        result = self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=False)

        assert result["permission"] == "deny"
        assert "agent_message" in result
        assert "[sequential-explore-gate]" in result["agent_message"]

    def test_empty_scope_treated_as_single(self):
        """Empty scope in read worker should be treated as single."""
        payload = {
            "turn_id": "turn-1",
            "tool_input": {
                "prompt": json.dumps({
                    "worker": "read",
                    "goal": "find stuff",
                    "scope": [],
                    "return": ["refs"],
                }),
            },
        }

        result = self.event_pre_task(payload, is_claude=True)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 1

    def test_non_json_prompt_with_explore_subagent(self):
        """Plain text prompt with Explore subagent_type should count as single."""
        payload = {
            "turn_id": "turn-1",
            "tool_input": {
                "subagent_type": "Explore",
                "prompt": "This is plain text, not JSON",
            },
        }

        result = self.event_pre_task(payload, is_claude=True)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

        state = load_json_state(self.state_file)
        assert state["seq_explore_count"] == 1

    def test_malformed_json_prompt_ignored(self):
        """Malformed JSON prompt should not be classified as a gate-relevant dispatch."""
        payload = {
            "turn_id": "turn-1",
            "tool_input": {
                "prompt": '{"worker": "read", invalid json here}',
            },
        }

        result = self.event_pre_task(payload, is_claude=True)
        # Should allow (pass through) since JSON is invalid
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

        state = load_json_state(self.state_file)
        # Counter should not have incremented (state file may not exist yet)
        assert state.get("seq_explore_count", 0) == 0

    def test_gate_fails_open_on_exception(self):
        """Gate should fail open (allow) on any exception."""
        payload = {
            "turn_id": "turn-1",
            # Deliberately malformed payload to trigger exception
            "tool_input": {"prompt": 12345},  # Not a string — triggers fail-open path
        }

        result = self.event_pre_task(payload, is_claude=True)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_threshold_configuration(self):
        """Gate should respect custom threshold from config."""
        # Mock config to return threshold=2
        with mock.patch("sequential_explore_gate.config_int", return_value=2):
            turn_id = "turn-1"

            # First call
            result1 = self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)
            assert result1["hookSpecificOutput"]["permissionDecision"] == "allow"

            # Second call should deny (threshold=2)
            result2 = self.event_pre_task(self._payload_single_read(turn_id=turn_id), is_claude=True)
            assert result2["hookSpecificOutput"]["permissionDecision"] == "deny"
            msg = result2["hookSpecificOutput"]["additionalContext"]
            assert "threshold: 2" in msg
