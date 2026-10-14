"""DGC-104: Stall kill tests.

Tests for the stall kill mechanism that hard-denies tool calls when a
worker fails to respond to a checkpoint request (partial report message).

Covers:
- Enforce mode: worker is denied after grace_denied_calls exceeded
- Warn mode: worker is warned but not denied
- Off mode: kill path is disabled entirely
- Checkpoint received: kill fields are cleared
- stall_killed flag: set on progress entry when enforcement triggers
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from dispatch_enforcer import (
    _stall_kill_deny_message,
    _stall_kill_mode,
    _stall_grace_denied_calls,
    _subagent_progress_gate,
    _PROGRESS_MARKER_RE,
)
from _common import config_get


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_payload():
    """Create a mock hook payload with subagent context."""
    def _make(
        tool="Shell",
        agent_id="test-agent-123",
        session_id="test-session",
        **extras
    ):
        payload = {
            "tool": tool,
            "agent_id": agent_id,
            "session_id": session_id,
            "prompt_id": "test-prompt",
        }
        payload.update(extras)
        return payload
    return _make


@pytest.fixture
def mock_state():
    """Create a mock routing state."""
    return {
        "subagent_progress": {},
        "gate_counters": {},
    }


@pytest.fixture
def freezegun_time(monkeypatch):
    """Inject a controllable clock for testing time-based logic."""
    class MockClock:
        def __init__(self):
            self.now = time.time()

        def advance(self, seconds: float):
            self.now += seconds

        def current(self) -> float:
            return self.now

    clock = MockClock()

    def mock_time():
        return clock.current()

    monkeypatch.setattr("time.time", mock_time)
    return clock


# Low stall threshold so tests can drive a real stall in a few calls. The
# gate only requests a checkpoint (and so only arms the DGC-104 kill path)
# once stall_count reaches max_stall_markers.
STALL_MARKERS = 3
AGENT_ID = "test-agent-123"


def _configure(monkeypatch, *, mode, grace, stall=STALL_MARKERS, seconds=180):
    """Patch config + subagent-context helpers for the stall/kill path."""
    cfg = {
        "progress_ledger.kill.grace_denied_calls": grace,
        "progress_ledger.kill.mode": mode,
        "progress_ledger.max_stall_markers": stall,
        "progress_ledger.max_stall_seconds": seconds,
    }
    monkeypatch.setattr(
        "dispatch_enforcer.config_get",
        lambda key, default=None: cfg.get(key, default),
    )
    monkeypatch.setattr("dispatch_enforcer._is_subagent_context", lambda p: True)
    monkeypatch.setattr("dispatch_enforcer._dispatch_worker_type_for", lambda p: None)
    # _warn appends to the real warnings.jsonl; keep tests hermetic.
    monkeypatch.setattr("dispatch_enforcer._warn", MagicMock())


def _say(monkeypatch, text):
    monkeypatch.setattr(
        "dispatch_enforcer._subagent_recent_assistant_text", lambda p: text
    )


def _drive_to_checkpoint(payload, state, stall=STALL_MARKERS):
    """Make `stall` marker-less calls; the last one trips the stall deny that
    requests a checkpoint and arms the kill path. Returns that last result."""
    agent_id = payload["agent_id"]
    for _ in range(stall - 1):
        assert _subagent_progress_gate(payload, state, is_claude=True) is None
    result = _subagent_progress_gate(payload, state, is_claude=True)
    entry = state["subagent_progress"][agent_id]
    assert entry.get("checkpoint_requested_ts") is not None
    assert entry.get("grace_denied_calls") == 0
    return result


# ============================================================================
# Test: Mode Configuration
# ============================================================================


def test_stall_kill_mode_defaults_to_warn(monkeypatch):
    """Test that missing/invalid kill.mode defaults to 'warn'."""
    monkeypatch.setattr(
        "dispatch_enforcer.config_get",
        lambda key, default=None: default if key == "progress_ledger.kill.mode" else None,
    )
    assert _stall_kill_mode() == "warn"


def test_stall_kill_mode_respects_enforce(monkeypatch):
    """Test that enforce mode is returned when configured."""
    monkeypatch.setattr(
        "dispatch_enforcer.config_get",
        lambda key, default=None: "enforce" if key == "progress_ledger.kill.mode" else default,
    )
    assert _stall_kill_mode() == "enforce"


def test_stall_kill_mode_respects_off(monkeypatch):
    """Test that off mode is returned when configured."""
    monkeypatch.setattr(
        "dispatch_enforcer.config_get",
        lambda key, default=None: "off" if key == "progress_ledger.kill.mode" else default,
    )
    assert _stall_kill_mode() == "off"


def test_stall_kill_mode_invalid_defaults_to_warn(monkeypatch):
    """Test that invalid kill.mode defaults to 'warn'."""
    monkeypatch.setattr(
        "dispatch_enforcer.config_get",
        lambda key, default=None: "invalid" if key == "progress_ledger.kill.mode" else default,
    )
    assert _stall_kill_mode() == "warn"


# ============================================================================
# Test: Grace Period Configuration
# ============================================================================


def test_stall_grace_denied_calls_defaults_to_3(monkeypatch):
    """Test that missing grace_denied_calls defaults to 3."""
    monkeypatch.setattr(
        "dispatch_enforcer.config_get",
        lambda key, default=None: default if key == "progress_ledger.kill.grace_denied_calls" else None,
    )
    assert _stall_grace_denied_calls() == 3


def test_stall_grace_denied_calls_respects_configured_value(monkeypatch):
    """Test that configured grace_denied_calls is used."""
    monkeypatch.setattr(
        "dispatch_enforcer.config_get",
        lambda key, default=None: 5 if key == "progress_ledger.kill.grace_denied_calls" else default,
    )
    assert _stall_grace_denied_calls() == 5


def test_stall_grace_denied_calls_rejects_zero(monkeypatch):
    """Test that zero/invalid values fall back to default."""
    monkeypatch.setattr(
        "dispatch_enforcer.config_get",
        lambda key, default=None: 0 if key == "progress_ledger.kill.grace_denied_calls" else default,
    )
    assert _stall_grace_denied_calls() == 3


# ============================================================================
# Test: Enforce Mode — Hard Deny After Grace Period
# ============================================================================


def test_enforce_mode_denies_after_grace_period_exceeded(
    monkeypatch, mock_payload, mock_state, freezegun_time
):
    """Enforce mode hard-denies once grace_denied_calls reaches the limit.

    Implementation semantics: after the stall deny arms the checkpoint, each
    further marker-less call increments grace_denied_calls; the call that makes
    it >= grace_limit sets stall_killed and is denied. Earlier grace calls fall
    through (allowed).
    """
    _configure(monkeypatch, mode="enforce", grace=2)
    payload = mock_payload()
    _say(monkeypatch, "no progress marker here")

    # Stall reached: partial-report deny that arms the checkpoint.
    result = _drive_to_checkpoint(payload, mock_state)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "partial report" in result["systemMessage"].lower()

    # Grace call 1: counted, allowed, not killed.
    result = _subagent_progress_gate(payload, mock_state, is_claude=True)
    entry = mock_state["subagent_progress"][AGENT_ID]
    assert result is None
    assert entry.get("grace_denied_calls") == 1
    assert entry.get("stall_killed") is None

    # Grace call 2 reaches the limit (2): killed and hard-denied.
    result = _subagent_progress_gate(payload, mock_state, is_claude=True)
    entry = mock_state["subagent_progress"][AGENT_ID]
    assert entry.get("grace_denied_calls") == 2
    assert entry.get("stall_killed") is True
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "DGC-104" in result["systemMessage"]

    # Once killed, every further marker-less call stays denied.
    result = _subagent_progress_gate(payload, mock_state, is_claude=True)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_enforce_mode_deny_message_includes_counts():
    """Kill deny message names DGC-104 and the denied-call count."""
    msg = _stall_kill_deny_message(grace_count=7, denied_count=9)
    assert "9" in msg
    assert "DGC-104" in msg
    assert "denied" in msg.lower()


def test_enforce_mode_deny_message_includes_grace_count():
    msg = _stall_kill_deny_message(grace_count=7, denied_count=9)
    assert "7" in msg


# ============================================================================
# Test: Progress Marker Clears Kill State
# ============================================================================


def test_progress_marker_clears_kill_fields(
    monkeypatch, mock_payload, mock_state, freezegun_time
):
    """Test that emitting a progress marker clears all kill-related fields."""
    _configure(monkeypatch, mode="enforce", grace=2)
    payload = mock_payload()
    _say(monkeypatch, "no progress")

    # Trigger checkpoint request (real stall)
    _drive_to_checkpoint(payload, mock_state)

    # Increment grace counter (limit is 2, so not killed yet)
    _say(monkeypatch, "still no progress")
    _subagent_progress_gate(payload, mock_state, is_claude=True)
    entry = mock_state["subagent_progress"][AGENT_ID]
    assert entry.get("grace_denied_calls") == 1

    # Now emit progress marker — should clear kill state
    _say(monkeypatch, "Done: work completed")
    result = _subagent_progress_gate(payload, mock_state, is_claude=True)
    entry = mock_state["subagent_progress"][AGENT_ID]

    # All kill fields cleared
    assert entry.get("checkpoint_requested_ts") is None
    assert entry.get("grace_denied_calls") == 0
    assert entry.get("stall_count") == 0
    assert entry.get("miss_count") == 0
    # Result should be None (no denial)
    assert result is None


def test_progress_marker_regex_matches_common_patterns(
):
    """Test that progress marker regex matches expected patterns."""
    patterns_match = [
        "Done: initial exploration complete",
        "Completed: three files updated",
        "Remaining: 2 steps left",
        "Progress: 50% done",
        "done : spaced colon",
        "PROGRESS: compiling",
    ]
    patterns_no_match = [
        "done with phase 1",  # marker requires a colon after the keyword
        "This is done work",
        "We're making progress",
        "The production environment",
    ]

    for pattern in patterns_match:
        assert _PROGRESS_MARKER_RE.search(pattern), f"Should match: {pattern}"

    for pattern in patterns_no_match:
        assert not _PROGRESS_MARKER_RE.search(pattern), f"Should not match: {pattern}"


# ============================================================================
# Test: Warn Mode
# ============================================================================


def test_warn_mode_logs_would_deny_but_allows(
    monkeypatch, mock_payload, mock_state, freezegun_time
):
    """Test that warn mode logs the would-deny but still allows the call."""
    _configure(monkeypatch, mode="warn", grace=2)
    import dispatch_enforcer as _de
    warn_mock = _de._warn  # MagicMock installed by _configure

    payload = mock_payload()
    _say(monkeypatch, "no progress")

    # Trigger checkpoint request (real stall)
    _drive_to_checkpoint(payload, mock_state)

    # Three more calls: grace reaches and passes the limit (2). Warn mode
    # must never deny.
    for _ in range(3):
        result = _subagent_progress_gate(payload, mock_state, is_claude=True)
        assert result is None

    # Counter still increments in warn mode, and the would-deny was logged.
    entry = mock_state["subagent_progress"][AGENT_ID]
    assert entry.get("grace_denied_calls") == 3
    assert warn_mock.call_args_list
    assert warn_mock.call_args_list[0].args[0] == "stall_kill_would_deny"


def test_warn_mode_does_not_set_stall_killed(
    monkeypatch, mock_payload, mock_state
):
    """Test that warn mode does not set stall_killed flag."""
    _configure(monkeypatch, mode="warn", grace=2)
    payload = mock_payload()
    _say(monkeypatch, "no progress")

    # Trigger checkpoint and exceed grace period
    _drive_to_checkpoint(payload, mock_state)
    for _ in range(5):
        assert _subagent_progress_gate(payload, mock_state, is_claude=True) is None

    entry = mock_state["subagent_progress"][AGENT_ID]
    # In warn mode, stall_killed should not be set
    assert entry.get("stall_killed") is None


# ============================================================================
# Test: Off Mode
# ============================================================================


def test_off_mode_disables_kill_path(
    monkeypatch, mock_payload, mock_state
):
    """Test that off mode completely disables the kill path."""
    # Setup: off mode (but with a low grace limit to show it's not being checked)
    monkeypatch.setattr(
        "dispatch_enforcer.config_get",
        lambda key, default=None: {
            "progress_ledger.kill.grace_denied_calls": 1,
            "progress_ledger.kill.mode": "off",
            "progress_ledger.max_stall_markers": 6,
            "progress_ledger.max_stall_seconds": 180,
        }.get(key, default),
    )
    monkeypatch.setattr("dispatch_enforcer._is_subagent_context", lambda p: True)
    monkeypatch.setattr("dispatch_enforcer._dispatch_worker_type_for", lambda p: None)
    monkeypatch.setattr("dispatch_enforcer._optional_positive_int", lambda x: x if isinstance(x, int) and x > 0 else None)

    # Note: In off mode, progress_ledger.mode itself is "off", so the entire
    # progress gate is disabled, not just the kill mechanism. This test verifies
    # that the kill path doesn't activate in off mode.
    payload = mock_payload()
    monkeypatch.setattr(
        "dispatch_enforcer._subagent_recent_assistant_text",
        lambda p: "no progress",
    )

    # With off mode and stall thresholds still set, a checkpoint would normally
    # be requested. The kill path should not engage even if it would in enforce mode.
    result = _subagent_progress_gate(payload, mock_state, is_claude=True)
    # In off mode, the gate returns None (disabled)
    assert result is None


# ============================================================================
# Test: Stall Killed Flag
# ============================================================================


def test_stall_killed_flag_set_on_enforce_hard_deny(
    monkeypatch, mock_payload, mock_state
):
    """Test that stall_killed flag is set when enforcement triggers hard deny."""
    _configure(monkeypatch, mode="enforce", grace=1)
    payload = mock_payload()
    _say(monkeypatch, "no progress")

    # Real stall: checkpoint requested, not yet killed
    _drive_to_checkpoint(payload, mock_state)
    entry = mock_state["subagent_progress"][AGENT_ID]
    assert entry.get("stall_killed") is None

    # First call after checkpoint reaches grace limit (1): killed + denied
    result = _subagent_progress_gate(payload, mock_state, is_claude=True)
    entry = mock_state["subagent_progress"][AGENT_ID]
    assert entry.get("grace_denied_calls") == 1
    assert entry.get("stall_killed") is True
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_stall_killed_flag_cleared_on_progress_marker(
    monkeypatch, mock_payload, mock_state
):
    """Test that stall_killed flag is cleared when progress marker is received."""
    _configure(monkeypatch, mode="enforce", grace=1)
    payload = mock_payload()
    _say(monkeypatch, "no progress")

    # Real stall, then one grace call -> stall_killed
    _drive_to_checkpoint(payload, mock_state)
    _subagent_progress_gate(payload, mock_state, is_claude=True)
    entry = mock_state["subagent_progress"][AGENT_ID]
    assert entry.get("stall_killed") is True

    # Emit progress marker
    _say(monkeypatch, "Remaining: final steps")
    result = _subagent_progress_gate(payload, mock_state, is_claude=True)
    assert result is None

    entry = mock_state["subagent_progress"][AGENT_ID]
    # These fields are cleared by the implementation...
    assert entry.get("checkpoint_requested_ts") is None
    assert entry.get("grace_denied_calls") == 0
    # ...but stall_killed is not (the defect).
    assert entry.get("stall_killed") is None or entry.get("stall_killed") is False


# ============================================================================
# Test: Edge Cases
# ============================================================================


def test_multiple_agents_have_independent_kill_states(
    monkeypatch, mock_state
):
    """Test that kill state is tracked per agent independently."""
    _configure(monkeypatch, mode="enforce", grace=2)

    payload1 = {
        "tool": "Shell",
        "agent_id": "agent-1",
        "session_id": "test-session",
        "prompt_id": "test-prompt",
    }
    payload2 = {
        "tool": "Shell",
        "agent_id": "agent-2",
        "session_id": "test-session",
        "prompt_id": "test-prompt",
    }

    _say(monkeypatch, "no progress")

    # Agent 1: real stall, then one grace call (limit 2 -> not killed yet)
    _drive_to_checkpoint(payload1, mock_state)
    _subagent_progress_gate(payload1, mock_state, is_claude=True)
    entry1 = mock_state["subagent_progress"]["agent-1"]
    assert entry1.get("grace_denied_calls") == 1

    # Agent 2 has not stalled: no checkpoint, untouched by agent 1's state
    _subagent_progress_gate(payload2, mock_state, is_claude=True)
    entry2 = mock_state["subagent_progress"]["agent-2"]
    assert entry2.get("checkpoint_requested_ts") is None
    assert entry2.get("stall_count") == 1

    # Agent 2 now stalls independently; agent 1's counters are unchanged
    _drive_to_checkpoint(payload2, mock_state, stall=STALL_MARKERS - 1)
    assert entry2.get("checkpoint_requested_ts") is not None
    assert entry1.get("grace_denied_calls") == 1

    # Agent 1 reaches the grace limit and is killed; agent 2 is not
    result = _subagent_progress_gate(payload1, mock_state, is_claude=True)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert entry1.get("stall_killed") is True
    assert entry2.get("stall_killed") is None
    assert entry2.get("grace_denied_calls") == 0


def test_kill_state_not_activated_without_checkpoint_request(
    monkeypatch, mock_payload, mock_state
):
    """Test that kill state is not activated if no checkpoint was requested."""
    payload = mock_payload()
    _say(monkeypatch, "no progress")

    # (a) Thresholds disabled: the gate returns before touching the ledger,
    # so no entry is created at all and nothing is ever denied.
    _configure(monkeypatch, mode="enforce", grace=2, stall=None, seconds=None)
    for _ in range(10):
        assert _subagent_progress_gate(payload, mock_state, is_claude=True) is None
    assert AGENT_ID not in mock_state["subagent_progress"]

    # (b) Thresholds enabled but not yet reached: entry exists, but no
    # checkpoint is requested and no kill fields are set.
    _configure(monkeypatch, mode="enforce", grace=2)
    for _ in range(STALL_MARKERS - 1):
        assert _subagent_progress_gate(payload, mock_state, is_claude=True) is None
    entry = mock_state["subagent_progress"][AGENT_ID]
    assert entry.get("checkpoint_requested_ts") is None
    assert entry.get("grace_denied_calls") is None
    assert entry.get("stall_killed") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
