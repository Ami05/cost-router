"""Journey: the report stage of the classify -> dispatch -> gate -> reward
-> report pipeline (TEST-001, journey 5).

Chains through REAL entrypoints only, no mocks of the system under test:
  impact_calculator._load_window_entries (real jsonl read)
  -> impact_calculator.compute_window (real cost-savings + gate-waste math)
  -> generate_reports.generate_markdown_digest -> reports.cost_report.generate
     (real markdown file write)
  -> generate_reports.generate_html_card -> reports.routing_report.generate
     (real HTML file write)
  -> read both artefacts back off disk and verify field-for-field against
     the same ImpactReport object that produced them.

Deliberately does NOT call generate_reports.main() -- that CLI entrypoint
parses sys.argv and reads real wall-clock dates via datetime.date.today(),
which isn't a real chained-journey shape (see test_proposals_reports.py's
test_since_flag_filtering / test_insufficient_data_message, which mock
compute_window and patch sys.argv rather than chaining real calls).
Also excluded:
  - impact_calculator.compute_session (single-session path) and the
    --dispatch report (reports/dispatch_report.py) -- separate entrypoints
    from the windowed card+digest path this journey exercises.
  - proposal_writer.score_proposals/write_proposals -- a distinct pipeline
    stage (shadow-eval proposals), not the report stage.
Preserved from tests/e2e/test_proposals_reports.py (non-mocked parts only):
  - the routing_savings vs waste_prevented no-double-counting invariant
    (routed turn_ids excluded from gate-waste accounting)
  - digest/card section and gate-display-name assertions
config.json and scripts/pricing.json are read as real repo config, exactly
like other journeys' real config/quality-signal files -- not faked.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import impact_calculator as ic  # noqa: E402
from generate_reports import generate_html_card, generate_markdown_digest  # noqa: E402
from reports import cost_report, routing_report  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(ic, "DATA_DIR", data_dir)
    monkeypatch.setattr(cost_report, "DATA_DIR", data_dir)
    monkeypatch.setattr(routing_report, "DATA_DIR", data_dir)
    return data_dir


def test_report_stage_journey(tmp_path):
    data_dir = ic.DATA_DIR
    sid = "journey-report-1"
    since, until = "2026-09-19", "2026-09-21"

    # Step 1: real session_log.jsonl written to disk, as prior pipeline
    # stages (session tracking + gate enforcement) would produce it -- one
    # fast-tier routed turn and one gate-denial event on an unrouted turn.
    session_log_path = data_dir / "session_log.jsonl"
    entries = [
        {
            "session_id": sid, "turn_id": "t1", "tier": "fast",
            "est_output_tokens": 1000, "content_kind": "code_output",
            "model_key": "haiku", "ts": "2026-09-20T10:00:00",
        },
        {
            "session_id": sid, "turn_id": "t2", "event_type": "scope_guard_deny",
            "ts": "2026-09-20T10:05:00",
        },
    ]
    session_log_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    # Step 2: real file read -- _load_window_entries must round-trip exactly
    # what Step 1 wrote, filtered to the requested date window.
    loaded = ic._load_window_entries(since, until)
    assert loaded == entries

    # Step 3: real compute_window call chains the routing-savings and
    # waste-prevented buckets over those entries. min_sessions=1 lets a
    # single-session window through (the default of 5 is a UX gate, not a
    # math requirement).
    report = ic.compute_window(since, until, min_sessions=1)
    assert report.has_enough_data is True
    assert report.total_sessions == 1
    assert report.baseline_model == "opus"
    assert report.total_turns == 1
    assert report.routing_savings > 0.0
    assert "scope_guard_deny" in report.gate_breakdown
    gate_count, gate_dollars = report.gate_breakdown["scope_guard_deny"]
    assert gate_count == 1
    assert gate_dollars > 0.0

    # Step 4: no-double-counting invariant -- the routed turn (t1) funded
    # routing_savings, and the gate event on the *other* turn (t2) funded
    # waste_prevented; total_est_saved is exactly their sum.
    assert report.total_est_saved == pytest.approx(report.routing_savings + report.waste_prevented)

    # Step 5: real markdown digest write via generate_markdown_digest ->
    # reports.cost_report.generate -- then read the real file back off disk.
    digest_path = generate_markdown_digest(report, since, until)
    assert digest_path == data_dir / f"weekly-digest-{until}.md"
    digest_md = digest_path.read_text()
    assert "## Summary" in digest_md
    assert "## Savings Breakdown" in digest_md
    assert "## Top Savings Moments" in digest_md
    assert f"${report.total_est_saved:.2f}" in digest_md
    assert "Scope-guard denied" in digest_md
    assert f"${gate_dollars:.2f}" in digest_md

    # Step 6: real HTML card write via generate_html_card ->
    # reports.routing_report.generate -- then read the real file back off
    # disk and verify it's well-formed and shows the same hero number as
    # the digest, since both are rendered from the one report object.
    card_path = generate_html_card(report, since, until)
    assert card_path == data_dir / "impact-card.html"
    card_html = card_path.read_text()
    assert "<!DOCTYPE html>" in card_html
    assert card_html.count("<div") == card_html.count("</div>")
    assert f"${report.total_est_saved:.0f}" in card_html


def test_compute_window_savings_positive_with_fast_tier():
    """All-fast-tier scenario for impact_calculator.compute_window (moved
    from tests/e2e/test_dispatch.py, TEST-006): 6 sessions of fast-tier
    turns should clear the min_sessions=5 threshold and produce positive
    routing_savings with routing_rate == 1.0.
    """
    today = datetime.date.today().isoformat()
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    entries = []
    for sess_idx in range(6):
        sid = f"sess-{sess_idx:03d}"
        for turn_idx in range(3):
            entries.append({
                "session_id": sid,
                "turn_id": f"turn-{sess_idx}-{turn_idx}",
                "tier": "fast",
                "model_key": "claude-haiku-4-5-20251001",
                "est_output_tokens": 1000,
                "content_kind": "code_output",
                "ts": ts,
            })

    mock_pricing = {
        "opus": {"output_per_1k": 0.075, "input_per_1k": 0.015},
        "claude-haiku-4-5-20251001": {"output_per_1k": 0.004, "input_per_1k": 0.0008},
    }
    mock_config = {
        "correction": {"default": {"code": 1.0, "prose": 1.0, "symbol": 1.0}},
    }

    with patch.object(ic, "_load_window_entries", return_value=entries), \
         patch.object(ic, "_load_pricing", return_value=mock_pricing), \
         patch.object(ic, "_load_config_impact", return_value=mock_config):
        report = ic.compute_window(today, tomorrow, min_sessions=5)

    assert report.has_enough_data is True, (
        f"Expected has_enough_data=True with 6 sessions (threshold=5), got False. "
        f"total_sessions={report.total_sessions}"
    )
    assert report.routing_savings > 0, (
        f"Expected routing_savings > 0 for all-fast-tier sessions routed away "
        f"from the opus baseline, got {report.routing_savings}"
    )
    assert report.total_sessions == 6, (
        f"Expected 6 unique sessions, got {report.total_sessions}"
    )
    assert report.routing_rate == pytest.approx(1.0), (
        f"All turns are fast-tier so routing_rate should be 1.0, got {report.routing_rate}"
    )
