"""Consolidated proposals and reports tests.

Sources:
- test_proposal_writer.py
- test_cost_report_proposals.py
- test_generate_reports.py
- test_impact_calculator.py

Scope note (TEST-006): tests/e2e/test_journey_report_stage.py chains the real
compute_window -> generate_markdown_digest -> generate_html_card path with
isolated DATA_DIR (TICKET-023 fix), but only exercises one gate type
(scope_guard_deny) and happy-path digest/card structure. It does NOT cover:
CSS self-containment, the disclosure footer, the "Policy Trend"/"Notes"
sections, empty-moments messaging, a second gate type (bulk_write_denied),
or the CLI entrypoints (--since, insufficient-data exit) -- the CLI tests
are excluded from the journey by design (see that file's docstring), not
redundant. Only test_digest_filename_uses_until_date duplicated the journey
exactly and was removed; the rest of the report-generation tests below stay
because they cover journey gaps, not because this file is proposal-only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from impact_calculator import (
    ImpactReport,
    _compute_routing_savings,
    _compute_waste_prevented,
    _compute_policy_trend,
    _detect_baseline_model,
    _find_top_moments,
    compute_session,
    compute_window,
)
import impact_calculator as ic
from generate_reports import generate_html_card, generate_markdown_digest
from proposal_writer import (
    load_shadow_evaluation,
    score_proposals,
    write_proposals,
    main as proposal_main,
)


# ===========================================================================
# --- from test_proposal_writer.py ---
# ===========================================================================

def _eval_entry(
    rule_id: str = "R1",
    actual_mean_reward: float = 0.4,
    candidate_expected_reward: float = 0.8,
    improvement_delta: float | None = None,
    sample_count: int = 20,
    actual_role: str = "DIRECT",
    candidate_role: str = "FW",
    selection_bias_warning: bool = False,
) -> dict:
    entry: dict = {
        "rule_id": rule_id,
        "actual_mean_reward": actual_mean_reward,
        "candidate_expected_reward": candidate_expected_reward,
        "improvement_delta": improvement_delta if improvement_delta is not None else (candidate_expected_reward - actual_mean_reward),
        "sample_count": sample_count,
        "actual_role": actual_role,
        "candidate_role": candidate_role,
    }
    if selection_bias_warning:
        entry["selection_bias_warning"] = True
    return entry


def _write_shadow_evaluation(tmp_path: Path, rules: dict) -> None:
    data = {"timestamp": "2026-08-20T00:00:00+00:00", "rules": rules}
    (tmp_path / "shadow_evaluation.json").write_text(json.dumps(data), encoding="utf-8")


def _write_confidence_scores(tmp_path: Path, entries: list[dict]) -> None:
    data = {"updated_at": "2026-08-20T00:00:00+00:00", "window": 20, "entries": entries}
    (tmp_path / "confidence_scores.json").write_text(json.dumps(data), encoding="utf-8")


class TestThresholdFiltering:
    def test_above_threshold_included(self, tmp_path):
        ev = _eval_entry(actual_mean_reward=0.4, candidate_expected_reward=0.7)
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals([ev], threshold=0.15)
        assert len(proposals) == 1
        assert proposals[0]["rule_id"] == "R1"

    def test_below_threshold_excluded(self, tmp_path):
        ev = _eval_entry(actual_mean_reward=0.4, candidate_expected_reward=0.5)
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals([ev], threshold=0.15)
        assert len(proposals) == 0

    def test_exactly_at_threshold_excluded(self, tmp_path):
        ev = _eval_entry(actual_mean_reward=0.4, candidate_expected_reward=0.55)
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals([ev], threshold=0.15)
        assert len(proposals) == 0

    def test_mixed_above_and_below(self, tmp_path):
        evals = [
            _eval_entry(rule_id="R1", actual_mean_reward=0.3, candidate_expected_reward=0.8),
            _eval_entry(rule_id="R2", actual_mean_reward=0.7, candidate_expected_reward=0.75),
            _eval_entry(rule_id="R3", actual_mean_reward=0.2, candidate_expected_reward=0.6),
        ]
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals(evals, threshold=0.15)
        rule_ids = [p["rule_id"] for p in proposals]
        assert "R1" in rule_ids
        assert "R3" in rule_ids
        assert "R2" not in rule_ids


class TestSortingByDelta:
    def test_sorted_descending(self, tmp_path):
        evals = [
            _eval_entry(rule_id="low", actual_mean_reward=0.3, candidate_expected_reward=0.5),
            _eval_entry(rule_id="high", actual_mean_reward=0.1, candidate_expected_reward=0.9),
            _eval_entry(rule_id="mid", actual_mean_reward=0.2, candidate_expected_reward=0.6),
        ]
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals(evals, threshold=0.15)
        deltas = [p["expected_reward_delta"] for p in proposals]
        assert deltas == sorted(deltas, reverse=True)
        assert proposals[0]["rule_id"] == "high"

    def test_single_entry(self, tmp_path):
        evals = [_eval_entry(rule_id="only")]
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals(evals, threshold=0.15)
        assert len(proposals) == 1


class TestConfidenceIntegration:
    def test_with_confidence_scores(self, tmp_path):
        _write_confidence_scores(tmp_path, [
            {"intent": "R1", "verdict_role": "DIRECT", "confidence": 0.92, "ready": True},
        ])
        ev = _eval_entry(rule_id="R1")
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals([ev], threshold=0.15)
        assert len(proposals) == 1
        assert proposals[0]["confidence"] == pytest.approx(0.92)

    def test_without_confidence_scores_file(self, tmp_path):
        ev = _eval_entry(rule_id="R1")
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals([ev], threshold=0.15)
        assert len(proposals) == 1
        assert proposals[0]["confidence"] == 0.0

    def test_rule_not_in_confidence_scores(self, tmp_path):
        _write_confidence_scores(tmp_path, [
            {"intent": "OTHER_RULE", "verdict_role": "FW", "confidence": 0.85, "ready": True},
        ])
        ev = _eval_entry(rule_id="R1")
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals([ev], threshold=0.15)
        assert proposals[0]["confidence"] == 0.0


class TestEmptyInputProposals:
    def test_empty_evaluations(self, tmp_path):
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals([], threshold=0.15)
        assert proposals == []

    def test_missing_shadow_evaluation_file(self, tmp_path):
        with patch("proposal_writer.DATA_DIR", tmp_path):
            result = load_shadow_evaluation()
        assert result == []

    def test_corrupt_shadow_evaluation_file(self, tmp_path):
        (tmp_path / "shadow_evaluation.json").write_text("not valid json", encoding="utf-8")
        with patch("proposal_writer.DATA_DIR", tmp_path):
            result = load_shadow_evaluation()
        assert result == []


class TestSelectionBiasFlag:
    def test_bias_warning_true_propagated(self, tmp_path):
        ev = _eval_entry(rule_id="R1", selection_bias_warning=True)
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals([ev], threshold=0.15)
        assert proposals[0]["selection_bias_flag"] is True

    def test_bias_warning_absent_defaults_false(self, tmp_path):
        ev = _eval_entry(rule_id="R1", selection_bias_warning=False)
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals([ev], threshold=0.15)
        assert proposals[0]["selection_bias_flag"] is False

    def test_multiple_entries_mixed_bias(self, tmp_path):
        evals = [
            _eval_entry(rule_id="R1", selection_bias_warning=True,
                        actual_mean_reward=0.2, candidate_expected_reward=0.7),
            _eval_entry(rule_id="R2", selection_bias_warning=False,
                        actual_mean_reward=0.1, candidate_expected_reward=0.8),
        ]
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals(evals, threshold=0.15)
        bias_by_rule = {p["rule_id"]: p["selection_bias_flag"] for p in proposals}
        assert bias_by_rule["R1"] is True
        assert bias_by_rule["R2"] is False


class TestLoadShadowEvaluation:
    def test_loads_rules_with_rule_id(self, tmp_path):
        _write_shadow_evaluation(tmp_path, {
            "R1": {"actual_mean_reward": 0.5, "candidate_expected_reward": 0.8,
                   "improvement_delta": 0.3, "sample_count": 10},
            "R2": {"actual_mean_reward": 0.6, "candidate_expected_reward": 0.7,
                   "improvement_delta": 0.1, "sample_count": 5},
        })
        with patch("proposal_writer.DATA_DIR", tmp_path):
            result = load_shadow_evaluation()
        assert len(result) == 2
        rule_ids = {e["rule_id"] for e in result}
        assert rule_ids == {"R1", "R2"}


class TestWriteProposals:
    def test_writes_json(self, tmp_path):
        proposals = [{
            "rule_id": "R1",
            "current_role": "DIRECT",
            "proposed_role": "FW",
            "expected_reward_delta": 0.3,
            "sample_count": 20,
            "confidence": 0.9,
            "selection_bias_flag": False,
            "created_at": "2026-08-20T00:00:00+00:00",
        }]
        with patch("proposal_writer.DATA_DIR", tmp_path):
            write_proposals(proposals)
        out_path = tmp_path / "policy_proposals.json"
        assert out_path.exists()
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert "proposals" in data
        assert len(data["proposals"]) == 1
        assert data["proposals"][0]["rule_id"] == "R1"


class TestProposalMain:
    def test_end_to_end(self, tmp_path):
        _write_shadow_evaluation(tmp_path, {
            "R1": {
                "actual_mean_reward": 0.3, "candidate_expected_reward": 0.7,
                "improvement_delta": 0.4, "sample_count": 15,
                "actual_role": "DIRECT", "candidate_role": "FW",
            },
        })
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposal_main()
        out_path = tmp_path / "policy_proposals.json"
        assert out_path.exists()
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert len(data["proposals"]) == 1
        assert data["proposals"][0]["rule_id"] == "R1"
        assert data["proposals"][0]["expected_reward_delta"] == pytest.approx(0.4)


class TestDualKeyConfidenceLookup:
    def test_dual_key_confidence_lookup(self, tmp_path):
        _write_confidence_scores(tmp_path, [
            {
                "intent": "agentic_action",
                "rule_id": "FO:agentic_action",
                "verdict_role": "FO",
                "confidence": 0.88,
                "ready": True,
            },
        ])
        ev = _eval_entry(
            rule_id="FO:agentic_action",
            actual_mean_reward=0.2,
            candidate_expected_reward=0.7,
            actual_role="FO",
            candidate_role="DIRECT",
        )
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals([ev], threshold=0.15)
        assert len(proposals) == 1
        assert proposals[0]["confidence"] > 0.0
        assert proposals[0]["confidence"] == pytest.approx(0.88)

    def test_confidence_fallback_to_intent_key(self, tmp_path):
        _write_confidence_scores(tmp_path, [
            {
                "intent": "agentic_action",
                "verdict_role": "FO",
                "confidence": 0.75,
                "ready": True,
            },
        ])
        ev = _eval_entry(
            rule_id="FO:agentic_action",
            actual_mean_reward=0.2,
            candidate_expected_reward=0.7,
            actual_role="FO",
            candidate_role="DIRECT",
        )
        with patch("proposal_writer.DATA_DIR", tmp_path):
            proposals = score_proposals([ev], threshold=0.15)
        assert len(proposals) == 1
        assert proposals[0]["confidence"] == 0.0


# ===========================================================================
# --- from test_cost_report_proposals.py ---
# ===========================================================================

def _write_proposals(tmp_path: Path, proposals: list[dict], timestamp: str = "2026-08-25T10:00:00Z") -> None:
    path = tmp_path / "policy_proposals.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"timestamp": timestamp, "proposals": proposals}, f)


@pytest.fixture(autouse=True)
def _patch_data_dir(tmp_path, monkeypatch):
    import scripts.render_proposals as mod
    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)


class TestLoadProposals:
    def test_with_proposals(self, tmp_path):
        from scripts.render_proposals import load_proposals

        proposals = [
            {
                "rule_id": "intent:qa",
                "current_role": "reasoning",
                "proposed_role": "fast",
                "expected_reward_delta": 0.150,
                "confidence": 0.85,
                "sample_count": 42,
                "selection_bias_flag": False,
            },
            {
                "rule_id": "intent:edit",
                "current_role": "reasoning",
                "proposed_role": "fast",
                "expected_reward_delta": 0.100,
                "confidence": 0.70,
                "sample_count": 30,
                "selection_bias_flag": True,
            },
        ]
        _write_proposals(tmp_path, proposals)
        result = load_proposals()
        assert len(result) == 2

    def test_without_proposals(self, tmp_path):
        from scripts.render_proposals import load_proposals
        _write_proposals(tmp_path, [])
        result = load_proposals()
        assert result == []

    def test_missing_file(self, tmp_path):
        from scripts.render_proposals import load_proposals
        result = load_proposals()
        assert result == []

    def test_limit(self, tmp_path):
        from scripts.render_proposals import load_proposals
        proposals = [
            {
                "rule_id": f"rule_{i}",
                "current_role": "reasoning",
                "proposed_role": "fast",
                "expected_reward_delta": 0.1 * (10 - i),
                "confidence": 0.8,
                "sample_count": 50,
                "selection_bias_flag": False,
            }
            for i in range(10)
        ]
        _write_proposals(tmp_path, proposals)
        result = load_proposals(limit=2)
        assert len(result) == 2


class TestRenderProposals:
    def test_render_with_proposals(self, tmp_path):
        from scripts.render_proposals import render_proposals
        proposals = [
            {
                "rule_id": "intent:qa",
                "current_role": "reasoning",
                "proposed_role": "fast",
                "expected_reward_delta": 0.150,
                "confidence": 0.85,
                "sample_count": 42,
                "selection_bias_flag": False,
            },
        ]
        _write_proposals(tmp_path, proposals)
        output = render_proposals()
        assert output.startswith("## Pending Proposals\n")
        assert "**intent:qa**" in output
        assert "reasoning" in output
        assert "fast" in output
        assert "+0.150" in output
        assert "0.85" in output
        assert "samples: 42" in output
        assert "(limited data)" not in output

    def test_render_without_proposals(self, tmp_path):
        from scripts.render_proposals import render_proposals
        _write_proposals(tmp_path, [])
        output = render_proposals()
        assert "## Pending Proposals" in output
        assert "No proposals" in output
        assert "accumulating shadow data" in output

    def test_render_selection_bias_flag(self, tmp_path):
        from scripts.render_proposals import render_proposals
        proposals = [
            {
                "rule_id": "intent:edit",
                "current_role": "reasoning",
                "proposed_role": "fast",
                "expected_reward_delta": 0.100,
                "confidence": 0.55,
                "sample_count": 8,
                "selection_bias_flag": True,
            },
            {
                "rule_id": "intent:qa",
                "current_role": "reasoning",
                "proposed_role": "fast",
                "expected_reward_delta": 0.200,
                "confidence": 0.90,
                "sample_count": 50,
                "selection_bias_flag": False,
            },
        ]
        _write_proposals(tmp_path, proposals)
        output = render_proposals()
        lines = output.strip().splitlines()
        edit_line = [l for l in lines if "intent:edit" in l][0]
        assert "(limited data)" in edit_line
        qa_line = [l for l in lines if "intent:qa" in l][0]
        assert "(limited data)" not in qa_line


# ===========================================================================
# --- from test_generate_reports.py ---
# ===========================================================================

SINCE = '2026-08-18'
UNTIL = '2026-08-25'

PRICING = {
    'opus': {'output_per_1k': 0.075, 'input_per_1k': 0.015},
    'fast': {'output_per_1k': 0.0015, 'input_per_1k': 0.0005},
    'haiku': {'output_per_1k': 0.004, 'input_per_1k': 0.0008},
}
CORRECTIONS = {'default': {'code': 1.0, 'prose': 1.0, 'symbol': 1.0}}


def _make_impact_report(**kwargs):
    defaults = dict(
        routing_savings=128.0, waste_prevented=14.0, total_est_saved=142.0,
        routing_rate=0.82, prev_week_routing_rate=None, total_spent=18.0,
        total_sessions=7, total_turns=45,
        gate_breakdown={'bulk_write_denied': (3, 9.0), 'scope_guard_deny': (2, 5.0)},
        top_moments=[
            {'session_id': 's1', 'ts': '2026-08-20T09:00:00', 'description': '12 turns routed to fast tier', 'est_saved_usd': 55.0},
            {'session_id': 's2', 'ts': '2026-08-21T14:00:00', 'description': '8 turns routed to fast tier', 'est_saved_usd': 40.0},
        ],
        baseline_model='opus', has_enough_data=True,
    )
    defaults.update(kwargs)
    return ImpactReport(**defaults)


def _make_entry_ic(session_id='s1', turn_id='t1', tier='reasoning', tokens=1000,
                content_kind='code_output', model_key='opus', ts='2026-08-18T10:00:00',
                event_type=None):
    entry = {
        'session_id': session_id, 'turn_id': turn_id, 'tier': tier,
        'est_output_tokens': tokens, 'content_kind': content_kind,
        'model_key': model_key, 'ts': ts,
    }
    if event_type:
        entry['event_type'] = event_type
    return entry


def test_html_card_is_valid_html(tmp_path):
    from reports import routing_report
    orig = routing_report.DATA_DIR
    routing_report.DATA_DIR = tmp_path
    try:
        path = generate_html_card(_make_impact_report(), SINCE, UNTIL)
        html = path.read_text()
    finally:
        routing_report.DATA_DIR = orig
    assert '<!DOCTYPE html>' in html
    assert '<html' in html
    assert '</html>' in html
    assert html.count('<body') == html.count('</body>')
    assert html.count('<div') == html.count('</div>')


def test_html_card_hero_number(tmp_path):
    from reports import routing_report
    orig = routing_report.DATA_DIR
    routing_report.DATA_DIR = tmp_path
    try:
        path = generate_html_card(_make_impact_report(total_est_saved=142.50), SINCE, UNTIL)
        html = path.read_text()
    finally:
        routing_report.DATA_DIR = orig
    assert '$142' in html


def test_html_card_three_metric_cards(tmp_path):
    from reports import routing_report
    orig = routing_report.DATA_DIR
    routing_report.DATA_DIR = tmp_path
    try:
        path = generate_html_card(_make_impact_report(), SINCE, UNTIL)
        html = path.read_text()
    finally:
        routing_report.DATA_DIR = orig
    assert '$128' in html
    assert 'denial' in html
    assert 'gathering data' in html or '%' in html


def test_html_card_self_contained(tmp_path):
    from reports import routing_report
    orig = routing_report.DATA_DIR
    routing_report.DATA_DIR = tmp_path
    try:
        path = generate_html_card(_make_impact_report(), SINCE, UNTIL)
        html = path.read_text()
    finally:
        routing_report.DATA_DIR = orig
    import re
    local_links = re.findall(r'<link[^>]+href=["\'](?!https?://)([^"\']+\.css)["\']', html)
    assert local_links == []


def test_html_card_disclosure(tmp_path):
    from reports import routing_report
    orig = routing_report.DATA_DIR
    routing_report.DATA_DIR = tmp_path
    try:
        path = generate_html_card(_make_impact_report(), SINCE, UNTIL)
        html = path.read_text()
    finally:
        routing_report.DATA_DIR = orig
    assert 'All numbers estimated' in html
    assert 'Why estimated' in html


def test_markdown_digest_structure(tmp_path):
    from reports import cost_report
    orig = cost_report.DATA_DIR
    cost_report.DATA_DIR = tmp_path
    try:
        path = generate_markdown_digest(_make_impact_report(), SINCE, UNTIL)
        md = path.read_text()
    finally:
        cost_report.DATA_DIR = orig
    assert '## Summary' in md
    assert '## Savings Breakdown' in md
    assert '### Policy Trend' in md
    assert '## Top Savings Moments' in md
    assert '## Notes' in md


def test_markdown_digest_gate_breakdown(tmp_path):
    from reports import cost_report
    orig = cost_report.DATA_DIR
    cost_report.DATA_DIR = tmp_path
    try:
        path = generate_markdown_digest(_make_impact_report(), SINCE, UNTIL)
        md = path.read_text()
    finally:
        cost_report.DATA_DIR = orig
    assert 'Bulk-write denied' in md
    assert '$9.00' in md
    assert 'Scope-guard denied' in md
    assert '$5.00' in md


def test_markdown_digest_empty_moments(tmp_path):
    from reports import cost_report
    orig = cost_report.DATA_DIR
    cost_report.DATA_DIR = tmp_path
    try:
        path = generate_markdown_digest(_make_impact_report(top_moments=[]), SINCE, UNTIL)
        md = path.read_text()
    finally:
        cost_report.DATA_DIR = orig
    assert 'Not enough session variety' in md


def test_since_flag_filtering():
    with patch('scripts.generate_reports.compute_window') as mock_cw:
        mock_cw.return_value = _make_impact_report(has_enough_data=False)
        with patch('sys.argv', ['generate_reports.py', '--card', '--since', '2026-08-01']):
            from scripts import generate_reports
            import importlib
            with pytest.raises(SystemExit):
                generate_reports.main()
        mock_cw.assert_called_once()
        call_args = mock_cw.call_args
        assert call_args[0][0] == '2026-08-01'


def test_insufficient_data_message(tmp_path, capsys):
    import scripts.generate_reports as gr
    orig = gr.DATA_DIR
    gr.DATA_DIR = tmp_path
    try:
        with patch('scripts.generate_reports.compute_window') as mock_cw:
            mock_cw.return_value = _make_impact_report(has_enough_data=False)
            with patch('sys.argv', ['generate_reports.py', '--both']):
                with pytest.raises(SystemExit):
                    gr.main()
        captured = capsys.readouterr()
        assert 'Not enough data yet' in captured.err
        assert not (tmp_path / 'impact-card.html').exists()
    finally:
        gr.DATA_DIR = orig


def test_routing_savings_basic():
    entries = [
        _make_entry_ic(turn_id='t1', tier='fast', tokens=1000, model_key='fast'),
        _make_entry_ic(turn_id='t2', tier='fast', tokens=1000, model_key='fast'),
        _make_entry_ic(turn_id='t3', tier='fast', tokens=1000, model_key='fast'),
        _make_entry_ic(turn_id='t4', tier='reasoning', tokens=1000, model_key='opus'),
        _make_entry_ic(turn_id='t5', tier='reasoning', tokens=1000, model_key='opus'),
    ]
    savings, total_spent, routing_rate, total_turns, routed = _compute_routing_savings(
        entries, PRICING, 'opus', CORRECTIONS
    )
    expected_savings = 3 * (0.075 - 0.0015)
    assert abs(savings - expected_savings) < 1e-9
    assert total_turns == 5
    assert routing_rate == pytest.approx(0.6)
    assert len(routed) == 3


def test_no_double_counting():
    turn_id = 'shared_turn'
    entries = [
        _make_entry_ic(turn_id=turn_id, tier='fast', tokens=500, model_key='fast'),
        {**_make_entry_ic(turn_id=turn_id, tier='fast', tokens=500), 'event_type': 'bulk_write_denied'},
    ]
    _, _, _, _, routed_turn_ids = _compute_routing_savings(entries, PRICING, 'opus', CORRECTIONS)
    assert turn_id in routed_turn_ids
    waste, breakdown = _compute_waste_prevented(entries, PRICING, 'opus', routed_turn_ids, CORRECTIONS)
    assert waste == pytest.approx(0.0)
    assert breakdown == {}


def test_zero_sessions():
    with patch('scripts.impact_calculator._load_session_entries', return_value=[]), \
         patch('scripts.impact_calculator._load_pricing', return_value=PRICING), \
         patch('scripts.impact_calculator._load_config_impact', return_value={'correction': CORRECTIONS}):
        report = compute_session('no_such_session')
    assert report.has_enough_data is False
    assert report.routing_savings == 0.0
    assert report.waste_prevented == 0.0
    assert report.total_est_saved == 0.0


def test_total_est_saved_is_sum():
    entries = [
        _make_entry_ic(turn_id='t1', tier='fast', tokens=1000, model_key='fast'),
        {**_make_entry_ic(turn_id='t2', tier='reasoning', tokens=800), 'event_type': 'bulk_write_denied'},
    ]
    with patch('scripts.impact_calculator._load_session_entries', return_value=entries), \
         patch('scripts.impact_calculator._load_pricing', return_value=PRICING), \
         patch('scripts.impact_calculator._load_config_impact', return_value={'correction': CORRECTIONS}), \
         patch('scripts.impact_calculator._compute_policy_trend', return_value=None):
        report = compute_session('s1')
    assert report.total_est_saved == pytest.approx(report.routing_savings + report.waste_prevented)


# ===========================================================================
# --- from test_impact_calculator.py ---
# ===========================================================================

def _ts_ic(hour: int = 12) -> str:
    return f"2026-09-04T{hour:02d}:00:00+00:00"


def _entry_ic(
    session_id: str,
    est_output_tokens: int = 1000,
    tier: str = "fast",
    model_key: str = "claude-haiku-4-5",
    hour: int = 12,
    content_kind: str = "code_output",
) -> dict:
    return {
        "session_id": session_id,
        "turn_id": f"{session_id}-t1",
        "ts": _ts_ic(hour),
        "model_key": model_key,
        "est_output_tokens": est_output_tokens,
        "tier": tier,
        "content_kind": content_kind,
    }


def _write_session_log_ic(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _write_pricing_ic(path: Path) -> None:
    pricing = {
        "claude-haiku-4-5": {"output": 0.001},
        "claude-sonnet-4-6": {"output": 0.015},
    }
    path.write_text(json.dumps(pricing), encoding="utf-8")


def _write_config_ic(path: Path) -> None:
    config = {
        "impact": {
            "output_token_correction": 1.0,
            "baseline_model": "claude-sonnet-4-6",
        }
    }
    path.write_text(json.dumps(config), encoding="utf-8")


_TESTS_DIR = Path(__file__).resolve().parent


class _ImpactBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = _TESTS_DIR / f".ic_{self.__class__.__name__}_{self._testMethodName}"
        self._tmp.mkdir(exist_ok=True)
        self._orig_data_dir = ic.DATA_DIR
        self._orig_config = ic.CONFIG_PATH
        self._orig_pricing = ic.PRICING_PATH
        ic.DATA_DIR = self._tmp
        ic.CONFIG_PATH = self._tmp / "config.json"
        ic.PRICING_PATH = self._tmp / "pricing.json"
        _write_pricing_ic(ic.PRICING_PATH)
        _write_config_ic(ic.CONFIG_PATH)

    def tearDown(self) -> None:
        ic.DATA_DIR = self._orig_data_dir
        ic.CONFIG_PATH = self._orig_config
        ic.PRICING_PATH = self._orig_pricing
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _session_log(self) -> Path:
        return self._tmp / "session_log.jsonl"

    def _window(self, entries: list[dict], min_sessions: int = 1) -> ic.ImpactReport:
        _write_session_log_ic(self._session_log(), entries)
        return ic.compute_window(
            "2026-09-04T00:00:00+00:00",
            "2026-09-04T23:59:59+00:00",
            min_sessions=min_sessions,
        )


class TestComputeWindowBasic(_ImpactBase):

    def test_returns_impact_report(self) -> None:
        report = self._window([_entry_ic("s1")])
        self.assertIsInstance(report, ic.ImpactReport)

    def test_has_enough_data_when_min_sessions_met(self) -> None:
        report = self._window([_entry_ic("s1"), _entry_ic("s2")], min_sessions=2)
        self.assertTrue(report.has_enough_data)

    def test_not_enough_data_below_min_sessions(self) -> None:
        report = self._window([_entry_ic("s1")], min_sessions=2)
        self.assertFalse(report.has_enough_data)

    def test_total_spent_positive_when_tokens_present(self) -> None:
        report = self._window([_entry_ic("s1", est_output_tokens=5000, tier="sonnet", model_key="claude-sonnet-4-6")])
        self.assertGreater(report.total_spent, 0.0)

    def test_routing_rate_between_0_and_1(self) -> None:
        report = self._window([_entry_ic("s1", tier="fast"), _entry_ic("s1", tier="sonnet")])
        self.assertGreaterEqual(report.routing_rate, 0.0)
        self.assertLessEqual(report.routing_rate, 1.0)

    def test_all_fast_tier_gives_high_routing_rate(self) -> None:
        entries = [_entry_ic(f"s{i}", tier="fast") for i in range(3)]
        report = self._window(entries, min_sessions=3)
        self.assertGreater(report.routing_rate, 0.5)

    def test_out_of_range_entries_excluded(self) -> None:
        in_range = _entry_ic("s1", hour=12)
        out_of_range = _entry_ic("s2", hour=12)
        out_of_range["ts"] = "2026-09-03T12:00:00+00:00"
        _write_session_log_ic(self._session_log(), [in_range, out_of_range])
        report = ic.compute_window(
            "2026-09-04T00:00:00+00:00",
            "2026-09-04T23:59:59+00:00",
            min_sessions=1,
        )
        self.assertEqual(report.total_sessions, 1)

    def test_empty_session_log_returns_no_data(self) -> None:
        report = self._window([])
        self.assertFalse(report.has_enough_data)


class TestComputeWindowRobustness(_ImpactBase):

    def test_malformed_jsonl_lines_skipped(self) -> None:
        log = self._session_log()
        with log.open("w") as fh:
            fh.write("not json\n")
            fh.write(json.dumps(_entry_ic("s1")) + "\n")
            fh.write("{broken\n")
        report = ic.compute_window(
            "2026-09-04T00:00:00+00:00",
            "2026-09-04T23:59:59+00:00",
            min_sessions=1,
        )
        self.assertIsInstance(report, ic.ImpactReport)

    def test_missing_session_log_returns_no_data(self) -> None:
        report = ic.compute_window(
            "2026-09-04T00:00:00+00:00",
            "2026-09-04T23:59:59+00:00",
            min_sessions=1,
        )
        self.assertFalse(report.has_enough_data)

    def test_entry_missing_optional_fields_does_not_raise(self) -> None:
        minimal = {"session_id": "s1", "ts": _ts_ic(), "est_output_tokens": 500}
        _write_session_log_ic(self._session_log(), [minimal])
        try:
            ic.compute_window(
                "2026-09-04T00:00:00+00:00",
                "2026-09-04T23:59:59+00:00",
                min_sessions=1,
            )
        except Exception as exc:
            self.fail(f"compute_window raised with minimal entry: {exc}")

    def test_zero_tokens_entry_does_not_crash(self) -> None:
        report = self._window([_entry_ic("s1", est_output_tokens=0)])
        self.assertIsInstance(report, ic.ImpactReport)


class TestComputeSession(_ImpactBase):

    def test_returns_impact_report_for_known_session(self) -> None:
        _write_session_log_ic(self._session_log(), [_entry_ic("my-sess", est_output_tokens=2000)])
        report = ic.compute_session("my-sess")
        self.assertIsInstance(report, ic.ImpactReport)

    def test_returns_no_data_for_unknown_session(self) -> None:
        _write_session_log_ic(self._session_log(), [_entry_ic("other-sess")])
        report = ic.compute_session("no-such-session")
        self.assertFalse(report.has_enough_data)

    def test_missing_session_log_does_not_raise(self) -> None:
        try:
            ic.compute_session("any-session")
        except Exception as exc:
            self.fail(f"compute_session raised with no session_log: {exc}")

    def test_total_spent_approximates_tokens_times_price(self) -> None:
        _write_session_log_ic(
            self._session_log(),
            [_entry_ic("s1", est_output_tokens=10_000, tier="sonnet", model_key="claude-sonnet-4-6")],
        )
        report = ic.compute_session("s1")
        if report.has_enough_data:
            self.assertAlmostEqual(report.total_spent, 0.15, places=2)


# ===========================================================================
# --- TestGcApplyFlag ---
# ===========================================================================

class TestGcApplyFlag:
    """Tests for conditional --apply flag on policy_gc.py in pipeline_trigger."""

    def test_gc_apply_flag_off_no_apply_arg(self, monkeypatch):
        """When pipeline_trigger_gc_apply() returns False, --apply is not appended to policy_gc.py call."""
        import pipeline_trigger

        # Capture subprocess.run calls
        called_args = []

        def mock_run(*args, **kwargs):
            called_args.append(args[0])
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(
            pipeline_trigger,
            "pipeline_trigger_gc_apply",
            lambda: False,
        )

        # Call _run_pipeline
        result = pipeline_trigger._run_pipeline()

        # Find the policy_gc.py call
        policy_gc_calls = [call for call in called_args if any("policy_gc.py" in a for a in call)]
        assert len(policy_gc_calls) == 1, "policy_gc.py should be called once"
        assert "--apply" not in policy_gc_calls[0], "--apply should NOT be in args when flag is off"
        assert result == "ok"

    def test_gc_apply_flag_on_includes_apply_arg(self, monkeypatch):
        """When pipeline_trigger_gc_apply() returns True, --apply is appended to policy_gc.py call."""
        import pipeline_trigger

        # Capture subprocess.run calls
        called_args = []

        def mock_run(*args, **kwargs):
            called_args.append(args[0])
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(
            pipeline_trigger,
            "pipeline_trigger_gc_apply",
            lambda: True,
        )

        # Call _run_pipeline
        result = pipeline_trigger._run_pipeline()

        # Find the policy_gc.py call
        policy_gc_calls = [call for call in called_args if any("policy_gc.py" in a for a in call)]
        assert len(policy_gc_calls) == 1, "policy_gc.py should be called once"
        assert "--apply" in policy_gc_calls[0], "--apply should be in args when flag is on"
        assert result == "ok"

    @pytest.mark.parametrize("raw,expected", [(True, True), (False, False), ("yes", False), (1, False), (None, False)])
    def test_gc_apply_accessor_only_true_for_bool_true(self, monkeypatch, raw, expected):
        """The real accessor is on only for bool True; any other config value is off."""
        import _config

        monkeypatch.setattr(_config, "config_get", lambda path, default=None, flat_key=None: raw)
        assert _config.pipeline_trigger_gc_apply() is expected
