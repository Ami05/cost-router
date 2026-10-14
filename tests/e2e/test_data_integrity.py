"""Tests for validate_data_integrity.py — schema validation of data pipeline files."""
from __future__ import annotations

import sys
import json
import warnings
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from validate_data_integrity import (  # noqa: E402
    validate_dict_record,
    validate_signals_record,
    validate_file,
    run_validation,
    Violation,
)
from data_schemas import SCHEMAS, SESSION_SIGNALS_SCHEMA, SESSION_LOG_TURN_SCHEMA, USAGE_EVENT_SCHEMAS  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "data"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _errors(violations: list[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity == "error"]


def _warnings(violations: list[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity == "warning"]


# ---------------------------------------------------------------------------
# TestValidateDictRecord
# ---------------------------------------------------------------------------

class TestValidateDictRecord:
    SCHEMA = SESSION_LOG_TURN_SCHEMA

    def _valid(self) -> dict:
        return {
            "session_id": "s1",
            "turn_id": "s1-t1",
            "ts": "2026-09-17T10:00:00Z",
            "model_key": "claude-sonnet-4-5",
            "est_output_tokens": 250,
            "tier": "sonnet",
            "content_kind": "code_output",
        }

    def test_clean_record_passes(self):
        violations = validate_dict_record(self._valid(), self.SCHEMA, "test.jsonl", 1)
        assert _errors(violations) == []

    def test_missing_required_field_caught(self):
        record = self._valid()
        del record["turn_id"]
        violations = validate_dict_record(record, self.SCHEMA, "test.jsonl", 1)
        errors = _errors(violations)
        assert len(errors) == 1
        assert errors[0].field == "turn_id"
        assert "missing" in errors[0].message

    def test_wrong_type_string_for_int_caught(self):
        record = self._valid()
        record["est_output_tokens"] = "not-an-int"
        violations = validate_dict_record(record, self.SCHEMA, "test.jsonl", 1)
        errors = _errors(violations)
        assert len(errors) == 1
        assert errors[0].field == "est_output_tokens"

    def test_invariant_violation_negative_est_cost_usd(self):
        # Use usage.jsonl schema which has an est_cost_usd >= 0 invariant
        usage_schema = USAGE_EVENT_SCHEMAS["session_summary"]
        record = {
            "type": "session_summary",
            "session_id": "s1",
            "started_at": None,
            "ended_at": None,
            "est_input_tokens": 0,
            "est_output_tokens": 0,
            "est_context_tokens": 0,
            "est_cost_usd": -1.0,
            "subagent_dispatch_count": 0,
            "subagent_unattributed_count": 0,
            "tool_call_count": 0,
            "tier_mix": {},
            "reward": 0.0,
            "has_real_tokens": False,
            "session_type": "additive",
            "explore_without_haiku": 0,
            "total_agents_spawned": 0,
            "agent_model_waste_usd": 0.0,
            "rd2_nudges": 0,
            "rd2_override_rate": 0.0,
            "resume_used_count": 0,
        }
        violations = validate_dict_record(record, usage_schema, "usage.jsonl", 1)
        errors = _errors(violations)
        assert any(v.field == "est_cost_usd" for v in errors)


# ---------------------------------------------------------------------------
# TestValidateSignalsRecord
# ---------------------------------------------------------------------------

class TestValidateSignalsRecord:

    def _valid(self) -> list:
        return [["clean_success"], "clean_success", "high", 300.0, 0.95, None]

    def test_valid_6_element_list_passes(self):
        violations = validate_signals_record(self._valid(), "session_signals.jsonl", 1)
        assert _errors(violations) == []

    def test_wrong_length_caught(self):
        violations = validate_signals_record([1, 2, 3], "session_signals.jsonl", 1)
        errors = _errors(violations)
        assert len(errors) == 1
        assert "6" in errors[0].message

    def test_unknown_outcome_emits_warning_not_error(self):
        record = self._valid()
        record[1] = "unexpected_outcome"
        violations = validate_signals_record(record, "session_signals.jsonl", 1)
        assert _errors(violations) == []
        warn = _warnings(violations)
        assert len(warn) == 1
        assert "unknown outcome" in warn[0].message

    def test_none_reward_at_index_4_is_valid(self):
        record = self._valid()
        record[4] = None
        violations = validate_signals_record(record, "session_signals.jsonl", 1)
        assert _errors(violations) == []


# ---------------------------------------------------------------------------
# TestValidateFile
# ---------------------------------------------------------------------------

class TestValidateFile:

    def test_usage_fixture_has_zero_errors(self):
        path = FIXTURES_DIR / "usage_fixture.jsonl"
        violations = validate_file(path, None)
        errors = _errors(violations)
        assert errors == [], f"Unexpected errors: {errors}"

    def test_nonexistent_file_returns_empty(self):
        path = FIXTURES_DIR / "does_not_exist_xyz.jsonl"
        violations = validate_file(path, None)
        assert violations == []

    def test_invalid_json_line_caught(self, tmp_path):
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text('not valid json {\n', encoding="utf-8")
        violations = validate_file(bad_file, None)
        errors = _errors(violations)
        assert len(errors) == 1
        assert "invalid JSON" in errors[0].message
        assert errors[0].line == 1


# ---------------------------------------------------------------------------
# TestRunValidation
# ---------------------------------------------------------------------------

class TestRunValidation:

    def test_fixtures_dir_returns_zero_errors_and_min_files(self, tmp_path):
        # Populate tmp_path with canonically-named files matching SCHEMAS keys
        # plus session_signals.jsonl (the array-per-line schema)
        canonical_fixtures = {
            "session_log.jsonl": (FIXTURES_DIR / "session_log_fixture.jsonl").read_text(),
            "shadow_routes.jsonl": (FIXTURES_DIR / "shadow_routes_fixture.jsonl").read_text(),
            "reward_log.jsonl": (FIXTURES_DIR / "reward_log_fixture.jsonl").read_text(),
            "calibration.jsonl": (FIXTURES_DIR / "calibration_fixture.jsonl").read_text(),
            "policy_changelog.jsonl": (FIXTURES_DIR / "policy_changelog_fixture.jsonl").read_text(),
            "session_signals.jsonl": (FIXTURES_DIR / "session_signals_fixture.jsonl").read_text(),
        }
        for name, content in canonical_fixtures.items():
            (tmp_path / name).write_text(content, encoding="utf-8")

        violations, files_checked = run_validation(tmp_path)
        errors = _errors(violations)
        assert errors == [], f"Unexpected errors: {errors}"
        assert files_checked >= 6


# ---------------------------------------------------------------------------
# TestDevSafety
# ---------------------------------------------------------------------------

class TestDevSafety:

    def test_nonexistent_dir_returns_empty(self):
        violations, files_checked = run_validation(Path("/nonexistent/dir"))
        assert violations == []
        assert files_checked == 0
