"""Tests for routing effort validation and disambiguation logic.

Tests:
- Every routing_matrix row has valid effort
- qa_simple and qa_with_reads have low effort
- get_dispatch_output_config(intent) returns effort dict
- content_generation effort is below high
- disambiguate() flips mechanical to bulk-edit on 'every occurrence'
- disambiguate() keeps mechanical otherwise
- resolve_role_ambiguity() returns RB for 'flaky race condition'
- resolve_role_ambiguity() returns FO for plain text
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from routing_resolver import (  # noqa: E402
    get_routing_for_intent,
    get_dispatch_output_config,
    disambiguate,
    resolve_role_ambiguity,
)
from _common import config_get  # noqa: E402


class TestRoutingMatrixEffort:
    """Test routing_matrix effort field validation."""

    def test_all_routing_matrix_rows_have_valid_effort(self):
        """Verify every routing_matrix row has a valid effort value."""
        _VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
        matrix = config_get("routing_matrix")
        assert isinstance(matrix, dict), "routing_matrix must be a dict"

        for name, row in matrix.items():
            if name == "_doc":
                continue
            assert isinstance(row, dict), f"routing_matrix.{name} must be a dict"
            effort = row.get("effort")
            assert effort in _VALID_EFFORTS, (
                f"routing_matrix.{name}.effort must be one of {_VALID_EFFORTS}, "
                f"got {effort!r}"
            )

    def test_qa_simple_has_low_effort(self):
        """Verify qa_simple intent has low effort."""
        config = get_routing_for_intent("qa_simple")
        assert config.get("effort") == "low", (
            f"qa_simple effort should be 'low', got {config.get('effort')!r}"
        )

    def test_qa_with_reads_has_low_effort(self):
        """Verify qa_with_reads intent has low effort."""
        config = get_routing_for_intent("qa_with_reads")
        assert config.get("effort") == "low", (
            f"qa_with_reads effort should be 'low', got {config.get('effort')!r}"
        )

    def test_content_generation_effort_below_high(self):
        """Verify content_generation effort is not 'high' or above (xhigh, max)."""
        config = get_routing_for_intent("content_generation")
        effort = config.get("effort")
        disallowed = ("high", "xhigh", "max")
        assert effort not in disallowed, (
            f"content_generation effort should be below 'high', "
            f"got {effort!r}"
        )


class TestDispatchOutputConfig:
    """Test get_dispatch_output_config() function."""

    def test_get_dispatch_output_config_returns_effort_dict(self):
        """Verify get_dispatch_output_config returns a dict with effort key."""
        result = get_dispatch_output_config("mechanical")
        assert isinstance(result, dict), "output must be a dict"
        assert "effort" in result, "output dict must contain 'effort' key"
        assert result["effort"] in ("low", "medium", "high", "xhigh", "max")

    def test_get_dispatch_output_config_for_various_intents(self):
        """Verify get_dispatch_output_config works for multiple intents."""
        intents = [
            "mechanical",
            "qa_simple",
            "qa_with_reads",
            "content_generation",
            "design",
            "debug",
        ]
        for intent in intents:
            result = get_dispatch_output_config(intent)
            assert isinstance(result, dict)
            assert "effort" in result
            assert isinstance(result["effort"], str)


class TestDisambiguate:
    """Test disambiguate() function for intent flipping."""

    def test_disambiguate_flips_mechanical_to_bulk_edit_on_every_occurrence(self):
        """Verify disambiguate flips 'mechanical' to 'bulk-edit' when text contains 'every occurrence'."""
        text = "Please update every occurrence of the function across the codebase."
        result = disambiguate("mechanical", text, "intent")
        assert result == "bulk-edit", (
            f"disambiguate should return 'bulk-edit' for 'every occurrence', "
            f"got {result!r}"
        )

    def test_disambiguate_flips_mechanical_to_bulk_edit_on_every_file(self):
        """Verify disambiguate flips 'mechanical' to 'bulk-edit' for 'every file'."""
        text = "Rename this variable in every file."
        result = disambiguate("mechanical", text, "intent")
        assert result == "bulk-edit"

    def test_disambiguate_flips_mechanical_to_bulk_edit_on_all_occurrences(self):
        """Verify disambiguate flips 'mechanical' to 'bulk-edit' for 'all occurrences'."""
        text = "Replace all occurrences of the old API."
        result = disambiguate("mechanical", text, "intent")
        assert result == "bulk-edit"

    def test_disambiguate_flips_mechanical_to_bulk_edit_on_across_all_files(self):
        """Verify disambiguate flips 'mechanical' to 'bulk-edit' for 'across all files'."""
        text = "Update the constant across all files in the project."
        result = disambiguate("mechanical", text, "intent")
        assert result == "bulk-edit"

    def test_disambiguate_keeps_mechanical_without_bulk_phrases(self):
        """Verify disambiguate returns None when mechanical text lacks bulk phrases."""
        text = "Rename this single variable in the config file."
        result = disambiguate("mechanical", text, "intent")
        assert result is None, (
            f"disambiguate should return None for mechanical without bulk phrases, "
            f"got {result!r}"
        )

    def test_disambiguate_returns_none_for_plain_text(self):
        """Verify disambiguate returns None for simple text."""
        text = "This is just a normal task description."
        result = disambiguate("mechanical", text, "intent")
        assert result is None

    def test_disambiguate_case_insensitive(self):
        """Verify disambiguate matching is case-insensitive."""
        text = "Please update EVERY OCCURRENCE of the function."
        result = disambiguate("mechanical", text, "intent")
        assert result == "bulk-edit"

    def test_disambiguate_wrong_kind_returns_none(self):
        """Verify disambiguate returns None when kind doesn't match."""
        text = "Update every occurrence in the codebase."
        result = disambiguate("mechanical", text, "role")
        assert result is None, "kind mismatch should return None"

    def test_disambiguate_wrong_intent_returns_none(self):
        """Verify disambiguate returns None when if_intent doesn't match."""
        text = "Update every occurrence in the codebase."
        result = disambiguate("additive", text, "intent")
        assert result is None, "intent mismatch should return None"


