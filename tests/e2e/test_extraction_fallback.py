"""E2E tests: extraction_fallback event emission for various failure modes.

Covers _tier_of_model_with_event (check_routing.py) and the payload
extraction functions in _payload.py that produce conditions under which
extraction_fallback events are (or should be) emitted:
  - model_none: model absent from payload → tier unknown → event emitted
  - tier_unrecognized: non-empty model matches no known tier → event emitted
  - prompt_text_empty: no prompt/text/message key → empty string returned
  - tool_name_missing: no tool_name/tool/toolName key → None returned
  - subagent_id_missing: no agent_id/subagent_id/agentId/subagentId key → None returned

Description: Test extraction_fallback events for model_none, prompt_text_empty,
tier_unrecognized, tool_name_missing, subagent_id_missing conditions
Author: cost-router-phase3
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_routing as cr  # noqa: E402
import session_state as ss  # noqa: E402
from _payload import (  # noqa: E402
    _extract_prompt_text,
    _extract_tool_name,
    _extract_subagent_id,
    _extract_model,
    normalize_payload,
)
from _model_utils import tier_of_model  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Isolate all data-file writes to a temp directory."""
    for mod in (cr, ss):
        monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(mod, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
        monkeypatch.setattr(mod, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")
    monkeypatch.setattr(cr, "RESUME_STATE_FILE", tmp_path / "resume_state.json")
    return tmp_path


# ---------------------------------------------------------------------------
# model_none — extraction_fallback emitted when model is absent
# ---------------------------------------------------------------------------

class TestModelNone:
    def test_tier_of_model_none_returns_unknown(self):
        """tier_of_model(None) must return 'unknown'."""
        assert tier_of_model(None) == "unknown"

    def test_tier_of_model_empty_string_returns_unknown(self):
        """tier_of_model('') must return 'unknown'."""
        assert tier_of_model("") == "unknown"

    def test_extraction_fallback_event_emitted_for_model_none(self):
        """_tier_of_model_with_event(None) must call append_event with type=extraction_fallback reason=model_none."""
        emitted = []
        with patch("check_routing.append_event", side_effect=emitted.append):
            result = cr._tier_of_model_with_event(None, session_id="test-sess-001")
        assert result == "unknown"
        assert any(
            e.get("type") == "extraction_fallback" and e.get("reason") == "model_none"
            for e in emitted
        ), f"Expected extraction_fallback/model_none in emitted events; got {emitted}"

    def test_extraction_fallback_event_emitted_for_empty_model(self):
        """_tier_of_model_with_event('') must emit extraction_fallback/model_none."""
        emitted = []
        with patch("check_routing.append_event", side_effect=emitted.append):
            result = cr._tier_of_model_with_event("", session_id="test-sess-002")
        assert result == "unknown"
        assert any(
            e.get("type") == "extraction_fallback" and e.get("reason") == "model_none"
            for e in emitted
        ), f"Expected extraction_fallback/model_none; got {emitted}"

    def test_payload_with_no_model_key_extracts_none(self):
        """Payload missing model/model_name/modelName → _extract_model returns None."""
        raw = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Edit"}
        assert _extract_model(raw) is None

    def test_normalize_payload_model_none_for_missing_key(self):
        """normalize_payload on a modelless payload must set .model to None."""
        raw = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Edit"}
        p = normalize_payload(raw)
        assert p.model is None


# ---------------------------------------------------------------------------
# tier_unrecognized — separate event type emitted for non-empty unknown model
# ---------------------------------------------------------------------------

class TestTierUnrecognized:
    def test_tier_of_model_unrecognized_string_returns_unknown(self):
        """tier_of_model with a novel model name not in FAST/REASONING markers → 'unknown'."""
        assert tier_of_model("llama-4-scout") == "unknown"

    def test_tier_unrecognized_event_emitted_for_novel_model(self):
        """_tier_of_model_with_event with a non-empty unrecognized model emits type=tier_unrecognized."""
        emitted = []
        with patch("check_routing.append_event", side_effect=emitted.append):
            result = cr._tier_of_model_with_event("llama-4-scout", session_id="test-sess-003")
        assert result == "unknown"
        assert any(
            e.get("type") == "tier_unrecognized" and e.get("model") == "llama-4-scout"
            for e in emitted
        ), f"Expected tier_unrecognized with model; got {emitted}"

    def test_tier_unrecognized_not_extraction_fallback(self):
        """tier_unrecognized event must NOT be type=extraction_fallback."""
        emitted = []
        with patch("check_routing.append_event", side_effect=emitted.append):
            cr._tier_of_model_with_event("grok-3-mini", session_id="test-sess-004")
        assert not any(e.get("type") == "extraction_fallback" for e in emitted), (
            "A non-empty unrecognized model should emit tier_unrecognized, not extraction_fallback"
        )

    def test_known_fast_model_emits_no_event(self):
        """A recognized fast-tier model must emit no fallback event."""
        emitted = []
        with patch("check_routing.append_event", side_effect=emitted.append):
            result = cr._tier_of_model_with_event("claude-haiku-4-5", session_id="test-sess-005")
        assert result == "fast"
        assert emitted == [], f"No event expected for known model; got {emitted}"

    def test_known_reasoning_model_emits_no_event(self):
        """A recognized reasoning-tier model must emit no fallback event."""
        emitted = []
        with patch("check_routing.append_event", side_effect=emitted.append):
            result = cr._tier_of_model_with_event("claude-sonnet-4-6", session_id="test-sess-006")
        assert result == "reasoning"
        assert emitted == [], f"No event expected for known model; got {emitted}"


# ---------------------------------------------------------------------------
# prompt_text_empty — extraction returns "" when no prompt/text/message key
# ---------------------------------------------------------------------------

class TestPromptTextEmpty:
    def test_prompt_text_empty_when_no_keys_present(self):
        """Payload without prompt/text/message → _extract_prompt_text returns ''."""
        raw = {"session_id": "s1", "hook_event_name": "UserPromptSubmit"}
        assert _extract_prompt_text(raw) == ""

    def test_prompt_text_extracted_from_prompt_key(self):
        """'prompt' key is first priority for prompt_text extraction."""
        raw = {"prompt": "implement auth", "text": "other"}
        assert _extract_prompt_text(raw) == "implement auth"

    def test_prompt_text_extracted_from_text_key(self):
        """'text' key is second priority when 'prompt' is absent."""
        raw = {"text": "Routing: DIRECT — task"}
        assert _extract_prompt_text(raw) == "Routing: DIRECT — task"

    def test_prompt_text_extracted_from_message_key(self):
        """'message' key is third priority."""
        raw = {"message": "hello world"}
        assert _extract_prompt_text(raw) == "hello world"

    def test_normalize_payload_prompt_text_empty_for_missing_keys(self):
        """normalize_payload on payload without prompt/text/message → .prompt_text == ''."""
        raw = {"session_id": "s1", "tool_name": "Edit"}
        p = normalize_payload(raw)
        assert p.prompt_text == ""

    def test_prompt_text_empty_string_value_preserved(self):
        """Explicit empty string value is preserved (not treated as missing)."""
        raw = {"prompt": ""}
        assert _extract_prompt_text(raw) == ""


# ---------------------------------------------------------------------------
# tool_name_missing — extraction returns None when no tool_name/tool/toolName
# ---------------------------------------------------------------------------

class TestToolNameMissing:
    def test_tool_name_none_when_no_keys_present(self):
        """Payload without tool_name/tool/toolName → _extract_tool_name returns None."""
        raw = {"session_id": "s1", "hook_event_name": "PreToolUse"}
        assert _extract_tool_name(raw) is None

    def test_tool_name_extracted_from_tool_name_key(self):
        """'tool_name' key is first priority."""
        raw = {"tool_name": "Edit", "tool": "Write"}
        assert _extract_tool_name(raw) == "Edit"

    def test_tool_name_extracted_from_tool_key(self):
        """'tool' key is second priority."""
        raw = {"tool": "Write"}
        assert _extract_tool_name(raw) == "Write"

    def test_tool_name_extracted_from_toolName_key(self):
        """camelCase 'toolName' key is accepted."""
        raw = {"toolName": "Bash"}
        assert _extract_tool_name(raw) == "Bash"

    def test_tool_name_empty_string_returns_none(self):
        """Empty string value for tool_name should not be returned (falsy guard)."""
        raw = {"tool_name": ""}
        assert _extract_tool_name(raw) is None

    def test_normalize_payload_tool_name_none_for_missing_keys(self):
        """normalize_payload on payload without any tool name key → .tool_name is None."""
        raw = {"session_id": "s1", "prompt": "do something"}
        p = normalize_payload(raw)
        assert p.tool_name is None


# ---------------------------------------------------------------------------
# subagent_id_missing — extraction returns None when no agent/subagent id key
# ---------------------------------------------------------------------------

class TestSubagentIdMissing:
    def test_subagent_id_none_when_no_keys_present(self):
        """Payload without agent_id/subagent_id/agentId/subagentId → returns None."""
        raw = {"session_id": "s1", "hook_event_name": "PreToolUse"}
        assert _extract_subagent_id(raw) is None

    def test_subagent_id_extracted_from_agent_id_key(self):
        """'agent_id' key is first priority."""
        raw = {"agent_id": "agent-abc123", "subagent_id": "other"}
        assert _extract_subagent_id(raw) == "agent-abc123"

    def test_subagent_id_extracted_from_subagent_id_key(self):
        """'subagent_id' key is second priority."""
        raw = {"subagent_id": "sub-xyz"}
        assert _extract_subagent_id(raw) == "sub-xyz"

    def test_subagent_id_extracted_from_agentId_camel(self):
        """camelCase 'agentId' key is accepted."""
        raw = {"agentId": "agent-camel-001"}
        assert _extract_subagent_id(raw) == "agent-camel-001"

    def test_subagent_id_extracted_from_subagentId_camel(self):
        """camelCase 'subagentId' key is accepted."""
        raw = {"subagentId": "subagent-camel-002"}
        assert _extract_subagent_id(raw) == "subagent-camel-002"

    def test_subagent_id_whitespace_only_returns_none(self):
        """Whitespace-only value must not be returned (strip guard)."""
        raw = {"agent_id": "   "}
        assert _extract_subagent_id(raw) is None

    def test_normalize_payload_subagent_id_none_for_missing_keys(self):
        """normalize_payload on top-level payload without agent id keys → .subagent_id is None."""
        raw = {"session_id": "s1", "model": "claude-haiku-4-5", "tool_name": "Edit"}
        p = normalize_payload(raw)
        assert p.subagent_id is None


# ---------------------------------------------------------------------------
# Combinatorial: multiple failure modes in one payload
# ---------------------------------------------------------------------------

class TestCombinedExtractionFailures:
    def test_payload_missing_model_and_tool_and_subagent(self):
        """A bare payload with only session_id hits all three missing-field modes at once."""
        raw = {"session_id": "s-combined"}
        p = normalize_payload(raw)
        assert p.model is None
        assert p.tool_name is None
        assert p.subagent_id is None
        assert p.prompt_text == ""

    def test_extraction_fallback_then_tier_unrecognized_are_distinct_events(self):
        """model_none and tier_unrecognized are distinct event shapes — never conflated."""
        emitted_none = []
        with patch("check_routing.append_event", side_effect=emitted_none.append):
            cr._tier_of_model_with_event(None)

        emitted_unk = []
        with patch("check_routing.append_event", side_effect=emitted_unk.append):
            cr._tier_of_model_with_event("unknown-vendor-model-v9")

        assert all(e.get("type") == "extraction_fallback" for e in emitted_none)
        assert all(e.get("type") == "tier_unrecognized" for e in emitted_unk)

    def test_append_event_exception_does_not_propagate(self):
        """If append_event raises, _tier_of_model_with_event must still return 'unknown' silently."""
        with patch("check_routing.append_event", side_effect=RuntimeError("disk full")):
            result = cr._tier_of_model_with_event(None, session_id="test-sess-err")
        assert result == "unknown"
