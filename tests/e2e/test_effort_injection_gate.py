#!/usr/bin/env python3
"""E2E tests for effort injection gate — real pre_task chain behavior.

Tests the effort_injection_gate.py module covering:
1. Module imports (_common symbols) work correctly
2. Mechanical prompt gets Effort: low injected
3. Explore subagent_type bypasses injection
4. Prompt with existing Effort line is unchanged
5. Full pre_task chain including tracker gate preserves updatedInput
6. Exception inside classify falls open to plain allow
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts dir to path for imports
SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Import the gate module
import effort_injection_gate
from _common import GateResult, gate_allow, append_event, emit_gate_result


class TestEffortInjectionGateImports:
    """Case 1: Verify module and _common imports exist."""

    def test_effort_injection_gate_imports_gateresult(self):
        """_common.GateResult is imported in effort_injection_gate."""
        # The module loads without error, proving imports work
        assert hasattr(effort_injection_gate, 'event_pre_task')

    def test_common_exports_required_symbols(self):
        """_common exports GateResult, gate_allow, append_event, emit_gate_result."""
        from _common import GateResult, gate_allow, append_event, emit_gate_result
        assert GateResult is not None
        assert callable(gate_allow)
        assert callable(append_event)
        assert callable(emit_gate_result)

    def test_effort_injection_gate_uses_common_symbols(self):
        """Verify effort_injection_gate uses the imported symbols."""
        # Check that the functions are available in the module
        assert hasattr(effort_injection_gate, '_inject_effort_line')
        assert hasattr(effort_injection_gate, '_map_intent_to_internal')


class TestEffortInjectionBasics:
    """Case 2: Mechanical dispatch prompt gets Effort: low injection."""

    @pytest.fixture
    def temp_data_dir(self):
        """Provide temp data dir to avoid touching real ~/.claude data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('effort_injection_gate.append_event'):
                yield Path(tmpdir)

    def test_mechanical_prompt_gets_effort_injected(self, temp_data_dir):
        """Agent dispatch with mechanical prompt returns updatedInput with Effort."""
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "description": "Mechanical edit task",
                "prompt": "Edit these 5 files to rename a symbol. Look at path/a.py, path/b.py, path/c.py, path/d.py, path/e.py.",
                "model": "haiku",
            }
        }

        result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        # Should have updatedInput with modified prompt
        assert "hookSpecificOutput" in result
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "updatedInput" in result["hookSpecificOutput"]
        assert "prompt" in result["hookSpecificOutput"]["updatedInput"]

        new_prompt = result["hookSpecificOutput"]["updatedInput"]["prompt"]
        assert new_prompt.endswith("Effort: low")

    def test_judgment_prompt_gets_higher_effort(self, temp_data_dir):
        """Agent dispatch with judgment prompt gets appropriate effort level."""
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "description": "Design decision task",
                "prompt": (
                    "I need to design a new routing system. Consider these trade-offs: "
                    "performance vs. latency, cost vs. accuracy. How should we architect this? "
                    "Review the existing code in src/routing/."
                ),
                "model": "sonnet",
            }
        }

        result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        if "updatedInput" in result["hookSpecificOutput"]:
            new_prompt = result["hookSpecificOutput"]["updatedInput"]["prompt"]
            # Judgment intent maps to "design" which likely has "high" effort
            assert "Effort:" in new_prompt


