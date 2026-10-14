#!/usr/bin/env python3
"""DGC-103: Tests for strict stall profile enforcement per dispatch worker type.

Covers:
- Shell/orchestrator: trip at 3 misses or 90s in enforce mode
- Warn mode returns None and bumps gate_warn_strict_stall counter
- Mutation/read unchanged at 600s (regression tests)
- Unresolved worker type falls back to top-level defaults (6 markers/180s)
- Mode off disables strict profile entirely
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys_path = str(Path(__file__).resolve().parent.parent / "scripts")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)


class TestStrictStallProfileConfig:
    """Test _strict_stall_profile_for configuration resolution."""

    @patch("dispatch_enforcer.config_get")
    def test_strict_profile_resolved_when_all_keys_present(self, mock_config_get):
        """Shell worker with all required config keys returns profile dict."""
        from dispatch_enforcer import _strict_stall_profile_for

        def config_side_effect(key, default=None):
            config_map = {
                "progress_ledger.mode": "enforce",
                "progress_ledger.strict_profile.shell.max_stall_markers": 3,
                "progress_ledger.strict_profile.shell.max_stall_seconds": 90,
                "progress_ledger.strict_profile.shell.heartbeat_seconds": 30,
            }
            return config_map.get(key, default)

        mock_config_get.side_effect = config_side_effect

        profile = _strict_stall_profile_for("shell")
        assert profile is not None
        assert profile["max_stall_markers"] == 3
        assert profile["max_stall_seconds"] == 90
        assert profile["heartbeat_seconds"] == 30

    @patch("dispatch_enforcer.config_get")
    def test_strict_profile_returns_none_when_mode_off(self, mock_config_get):
        """When progress_ledger.mode is 'off', returns None."""
        from dispatch_enforcer import _strict_stall_profile_for

        def config_side_effect(key, default=None):
            if key == "progress_ledger.mode":
                return "off"
            return default

        mock_config_get.side_effect = config_side_effect

        profile = _strict_stall_profile_for("shell")
        assert profile is None

    @patch("dispatch_enforcer.config_get")
    def test_strict_profile_returns_none_when_worker_type_none(self, mock_config_get):
        """When worker_type is None, returns None (unresolved)."""
        from dispatch_enforcer import _strict_stall_profile_for

        mock_config_get.return_value = "enforce"

        profile = _strict_stall_profile_for(None)
        assert profile is None

    @patch("dispatch_enforcer.config_get")
    def test_strict_profile_returns_none_when_keys_missing(self, mock_config_get):
        """Missing any required key (max_markers/seconds/heartbeat) returns None."""
        from dispatch_enforcer import _strict_stall_profile_for

        def config_side_effect(key, default=None):
            if key == "progress_ledger.mode":
                return "enforce"
            # Missing max_stall_seconds
            if key == "progress_ledger.strict_profile.orchestrator.max_stall_seconds":
                return None
            if key == "progress_ledger.strict_profile.orchestrator.max_stall_markers":
                return 3
            if key == "progress_ledger.strict_profile.orchestrator.heartbeat_seconds":
                return 30
            return default

        mock_config_get.side_effect = config_side_effect

        profile = _strict_stall_profile_for("orchestrator")
        assert profile is None


class TestProgressLedgerMode:
    """Test _progress_ledger_mode mode resolution."""

    @patch("dispatch_enforcer.config_get")
    def test_mode_enforce(self, mock_config_get):
        """Mode 'enforce' returns 'enforce'."""
        from dispatch_enforcer import _progress_ledger_mode

        mock_config_get.return_value = "enforce"
        assert _progress_ledger_mode() == "enforce"

    @patch("dispatch_enforcer.config_get")
    def test_mode_warn(self, mock_config_get):
        """Mode 'warn' returns 'warn'."""
        from dispatch_enforcer import _progress_ledger_mode

        mock_config_get.return_value = "warn"
        assert _progress_ledger_mode() == "warn"

    @patch("dispatch_enforcer.config_get")
    def test_mode_off(self, mock_config_get):
        """Mode 'off' returns 'off'."""
        from dispatch_enforcer import _progress_ledger_mode

        mock_config_get.return_value = "off"
        assert _progress_ledger_mode() == "off"

    @patch("dispatch_enforcer.config_get")
    def test_mode_invalid_defaults_to_warn(self, mock_config_get):
        """Invalid/missing mode defaults to 'warn'."""
        from dispatch_enforcer import _progress_ledger_mode

        mock_config_get.return_value = "invalid_mode"
        assert _progress_ledger_mode() == "warn"


class TestSubagentProgressGateWithStrictProfile:
    """Test _subagent_progress_gate with strict stall profile enforcement."""

    def _make_payload(self, agent_id="agent_123"):
        """Create minimal payload for gate testing."""
        return {
            "agent_id": agent_id,
            "session_id": "session_456",
        }

    def _make_state(self):
        """Create minimal state dict."""
        return {
            "subagent_progress": {},
        }

    @patch("dispatch_enforcer._subagent_recent_assistant_text")
    @patch("dispatch_enforcer._dispatch_worker_type_for")
    @patch("dispatch_enforcer._stall_thresholds_for")
    @patch("dispatch_enforcer._strict_stall_profile_for")
    @patch("dispatch_enforcer.config_get")
    @patch("dispatch_enforcer._warn")
    @patch("dispatch_enforcer._bump")
    def test_shell_worker_enforce_mode_trips_at_3_misses(
        self,
        mock_bump,
        mock_warn,
        mock_config_get,
        mock_strict_profile,
        mock_thresholds,
        mock_worker_type,
        mock_recent_text,
    ):
        """Shell worker in enforce mode trips after 3 misses (>30s heartbeat)."""
        from dispatch_enforcer import _subagent_progress_gate

        payload = self._make_payload()
        state = self._make_state()

        mock_worker_type.return_value = "shell"
        mock_thresholds.return_value = (6, 180)
        mock_strict_profile.return_value = {
            "max_stall_markers": 3,
            "max_stall_seconds": 90,
            "heartbeat_seconds": 30,
        }
        mock_config_get.return_value = "enforce"
        mock_recent_text.return_value = "progress: running"

        # First call with progress marker - initializes entry
        result = _subagent_progress_gate(payload, state, True)
        assert result is None

        # Simulate 3 misses by calling gate with stale timestamps
        # Miss 1: elapsed 35s (> 30s heartbeat)
        base_time = time.time()
        state["subagent_progress"]["agent_123"] = {
            "last_marker_ts": base_time - 35,
            "stall_count": 0,
            "miss_count": 0,
        }
        mock_recent_text.return_value = "continuing work"
        result = _subagent_progress_gate(payload, state, True)
        assert result is None
        assert state["subagent_progress"]["agent_123"]["miss_count"] == 1

        # Miss 2: elapsed 40s
        state["subagent_progress"]["agent_123"]["last_marker_ts"] = base_time - 40
        result = _subagent_progress_gate(payload, state, True)
        assert result is None
        assert state["subagent_progress"]["agent_123"]["miss_count"] == 2

        # Miss 3: should deny
        state["subagent_progress"]["agent_123"]["last_marker_ts"] = base_time - 45
        result = _subagent_progress_gate(payload, state, True)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    @patch("dispatch_enforcer._subagent_recent_assistant_text")
    @patch("dispatch_enforcer._dispatch_worker_type_for")
    @patch("dispatch_enforcer._stall_thresholds_for")
    @patch("dispatch_enforcer._strict_stall_profile_for")
    @patch("dispatch_enforcer.config_get")
    @patch("dispatch_enforcer._warn")
    @patch("dispatch_enforcer._bump")
    def test_shell_worker_enforce_mode_trips_at_90s(
        self,
        mock_bump,
        mock_warn,
        mock_config_get,
        mock_strict_profile,
        mock_thresholds,
        mock_worker_type,
        mock_recent_text,
    ):
        """Shell worker in enforce mode trips at 90s elapsed regardless of miss count."""
        from dispatch_enforcer import _subagent_progress_gate

        payload = self._make_payload()
        state = self._make_state()

        mock_worker_type.return_value = "shell"
        mock_thresholds.return_value = (6, 180)
        mock_strict_profile.return_value = {
            "max_stall_markers": 3,
            "max_stall_seconds": 90,
            "heartbeat_seconds": 30,
        }
        mock_config_get.return_value = "enforce"

        base_time = time.time()
        state["subagent_progress"]["agent_123"] = {
            "last_marker_ts": base_time - 95,  # 95s elapsed > 90s max
            "stall_count": 0,
            "miss_count": 1,
        }
        mock_recent_text.return_value = "still working"

        result = _subagent_progress_gate(payload, state, True)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    @patch("dispatch_enforcer._subagent_recent_assistant_text")
    @patch("dispatch_enforcer._dispatch_worker_type_for")
    @patch("dispatch_enforcer._stall_thresholds_for")
    @patch("dispatch_enforcer._strict_stall_profile_for")
    @patch("dispatch_enforcer.config_get")
    @patch("dispatch_enforcer._warn")
    @patch("dispatch_enforcer._bump")
    def test_warn_mode_does_not_deny(
        self,
        mock_bump,
        mock_warn,
        mock_config_get,
        mock_strict_profile,
        mock_thresholds,
        mock_worker_type,
        mock_recent_text,
    ):
        """Warn mode returns None (does not deny) and bumps gate_warn_strict_stall."""
        from dispatch_enforcer import _subagent_progress_gate

        payload = self._make_payload()
        state = self._make_state()

        mock_worker_type.return_value = "shell"
        mock_thresholds.return_value = (6, 180)
        mock_strict_profile.return_value = {
            "max_stall_markers": 3,
            "max_stall_seconds": 90,
            "heartbeat_seconds": 30,
        }
        mock_config_get.return_value = "warn"

        base_time = time.time()
        state["subagent_progress"]["agent_123"] = {
            "last_marker_ts": base_time - 95,  # Would trip in enforce mode
            "stall_count": 0,
            "miss_count": 3,
        }
        mock_recent_text.return_value = "still working"

        result = _subagent_progress_gate(payload, state, True)
        assert result is None  # Warn mode: allow
        mock_bump.assert_called_once_with(state, "gate_warn_strict_stall")

    @patch("dispatch_enforcer._subagent_recent_assistant_text")
    @patch("dispatch_enforcer._dispatch_worker_type_for")
    @patch("dispatch_enforcer._stall_thresholds_for")
    @patch("dispatch_enforcer._strict_stall_profile_for")
    @patch("dispatch_enforcer.config_get")
    def test_mode_off_disables_strict_profile(
        self,
        mock_config_get,
        mock_strict_profile,
        mock_thresholds,
        mock_worker_type,
        mock_recent_text,
    ):
        """Mode 'off' causes _strict_stall_profile_for to return None."""
        from dispatch_enforcer import _subagent_progress_gate

        payload = self._make_payload()
        state = self._make_state()

        mock_worker_type.return_value = "shell"
        mock_thresholds.return_value = (6, 180)
        # Strict profile returns None when mode is "off"
        mock_strict_profile.return_value = None
        mock_config_get.return_value = "off"

        base_time = time.time()
        state["subagent_progress"]["agent_123"] = {
            "last_marker_ts": base_time - 95,  # Would trip in enforce
            "stall_count": 0,
            "miss_count": 3,
        }
        mock_recent_text.return_value = "continuing"

        # Falls back to top-level thresholds only (6 markers/180s)
        # Should not deny because stall_count < 6
        result = _subagent_progress_gate(payload, state, True)
        # No denial because top-level threshold (6 markers) not yet hit
        assert result is None

    @patch("dispatch_enforcer._subagent_recent_assistant_text")
    @patch("dispatch_enforcer._dispatch_worker_type_for")
    @patch("dispatch_enforcer._stall_thresholds_for")
    @patch("dispatch_enforcer._strict_stall_profile_for")
    @patch("dispatch_enforcer.config_get")
    def test_mutation_worker_at_600s_top_level_unchanged(
        self,
        mock_config_get,
        mock_strict_profile,
        mock_thresholds,
        mock_worker_type,
        mock_recent_text,
    ):
        """Mutation worker at 600s (top-level default) denies; strict profile doesn't interfere."""
        from dispatch_enforcer import _subagent_progress_gate

        payload = self._make_payload()
        state = self._make_state()

        # Mutation gets different thresholds (top-level overrides)
        mock_worker_type.return_value = "mutation"
        mock_thresholds.return_value = (6, 600)  # 600s for mutation
        mock_strict_profile.return_value = None  # No strict profile for mutation
        mock_config_get.return_value = "enforce"

        base_time = time.time()
        state["subagent_progress"]["agent_123"] = {
            "last_marker_ts": base_time - 605,  # 605s > 600s max_seconds
            "stall_count": 0,
            "miss_count": 0,
        }
        mock_recent_text.return_value = "working"

        result = _subagent_progress_gate(payload, state, True)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    @patch("dispatch_enforcer._subagent_recent_assistant_text")
    @patch("dispatch_enforcer._dispatch_worker_type_for")
    @patch("dispatch_enforcer._stall_thresholds_for")
    @patch("dispatch_enforcer._strict_stall_profile_for")
    @patch("dispatch_enforcer.config_get")
    def test_read_worker_at_600s_top_level_unchanged(
        self,
        mock_config_get,
        mock_strict_profile,
        mock_thresholds,
        mock_worker_type,
        mock_recent_text,
    ):
        """Read worker at 600s (top-level default) denies; strict profile doesn't interfere."""
        from dispatch_enforcer import _subagent_progress_gate

        payload = self._make_payload()
        state = self._make_state()

        mock_worker_type.return_value = "read"
        mock_thresholds.return_value = (6, 600)  # 600s for read
        mock_strict_profile.return_value = None
        mock_config_get.return_value = "enforce"

        base_time = time.time()
        state["subagent_progress"]["agent_123"] = {
            "last_marker_ts": base_time - 610,  # 610s > 600s
            "stall_count": 0,
            "miss_count": 0,
        }
        mock_recent_text.return_value = "reading"

        result = _subagent_progress_gate(payload, state, True)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    @patch("dispatch_enforcer._subagent_recent_assistant_text")
    @patch("dispatch_enforcer._dispatch_worker_type_for")
    @patch("dispatch_enforcer._stall_thresholds_for")
    @patch("dispatch_enforcer._strict_stall_profile_for")
    @patch("dispatch_enforcer.config_get")
    def test_unresolved_worker_type_uses_top_level_defaults(
        self,
        mock_config_get,
        mock_strict_profile,
        mock_thresholds,
        mock_worker_type,
        mock_recent_text,
    ):
        """Unresolved worker type (None) falls back to top-level (6 markers/180s)."""
        from dispatch_enforcer import _subagent_progress_gate

        payload = self._make_payload()
        state = self._make_state()

        mock_worker_type.return_value = None  # Unresolved
        mock_thresholds.return_value = (6, 180)  # Top-level defaults
        mock_strict_profile.return_value = None
        mock_config_get.return_value = "enforce"

        base_time = time.time()
        state["subagent_progress"]["agent_123"] = {
            "last_marker_ts": base_time - 185,  # 185s > 180s
            "stall_count": 0,
            "miss_count": 0,
        }
        mock_recent_text.return_value = "working"

        result = _subagent_progress_gate(payload, state, True)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    @patch("dispatch_enforcer._subagent_recent_assistant_text")
    @patch("dispatch_enforcer._dispatch_worker_type_for")
    @patch("dispatch_enforcer._stall_thresholds_for")
    @patch("dispatch_enforcer._strict_stall_profile_for")
    @patch("dispatch_enforcer.config_get")
    def test_progress_marker_resets_miss_count(
        self,
        mock_config_get,
        mock_strict_profile,
        mock_thresholds,
        mock_worker_type,
        mock_recent_text,
    ):
        """Progress marker in assistant text resets miss_count and stall_count."""
        from dispatch_enforcer import _subagent_progress_gate

        payload = self._make_payload()
        state = self._make_state()

        mock_worker_type.return_value = "shell"
        mock_thresholds.return_value = (6, 180)
        mock_strict_profile.return_value = {
            "max_stall_markers": 3,
            "max_stall_seconds": 90,
            "heartbeat_seconds": 30,
        }
        mock_config_get.return_value = "enforce"

        base_time = time.time()
        state["subagent_progress"]["agent_123"] = {
            "last_marker_ts": base_time - 95,
            "stall_count": 5,
            "miss_count": 2,
        }
        # Marker found in text
        mock_recent_text.return_value = "Progress: completed step 1"

        result = _subagent_progress_gate(payload, state, True)
        assert result is None
        assert state["subagent_progress"]["agent_123"]["miss_count"] == 0
        assert state["subagent_progress"]["agent_123"]["stall_count"] == 0

    @patch("dispatch_enforcer._subagent_recent_assistant_text")
    @patch("dispatch_enforcer._dispatch_worker_type_for")
    @patch("dispatch_enforcer._stall_thresholds_for")
    @patch("dispatch_enforcer._strict_stall_profile_for")
    @patch("dispatch_enforcer.config_get")
    def test_orchestrator_worker_enforce_mode(
        self,
        mock_config_get,
        mock_strict_profile,
        mock_thresholds,
        mock_worker_type,
        mock_recent_text,
    ):
        """Orchestrator worker with strict profile trips at 3 misses/90s (same as shell)."""
        from dispatch_enforcer import _subagent_progress_gate

        payload = self._make_payload()
        state = self._make_state()

        mock_worker_type.return_value = "orchestrator"
        mock_thresholds.return_value = (6, 180)
        mock_strict_profile.return_value = {
            "max_stall_markers": 3,
            "max_stall_seconds": 90,
            "heartbeat_seconds": 30,
        }
        mock_config_get.return_value = "enforce"

        base_time = time.time()
        state["subagent_progress"]["agent_123"] = {
            "last_marker_ts": base_time - 95,  # 95s elapsed > 90s max
            "stall_count": 0,
            "miss_count": 1,
        }
        mock_recent_text.return_value = "orchestrating"

        result = _subagent_progress_gate(payload, state, True)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
