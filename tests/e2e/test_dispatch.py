"""E2E tests: dispatch context estimation and gate (scripts/estimate_dispatch_context.py,
scripts/dispatch_context_gate.py).

All file I/O redirected to tmp_path via monkeypatching.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _common  # noqa: E402
import dispatch_context_gate as dcg  # noqa: E402
import estimate_dispatch_context as edc  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(_common, "DATA_DIR", data_dir)
    monkeypatch.setattr(_common, "USAGE_LOG", data_dir / "routing_events.jsonl")
    monkeypatch.setattr(_common, "DISPATCH_REGISTRY_FILE", data_dir / "dispatch_registry.json")
    monkeypatch.setattr(dcg, "DATA_DIR", data_dir)
    return data_dir


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(root))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return root


# 1 ──────────────────────────────────────────────────────────────────────────

def test_estimate_dispatch_context_prompt_only(project_root):
    """Prompt-only estimate: infra_tokens + prompt_tokens, file_tokens == 0."""
    result = edc.estimate_dispatch_context(
        "Fix the login bug in the auth module.",
        model="composer-2.5-fast",
    )
    assert result["infra_tokens"] == _common.infra_tokens("composer-2.5-fast")
    assert result["prompt_tokens"] > 0
    assert result["file_tokens"] == 0
    assert result["total_tokens"] == result["infra_tokens"] + result["prompt_tokens"]
    assert result["est_usd"] > 0


# 2 ──────────────────────────────────────────────────────────────────────────

def test_estimate_dispatch_context_with_file(project_root):
    """File reference adds file_tokens to total."""
    rel = "src/auth.py"
    fpath = project_root / rel
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text("def login():\n    pass\n" * 50, encoding="utf-8")

    result = edc.estimate_dispatch_context(
        f"Edit @{rel} to add validation.",
        file_paths=[rel],
        model="composer-2.5-fast",
    )
    assert result["file_tokens"] > 0
    assert result["total_tokens"] == (
        result["infra_tokens"] + result["prompt_tokens"] + result["file_tokens"]
    )


# 3 ──────────────────────────────────────────────────────────────────────────

def test_resume_skips_infra_tokens(project_root):
    """Resume=True skips the cold-start infra overhead."""
    cold = edc.estimate_dispatch_context("Continue where we left off.", model="composer-2.5-fast", resume=False)
    warm = edc.estimate_dispatch_context("Continue where we left off.", model="composer-2.5-fast", resume=True)
    assert warm["infra_tokens"] == 0
    assert cold["infra_tokens"] > 0


# 4 ──────────────────────────────────────────────────────────────────────────

def test_inline_body_nudge_then_deny():
    """First inline code-body violation → nudge (allow). Second → deny."""
    large_code = "line\n" * 25
    prompt = f"Fix this:\n```python\n{large_code}```\nThanks"
    payload = {"session_id": "dcg-1", "toolInput": {"prompt": prompt}}

    result1 = dcg.event_pre_task(payload, is_claude=True)
    assert result1["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "dispatch-context-inline-override" in result1["hookSpecificOutput"].get("additionalContext", "")

    result2 = dcg.event_pre_task(payload, is_claude=True)
    assert result2["hookSpecificOutput"]["permissionDecision"] == "deny"


# 5 ──────────────────────────────────────────────────────────────────────────

def test_dispatch_registry_roundtrip():
    """mark_registry_busy persists a busy entry; mark_registry_idle clears it."""
    import json as _json
    sid = "reg-sess"
    subagent_id = "agent-abc123"

    edc.mark_registry_busy(sid, subagent_id, subagent_type="Explore", model="haiku")

    reg_file: Path = _common.DISPATCH_REGISTRY_FILE
    assert reg_file.exists(), "registry file must be created after mark_registry_busy"
    data = _json.loads(reg_file.read_text())
    sessions = data.get("sessions", {})
    agents = sessions.get(sid, {}).get("agents", {})
    assert subagent_id in agents, "subagent must appear in registry"
    assert agents[subagent_id]["status"] == "busy"
    assert agents[subagent_id]["model"] == "haiku", "model must be stored in registry"
    assert agents[subagent_id]["subagent_type"] == "Explore", "subagent_type must be stored in registry"

    edc.mark_registry_idle(sid, subagent_id)
    data2 = _json.loads(reg_file.read_text())
    agents2 = data2.get("sessions", {}).get(sid, {}).get("agents", {})
    still_busy = [a for a in agents2.values() if a.get("status") == "busy"]
    assert len(still_busy) == 0, "no agents should be busy after mark_registry_idle"


def test_subagent_usage_record_resolves_model_and_agent_type_from_registry(isolated_data):
    """_subagent_usage_record falls back to registry for model and agent_type when not in payload."""
    import sys
    sys.path.insert(0, str(SCRIPTS.parent))
    from scripts.check_routing import _subagent_usage_record

    sid = "usage-sess"
    subagent_id = "agent-xyz789"

    # Register agent with model and subagent_type
    edc.mark_registry_busy(sid, subagent_id, subagent_type="Explore", model="sonnet")

    # Create SubagentStop payload with NO model and NO agent_type (simulating payload from host)
    payload = {
        "session_id": sid,
        "agent_id": subagent_id,
        "status": "completed",
        "agent_transcript_path": None,
        # Note: no agent_type, no model in payload
    }

    # Build usage record - should fall back to registry for both model and agent_type
    record = _subagent_usage_record(payload, sid)

    assert record["model"] == "sonnet", f"model should be 'sonnet' from registry, got {record['model']}"
    assert record["agent_type"] == "Explore", f"agent_type should be 'Explore' from registry, got {record['agent_type']}"


# =============================================================================
# IMP-204: Session Impact Unit Tests (merged from tests/test_session_impact.py)
# =============================================================================

from scripts.session_impact import (
    format_session_box,
    format_cold_start_message,
    should_show_summary,
    main,
)
from impact_calculator import ImpactReport


def _make_report(**kwargs):
    defaults = dict(
        routing_savings=5.70,
        waste_prevented=0.72,
        total_est_saved=6.42,
        routing_rate=0.87,
        prev_week_routing_rate=None,
        total_spent=1.84,
        total_sessions=1,
        total_turns=10,
        gate_breakdown={'bulk_write_denied': (2, 0.72)},
        top_moments=[],
        baseline_model='opus',
        has_enough_data=True,
    )
    defaults.update(kwargs)
    return ImpactReport(**defaults)


_SESSION_IMPACT_CONFIG = {'min_turns': 3, 'min_savings_usd': 0.10, 'cold_start_sessions': 3}


def test_format_box_basic():
    """Box contains all three dollar amounts and routing rate percentage."""
    report = _make_report()
    box = format_session_box(report)
    assert '$1.84' in box
    assert '$5.70' in box
    assert '$0.72' in box
    assert '87%' in box


def test_cold_start_message():
    """historical sessions < cold_start_sessions -> show_cold_start."""
    report = _make_report(total_turns=5, total_est_saved=1.00)
    result = should_show_summary(report, {'min_turns': 3, 'min_savings_usd': 0.10, 'cold_start_sessions': 3}, 1)
    assert result == 'show_cold_start'


def test_show_box_normal():
    """All thresholds met -> show_box."""
    report = _make_report(total_turns=10, total_est_saved=2.00)
    result = should_show_summary(report, _SESSION_IMPACT_CONFIG, 10)
    assert result == 'show_box'


def test_main_never_raises(tmp_path):
    """Even if compute_session raises, main() exits 0 without re-raising."""
    usage_log = tmp_path / "routing_events.jsonl"
    usage_log.write_text("")
    with patch('scripts.session_impact.USAGE_LOG', usage_log), \
         patch('scripts.session_impact._load_config_impact', return_value={'session_summary': True, **_SESSION_IMPACT_CONFIG}), \
         patch('scripts.session_impact._count_historical_sessions', return_value=10), \
         patch('scripts.session_impact.compute_session', side_effect=RuntimeError('boom')), \
         patch.dict(os.environ, {'COST_ROUTER_SESSION_ID': 'test_session'}):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0


# =============================================================================
# Session Log Pipeline Tests (merged from tests/test_session_log_pipeline.py)
# =============================================================================

