"""Journey: the reward stage of the classify -> dispatch -> gate -> reward
-> report pipeline (TEST-001, journey 4).

Chains through REAL entrypoints only, no mocks of the system under test:
  reward_signal._load_quality_signal (real file read)
  -> reward_signal.extract_quality_multiplier / build_reward_entries (pure)
  -> reward_signal._write_reward_entries_to_disk (real jsonl append)
  -> read reward_log.jsonl back off disk and verify field-for-field.

Deliberately does NOT call reward_signal.main() -- that hook entrypoint
reads stdin and the full event log, which isn't a real chained-journey shape
(see research notes: it orchestrates shadow_log/quality_signal internals
that are separate pipeline stages, mocked in the legacy suite). Preserved
from tests/e2e/test_reward_pipeline.py's TestWriteRewardEntries /
TestFilterGateOnly (non-mocked parts only):
  - quality_multiplier = max(0.1, 1.0 - revert_rate) formula
  - base_reward_value: -1.0 under-route vs 1.0 no-escalation
  - reward_value = base_reward_value * quality_multiplier
  - full field completeness on the written-then-read-back entries
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import reward_signal as rs  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(rs, "DATA_DIR", data_dir)
    monkeypatch.delenv("REWARD_UNDER_ROUTE", raising=False)
    monkeypatch.delenv("REWARD_NO_ESCALATION", raising=False)
    return data_dir


def test_reward_stage_journey(tmp_path):
    data_dir = rs.DATA_DIR
    sid = "journey-reward-1"

    # Step 1: real quality_signal.json written to disk, as a prior pipeline
    # stage (quality_signal.py) would produce it.
    quality_path = data_dir / "quality_signal.json"
    quality_path.write_text(json.dumps({"agentic_action": {"revert_rate": 0.3}}))

    # Step 2: real file read -- _load_quality_signal must round-trip exactly
    # what Step 1 wrote.
    quality_data = rs._load_quality_signal()
    assert quality_data == {"agentic_action": {"revert_rate": 0.3}}

    # Step 3: a rule that escalated cleanly (no under-route) for an intent
    # WITH a known revert rate -- multiplier must dampen the reward.
    clean_verdicts = [{
        "rule_id": "FO:agentic_action", "intent": "agentic_action",
        "role": "FO", "model": "sonnet",
    }]
    clean_entries = rs.build_reward_entries(clean_verdicts, sid, has_under_route=False, quality_data=quality_data)
    assert len(clean_entries) == 1
    clean = clean_entries[0]
    assert clean["base_reward_value"] == 1.0
    assert clean["quality_multiplier"] == pytest.approx(0.7)
    assert clean["revert_rate_at_compute"] == pytest.approx(0.3)
    assert clean["reward_value"] == pytest.approx(0.7)

    # Step 4: an under-route rule for an intent with NO quality data -- must
    # fall back to (multiplier=1.0, revert_rate=0.0) and the negative base.
    under_verdicts = [{
        "rule_id": "DIRECT:qa_simple", "intent": "qa_simple",
        "role": "DIRECT", "model": "haiku",
    }]
    under_entries = rs.build_reward_entries(under_verdicts, sid, has_under_route=True, quality_data=quality_data)
    under = under_entries[0]
    assert under["base_reward_value"] == -1.0
    assert under["quality_multiplier"] == 1.0
    assert under["revert_rate_at_compute"] == 0.0
    assert under["reward_value"] == pytest.approx(-1.0)

    # Step 5: real append-to-disk for both entries.
    reward_log_path = data_dir / "reward_log.jsonl"
    rs._write_reward_entries_to_disk(clean_entries + under_entries, reward_log_path)

    # Step 6: read the real file back off disk -- not the in-memory entries.
    lines = reward_log_path.read_text().strip().splitlines()
    assert len(lines) == 2
    from_disk = [json.loads(line) for line in lines]
    assert from_disk[0]["rule_id"] == "FO:agentic_action"
    assert from_disk[0]["reward_value"] == pytest.approx(0.7)
    assert from_disk[1]["rule_id"] == "DIRECT:qa_simple"
    assert from_disk[1]["reward_value"] == pytest.approx(-1.0)

    # Step 7: field completeness on what actually landed on disk.
    expected_fields = {
        "rule_id", "routing_role", "actual_model", "model_key",
        "base_reward_value", "quality_multiplier", "revert_rate_at_compute",
        "reward_value", "timestamp", "session_id",
    }
    for entry in from_disk:
        assert expected_fields.issubset(entry.keys())
        assert entry["session_id"] == sid
