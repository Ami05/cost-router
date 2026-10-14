"""E2E test: subagent Routing: DIRECT not shadowed by parent Routing: FO."""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_routing import _scan_role_backward


def _make_transcript_lines(records: list[dict]) -> list[str]:
    return [json.dumps(r) for r in records]


def _assistant_record(text: str, agent_id: str = "") -> dict:
    record = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    if agent_id:
        record["agentId"] = agent_id
    return record


def _user_prompt_record(text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
    }


class TestAgentIdScoping:
    def test_child_direct_not_shadowed_by_parent_fo(self):
        lines = _make_transcript_lines([
            _user_prompt_record("implement the feature"),
            _assistant_record("Routing: FO - dispatching subagent", agent_id="parent-1"),
            _assistant_record("Routing: DIRECT - editing file", agent_id="child-1"),
        ])
        role = _scan_role_backward(lines, agent_id="child-1")
        assert role == "DIRECT"

    def test_parent_fo_still_blocks_parent(self):
        lines = _make_transcript_lines([
            _user_prompt_record("implement the feature"),
            _assistant_record("Routing: FO - dispatching subagent", agent_id="parent-1"),
            _assistant_record("Routing: DIRECT - editing file", agent_id="child-1"),
        ])
        role = _scan_role_backward(lines, agent_id="parent-1")
        assert role == "FO"

    def test_no_agent_id_falls_back_to_last_match(self):
        lines = _make_transcript_lines([
            _user_prompt_record("implement the feature"),
            _assistant_record("Routing: FO - dispatching subagent", agent_id="parent-1"),
            _assistant_record("Routing: DIRECT - editing file", agent_id="child-1"),
        ])
        role = _scan_role_backward(lines, agent_id=None)
        assert role == "DIRECT"

    def test_child_with_no_routing_returns_none(self):
        lines = _make_transcript_lines([
            _user_prompt_record("implement the feature"),
            _assistant_record("Routing: FO - dispatching subagent", agent_id="parent-1"),
            _assistant_record("I will read the file now", agent_id="child-1"),
        ])
        role = _scan_role_backward(lines, agent_id="child-1")
        assert role is None

    def test_stops_at_user_prompt_boundary(self):
        lines = _make_transcript_lines([
            _assistant_record("Routing: FO - old turn", agent_id="parent-1"),
            _user_prompt_record("new request"),
            _assistant_record("Routing: DIRECT - new turn", agent_id="parent-1"),
        ])
        role = _scan_role_backward(lines, agent_id="parent-1")
        assert role == "DIRECT"

    def test_safety_cap_limits_parsing(self):
        lines = _make_transcript_lines(
            [_assistant_record("Routing: DIRECT - buried deep", agent_id="child-1")]
            + [_assistant_record(f"message {i}", agent_id="child-1") for i in range(30)]
        )
        role = _scan_role_backward(lines, agent_id="child-1")
        assert role is None  # beyond 20-line cap

    def test_backward_compat_no_agent_id_in_records(self):
        lines = _make_transcript_lines([
            _user_prompt_record("do something"),
            _assistant_record("Routing: DIRECT - simple edit"),
        ])
        role = _scan_role_backward(lines, agent_id=None)
        assert role == "DIRECT"
