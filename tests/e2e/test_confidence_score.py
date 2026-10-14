"""Tests for ENRICH-001 features in scripts/confidence_score.py.

Covers: rule_id field in entries, classify_blast_radius variants,
and session-skip when prompt_intent is absent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from confidence_score import classify_blast_radius, compute_confidence_scores
import confidence_score


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    import _common
    import _config
    import _events
    import _fileio
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # USAGE_LOG is a backwards-compat alias for routing_events.jsonl after the
    # SRP split. Keep patching it so tests that reference _common.USAGE_LOG
    # directly still write to the temp dir.
    routing_log = data_dir / "routing_events.jsonl"
    # _fileio/_events/_config hold their own import-time bindings; patch all of
    # them or read_events/append_event hit the shared tests/data log.
    for mod in (_common, _config, _fileio, _events, confidence_score):
        monkeypatch.setattr(mod, "DATA_DIR", data_dir, raising=False)
        monkeypatch.setattr(mod, "USAGE_LOG", routing_log, raising=False)
        monkeypatch.setattr(mod, "ROUTING_EVENTS_LOG", routing_log, raising=False)
    return data_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _setup_events_and_state(
    data_dir: Path,
    session_id: str = "sess-abc",
    verdict_role: str = "FW",
    signal: str = "ok",
    prompt_intent: str = "qa_simple",
) -> None:
    """Write usage.jsonl and routing_state.json for a single session."""
    events = [
        {
            "type": "routing_declared",
            "session_id": session_id,
            "role": verdict_role,
            "ts": "2026-01-01T00:00:00+00:00",
        },
        {
            "type": "reward_signal",
            "session_id": session_id,
            "verdict_role": verdict_role,
            "signal": signal,
            "ts": "2026-01-01T00:01:00+00:00",
        },
    ]
    _write_jsonl(data_dir / "routing_events.jsonl", events)

    routing_state = {
        "sessions": {
            session_id: {
                "prompt_intent": prompt_intent,
            }
        }
    }
    (data_dir / "routing_state.json").write_text(
        json.dumps(routing_state), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 1. rule_id present and in "{verdict_role}:{intent}" format
# ---------------------------------------------------------------------------

class TestRuleIdInEntries:
    def test_rule_id_in_entries(self, isolated_data):
        """compute_confidence_scores() entries each carry rule_id = verdict_role:intent."""
        _setup_events_and_state(
            isolated_data,
            session_id="sess-r1",
            verdict_role="FW",
            signal="ok",
            prompt_intent="qa_simple",
        )

        with patch("confidence_score.read_stdin_json", return_value={}):
            result = compute_confidence_scores()

        entries = result["entries"]
        assert len(entries) >= 1

        for entry in entries:
            assert "rule_id" in entry
            expected = f"{entry['verdict_role']}:{entry['intent']}"
            assert entry["rule_id"] == expected


# ---------------------------------------------------------------------------
# 2–6. classify_blast_radius
# ---------------------------------------------------------------------------

class TestClassifyBlastRadius:
    def test_classify_blast_radius_low(self):
        """qa_simple + DIRECT->FW (tier_shift=1) => low."""
        result = classify_blast_radius("qa_simple", "DIRECT", "FW")
        assert result == "low"

    def test_classify_blast_radius_high_pinned(self):
        """Pinned rule always returns high regardless of roles."""
        result = classify_blast_radius("qa_simple", "DIRECT", "FW", pinned=True)
        assert result == "high"

    def test_classify_blast_radius_high_demotion(self):
        """Reasoning->fast demotion (RB->FW) always returns high."""
        result = classify_blast_radius("agentic_action", "RB", "FW")
        assert result == "high"

    def test_classify_blast_radius_medium(self):
        """agentic_action intent => medium (regardless of small tier shift)."""
        result = classify_blast_radius("agentic_action", "DIRECT", "FO")
        assert result == "medium"

    def test_classify_blast_radius_no_suggested(self):
        """to_role=None => high (no change direction)."""
        result = classify_blast_radius("qa_simple", "DIRECT", None)
        assert result == "high"


# ---------------------------------------------------------------------------
# 7. Sessions without prompt_intent in routing_state are skipped
# ---------------------------------------------------------------------------

class TestComputeConfidenceSkipsNoIntent:
    def test_compute_confidence_skips_no_intent(self, isolated_data):
        """Sessions whose routing_state entry lacks prompt_intent are excluded."""
        session_id = "sess-no-intent"
        events = [
            {
                "type": "routing_declared",
                "session_id": session_id,
                "role": "FW",
                "ts": "2026-01-01T00:00:00+00:00",
            },
            {
                "type": "reward_signal",
                "session_id": session_id,
                "verdict_role": "FW",
                "signal": "ok",
                "ts": "2026-01-01T00:01:00+00:00",
            },
        ]
        _write_jsonl(isolated_data / "routing_events.jsonl", events)

        # routing_state entry for the session has NO prompt_intent
        routing_state = {
            "sessions": {
                session_id: {
                    # prompt_intent deliberately absent
                    "some_other_key": "value",
                }
            }
        }
        (isolated_data / "routing_state.json").write_text(
            json.dumps(routing_state), encoding="utf-8"
        )

        with patch("confidence_score.read_stdin_json", return_value={}):
            result = compute_confidence_scores()

        # No entries should be produced for a session without prompt_intent
        assert result["entries"] == []
