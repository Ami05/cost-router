"""Consolidated session pipeline tests.

Sources:
- test_session_report.py
- test_session_impact.py
- test_session_log_pipeline.py
- test_cost_tracking.py
- test_round_trip_budget.py
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # _accounting_fixture

import _common
import analyze_routing
import session_summary
import cost_report_renderer
import session_impact as si
import impact_calculator as ic
import compact_log
from compact_log import compact_usage_log, WORKING_SET_BYTES, ARCHIVE_ROTATE_BYTES
from _common import append_event, read_events
from _model_utils import estimate_tokens, load_pricing, price_for
import agent_call_tracker as act
import check_budget_status as cbs
import check_routing as cr
import _config
import _fileio
import _events
import log_compactor
import spawn_tracker

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ===========================================================================
# --- from test_session_report.py ---
# ===========================================================================

@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    usage_log = data_dir / "routing_events.jsonl"

    monkeypatch.setattr(_common, "DATA_DIR", data_dir)
    monkeypatch.setattr(_common, "USAGE_LOG", usage_log)
    monkeypatch.setattr(session_summary, "DATA_DIR", data_dir)
    monkeypatch.setattr(session_summary, "USAGE_LOG", usage_log)
    monkeypatch.setattr(analyze_routing, "TUNE_STATE_FILE", data_dir / "tune_state.json")
    # Also patch compact_log module references
    usage_archive = data_dir / "usage_archive.jsonl"
    monkeypatch.setattr(compact_log, "USAGE_LOG", usage_log)
    monkeypatch.setattr(compact_log, "USAGE_ARCHIVE", usage_archive)
    monkeypatch.setattr(_common, "MAX_LOG_BYTES", 5 * 1024 * 1024)
    # Patch module-local bindings used by append_event and read_events
    monkeypatch.setattr(_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(_config, "USAGE_LOG", usage_log)
    monkeypatch.setattr(_fileio, "DATA_DIR", data_dir)
    monkeypatch.setattr(_events, "DATA_DIR", data_dir)
    monkeypatch.setattr(log_compactor, "USAGE_LOG", usage_log)
    monkeypatch.setattr(log_compactor, "USAGE_ARCHIVE", usage_archive)
    monkeypatch.setattr(spawn_tracker, "STATS_FILE", data_dir / "agent_call_stats.json")
    return {"data_dir": data_dir, "usage_log": usage_log, "usage_archive": usage_archive}


def _tool_event(session_id: str, tier: str = "fast", tool: str = "Edit", tokens: int = 100) -> dict:
    return {
        "type": "tool",
        "session_id": session_id,
        "tier": tier,
        "tool_name": tool,
        "est_tokens": tokens,
        "ts": "2025-01-01T00:00:00+00:00",
    }


def test_session_report_renders_for_session(monkeypatch):
    """render_report returns a non-empty markdown string for a session with events."""
    sid = "rep-sess-1"
    events = [
        {**_tool_event(sid, tier="fast"), "type": "session_summary"},
        {**_tool_event(sid, tier="fast", tool="Write"), "type": "session_summary"},
    ]
    monkeypatch.setattr(session_summary, "read_events", lambda: events)
    monkeypatch.setattr(session_summary, "load_pricing", lambda: {})

    report = session_summary.render_report(limit=10)
    assert isinstance(report, str)
    assert len(report) > 0


def test_session_report_empty_session_returns_string(monkeypatch):
    """render_report with no session_summary events still returns a string without crashing."""
    monkeypatch.setattr(session_summary, "read_events", lambda: [])
    monkeypatch.setattr(session_summary, "load_pricing", lambda: {})

    report = session_summary.render_report(limit=10)
    assert isinstance(report, str)


def test_gate_health_surfaces_judgment_on_fast_counters():
    counters = {
        "gate_allow_routed": 5,
        "dispatch_nudge_judgment_on_fast": 7,
        "gate_deny_judgment_on_fast_escalation": 2,
        "gate_allow_judgment_on_fast_escalation_rt_exhausted": 1,
    }

    stats = analyze_routing.gate_health_stats(counters)
    assert stats["dispatch_nudge_judgment_on_fast"] == 7
    assert stats["gate_deny_judgment_on_fast_escalation"] == 2
    assert stats["gate_allow_judgment_on_fast_escalation_rt_exhausted"] == 1

    rendered = analyze_routing.render_gate_health(stats)
    assert "Judgment-on-fast" in rendered
    assert "nudged: 7" in rendered
    assert "escalation denied: 2" in rendered
    assert "escalation allowed (rt exhausted): 1" in rendered


def test_gate_health_omits_judgment_on_fast_line_when_zero():
    stats = analyze_routing.gate_health_stats({"gate_allow_routed": 5})
    rendered = analyze_routing.render_gate_health(stats)
    assert stats["total_gate_calls"] > 0
    assert "Judgment-on-fast" not in rendered


def test_receipt_renders_applied_changes(monkeypatch):
    change_id = "abc123receipt"
    changelog = {
        "last_receipt_ts": None,
        "changes": [{
            "change_id": change_id,
            "ts": "2025-01-09T10:00:00+00:00",
            "intent": "qa_with_reads",
            "from_role": "FW",
            "to_role": "RB",
            "confidence": 0.857,
            "blast_radius": "low",
            "status": "applied",
            "rollback_reason": None,
        }],
    }

    saved: dict = {}

    def _load(path):
        if path.name == "policy_changelog.json":
            return changelog
        if path.name == "confidence_scores.json":
            return {"entries": []}
        return {}

    def _save(path, data):
        saved[path.name] = data

    monkeypatch.setattr(session_summary, "load_json_state", _load)
    monkeypatch.setattr(session_summary, "save_json_state", _save)
    monkeypatch.setattr(cost_report_renderer, "load_json_state", _load)
    monkeypatch.setattr(cost_report_renderer, "save_json_state", _save)
    monkeypatch.setattr(session_summary, "read_events", lambda: [])
    monkeypatch.setattr(session_summary, "load_pricing", lambda: {})
    monkeypatch.setattr(session_summary, "price_for", lambda m, p: {"input_per_1k": 0.003, "output_per_1k": 0.015})
    monkeypatch.setattr(session_summary, "now_iso", lambda: "2025-02-10T12:00:00+00:00")

    receipt = session_summary._render_receipt()
    assert isinstance(receipt, str)
    assert change_id[:6] in receipt


def test_render_warnings_empty_state(monkeypatch, tmp_path):
    monkeypatch.setattr(session_summary, "WARNINGS_LOG", tmp_path / "data" / "warnings.jsonl")
    import cost_report_renderer
    monkeypatch.setattr(cost_report_renderer, "WARNINGS_LOG", tmp_path / "data" / "warnings.jsonl")
    result = session_summary._render_warnings()
    assert result == "No silent failures recorded."


def test_warnings_flag_exits_zero(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(session_summary, "WARNINGS_LOG", tmp_path / "data" / "warnings.jsonl")
    import cost_report_renderer
    monkeypatch.setattr(cost_report_renderer, "WARNINGS_LOG", tmp_path / "data" / "warnings.jsonl")
    monkeypatch.setattr(sys, "argv", ["session_summary.py", "--warnings"])

    exit_code = session_summary.main()

    assert exit_code == 0
    assert "No silent failures recorded." in capsys.readouterr().out


def test_savings_computation_multi_tier(monkeypatch):
    sid = "savings-sess"
    pricing = {
        "claude-haiku-4-5-20251001": {"input_per_1k": 0.001, "output_per_1k": 0.002},
        "claude-opus-4-6": {"input_per_1k": 0.015, "output_per_1k": 0.075},
    }
    events = [
        {**_tool_event(sid, tier="fast", tokens=1000), "model": "claude-haiku-4-5-20251001"},
        {**_tool_event(sid, tier="reasoning", tokens=500), "model": "claude-opus-4-6"},
    ]
    monkeypatch.setattr(session_summary, "read_events", lambda: events)
    monkeypatch.setattr(session_summary, "load_pricing", lambda: pricing)

    report = session_summary.render_report(limit=10)
    assert isinstance(report, str)
    assert len(report) > 0


# ===========================================================================
# --- from test_session_impact.py ---
# ===========================================================================

def _make_report(
    has_enough_data: bool = True,
    total_spent: float = 1.50,
    total_est_saved: float = 0.75,
    routing_savings: float = 0.60,
    waste_prevented: float = 0.15,
    routing_rate: float = 0.70,
    prev_week_routing_rate: float | None = None,
    total_sessions: int = 5,
    total_turns: int = 20,
    gate_breakdown: dict | None = None,
) -> ic.ImpactReport:
    return ic.ImpactReport(
        routing_savings=routing_savings,
        waste_prevented=waste_prevented,
        total_est_saved=total_est_saved,
        routing_rate=routing_rate,
        prev_week_routing_rate=prev_week_routing_rate,
        total_spent=total_spent,
        total_sessions=total_sessions,
        total_turns=total_turns,
        gate_breakdown=gate_breakdown or {},
        top_moments=[],
        baseline_model="claude-sonnet-4-6",
        has_enough_data=has_enough_data,
    )


class TestFormatSessionBox(unittest.TestCase):

    def test_contains_cost_router_header(self) -> None:
        box = si.format_session_box(_make_report())
        self.assertIn("cost-router", box)

    def test_contains_spent_amount(self) -> None:
        box = si.format_session_box(_make_report(total_spent=2.34))
        self.assertIn("2.34", box)

    def test_contains_saved_amount(self) -> None:
        box = si.format_session_box(_make_report(total_est_saved=0.99))
        self.assertIn("0.99", box)

    def test_contains_routing_savings(self) -> None:
        box = si.format_session_box(_make_report(routing_savings=0.60))
        self.assertIn("0.60", box)

    def test_no_trend_line_when_prev_week_none(self) -> None:
        box = si.format_session_box(_make_report(prev_week_routing_rate=None))
        self.assertNotIn("Trend", box)

    def test_trend_line_present_when_prev_week_set(self) -> None:
        box = si.format_session_box(_make_report(prev_week_routing_rate=0.50, routing_rate=0.70))
        self.assertIn("Trend", box)

    def test_box_is_non_empty_string(self) -> None:
        box = si.format_session_box(_make_report())
        self.assertIsInstance(box, str)
        self.assertGreater(len(box), 10)


class TestShouldShowSummary(unittest.TestCase):

    def _config(self, min_turns: int = 1, min_savings_usd: float = 0.01, cold_start_sessions: int = 0) -> dict:
        return {
            "min_turns": min_turns,
            "min_savings_usd": min_savings_usd,
            "cold_start_sessions": cold_start_sessions,
        }

    def test_show_box_when_enough_data(self) -> None:
        report = _make_report(has_enough_data=True, total_est_saved=0.50, total_turns=5)
        result = si.should_show_summary(report, self._config(), total_historical_sessions=10)
        self.assertEqual(result, "show_box")

    def test_suppress_when_not_enough_data_and_cold_start_done(self) -> None:
        report = _make_report(has_enough_data=False, total_est_saved=0.0)
        result = si.should_show_summary(report, self._config(cold_start_sessions=0, min_savings_usd=0.01), total_historical_sessions=5)
        self.assertEqual(result, "suppress")

    def test_show_cold_start_when_below_cold_start_threshold(self) -> None:
        report = _make_report(has_enough_data=False)
        result = si.should_show_summary(report, self._config(cold_start_sessions=3), total_historical_sessions=1)
        self.assertEqual(result, "show_cold_start")

    def test_suppress_when_savings_below_threshold(self) -> None:
        report = _make_report(has_enough_data=True, total_est_saved=0.001, total_turns=5)
        result = si.should_show_summary(report, self._config(min_savings_usd=0.01), total_historical_sessions=10)
        self.assertEqual(result, "suppress")

    def test_suppress_when_turns_below_min(self) -> None:
        report = _make_report(has_enough_data=True, total_est_saved=1.00, total_turns=0)
        result = si.should_show_summary(report, self._config(min_turns=1), total_historical_sessions=10)
        self.assertEqual(result, "suppress")


class TestMainOutput(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._orig_data_dir = si.DATA_DIR
        self._orig_usage_log = si.USAGE_LOG
        si.DATA_DIR = self._tmp
        si.USAGE_LOG = self._tmp / "routing_events.jsonl"
        si.USAGE_LOG.touch()
        os.environ.pop("COST_ROUTER_SESSION_ID", None)

    def tearDown(self) -> None:
        si.DATA_DIR = self._orig_data_dir
        si.USAGE_LOG = self._orig_usage_log
        self._tmpdir.cleanup()

    def _write_session_log(self, session_id: str) -> None:
        log = self._tmp / "session_log.jsonl"
        entry = {"session_id": session_id, "ts": "2026-09-04T12:00:00+00:00", "est_output_tokens": 1000}
        log.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    def test_main_prints_box_to_stderr_when_data_available(self) -> None:
        self._write_session_log("test-sess")
        report = _make_report(has_enough_data=True, total_spent=1.50, total_est_saved=0.75)

        with patch("session_impact.compute_session", return_value=report), \
             patch("session_impact.should_show_summary", return_value="show_box"), \
             patch("session_impact._count_historical_sessions", return_value=10), \
             patch("session_impact._load_config_impact", return_value={"session_summary": True, "min_turns": 1, "min_savings_usd": 0.01, "cold_start_sessions": 0}), \
             patch("session_impact._is_sunday_auto_trigger", return_value=False), \
             patch("session_impact._check_warning_threshold"), \
             patch("sys.exit"):
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                si.main()
            output = stderr_buf.getvalue()

        self.assertIn("cost-router", output)
        self.assertIn("1.50", output)

    def test_main_suppresses_output_when_disabled(self) -> None:
        self._write_session_log("test-sess")

        with patch("session_impact._load_config_impact", return_value={"session_summary": False}):
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                si.main()
            output = stderr_buf.getvalue()

        self.assertEqual(output, "")

    def test_main_suppresses_when_no_session_log(self) -> None:
        stderr_buf = io.StringIO()
        with patch("session_impact._load_config_impact", return_value={"session_summary": True, "min_turns": 1, "min_savings_usd": 0.01, "cold_start_sessions": 0}), \
             patch("session_impact._check_warning_threshold"), \
             patch("sys.stderr", stderr_buf):
            si.main()
        self.assertEqual(stderr_buf.getvalue(), "")

    def test_main_shows_cold_start_message(self) -> None:
        self._write_session_log("test-sess")
        report = _make_report(has_enough_data=False)

        with patch("session_impact.compute_session", return_value=report), \
             patch("session_impact.should_show_summary", return_value="show_cold_start"), \
             patch("session_impact._count_historical_sessions", return_value=1), \
             patch("session_impact._load_config_impact", return_value={"session_summary": True, "min_turns": 1, "min_savings_usd": 0.01, "cold_start_sessions": 3}), \
             patch("session_impact._is_sunday_auto_trigger", return_value=False), \
             patch("session_impact._check_warning_threshold"), \
             patch("sys.exit"):
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                si.main()
            output = stderr_buf.getvalue()

        self.assertIn("learning", output.lower())


class TestCheckWarningThreshold:
    def _make_warnings_log(self, path: Path, entries: list[tuple[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for ts, event in entries:
                f.write(json.dumps({"ts": ts, "event": event}) + "\n")

    def test_old_warnings_not_counted(self, tmp_path, monkeypatch) -> None:
        warnings_path = tmp_path / "warnings.jsonl"
        entries = [("2020-01-01T00:00:00+00:00", "old_failure") for _ in range(10)]
        self._make_warnings_log(warnings_path, entries)

        monkeypatch.setattr(si, "WARNINGS_LOG", warnings_path)
        monkeypatch.setattr(si, "_SENTINEL", tmp_path / ".sentinel_does_not_exist")

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            si._check_warning_threshold()

        assert stderr_buf.getvalue() == ""

    def test_recent_warnings_above_threshold_triggers_banner(self, tmp_path, monkeypatch) -> None:
        from _common import now_iso
        warnings_path = tmp_path / "warnings.jsonl"
        entries = [(now_iso(), f"failure_{i}") for i in range(6)]
        self._make_warnings_log(warnings_path, entries)

        monkeypatch.setattr(si, "WARNINGS_LOG", warnings_path)
        monkeypatch.setattr(si, "_SENTINEL", tmp_path / ".sentinel_does_not_exist")

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            si._check_warning_threshold()

        assert "silent failures" in stderr_buf.getvalue()

    def test_mixed_old_and_recent_counts_only_recent(self, tmp_path, monkeypatch) -> None:
        from _common import now_iso
        warnings_path = tmp_path / "warnings.jsonl"
        entries = (
            [("2020-01-01T00:00:00+00:00", "old_failure") for _ in range(3)] +
            [(now_iso(), f"recent_failure_{i}") for i in range(3)]
        )
        self._make_warnings_log(warnings_path, entries)

        monkeypatch.setattr(si, "WARNINGS_LOG", warnings_path)
        monkeypatch.setattr(si, "_SENTINEL", tmp_path / ".sentinel_does_not_exist")

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            si._check_warning_threshold()

        assert stderr_buf.getvalue() == ""

    def test_sentinel_prevents_repeat_banner(self, tmp_path, monkeypatch) -> None:
        from _common import now_iso
        warnings_path = tmp_path / "warnings.jsonl"
        sentinel_path = tmp_path / ".warnings_alerted"
        entries = [(now_iso(), f"failure_{i}") for i in range(6)]
        self._make_warnings_log(warnings_path, entries)
        sentinel_path.touch()

        monkeypatch.setattr(si, "WARNINGS_LOG", warnings_path)
        monkeypatch.setattr(si, "_SENTINEL", sentinel_path)

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            si._check_warning_threshold()

        assert stderr_buf.getvalue() == ""


# ===========================================================================
# --- from test_session_log_pipeline.py ---
# ===========================================================================

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"


def _iso(offset_days: int = 0) -> str:
    import datetime
    dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=offset_days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_iso(offset_days: int = 0) -> str:
    import datetime
    d = datetime.date.today() + datetime.timedelta(days=offset_days)
    return d.isoformat()


def test_estimate_tokens_response_event_written(tmp_path, monkeypatch):
    transcript_file = tmp_path / "transcript.jsonl"
    assistant_turn = {
        "role": "assistant",
        "content": [{"type": "text", "text": "Here is the fixed implementation:\n\n```python\nprint('hello')\n```"}],
        "usage": {
            "input_tokens": 1500,
            "output_tokens": 42,
            "cache_read_input_tokens": 0,
        },
    }
    transcript_file.write_text(json.dumps(assistant_turn) + "\n")

    usage_log = tmp_path / "token_events.jsonl"
    monkeypatch.setenv("COST_ROUTER_DATA_DIR", str(tmp_path))

    import importlib
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_common, "USAGE_LOG", tmp_path / "routing_events.jsonl")
    monkeypatch.setattr(_fileio, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_events, "DATA_DIR", tmp_path)

    stop_payload = {
        "hook_event_name": "Stop",
        "session_id": "test-sess-estimate-1",
        "transcript_path": str(transcript_file),
    }

    import estimate_tokens as et
    import importlib as il
    il.reload(et)

    text = et._text_for_response(stop_payload)
    assert text, "Expected non-empty text from transcript_path fallback after fix"

    from _model_utils import estimate_tokens as _est, tier_of_model

    model = stop_payload.get("model")
    record = {
        "type": "response",
        "session_id": "test-sess-estimate-1",
        "tier": tier_of_model(model),
        "model": model,
        "direction": "output",
        "bytes": len(text),
        "est_tokens": _est(text),
        "token_source": "heuristic",
    }
    append_event(record)

    assert usage_log.exists(), "token_events.jsonl was not created"
    lines = [l for l in usage_log.read_text().splitlines() if l.strip()]
    assert lines, "token_events.jsonl is empty"

    records = [json.loads(l) for l in lines]
    output_records = [r for r in records if r.get("direction") == "output"]
    assert output_records, "No output-direction record found in usage.jsonl"

    rec = output_records[-1]
    assert rec.get("est_tokens", 0) > 0


def test_session_summary_writes_session_log_with_tier(tmp_path, monkeypatch):
    session_id = "test-sess-1"
    model = "claude-haiku-4-5-20251001"

    usage_log = tmp_path / "routing_events.jsonl"
    events = []
    for i in range(3):
        events.append({
            "type": "response",
            "session_id": session_id,
            "direction": "output",
            "tier": "fast",
            "model": model,
            "est_tokens": 500,
            "token_source": "heuristic",
            "bytes": 2000,
            "ts": _iso(),
        })
    usage_log.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    monkeypatch.setenv("COST_ROUTER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_common, "USAGE_LOG", usage_log)

    stop_payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": session_id,
        "cwd": str(tmp_path),
    })

    env = {
        **__import__("os").environ,
        "COST_ROUTER_DATA_DIR": str(tmp_path),
    }

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "session_summary.py")],
        input=stop_payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    session_log = tmp_path / "session_log.jsonl"
    assert session_log.exists(), (
        f"session_log.jsonl was not created by session_summary.py stop processing. "
        f"stderr={result.stderr!r}"
    )

    lines = [l for l in session_log.read_text().splitlines() if l.strip()]
    assert lines, "session_log.jsonl is empty"

    records = [json.loads(l) for l in lines]
    matching = [r for r in records if r.get("session_id") == session_id]
    assert matching, f"No record with session_id=={session_id!r} in session_log.jsonl"

    rec = matching[0]
    assert rec.get("tier") == "fast"
    assert rec.get("est_output_tokens") == 500


# NOTE (TEST-006): test_compute_window_savings_positive_with_fast_tier was an
# exact duplicate of the test of the same name in test_journey_report_stage.py
# (same patched entries/pricing/config, same assertions) and was removed from
# here. See test_journey_report_stage.py for the surviving copy.


# ===========================================================================
# --- from test_cost_tracking.py ---
# ===========================================================================

def _record(n: int, session: str = "sess") -> dict:
    return {
        "type": "tool",
        "session_id": f"{session}-{n}",
        "direction": "context",
        "tool_name": "Edit",
        "bytes": 100 + n,
        "est_tokens": 25 + n,
        "target_hash": f"hash{n:012d}",
    }


def test_append_read_roundtrip():
    """append_event x 3 -> read_events returns all 3 in order."""
    for i in range(3):
        append_event(_record(i))

    events = read_events()
    assert len(events) == 3
    for ev in events:
        assert "ts" in ev
        assert json.dumps(ev)


def test_corrupt_jsonl_line_skipped():
    """A corrupt JSONL line is silently skipped; valid lines still returned."""
    usage_log: Path = _common.USAGE_LOG
    usage_log.parent.mkdir(parents=True, exist_ok=True)
    usage_log.write_text(
        '{"type":"tool","session_id":"ok","ts":"2025-01-01"}\n'
        "{not valid json}\n"
        '{"type":"tool","session_id":"ok2","ts":"2025-01-02"}\n',
        encoding="utf-8",
    )
    events = read_events()
    assert len(events) == 2
    assert all(isinstance(e, dict) for e in events)


def test_read_events_missing_file_returns_empty():
    """read_events on a non-existent file returns []."""
    assert read_events() == []


def test_token_estimation_positive():
    """estimate_tokens returns a positive integer for any non-empty text."""
    text = "def authenticate(user, password):\n    return user == 'admin'\n"
    count = estimate_tokens(text)
    assert isinstance(count, int)
    assert count > 0


def _make_session_lines(session_id: str, count: int, ts_prefix: str = "2025-01-01") -> list[str]:
    return [
        json.dumps({"type": "tool", "session_id": session_id, "ts": f"{ts_prefix}T00:00:{i:02d}Z", "bytes": 100})
        for i in range(count)
    ]


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_compact_under_threshold_no_op():
    usage_log: Path = compact_log.USAGE_LOG
    small_content = json.dumps({"type": "tool", "session_id": "s1", "ts": "2025-01-01T00:00:00Z"}) + "\n"
    _write_lines(usage_log, [small_content.strip()])
    original_mtime = usage_log.stat().st_mtime
    compact_usage_log()
    assert usage_log.stat().st_mtime == original_mtime


def test_compact_over_threshold_file_not_deleted(isolated_data):
    usage_log: Path = compact_log.USAGE_LOG
    needed = WORKING_SET_BYTES + 1
    lines = []
    size = 0
    i = 0
    while size < needed:
        line = json.dumps({"type": "tool", "session_id": "sess1", "ts": f"2025-01-01T00:00:{i % 60:02d}Z", "data": "x" * 500})
        lines.append(line)
        size += len(line.encode()) + 1
        i += 1
    _write_lines(usage_log, lines)
    assert usage_log.stat().st_size > WORKING_SET_BYTES

    compact_usage_log()

    assert usage_log.exists(), "usage.jsonl must not be deleted"
    assert usage_log.stat().st_size > 0, "usage.jsonl must not be empty after compaction"


def test_compact_working_set_within_budget(isolated_data):
    usage_log: Path = compact_log.USAGE_LOG
    newer_lines = _make_session_lines("new-sess", 5, "2025-06-01")
    big_lines = []
    size = 0
    i = 0
    while size <= WORKING_SET_BYTES + 20_000:
        line = json.dumps({"type": "tool", "session_id": "old-sess", "ts": f"2024-01-01T00:{i//60:02d}:{i%60:02d}Z", "data": "y" * 1000})
        big_lines.append(line)
        size += len(line.encode()) + 1
        i += 1
    combined = big_lines + newer_lines
    _write_lines(usage_log, combined)

    if usage_log.stat().st_size <= WORKING_SET_BYTES:
        pytest.skip("test setup didn't exceed threshold")

    compact_usage_log()

    assert usage_log.exists()
    events = read_events()
    session_ids = {e.get("session_id") for e in events}
    assert "new-sess" in session_ids


def test_compact_no_data_loss(isolated_data):
    usage_log: Path = compact_log.USAGE_LOG
    usage_archive: Path = compact_log.USAGE_ARCHIVE

    old_lines = []
    size = 0
    i = 0
    while size <= WORKING_SET_BYTES + 20_000:
        line = json.dumps({"type": "tool", "session_id": "old", "ts": f"2024-01-01T00:{i//60:02d}:{i%60:02d}Z", "data": "z" * 800})
        old_lines.append(line)
        size += len(line.encode()) + 1
        i += 1
    new_lines = _make_session_lines("new", 3, "2025-06-01")
    _write_lines(usage_log, old_lines + new_lines)

    if usage_log.stat().st_size <= WORKING_SET_BYTES:
        pytest.skip("test setup didn't exceed threshold")

    compact_usage_log()

    working_events = read_events()
    archive_events = []
    if usage_archive.exists():
        for line in usage_archive.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    archive_events.append(json.loads(line))
                except Exception:
                    pass

    all_session_ids = {e.get("session_id") for e in working_events + archive_events}
    assert "old" in all_session_ids or "new" in all_session_ids
    total_events = len(working_events) + len(archive_events)
    assert total_events == len(old_lines) + len(new_lines)


def test_compact_unknown_session_goes_to_archive(isolated_data):
    usage_log: Path = compact_log.USAGE_LOG
    usage_archive: Path = compact_log.USAGE_ARCHIVE

    unknown_lines = []
    for i in range(50):
        unknown_lines.append(json.dumps({"type": "tool", "ts": f"2025-01-01T00:00:{i:02d}Z", "data": "u" * 500}))
    known_lines = []
    size = 0
    i = 0
    while size <= WORKING_SET_BYTES + 20_000:
        line = json.dumps({"type": "tool", "session_id": "known", "ts": f"2025-06-01T00:{i//60:02d}:{i%60:02d}Z", "data": "k" * 500})
        known_lines.append(line)
        size += len(line.encode()) + 1
        i += 1
    _write_lines(usage_log, unknown_lines + known_lines)

    if usage_log.stat().st_size <= WORKING_SET_BYTES:
        pytest.skip("test setup didn't exceed threshold")

    compact_usage_log()

    working_events = read_events()
    for ev in working_events:
        assert ev.get("session_id"), "Unknown-session events must not remain in usage.jsonl"

    assert usage_archive.exists()
    archive_text = usage_archive.read_text()
    archive_events = [json.loads(ln) for ln in archive_text.splitlines() if ln.strip()]
    unknown_in_archive = [e for e in archive_events if not e.get("session_id")]
    assert len(unknown_in_archive) == len(unknown_lines)


def test_compact_single_oversized_session_kept(isolated_data):
    usage_log: Path = compact_log.USAGE_LOG

    lines = []
    size = 0
    i = 0
    while size <= WORKING_SET_BYTES + 10_000:
        line = json.dumps({"type": "tool", "session_id": "giant", "ts": f"2025-06-01T00:{i//60:02d}:{i%60:02d}Z", "data": "g" * 1000})
        lines.append(line)
        size += len(line.encode()) + 1
        i += 1
    _write_lines(usage_log, lines)

    compact_usage_log()

    assert usage_log.exists()
    events = read_events()
    assert len(events) > 0
    session_ids = {e.get("session_id") for e in events}
    assert "giant" in session_ids


def test_compact_interleaved_sessions_grouped(isolated_data):
    usage_log: Path = compact_log.USAGE_LOG
    usage_archive: Path = compact_log.USAGE_ARCHIVE

    lines = []
    for i in range(3):
        lines.append(json.dumps({"type": "tool", "session_id": "sess-B", "ts": f"2025-06-01T00:00:{i:02d}Z", "data": "b" * 10}))
    size = 0
    i = 0
    while size <= WORKING_SET_BYTES + 20_000:
        line = json.dumps({"type": "tool", "session_id": "sess-A", "ts": f"2024-01-01T00:{i//60:02d}:{i%60:02d}Z", "data": "a" * 700})
        lines.append(line)
        size += len(line.encode()) + 1
        i += 1
    _write_lines(usage_log, lines)

    if usage_log.stat().st_size <= WORKING_SET_BYTES:
        pytest.skip("test setup didn't exceed threshold")

    compact_usage_log()

    working = read_events()
    archive_events = []
    if usage_archive.exists():
        for line in usage_archive.read_text().splitlines():
            if line.strip():
                archive_events.append(json.loads(line))

    working_ids = {e.get("session_id") for e in working}
    archive_ids = {e.get("session_id") for e in archive_events}
    assert "sess-B" in working_ids
    assert "sess-A" not in working_ids or "sess-A" not in archive_ids


def test_compact_archive_rotation(isolated_data, tmp_path):
    usage_log: Path = compact_log.USAGE_LOG
    usage_archive: Path = compact_log.USAGE_ARCHIVE

    pre_archive_data = "x" * (ARCHIVE_ROTATE_BYTES - 100)
    usage_archive.write_text(pre_archive_data, encoding="utf-8")

    lines = []
    size = 0
    i = 0
    while size <= WORKING_SET_BYTES + 10_000:
        line = json.dumps({"type": "tool", "session_id": "evict-me", "ts": f"2024-01-01T00:{i//60:02d}:{i%60:02d}Z", "data": "e" * 500})
        lines.append(line)
        size += len(line.encode()) + 1
        i += 1
    lines.append(json.dumps({"type": "tool", "session_id": "keep", "ts": "2025-06-01T00:00:00Z"}))
    _write_lines(usage_log, lines)

    compact_usage_log()

    rotated = usage_archive.parent / "usage_archive.1.jsonl"
    assert rotated.exists()


def test_compact_atomic_write(isolated_data, monkeypatch):
    usage_log: Path = compact_log.USAGE_LOG
    original_replace = os.replace

    replace_called = []

    def mock_replace(src, dst):
        replace_called.append((src, dst))
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", mock_replace)
    monkeypatch.setattr(compact_log.os, "replace", mock_replace)

    lines = []
    size = 0
    i = 0
    while size <= WORKING_SET_BYTES + 5_000:
        line = json.dumps({"type": "tool", "session_id": "atomic-test", "ts": f"2025-01-01T00:{i//60:02d}:{i%60:02d}Z", "data": "a" * 200})
        lines.append(line)
        size += len(line.encode()) + 1
        i += 1
    _write_lines(usage_log, lines)

    compact_usage_log()

    assert any(str(usage_log) in str(dst) for _, dst in replace_called)


def test_session_impact_no_data_returns_gracefully(isolated_data, monkeypatch):
    import session_impact
    usage_log: Path = compact_log.USAGE_LOG
    if usage_log.exists():
        usage_log.unlink()

    monkeypatch.setattr(session_impact, "USAGE_LOG", usage_log)

    warned = []
    monkeypatch.setattr(session_impact, "_warn", lambda t, d=None: warned.append(t))

    try:
        session_impact.main()
    except Exception as e:
        pytest.fail(f"session_impact.main() raised unexpectedly: {e}")

    assert any("session_impact_no_data" in w for w in warned)


# ===========================================================================
# --- from test_round_trip_budget.py ---
# ===========================================================================

def _iso_rt(days_ago: float = 0.0) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).isoformat()


class _TrackerBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._stats = tmp / "agent_call_stats.json"
        self._rstate = tmp / "routing_state.json"

        import spawn_tracker as _st
        self._orig = {
            "stats": act.STATS_FILE,
            "spawn_stats": _st.STATS_FILE,
            "rstate": act.ROUTING_STATE_FILE,
            "append_event": act.append_event,
            "spawn_append_event": _st.append_event,
            "limit": act.SESSION_LIMIT_AGENT_CALL_TRACKER,
            "spawn_limit": _st.SESSION_LIMIT_AGENT_CALL_TRACKER,
            "cr_routing": cr.get_routing_for_intent,
        }
        act.STATS_FILE = self._stats
        _st.STATS_FILE = self._stats
        act.ROUTING_STATE_FILE = self._rstate
        self.events: list[dict] = []
        act.append_event = self.events.append
        _st.append_event = self.events.append
        self.addCleanup(self._restore)

    def _events_of(self, event_type: str) -> list[dict]:
        return [e for e in self.events if isinstance(e, dict) and e.get("type") == event_type]

    def _set_session_limit(self, n: int) -> None:
        import spawn_tracker as _st
        act.SESSION_LIMIT_AGENT_CALL_TRACKER = n
        _st.SESSION_LIMIT_AGENT_CALL_TRACKER = n

    def _restore(self) -> None:
        import spawn_tracker as _st
        act.STATS_FILE = self._orig["stats"]
        _st.STATS_FILE = self._orig["spawn_stats"]
        act.ROUTING_STATE_FILE = self._orig["rstate"]
        act.append_event = self._orig["append_event"]
        _st.append_event = self._orig["spawn_append_event"]
        act.SESSION_LIMIT_AGENT_CALL_TRACKER = self._orig["limit"]
        _st.SESSION_LIMIT_AGENT_CALL_TRACKER = self._orig["spawn_limit"]
        cr.get_routing_for_intent = self._orig["cr_routing"]
        self._tmp.cleanup()

    def _set_routing(self, **routing: object) -> None:
        cr.get_routing_for_intent = lambda _intent: dict(routing)

    def _dispatch(self, session_id: str, model: str | None = "sonnet", **tool_input: object) -> dict:
        payload = {
            "tool_name": "Task",
            "session_id": session_id,
            "tool_input": {"prompt": "do the thing", "model": model, **tool_input},
        }
        return act.event_preuse(payload, False)

    def _session(self, session_id: str) -> dict:
        data = json.loads(self._stats.read_text(encoding="utf-8"))
        return data["sessions"][session_id]

    def _write_sessions(self, sessions: dict) -> None:
        self._stats.write_text(json.dumps({"sessions": sessions}), encoding="utf-8")


class TestTicket01SessionPruning(_TrackerBase):
    def test_last_activity_ts_parses_missing_and_garbage(self) -> None:
        self.assertGreater(act._session_last_activity_ts({"last_activity": _iso_rt(0)}), 0.0)
        self.assertEqual(act._session_last_activity_ts({}), 0.0)
        self.assertEqual(act._session_last_activity_ts({"last_activity": "not-a-date"}), 0.0)
        self.assertEqual(act._session_last_activity_ts({"last_activity": None}), 0.0)
        self.assertEqual(act._session_last_activity_ts("not-a-dict"), 0.0)

    def test_ttl_prune_drops_stale_sessions(self) -> None:
        act._save_stats({
            "fresh": {"dispatch_count": 3, "last_activity": _iso_rt(1)},
            "stale": {"dispatch_count": 99, "last_activity": _iso_rt(20)},
        })
        kept = json.loads(self._stats.read_text(encoding="utf-8"))["sessions"]
        self.assertIn("fresh", kept)
        self.assertNotIn("stale", kept)

    def test_legacy_session_without_last_activity_is_pruned_not_crashed(self) -> None:
        act._save_stats({
            "legacy": {"dispatch_count": 12},
            "current": {"dispatch_count": 1, "last_activity": _iso_rt(0)},
        })
        kept = json.loads(self._stats.read_text(encoding="utf-8"))["sessions"]
        self.assertIn("current", kept)
        self.assertNotIn("legacy", kept)

    def test_count_cap_evicts_by_recency_not_alphabetically(self) -> None:
        self._set_session_limit(2)
        act._save_stats({
            "aaa": {"dispatch_count": 1, "last_activity": _iso_rt(0)},
            "bbb": {"dispatch_count": 1, "last_activity": _iso_rt(3)},
            "ccc": {"dispatch_count": 1, "last_activity": _iso_rt(6)},
        })
        kept = json.loads(self._stats.read_text(encoding="utf-8"))["sessions"]
        self.assertEqual(set(kept), {"aaa", "bbb"})

    def test_event_preuse_stamps_last_activity_on_allow(self) -> None:
        self._set_routing(max_round_trips=8)
        out = self._dispatch("sid-allow")
        self.assertEqual(out.get("permission"), "allow")
        self.assertTrue(self._session("sid-allow").get("last_activity"))

    def test_prune_emits_measurable_telemetry(self) -> None:
        self._set_session_limit(1)
        act._save_stats({
            "stale": {"dispatch_count": 99, "last_activity": _iso_rt(30)},
            "older": {"dispatch_count": 2, "last_activity": _iso_rt(4)},
            "newest": {"dispatch_count": 1, "last_activity": _iso_rt(0)},
        })
        pruned = {(e["session_id"], e["reason"]) for e in self._events_of("session_pruned")}
        self.assertIn(("stale", "ttl"), pruned)
        self.assertIn(("older", "count_cap"), pruned)
        self.assertNotIn("newest", {sid for sid, _r in pruned})

    def test_concurrent_save_stats_does_not_drop_writes(self) -> None:
        n_threads = 20
        barrier = threading.Barrier(n_threads)

        def _writer(idx: int) -> None:
            barrier.wait()
            act._save_stats({
                f"concurrent-{idx}": {"dispatch_count": 1, "last_activity": _iso_rt(0)},
            })

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        kept = json.loads(self._stats.read_text(encoding="utf-8"))["sessions"]
        expected = {f"concurrent-{i}" for i in range(n_threads)}
        self.assertEqual(expected - set(kept), set())

    def test_config_declares_ttl_key(self) -> None:
        config = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
        ttl_days = config["session_limits"]["agent_call_tracker_ttl_days"]
        self.assertIsInstance(ttl_days, int)
        self.assertGreater(ttl_days, 0)


class TestTicket014FinalizeDispatchChargeWiring(_TrackerBase):
    """TICKET-014: main() must route SubagentStop events (Claude Code fires
    agent_call_tracker.py with "--event subagent_stop", see hooks/hooks.json)
    to spawn_tracker.finalize_dispatch_charge instead of always calling the
    PreToolUse-shaped event_preuse handler. See agent_call_tracker.py:main()."""

    def _run_stop(self, session_id: str, agent_id: str) -> int:
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": session_id,
            "agent_id": agent_id,
        }
        with patch.object(act, "read_stdin_json", return_value=payload), \
             patch.object(sys, "argv", ["agent_call_tracker.py", "--event", "subagent_stop"]):
            return act.main()

    def test_main_calls_finalize_dispatch_charge_on_subagent_stop_event(self) -> None:
        # finalize charges once per DISPATCHED agent_id (registry entry with
        # dispatch metadata), at that entry's model rate. Use a real dispatched
        # registry entry from the accounting fixture, not a bare stop payload.
        import _accounting_fixture as acct
        import spawn_tracker as _st

        session = acct.load_fixture()["sess-b11b"]
        dispatched = acct.dispatched_entries(session)
        first = dispatched[0]
        registry_file = Path(self._tmp.name) / "dispatch_registry.json"
        acct.write_registry(registry_file, "sid-finalize", {first["agent_id"]: first})
        self._write_sessions({
            "sid-finalize": {
                "agents": [], "explore_without_haiku": 0, "total_agents": 0,
                "dispatch_count": 0, "cumulative_cost_usd": 0.0,
                "last_activity": _iso_rt(0),
            }
        })

        with patch.object(_st, "DISPATCH_REGISTRY_FILE", registry_file):
            exit_code = self._run_stop("sid-finalize", first["agent_id"])

        self.assertEqual(exit_code, 0)
        sess = self._session("sid-finalize")
        self.assertGreater(sess["cumulative_cost_usd"], 0.0)
        self.assertAlmostEqual(
            sess["cumulative_cost_usd"], acct.nominal_cost_usd(first["model"]), places=6
        )
        self.assertEqual(sess["charged_agent_ids"], [first["agent_id"]])

    def test_main_subagent_stop_for_stop_only_id_charges_nothing(self) -> None:
        import _accounting_fixture as acct
        import spawn_tracker as _st

        session = acct.load_fixture()["sess-b11b"]
        stop_only = acct.stop_only_ids(session)[0]
        registry_file = Path(self._tmp.name) / "dispatch_registry.json"
        acct.write_registry(
            registry_file, "sid-stop-only", {stop_only: session["registry"]["agents"][stop_only]}
        )
        self._write_sessions({
            "sid-stop-only": {
                "agents": [], "explore_without_haiku": 0, "total_agents": 0,
                "dispatch_count": 0, "cumulative_cost_usd": 0.0,
                "last_activity": _iso_rt(0),
            }
        })

        with patch.object(_st, "DISPATCH_REGISTRY_FILE", registry_file):
            self.assertEqual(self._run_stop("sid-stop-only", stop_only), 0)

        sess = self._session("sid-stop-only")
        self.assertEqual(sess["cumulative_cost_usd"], 0.0)
        self.assertFalse(sess.get("charged_agent_ids"))

    def test_main_still_uses_event_preuse_when_no_stop_event_flag(self) -> None:
        # Cursor's registration (hooks-cursor.json) fires main() at preToolUse
        # with no --event flag -- that path must keep going through
        # event_preuse's allow/deny logic, not finalize_dispatch_charge.
        payload = {
            "tool_name": "Task",
            "session_id": "sid-preuse",
            "tool_input": {"prompt": "do the thing", "model": "sonnet"},
        }
        with patch.object(act, "read_stdin_json", return_value=payload), \
             patch.object(sys, "argv", ["agent_call_tracker.py"]):
            exit_code = act.main()

        self.assertEqual(exit_code, 0)
        self.assertTrue(self._session("sid-preuse").get("last_activity"))


class TestTicket02BudgetObservability(_TrackerBase):
    def setUp(self) -> None:
        super().setUp()
        self._cbs_orig = (cbs.STATS_FILE, cbs.ROUTING_STATE_FILE, cbs.get_routing_for_intent)
        cbs.STATS_FILE = self._stats
        cbs.ROUTING_STATE_FILE = self._rstate
        cbs.get_routing_for_intent = lambda _i: {"max_round_trips": 8}
        self.addCleanup(self._restore_cbs)

    def _restore_cbs(self) -> None:
        cbs.STATS_FILE, cbs.ROUTING_STATE_FILE, cbs.get_routing_for_intent = self._cbs_orig

    def _run_cli(self, *argv: str) -> tuple[int, str]:
        orig_argv = sys.argv
        sys.argv = ["check_budget_status.py", *argv]
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = cbs.main()
        finally:
            sys.argv = orig_argv
        return code, buf.getvalue()

    def test_cli_reports_count_ceiling_remaining_and_cost(self) -> None:
        self._write_sessions({
            "sid-a": {"dispatch_count": 3.5, "cumulative_cost_usd": 1.234, "last_activity": _iso_rt(0)},
        })
        before = self._stats.read_text(encoding="utf-8")
        code, out = self._run_cli("--session-id", "sid-a")

        self.assertEqual(code, 0)
        self.assertIn("dispatch_count: 3.5", out)
        self.assertIn("max_round_trips: 8", out)
        self.assertIn("remaining: 4.5", out)
        self.assertIn("cumulative_cost_usd: $1.23", out)
        self.assertEqual(self._stats.read_text(encoding="utf-8"), before)

    def test_cli_defaults_to_most_recently_active_session(self) -> None:
        self._write_sessions({
            "old": {"dispatch_count": 1, "last_activity": _iso_rt(5)},
            "new": {"dispatch_count": 7, "last_activity": _iso_rt(0)},
        })
        _code, out = self._run_cli()
        self.assertIn("session_id: new", out)

    def test_cli_never_reports_opus_dispatch_count(self) -> None:
        self._write_sessions({
            "sid-o": {"dispatch_count": 2, "opus_dispatch_count": 1, "last_activity": _iso_rt(0)},
        })
        _c, out = self._run_cli("--session-id", "sid-o")
        self.assertNotIn("opus_dispatch_count", out)
        self.assertNotIn("max_opus_round_trips", out)

    def test_cli_fails_open_on_missing_stats_file(self) -> None:
        code, out = self._run_cli()
        self.assertEqual(code, 0)
        self.assertIn("No round-trip budget data found", out)

    def test_cli_fails_open_on_unknown_session_id(self) -> None:
        self._write_sessions({"sid-a": {"dispatch_count": 1, "last_activity": _iso_rt(0)}})
        code, out = self._run_cli("--session-id", "nope")
        self.assertEqual(code, 0)
        self.assertIn("No budget data for session_id='nope'", out)


class TestTicket03WarningThreshold(_TrackerBase):
    def _seed(self, session_id: str, count: float) -> None:
        self._write_sessions({
            session_id: {"agents": [], "dispatch_count": count, "cumulative_cost_usd": 0.0,
                         "last_activity": _iso_rt(0)},
        })

    def test_no_warning_below_threshold(self) -> None:
        self._set_routing(max_round_trips=10)
        self._seed("sid-low", 3)
        out = self._dispatch("sid-low")
        self.assertIsNone(out.get("agent_message"))

    def test_config_declares_default_warning_pct(self) -> None:
        config = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
        warning_pct = config["routing_matrix"]["_default"]["warning_pct"]
        self.assertIsInstance(warning_pct, int)
        self.assertTrue(0 < warning_pct < 100)


class TestOpusSubagentPolicy(_TrackerBase):
    def test_config_has_no_opus_subagent_toggle(self) -> None:
        config = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertNotIn("allow_opus_subagents", config.get("routing", {}))

    def test_routing_matrix_has_no_max_opus_round_trips(self) -> None:
        config = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
        routing_matrix = config["routing_matrix"]
        for intent, entry in routing_matrix.items():
            if not isinstance(entry, dict):
                continue
            with self.subTest(intent=intent):
                self.assertNotIn("max_opus_round_trips", entry)