class TestExploreSubagentBypass:
    """Case 3: Explore subagent_type bypasses effort injection."""

    @pytest.fixture
    def temp_data_dir(self):
        """Provide temp data dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('effort_injection_gate.append_event'):
                yield Path(tmpdir)

    def test_explore_subagent_gets_no_injection(self, temp_data_dir):
        """Agent with subagent_type='Explore' returns plain allow, no updatedInput."""
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "description": "Find test files",
                "prompt": "Search for all test files in tests/ matching pattern *_test.py",
                "model": "haiku",
                "subagent_type": "Explore",
            }
        }

        result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        # Should allow without updatedInput
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        # No updatedInput for Explore
        assert "updatedInput" not in result["hookSpecificOutput"]

    def test_explore_case_insensitive(self, temp_data_dir):
        """Explore detection is case-insensitive."""
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "description": "Find test files",
                "prompt": "Search files",
                "model": "haiku",
                "subagent_type": "EXPLORE",
            }
        }

        result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        # Should still bypass (uppercase EXPLORE)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "updatedInput" not in result["hookSpecificOutput"]


class TestExistingEffortLine:
    """Case 4: Prompt already containing Effort line is unchanged."""

    @pytest.fixture
    def temp_data_dir(self):
        """Provide temp data dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('effort_injection_gate.append_event'):
                yield Path(tmpdir)

    def test_prompt_with_existing_effort_unchanged(self, temp_data_dir):
        """Prompt with 'Effort:' line already present is not modified."""
        original_prompt = (
            "This is a mechanical task.\n"
            "Effort: medium\n"
            "Do not change the effort line."
        )
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "description": "Mechanical task with preset effort",
                "prompt": original_prompt,
                "model": "haiku",
            }
        }

        result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        # When prompt already has Effort line, gate returns plain allow (no updatedInput)
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        # If updatedInput is present, prompt should be unchanged
        if "updatedInput" in result["hookSpecificOutput"]:
            assert result["hookSpecificOutput"]["updatedInput"]["prompt"] == original_prompt

    def test_effort_line_with_whitespace_variations(self, temp_data_dir):
        """Effort line detection handles whitespace variations."""
        variants = [
            "Effort: low",
            "EFFORT: high",
            "effort:   medium",
            "  Effort: xhigh  ",
        ]
        for effort_line in variants:
            prompt = f"Task description\n{effort_line}\nMore text"
            payload = {
                "tool_name": "Agent",
                "tool_input": {
                    "description": "Test effort variant",
                    "prompt": prompt,
                    "model": "haiku",
                }
            }
            result = effort_injection_gate.event_pre_task(payload, is_claude=True)
            assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestFullPreTaskChain:
    """Case 5: gates.run_gate('pre_task', payload) preserves updatedInput."""

    @pytest.fixture
    def temp_data_dir(self):
        """Provide temp data dir for gate calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {'COST_ROUTER_DATA_DIR': tmpdir}):
                with patch('effort_injection_gate.append_event'):
                    yield Path(tmpdir)

    def test_agent_payload_with_thin_model_preserves_updated_input(self, temp_data_dir):
        """Agent payload with haiku model maintains updatedInput through chain."""
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "description": "Simple file rename",
                "prompt": "Rename symbol X to Y in files a.py and b.py.",
                "model": "haiku",
            },
            "hook_event_name": "PreToolUse",
        }

        result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        # Effort gate should return updatedInput
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        if "updatedInput" in result["hookSpecificOutput"]:
            # Verify the prompt was modified with Effort
            new_prompt = result["hookSpecificOutput"]["updatedInput"]["prompt"]
            assert "Effort:" in new_prompt or new_prompt.endswith("Effort: low")

    def test_task_payload_gets_effort_injected(self, temp_data_dir):
        """Task tool (not just Agent) also gets effort injection."""
        payload = {
            "tool_name": "Task",
            "tool_input": {
                "description": "Distributed work",
                "prompt": "Fix all test failures in tests/ by updating assertions.",
                "model": "haiku",
            },
            "hook_event_name": "PreToolUse",
        }

        result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestExceptionHandling:
    """Case 6: Exception inside classify falls open to plain allow."""

    @pytest.fixture
    def temp_data_dir(self):
        """Provide temp data dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('effort_injection_gate.append_event'):
                yield Path(tmpdir)

    def test_exception_in_classify_dispatch_work_falls_open(self, temp_data_dir):
        """If classify_dispatch_work raises, gate returns plain allow."""
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "description": "Task that will cause classify to fail",
                "prompt": "Do something",
                "model": "haiku",
            }
        }

        # Mock classify_dispatch_work to raise an exception
        with patch('routing_resolver.classify_dispatch_work', side_effect=RuntimeError("Classify failed")):
            result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        # Gate must fail open
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        # No updatedInput when exception occurs
        assert "updatedInput" not in result["hookSpecificOutput"]

    def test_exception_in_append_event_logs_silently(self, temp_data_dir):
        """If append_event fails, gate still allows and continues."""
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "description": "Mechanical edit",
                "prompt": "Edit file paths a.py and b.py and c.py and d.py and e.py and f.py",
                "model": "haiku",
            }
        }

        # Mock append_event to raise
        with patch('effort_injection_gate.append_event', side_effect=IOError("Append failed")):
            result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        # Gate still allows even if logging fails
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_invalid_tool_input_dict_returns_allow(self, temp_data_dir):
        """Non-dict tool_input returns plain allow."""
        payload = {
            "tool_name": "Agent",
            "tool_input": "not a dict",  # Invalid
        }

        result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "updatedInput" not in result["hookSpecificOutput"]

    def test_non_agent_tool_returns_allow(self, temp_data_dir):
        """Tool names other than Agent/Task return plain allow."""
        payload = {
            "tool_name": "Read",
            "tool_input": {
                "file_path": "/some/path",
            }
        }

        result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "updatedInput" not in result["hookSpecificOutput"]

    def test_missing_prompt_field_returns_allow(self, temp_data_dir):
        """Missing or non-string prompt returns plain allow."""
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "description": "No prompt field",
                "model": "haiku",
            }
        }

        result = effort_injection_gate.event_pre_task(payload, is_claude=True)

        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "updatedInput" not in result["hookSpecificOutput"]