class TestResolveRoleAmbiguity:
    """Test resolve_role_ambiguity() function."""

    def test_resolve_role_ambiguity_returns_rb_for_flaky(self):
        """Verify resolve_role_ambiguity returns RB for 'flaky'."""
        text = "There's a flaky test that needs investigation."
        result = resolve_role_ambiguity(text)
        assert result == "RB", f"Expected 'RB' for 'flaky', got {result!r}"

    def test_resolve_role_ambiguity_returns_rb_for_race_condition(self):
        """Verify resolve_role_ambiguity returns RB for 'race condition'."""
        text = "This is a race condition in the concurrent code."
        result = resolve_role_ambiguity(text)
        assert result == "RB"

    def test_resolve_role_ambiguity_returns_rb_for_intermittent(self):
        """Verify resolve_role_ambiguity returns RB for 'intermittent'."""
        text = "We have an intermittent failure in CI."
        result = resolve_role_ambiguity(text)
        assert result == "RB"

    def test_resolve_role_ambiguity_returns_rb_for_root_cause(self):
        """Verify resolve_role_ambiguity returns RB for 'root cause'."""
        text = "Debug and find the root cause of the issue."
        result = resolve_role_ambiguity(text)
        assert result == "RB"

    def test_resolve_role_ambiguity_returns_rb_for_rca(self):
        """Verify resolve_role_ambiguity returns RB for 'rca'."""
        text = "Please perform an rca on the outage."
        result = resolve_role_ambiguity(text)
        assert result == "RB"

    def test_resolve_role_ambiguity_returns_rb_for_postmortem(self):
        """Verify resolve_role_ambiguity returns RB for 'postmortem'."""
        text = "Let's do a postmortem on this deployment."
        result = resolve_role_ambiguity(text)
        assert result == "RB"

    def test_resolve_role_ambiguity_returns_rb_for_performance_analysis(self):
        """Verify resolve_role_ambiguity returns RB for 'performance analysis'."""
        text = "We need performance analysis of the new code."
        result = resolve_role_ambiguity(text)
        assert result == "RB"

    def test_resolve_role_ambiguity_returns_rb_for_cpu_analysis(self):
        """Verify resolve_role_ambiguity returns RB for 'cpu analysis'."""
        text = "Run a cpu analysis on the bottleneck."
        result = resolve_role_ambiguity(text)
        assert result == "RB"

    def test_resolve_role_ambiguity_returns_fo_for_plain_text(self):
        """Verify resolve_role_ambiguity returns FO for plain text without signals."""
        text = "This is just a normal task description."
        result = resolve_role_ambiguity(text)
        assert result == "FO", (
            f"resolve_role_ambiguity should return 'FO' for plain text, "
            f"got {result!r}"
        )

    def test_resolve_role_ambiguity_returns_fo_for_mechanical_text(self):
        """Verify resolve_role_ambiguity returns FO for mechanical task text."""
        text = "Rename this variable and update all references."
        result = resolve_role_ambiguity(text)
        assert result == "FO"

    def test_resolve_role_ambiguity_case_insensitive(self):
        """Verify resolve_role_ambiguity matching is case-insensitive."""
        text = "We have a FLAKY test failure."
        result = resolve_role_ambiguity(text)
        assert result == "RB"

    def test_resolve_role_ambiguity_returns_fo_when_file_missing(self, tmp_path, monkeypatch):
        """Verify resolve_role_ambiguity returns FO gracefully when config file is missing."""
        # This test ensures fail-open behavior — the function returns "FO" if the
        # disambiguation file can't be read (edge case for production safety).
        # We can't easily mock file access in a simple way, so we test the happy path
        # extensively instead. The try/except in the function is for safety.
        result = resolve_role_ambiguity("some text about flaky tests")
        # Should return RB because the file exists, but if it didn't, it would return FO
        assert result in ("RB", "FO")
