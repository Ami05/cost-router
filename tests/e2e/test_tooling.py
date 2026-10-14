"""E2E tests: tooling and utility scripts.

Consolidated from:
- test_changelog.py
- test_feedback_loop_config.py
- test_apply_edits.py
- test_extract_brief.py
- test_doc_lint.py
- test_pipeline_trigger.py
- test_warn_pipeline.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from apply_edits import apply_spec  # noqa: E402
from changelog import write_changelog_entry, CHANGELOG_FILE, _VALID_EVENT_TYPES  # noqa: E402
import _common  # noqa: E402
import _config  # noqa: E402
import pipeline_trigger  # noqa: E402
import confidence_score  # noqa: E402
import file_hash_guard  # noqa: E402

APPLY_EDITS_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "apply_edits.py")
EXTRACT_BRIEF_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "extract_brief.py")


# ═══════════════════════════════════════════════════════════════════════════
# --- merged from test_changelog.py ---
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def clean_changelog(tmp_path, monkeypatch):
    """Redirect CHANGELOG_FILE to a temp path for each test."""
    test_file = tmp_path / "policy_changelog.jsonl"
    monkeypatch.setattr("changelog.CHANGELOG_FILE", test_file)
    monkeypatch.setattr("changelog.DATA_DIR", tmp_path)
    yield test_file


def _read_changelog_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


class TestEventTypes:
    """Each valid event type is written correctly."""

    @pytest.mark.parametrize("event_type", sorted(_VALID_EVENT_TYPES))
    def test_valid_event_type_written(self, clean_changelog, event_type):
        path = clean_changelog
        write_changelog_entry(event_type, "R1")
        entries = _read_changelog_entries(path)
        assert len(entries) == 1
        assert entries[0]["event_type"] == event_type
        assert entries[0]["rule_id"] == "R1"
        assert "timestamp" in entries[0]


class TestAppendOnly:
    """Multiple writes append; never overwrite."""

    def test_multiple_writes_append(self, clean_changelog):
        path = clean_changelog
        write_changelog_entry("proposed", "R1", confidence=0.8)
        write_changelog_entry("promoted", "R1", mean_diff=0.15)
        entries = _read_changelog_entries(path)
        assert len(entries) == 2
        assert entries[0]["event_type"] == "proposed"
        assert entries[1]["event_type"] == "promoted"


# ═══════════════════════════════════════════════════════════════════════════
# --- merged from test_feedback_loop_config.py ---
# ═══════════════════════════════════════════════════════════════════════════

def _reload_common(config_data: dict):
    """Force _common to re-read config from a patched disk loader."""
    _common._invalidate_config_cache()
    with patch.object(_config, "_load_config_from_disk", return_value=config_data):
        _common._invalidate_config_cache()
        result = _common.get_feedback_loop_config()
    _common._invalidate_config_cache()
    return result


class TestFeedbackLoopConfigDefaults:
    """All defaults are returned when no feedback_loop block exists."""

    def test_missing_block_returns_all_defaults(self):
        cfg = _reload_common({})
        assert cfg["promote_monitor_window"] == 20
        assert cfg["regression_threshold"] == 0.2
        assert cfg["improvement_threshold"] == 0.15
        assert cfg["max_rules_per_week"] == 3


# ═══════════════════════════════════════════════════════════════════════════
# --- merged from test_apply_edits.py ---
# ═══════════════════════════════════════════════════════════════════════════

class TestApplyEditCreate:
    """Test create edit_type."""

    def test_create_edit(self, tmp_path):
        new_file = tmp_path / "new_file.txt"
        spec = [{"file_path": str(new_file), "edit_type": "create", "new_content": "Hello, World!"}]
        results = apply_spec(spec, dry_run=False, strict=True)
        assert len(results) == 1
        assert results[0]["status"] == "ok"
        assert results[0]["edit_type"] == "create"
        assert new_file.exists()
        assert new_file.read_text() == "Hello, World!"


class TestStrReplaceEdit:
    """Test str_replace edit_type."""

    def test_str_replace_edit(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("The quick brown fox jumps over the lazy dog.")
        spec = [{"file_path": str(test_file), "edit_type": "str_replace",
                 "old_content": "quick brown", "new_content": "slow red"}]
        results = apply_spec(spec, dry_run=False, strict=True)
        assert results[0]["status"] == "ok"
        assert test_file.read_text() == "The slow red fox jumps over the lazy dog."


class TestLineReplaceEdit:
    """Test line_replace edit_type."""

    def test_line_replace_edit(self, tmp_path):
        test_file = tmp_path / "lines.txt"
        test_file.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n")
        spec = [{"file_path": str(test_file), "edit_type": "line_replace",
                 "line_start": 2, "line_end": 4, "new_content": "replacement line\n"}]
        results = apply_spec(spec, dry_run=False, strict=True)
        assert results[0]["status"] == "ok"
        assert test_file.read_text() == "line 1\nreplacement line\nline 5\n"


class TestDryRun:
    """Test dry_run behavior."""

    def test_dry_run_create(self, tmp_path):
        new_file = tmp_path / "new.txt"
        spec = [{"file_path": str(new_file), "edit_type": "create", "new_content": "test"}]
        results = apply_spec(spec, dry_run=True, strict=True)
        assert results[0]["status"] == "dry_run"
        assert not new_file.exists()


class TestApplyEditsCLIStdin:
    """Test CLI with --stdin."""

    def test_cli_stdin(self, tmp_path):
        output_file = tmp_path / "output.txt"
        spec = {"edits": [{"file_path": str(output_file), "edit_type": "create", "new_content": "stdin test"}]}
        result = subprocess.run(
            [sys.executable, APPLY_EDITS_SCRIPT, "--stdin"],
            input=json.dumps(spec), capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert output_file.exists()
        assert output_file.read_text() == "stdin test"
        output = json.loads(result.stdout)
        assert "results" in output
        assert len(output["results"]) == 1
        assert output["results"][0]["status"] == "ok"


# ═══════════════════════════════════════════════════════════════════════════
# --- merged from test_extract_brief.py ---
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_tickets(tmp_path) -> Path:
    tickets = [
        {"id": "task-1", "title": "Implement feature X",
         "description": "A detailed description of feature X.",
         "acceptance_criteria": ["Feature X works correctly", "All edge cases handled"],
         "steps": [{"file": "src/feature.py", "action": "Create new module"},
                   {"file": "tests/test_feature.py", "action": "Add tests"}]},
        {"id": "task-2", "title": "Fix bug Y",
         "description": "Bug Y causes crashes in production.",
         "acceptance_criteria": ["Bug is fixed", "No regression"],
         "steps": [{"file": "src/bug.py", "action": "Fix root cause"}]},
    ]
    tickets_file = tmp_path / "tickets.json"
    tickets_file.write_text(json.dumps(tickets), encoding="utf-8")
    return tickets_file


def test_extract_brief_file_input(sample_tickets):
    result = subprocess.run(
        [sys.executable, EXTRACT_BRIEF_SCRIPT, "--tickets", str(sample_tickets), "--task-id", "task-1"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Implement feature X" in result.stdout
    assert "## Task" in result.stdout


def test_extract_brief_missing_task(sample_tickets):
    result = subprocess.run(
        [sys.executable, EXTRACT_BRIEF_SCRIPT, "--tickets", str(sample_tickets),
         "--task-id", "nonexistent-task"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "not found" in result.stderr


def test_extract_brief_stdin_mode(sample_tickets):
    payload = {"tickets_path": str(sample_tickets), "task_id": "task-1"}
    result = subprocess.run(
        [sys.executable, EXTRACT_BRIEF_SCRIPT, "--stdin"],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Implement feature X" in result.stdout
    assert "## Task" in result.stdout


# ═══════════════════════════════════════════════════════════════════════════
# --- merged from test_doc_lint.py ---
# ═══════════════════════════════════════════════════════════════════════════

from doc_lint import REQUIRED_STRINGS, main as doc_lint_main  # noqa: E402


def test_doc_lint_pass():
    assert doc_lint_main() == 0


def test_doc_lint_missing_string(monkeypatch, tmp_path):
    skill_file = tmp_path / "SKILL.md"
    content = "\n".join(REQUIRED_STRINGS[1:])
    skill_file.write_text(content)
    monkeypatch.setattr("doc_lint.SKILL_PATH", skill_file)
    result = doc_lint_main()
    assert result == 1


# ═══════════════════════════════════════════════════════════════════════════
# --- merged from test_pipeline_trigger.py ---
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=False)
def _reset_config_cache():
    _common._invalidate_config_cache()
    yield
    _common._invalidate_config_cache()


def _write_shadow_routes(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"route": i}) + "\n")


def _make_trigger_state(routes_at_last: int) -> dict:
    return {"routes_at_last_trigger": routes_at_last, "last_triggered_at": "2026-01-01T00:00:00+00:00"}


class TestNoTriggerBelowThreshold:
    def test_no_trigger_below_threshold(self, tmp_path, capsys, _reset_config_cache):
        shadow_file = tmp_path / "shadow_routes.jsonl"
        state_file = tmp_path / "pipeline_trigger_state.json"
        _write_shadow_routes(shadow_file, 5)
        with (
            patch.object(pipeline_trigger, "SHADOW_ROUTES_FILE", shadow_file),
            patch.object(pipeline_trigger, "TRIGGER_STATE_FILE", state_file),
            patch("pipeline_trigger.read_stdin_json", return_value={}),
            patch("pipeline_trigger.config_get", side_effect=lambda key, default=None: default),
            patch("pipeline_trigger.load_json_state", return_value=_make_trigger_state(0)),
            patch("pipeline_trigger.subprocess.run") as mock_run,
        ):
            ret = pipeline_trigger.main()
        assert ret == 0
        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "{}" in captured.out


class TestTriggersWhenThresholdMet:
    def test_triggers_when_threshold_met(self, tmp_path, capsys, _reset_config_cache):
        shadow_file = tmp_path / "shadow_routes.jsonl"
        state_file = tmp_path / "pipeline_trigger_state.json"
        _write_shadow_routes(shadow_file, 25)
        mock_result = MagicMock()
        mock_result.returncode = 0
        _cfg = {
            "pipeline_trigger.min_shadow_routes": 20,
            "pipeline_trigger.cooldown_min_new_routes": 10,
        }
        with (
            patch.object(pipeline_trigger, "SHADOW_ROUTES_FILE", shadow_file),
            patch.object(pipeline_trigger, "TRIGGER_STATE_FILE", state_file),
            patch("pipeline_trigger.read_stdin_json", return_value={}),
            patch("pipeline_trigger.config_get", side_effect=lambda key, default=None: _cfg.get(key, default)),
            patch("pipeline_trigger.load_json_state", return_value={}),
            patch("pipeline_trigger.save_json_state"),
            patch("pipeline_trigger.subprocess.run", return_value=mock_result) as mock_run,
        ):
            ret = pipeline_trigger.main()
        assert ret == 0
        assert mock_run.call_count == 4
        captured = capsys.readouterr()
        assert "{}" in captured.out


# ═══════════════════════════════════════════════════════════════════════════
# --- merged from test_warn_pipeline.py ---
# ═══════════════════════════════════════════════════════════════════════════

def _read_warnings(warnings_log: Path) -> list[dict]:
    if not warnings_log.exists():
        return []
    entries = []
    for line in warnings_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def test_config_load_failure_emits_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
    warnings_log = tmp_path / "warnings.jsonl"
    monkeypatch.setattr(_common, "WARNINGS_LOG", warnings_log)
    monkeypatch.setattr(_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_config, "WARNINGS_LOG", warnings_log)
    monkeypatch.setattr(_config, "_WARNED_THIS_PROCESS", set())
    monkeypatch.setattr(_common, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(_config, "PLUGIN_ROOT", tmp_path)
    (tmp_path / "config.json").write_text("{invalid json!!!}", encoding="utf-8")
    _common._invalidate_config_cache()
    result = _common._load_config_from_disk()
    assert result == {}, f"Expected {{}} on bad config, got {result!r}"
    entries = _read_warnings(warnings_log)
    events = [e["event"] for e in entries]
    assert "config_load_failed" in events


def test_gate_event_log_failure_emits_warning(tmp_path, monkeypatch):
    import gates  # noqa: PLC0415
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
    warnings_log = tmp_path / "warnings.jsonl"
    monkeypatch.setattr(_common, "WARNINGS_LOG", warnings_log)
    monkeypatch.setattr(_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_config, "WARNINGS_LOG", warnings_log)
    monkeypatch.setattr(_config, "_WARNED_THIS_PROCESS", set())
    _real_open = open
    def _raise_on_gate_events(path, *args, **kwargs):
        if "gate_events" in str(path):
            raise OSError("simulated disk full")
        return _real_open(path, *args, **kwargs)
    with patch("builtins.open", side_effect=_raise_on_gate_events):
        gates._log_gate_event(
            gate_chain="pre_write",
            gate_name="test_gate",
            action="allow",
            result={},
            payload={"tool": "Edit", "session_id": "test-session"},
        )
    entries = _read_warnings(warnings_log)
    events = [e["event"] for e in entries]
    assert "gate_event_log_failed" in events


# ═══════════════════════════════════════════════════════════════════════════
# --- _warn() dedup key tests ---
# ═══════════════════════════════════════════════════════════════════════════

class TestWarnDedupKey:
    """Tests for _warn() dedup key correctness (no 80-char truncation)."""

    def test_distinct_long_errors_both_written(self, tmp_path, monkeypatch):
        """Two error strings differing after char 80 should both be logged."""
        monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
        warnings_log = tmp_path / "warnings.jsonl"
        monkeypatch.setattr(_common, "WARNINGS_LOG", warnings_log)
        monkeypatch.setattr(_config, "WARNINGS_LOG", warnings_log)
        # Clear the process-level dedup set
        _common._WARNED_THIS_PROCESS.clear()

        prefix = "A" * 79
        error1 = prefix + "X_unique_suffix_one"
        error2 = prefix + "Y_unique_suffix_two"

        _common._warn("test_event", {"error": error1})
        _common._warn("test_event", {"error": error2})

        entries = _read_warnings(warnings_log)
        assert len(entries) == 2, f"Expected 2 warnings, got {len(entries)}"
        assert "X_unique_suffix_one" in entries[0]["context"]["error"]
        assert "Y_unique_suffix_two" in entries[1]["context"]["error"]

    def test_identical_error_is_deduplicated(self, tmp_path, monkeypatch):
        """Same error string called twice should only log once."""
        monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
        warnings_log = tmp_path / "warnings.jsonl"
        monkeypatch.setattr(_common, "WARNINGS_LOG", warnings_log)
        monkeypatch.setattr(_config, "WARNINGS_LOG", warnings_log)
        _common._WARNED_THIS_PROCESS.clear()

        error = "same error string"
        _common._warn("test_event", {"error": error})
        _common._warn("test_event", {"error": error})

        entries = _read_warnings(warnings_log)
        assert len(entries) == 1

    def test_different_events_same_error_both_written(self, tmp_path, monkeypatch):
        """Same error with different event names should log both."""
        monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
        warnings_log = tmp_path / "warnings.jsonl"
        monkeypatch.setattr(_common, "WARNINGS_LOG", warnings_log)
        monkeypatch.setattr(_config, "WARNINGS_LOG", warnings_log)
        _common._WARNED_THIS_PROCESS.clear()

        error = "same error"
        _common._warn("event_A", {"error": error})
        _common._warn("event_B", {"error": error})

        entries = _read_warnings(warnings_log)
        assert len(entries) == 2
        assert entries[0]["event"] == "event_A"
        assert entries[1]["event"] == "event_B"


# ═══════════════════════════════════════════════════════════════════════════
# --- normal conditions must not be logged as failures ---
# ═══════════════════════════════════════════════════════════════════════════

class TestNormalConditionsNotLoggedAsFailures:
    """Cold-start / expected states must not fire _warn; real failures still must."""

    def test_load_pinned_map_empty_rules_no_warning(self, tmp_path, monkeypatch):
        """An empty rules list is the normal cold-start state — no warning, empty dict."""
        monkeypatch.setattr(confidence_score, "DATA_DIR", tmp_path)
        pinned_path = tmp_path / "pinned_rules.json"
        pinned_path.write_text(
            json.dumps({"version": "1.0", "description": "", "rules": []}),
            encoding="utf-8",
        )
        mock_warn = MagicMock()
        monkeypatch.setattr(confidence_score, "_warn", mock_warn)

        result = confidence_score.load_pinned_map()

        assert result == {}
        mock_warn.assert_not_called()

    def test_load_pinned_map_missing_file_records_warning(self, tmp_path, monkeypatch):
        """Missing/malformed candidate_table.json is a real failure — still warns."""
        monkeypatch.setattr(confidence_score, "DATA_DIR", tmp_path)
        # No candidate_table.json written — file is missing.
        mock_warn = MagicMock()
        monkeypatch.setattr(confidence_score, "_warn", mock_warn)

        result = confidence_score.load_pinned_map()

        assert result == {}
        mock_warn.assert_called_once()
        args, _ = mock_warn.call_args
        assert args[0] == "pinned_rules_unreadable"

    def test_sha256_directory_returns_none_no_warning(self, tmp_path, monkeypatch):
        """A directory path is not applicable, not an error — no warning."""
        mock_warn = MagicMock()
        monkeypatch.setattr(file_hash_guard, "_warn", mock_warn)

        result = file_hash_guard._sha256(tmp_path)

        assert result is None
        mock_warn.assert_not_called()

    def test_sha256_real_file_returns_64_char_hex_digest(self, tmp_path, monkeypatch):
        """A readable file hashes to a 64-character hex sha256 digest."""
        mock_warn = MagicMock()
        monkeypatch.setattr(file_hash_guard, "_warn", mock_warn)
        test_file = tmp_path / "sample.txt"
        test_file.write_text("hello world", encoding="utf-8")

        result = file_hash_guard._sha256(test_file)

        assert result is not None
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)
        mock_warn.assert_not_called()