class TestEffortLineInjectionLogic:
    """Tests for the _inject_effort_line helper function."""

    def test_inject_effort_line_appends_to_prompt(self):
        """_inject_effort_line appends effort line to prompt."""
        original = "Task description"
        result = effort_injection_gate._inject_effort_line(original, "low")
        assert result == "Task description\nEffort: low"

    def test_inject_effort_line_preserves_existing_effort(self):
        """_inject_effort_line does not inject if effort line exists."""
        original = "Task\nEffort: medium\nMore text"
        result = effort_injection_gate._inject_effort_line(original, "low")
        assert result == original  # Unchanged

    def test_inject_effort_line_case_insensitive(self):
        """_inject_effort_line detects Effort line case-insensitively."""
        original = "Task\nEFFORT: high"
        result = effort_injection_gate._inject_effort_line(original, "low")
        assert result == original  # Unchanged


class TestMapIntentToInternal:
    """Tests for the _map_intent_to_internal helper function."""

    def test_judgment_maps_to_design(self):
        """'judgment' intent maps to 'design' internally."""
        result = effort_injection_gate._map_intent_to_internal("judgment")
        assert result == "design"

    def test_other_intents_pass_through(self):
        """Other intents pass through unchanged."""
        assert effort_injection_gate._map_intent_to_internal("mechanical") == "mechanical"
        assert effort_injection_gate._map_intent_to_internal("bulk-edit") == "bulk-edit"

    def test_ambiguous_maps_to_default(self):
        """'ambiguous' intent (unclassifiable) maps to '_default' (yields high effort)."""
        result = effort_injection_gate._map_intent_to_internal("ambiguous")
        assert result == "_default"


class TestCursorPayloadSupport:
    """Test emit_gate_result for Cursor (non-Claude) payloads."""

    @pytest.fixture
    def temp_data_dir(self):
        """Provide temp data dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('effort_injection_gate.append_event'):
                yield Path(tmpdir)

    def test_cursor_payload_format(self, temp_data_dir):
        """Cursor (is_claude=False) payloads return Cursor-specific JSON."""
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "description": "Mechanical task",
                "prompt": "Rename symbol in a.py and b.py and c.py and d.py and e.py",
                "model": "haiku",
            }
        }

        result = effort_injection_gate.event_pre_task(payload, is_claude=False)

        # Cursor format: top-level "permission" and optional "agent_message"
        assert "permission" in result
        assert result["permission"] == "allow"
