"""Test _build_subagent_stop_telemetry_row outcome derivation from manifest.

DGC-006b: Verify result_status is correctly computed from manifest (not from
unpopulated registry), and outcome is properly derived from result_status.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from check_routing import _build_subagent_stop_telemetry_row


@pytest.fixture(autouse=True)
def _clear_session_env(monkeypatch):
    """Clear CURSOR_SESSION_ID and CLAUDE_SESSION_ID to prevent live env vars from breaking tests."""
    monkeypatch.delenv("CURSOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)


def test_subagent_stop_outcome_from_manifest_complete():
    """Test outcome derivation when manifest status is COMPLETE."""
    payload = {
        "agent_id": "test-agent-1",
        "conversation_id": "test-conversation",
        "model": "haiku",
        "last_assistant_message": "Some text\n\n```continuation-manifest\n{\"status\": \"COMPLETE\", \"depth\": 1}\n```",
    }

    state = {
        "sessions": {},
        "subagent_progress": {},
        "counters": {},
    }

    with patch("check_routing.load_dispatch_registry", return_value={"sessions": {}}):
        row = _build_subagent_stop_telemetry_row(payload, "test-conversation", state)

    assert row["result_status"] == "COMPLETE"
    assert row["outcome"] == "success"
    assert row["outcome_source"] == "result_status"


def test_subagent_stop_outcome_from_manifest_partial():
    """Test outcome derivation when manifest status is PARTIAL."""
    payload = {
        "agent_id": "test-agent-2",
        "conversation_id": "test-conversation",
        "model": "haiku",
        "last_assistant_message": "Some text\n\n```continuation-manifest\n{\"status\": \"PARTIAL\", \"depth\": 1}\n```",
    }

    state = {
        "sessions": {},
        "subagent_progress": {},
        "counters": {},
    }

    with patch("check_routing.load_dispatch_registry", return_value={"sessions": {}}):
        row = _build_subagent_stop_telemetry_row(payload, "test-conversation", state)

    assert row["result_status"] == "PARTIAL"
    assert row["outcome"] == "partial"
    assert row["outcome_source"] == "result_status"


def test_subagent_stop_outcome_from_stall_killed():
    """Test outcome derivation when stall_killed is True (no manifest status)."""
    payload = {
        "agent_id": "test-agent-3",
        "conversation_id": "test-conversation",
        "model": "haiku",
        "last_assistant_message": "Some text\n\n```continuation-manifest\n{\"status\": \"UNKNOWN\", \"depth\": 1}\n```",
    }

    state = {
        "sessions": {},
        "subagent_progress": {
            "test-agent-3": {"stall_killed": True, "stall_count": 5}
        },
        "counters": {},
    }

    with patch("check_routing.load_dispatch_registry", return_value={"sessions": {}}):
        row = _build_subagent_stop_telemetry_row(payload, "test-conversation", state)

    # Matches event_subagent_stop: stall without a COMPLETE/PARTIAL manifest stays UNKNOWN.
    assert row["result_status"] == "UNKNOWN"
    assert row["outcome"] == "failed"
    assert row["outcome_source"] == "stall"


def test_subagent_stop_outcome_unknown_no_manifest():
    """Test outcome is unknown when no manifest found and no stall_killed."""
    payload = {
        "agent_id": "test-agent-4",
        "conversation_id": "test-conversation",
        "model": "haiku",
        "last_assistant_message": "Some text without manifest",
    }

    state = {
        "sessions": {},
        "subagent_progress": {},
        "counters": {},
    }

    with patch("check_routing.load_dispatch_registry", return_value={"sessions": {}}):
        row = _build_subagent_stop_telemetry_row(payload, "test-conversation", state)

    assert row["result_status"] == "UNKNOWN"
    assert row["outcome"] == "unknown"
    assert row["outcome_source"] == "none"


def test_subagent_stop_stall_downgrade_complete_to_partial():
    """Test stall_killed downgrades COMPLETE result_status to PARTIAL."""
    payload = {
        "agent_id": "test-agent-5",
        "conversation_id": "test-conversation",
        "model": "haiku",
        "last_assistant_message": "Some text\n\n```continuation-manifest\n{\"status\": \"COMPLETE\", \"depth\": 1}\n```",
    }

    state = {
        "sessions": {},
        "subagent_progress": {
            "test-agent-5": {"stall_killed": True, "stall_count": 3}
        },
        "counters": {},
    }

    with patch("check_routing.load_dispatch_registry", return_value={"sessions": {}}):
        row = _build_subagent_stop_telemetry_row(payload, "test-conversation", state)

    # When stall_killed and manifest_status is COMPLETE, result_status should be PARTIAL
    assert row["result_status"] == "PARTIAL"
    assert row["outcome"] == "partial"
