"""Consolidated reward pipeline tests.

Sources:
- test_shadow_log.py
- test_shadow_evaluator.py
- test_reward.py (reward.py tests only; shadow_evaluator + signal_filter_rules sections de-duped)
- test_reward_signal_quality.py
- test_quality_signal.py
- test_signal_filter_rules.py
"""
from __future__ import annotations

import json
import sys
import statistics
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import shadow_log
from shadow_log import log_shadow_routes, _CANDIDATE_MAP
from shadow_evaluator import (
    compute_rule_evaluation,
    join_shadow_reward,
    load_shadow_routes,
    load_filtered_rewards,
)
import reward as rw
from reward_signal import _quality_multiplier
from quality_signal import compute_quality_signal, DEFAULT_WINDOW
from signal_filter_rules import (
    filter_short_sessions,
    filter_retry_storms,
    filter_accidental_overrides,
    filter_gate_only,
    apply_all_filters,
)


# ===========================================================================
# --- from test_shadow_log.py ---
# ===========================================================================

@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    import _common
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(_common, "DATA_DIR", data_dir)
    monkeypatch.setattr(shadow_log, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        shadow_log,
        "_SHADOW_STATE_FILE",
        data_dir / "shadow_log_state.json",
    )
    return data_dir


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if l.strip()]


class TestLogShadowRoutesEnrichedEvent:
    def test_log_shadow_routes_enriched_event(self, isolated_data):
        """Enriched verdict carries model + composite rule_id."""
        verdict = {
            "type": "routing_declared",
            "session_id": "sess-001",
            "role": "DIRECT",
            "model": "claude-opus-4-5",
            "intent": "qa_simple",
            "rule_id": "DIRECT:qa_simple",
        }
        count = log_shadow_routes([verdict], "sess-001")

        assert count == 1
        entries = _read_jsonl(isolated_data / "shadow_routes.jsonl")
        assert len(entries) == 1
        entry = entries[0]

        assert entry["actual_model"] != "unknown"
        assert entry["actual_model"] == "claude-opus-4-5"
        assert entry["rule_id"] == "DIRECT:qa_simple"
        assert entry["rule_id"] != "DIRECT"
        assert entry["candidate_role"] == _CANDIDATE_MAP["DIRECT"]


class TestLogShadowRoutesFallbackWithoutEnrichment:
    def test_log_shadow_routes_fallback_without_enrichment(self, isolated_data):
        """Legacy verdict (no model, no rule_id) falls back to 'unknown' and bare role."""
        verdict = {
            "type": "routing_declared",
            "session_id": "sess-002",
            "role": "FW",
        }
        count = log_shadow_routes([verdict], "sess-002")

        assert count == 1
        entries = _read_jsonl(isolated_data / "shadow_routes.jsonl")
        assert len(entries) == 1
        entry = entries[0]

        assert entry["actual_model"] == "unknown"
        assert entry["rule_id"] == "FW"


class TestLogShadowRoutesSkipsUnknownRole:
    def test_log_shadow_routes_skips_unknown_role(self, isolated_data):
        """Verdicts whose role is not in _CANDIDATE_MAP produce 0 entries."""
        verdict = {
            "type": "routing_declared",
            "session_id": "sess-003",
            "role": "UNKNOWN_ROLE_XYZ",
            "model": "claude-haiku-4-5",
            "rule_id": "UNKNOWN_ROLE_XYZ:qa_simple",
        }
        count = log_shadow_routes([verdict], "sess-003")

        assert count == 0
        shadow_file = isolated_data / "shadow_routes.jsonl"
        entries = _read_jsonl(shadow_file)
        assert len(entries) == 0


class TestHwmResetMigration:
    def test_hwm_reset_migration(self, isolated_data, monkeypatch):
        """State file without enrich_001_reset gets migrated: flag set and hwm bumped."""
        state_file = isolated_data / "shadow_log_state.json"
        state_file.write_text(json.dumps({"hwm": 0}), encoding="utf-8")
        monkeypatch.setattr(shadow_log, "_SHADOW_STATE_FILE", state_file)

        usage = isolated_data / "routing_events.jsonl"
        events = [
            {"type": "routing_declared", "session_id": "s1", "role": "DIRECT", "ts": "2026-01-01T00:00:00+00:00"},
            {"type": "routing_declared", "session_id": "s1", "role": "FW", "ts": "2026-01-01T00:00:01+00:00"},
            {"type": "tool_call", "session_id": "s1", "ts": "2026-01-01T00:00:02+00:00"},
        ]
        _write_jsonl(usage, events)

        import _common
        monkeypatch.setattr(_common, "DATA_DIR", isolated_data)
        monkeypatch.setattr(shadow_log, "DATA_DIR", isolated_data)

        with patch("shadow_log.read_stdin_json", return_value={}), \
             patch("shadow_log.read_events", return_value=events), \
             patch("builtins.print"):
            shadow_log.main()

        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert saved.get("enrich_001_reset") is True
        assert saved.get("hwm") == len(events)


class TestHwmPastReadWindow:
    def test_stuck_index_hwm_resumes_after_last_shadow_entry(self, isolated_data, monkeypatch):
        """Legacy hwm beyond len(events) must not skip new rows; resume after last shadow entry."""
        state_file = isolated_data / "shadow_log_state.json"
        state_file.write_text(
            json.dumps({"hwm": 2105, "enrich_001_reset": True}), encoding="utf-8"
        )
        _write_jsonl(
            isolated_data / "shadow_routes.jsonl",
            [{"session_id": "s0", "timestamp": "2026-01-01T00:00:01+00:00"}],
        )
        events = [
            {"type": "routing_declared", "role": "FW", "ts": "2026-01-01T00:00:00+00:00"},
            {"type": "routing_declared", "role": "DIRECT", "ts": "2026-01-01T00:00:05+00:00"},
        ]
        with patch("shadow_log.read_stdin_json", return_value={}), \
             patch("shadow_log.read_events", return_value=events), \
             patch("builtins.print"):
            shadow_log.main()

        entries = _read_jsonl(isolated_data / "shadow_routes.jsonl")
        assert [e.get("actual_role") for e in entries[1:]] == ["DIRECT"]
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert saved["hwm_ts"] == "2026-01-01T00:00:05+00:00"

    def test_ts_cursor_survives_window_shift(self, isolated_data, monkeypatch):
        """With hwm_ts set, a shrunken window still yields only newer rows."""
        state_file = isolated_data / "shadow_log_state.json"
        state_file.write_text(
            json.dumps({"hwm": 999, "hwm_ts": "2026-01-01T00:00:02+00:00",
                        "enrich_001_reset": True}),
            encoding="utf-8",
        )
        events = [
            {"type": "routing_declared", "role": "FW", "ts": "2026-01-01T00:00:01+00:00"},
            {"type": "routing_declared", "role": "RB", "ts": "2026-01-01T00:00:03+00:00"},
        ]
        with patch("shadow_log.read_stdin_json", return_value={}), \
             patch("shadow_log.read_events", return_value=events), \
             patch("builtins.print"):
            shadow_log.main()

        entries = _read_jsonl(isolated_data / "shadow_routes.jsonl")
        assert [e["actual_role"] for e in entries] == ["RB"]


class TestHwmPreservesStateKeys:
    def test_hwm_preserves_state_keys(self, isolated_data, monkeypatch):
        """State file that already has enrich_001_reset=True keeps the flag after processing."""
        state_file = isolated_data / "shadow_log_state.json"
        initial_state = {"hwm": 5, "enrich_001_reset": True}
        state_file.write_text(json.dumps(initial_state), encoding="utf-8")
        monkeypatch.setattr(shadow_log, "_SHADOW_STATE_FILE", state_file)

        import _common
        monkeypatch.setattr(_common, "DATA_DIR", isolated_data)
        monkeypatch.setattr(shadow_log, "DATA_DIR", isolated_data)

        events = [
            {"type": "tool_call", "session_id": "s1", "ts": "2026-01-01T00:00:00+00:00"},
            {"type": "tool_call", "session_id": "s1", "ts": "2026-01-01T00:00:01+00:00"},
            {"type": "tool_call", "session_id": "s1", "ts": "2026-01-01T00:00:02+00:00"},
            {"type": "tool_call", "session_id": "s1", "ts": "2026-01-01T00:00:03+00:00"},
            {"type": "tool_call", "session_id": "s1", "ts": "2026-01-01T00:00:04+00:00"},
            {"type": "routing_declared", "session_id": "s1", "role": "FW",
             "model": "claude-haiku-4-5", "rule_id": "FW:qa_simple",
             "ts": "2026-01-01T00:00:05+00:00"},
        ]

        with patch("shadow_log.read_stdin_json", return_value={}), \
             patch("shadow_log.read_events", return_value=events), \
             patch("builtins.print"):
            shadow_log.main()

        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert "enrich_001_reset" in saved
        assert saved["enrich_001_reset"] is True
        assert saved["hwm"] == len(events)


# ===========================================================================
# --- from test_shadow_evaluator.py ---
# ===========================================================================

def _shadow(session_id: str = "s1", rule_id: str = "R1",
            actual_role: str = "DIRECT", candidate_role: str = "FW",
            ts: str = "2026-08-12T18:00:00+00:00") -> dict:
    return {
        "session_id": session_id,
        "timestamp": ts,
        "rule_id": rule_id,
        "actual_role": actual_role,
        "candidate_role": candidate_role,
        "actual_model": "opus",
        "candidate_model": "",
        "reason": f"next-best alternative to {actual_role}",
    }


def _reward(session_id: str = "s1", rule_id: str = "R1",
            routing_role: str = "DIRECT", reward_value: float = 0.5,
            ts: str = "2026-08-12T18:00:00+00:00") -> dict:
    return {
        "rule_id": rule_id,
        "routing_role": routing_role,
        "actual_model": "",
        "reward_value": reward_value,
        "timestamp": ts,
        "session_id": session_id,
    }


class TestJoinShadowReward:
    def test_basic_join_by_session_id(self):
        shadows = [_shadow(session_id="s1")]
        rewards = [_reward(session_id="s1", reward_value=0.8)]
        matched = join_shadow_reward(shadows, rewards)
        assert len(matched) == 1
        assert matched[0]["session_id"] == "s1"
        assert matched[0]["reward_value"] == 0.8
        assert matched[0]["actual_role"] == "DIRECT"
        assert matched[0]["candidate_role"] == "FW"

    def test_no_matching_sessions(self):
        shadows2 = [_shadow(session_id="s1", ts="2026-08-01T00:00:00+00:00")]
        rewards2 = [_reward(session_id="s2", rule_id="R_OTHER", ts="2026-08-20T00:00:00+00:00")]
        matched2 = join_shadow_reward(shadows2, rewards2)
        assert len(matched2) == 0

    def test_fuzzy_timestamp_match(self):
        """When session_id doesn't match, fall back to 60s timestamp window."""
        shadows = [_shadow(session_id="s1", ts="2026-08-12T18:00:00+00:00")]
        rewards = [_reward(session_id="s2", ts="2026-08-12T18:00:30+00:00")]
        matched = join_shadow_reward(shadows, rewards)
        assert len(matched) == 1

    def test_fuzzy_timestamp_outside_window(self):
        """Timestamps more than 60s apart should not fuzzy-match."""
        shadows = [_shadow(session_id="s1", ts="2026-08-12T18:00:00+00:00")]
        rewards = [_reward(session_id="s2", rule_id="R_OTHER", ts="2026-08-12T18:05:00+00:00")]
        matched = join_shadow_reward(shadows, rewards)
        assert len(matched) == 0

    def test_multiple_rewards_per_session(self):
        shadows = [_shadow(session_id="s1")]
        rewards = [
            _reward(session_id="s1", reward_value=0.3),
            _reward(session_id="s1", reward_value=0.7),
        ]
        matched = join_shadow_reward(shadows, rewards)
        assert len(matched) == 2
        reward_vals = [m["reward_value"] for m in matched]
        assert 0.3 in reward_vals
        assert 0.7 in reward_vals

    def test_aggregate_fallback_when_no_session_or_timestamp_match(self):
        """When no session_id or timestamp match, use aggregate fallback."""
        shadows = [_shadow(session_id="s_no_match", rule_id="R1", actual_role="DIRECT",
                           ts="2026-01-01T00:00:00+00:00")]
        rewards = [_reward(session_id="s_other", rule_id="R1", routing_role="DIRECT",
                          reward_value=0.7, ts="2026-06-01T00:00:00+00:00")]
        matched = join_shadow_reward(shadows, rewards)
        assert len(matched) == 1
        assert matched[0]["reward_value"] == pytest.approx(0.7)

    def test_aggregate_fallback_uses_mean_of_multiple_rewards(self):
        """Aggregate fallback computes the mean of multiple rewards."""
        shadows = [_shadow(session_id="s_no_match", rule_id="R1", actual_role="DIRECT",
                           ts="2026-01-01T00:00:00+00:00")]
        rewards = [
            _reward(session_id="s_other", rule_id="R1", routing_role="DIRECT",
                   reward_value=0.6, ts="2026-06-01T00:00:00+00:00"),
            _reward(session_id="s_other2", rule_id="R1", routing_role="DIRECT",
                   reward_value=0.8, ts="2026-06-01T00:00:00+00:00"),
            _reward(session_id="s_other3", rule_id="R1", routing_role="DIRECT",
                   reward_value=1.0, ts="2026-06-01T00:00:00+00:00"),
        ]
        matched = join_shadow_reward(shadows, rewards)
        assert len(matched) == 1
        assert matched[0]["reward_value"] == pytest.approx(0.8)

    def test_aggregate_fallback_drops_route_when_no_matching_rule_role(self):
        shadows = [_shadow(session_id="s_no_match", rule_id="R_UNKNOWN", actual_role="DIRECT",
                           ts="2026-01-01T00:00:00+00:00")]
        rewards = [_reward(session_id="s_other", rule_id="R1", routing_role="DIRECT",
                          reward_value=0.7, ts="2026-06-01T00:00:00+00:00")]
        matched = join_shadow_reward(shadows, rewards)
        assert len(matched) == 0

    def test_session_match_takes_priority_over_aggregate_fallback(self):
        shadows = [_shadow(session_id="s1", rule_id="R1", actual_role="DIRECT",
                           ts="2026-08-12T18:00:00+00:00")]
        rewards = [
            _reward(session_id="s1", rule_id="R1", routing_role="DIRECT",
                   reward_value=0.3, ts="2026-08-12T18:00:00+00:00"),
            _reward(session_id="s_other", rule_id="R1", routing_role="DIRECT",
                   reward_value=0.9, ts="2026-06-01T00:00:00+00:00"),
        ]
        matched = join_shadow_reward(shadows, rewards)
        assert len(matched) == 1
        assert matched[0]["reward_value"] == 0.3


class TestComputeRuleEvaluation:
    def test_basic_evaluation(self):
        matched = [
            {
                "session_id": "s1", "rule_id": "R1",
                "actual_role": "DIRECT", "candidate_role": "FW",
                "routing_role": "DIRECT", "reward_value": 0.5,
                "timestamp": "2026-08-12T18:00:00+00:00",
            },
        ]
        all_rewards = [
            _reward(session_id=f"h{i}", rule_id="R1",
                    routing_role="FW", reward_value=0.8)
            for i in range(15)
        ]
        result = compute_rule_evaluation(matched, all_rewards)
        assert "R1" in result
        r = result["R1"]
        assert r["actual_mean_reward"] == pytest.approx(0.5)
        assert r["candidate_expected_reward"] == pytest.approx(0.8)
        assert r["improvement_delta"] == pytest.approx(0.3)
        assert r["sample_count"] == 1
        assert "selection_bias_warning" not in r

    def test_insufficient_candidate_history_warns(self):
        matched = [
            {
                "session_id": "s1", "rule_id": "R1",
                "actual_role": "DIRECT", "candidate_role": "FW",
                "routing_role": "DIRECT", "reward_value": 0.5,
                "timestamp": "2026-08-12T18:00:00+00:00",
            },
        ]
        all_rewards = [
            _reward(session_id=f"h{i}", rule_id="R1",
                    routing_role="FW", reward_value=0.9)
            for i in range(5)
        ]
        result = compute_rule_evaluation(matched, all_rewards)
        assert result["R1"]["selection_bias_warning"] is True

    def test_multiple_rules(self):
        matched = [
            {
                "session_id": "s1", "rule_id": "R1",
                "actual_role": "DIRECT", "candidate_role": "FW",
                "routing_role": "DIRECT", "reward_value": 0.4,
                "timestamp": "2026-08-12T18:00:00+00:00",
            },
            {
                "session_id": "s2", "rule_id": "R2",
                "actual_role": "FW", "candidate_role": "DIRECT",
                "routing_role": "FW", "reward_value": 0.6,
                "timestamp": "2026-08-12T18:01:00+00:00",
            },
        ]
        all_rewards = (
            [_reward(session_id=f"h{i}", rule_id="R1",
                     routing_role="FW", reward_value=0.7) for i in range(12)]
            + [_reward(session_id=f"g{i}", rule_id="R2",
                       routing_role="DIRECT", reward_value=0.3) for i in range(12)]
        )
        result = compute_rule_evaluation(matched, all_rewards)
        assert "R1" in result
        assert "R2" in result
        assert result["R1"]["improvement_delta"] == pytest.approx(0.3, abs=1e-4)
        assert result["R2"]["improvement_delta"] == pytest.approx(-0.3, abs=1e-4)

    def test_no_candidate_history(self):
        matched = [
            {
                "session_id": "s1", "rule_id": "R1",
                "actual_role": "DIRECT", "candidate_role": "FW",
                "routing_role": "DIRECT", "reward_value": 0.5,
                "timestamp": "2026-08-12T18:00:00+00:00",
            },
        ]
        result = compute_rule_evaluation(matched, all_rewards=[])
        assert result["R1"]["candidate_expected_reward"] == pytest.approx(0.0)
        assert result["R1"]["selection_bias_warning"] is True

    def test_empty_matched(self):
        result = compute_rule_evaluation([], all_rewards=[])
        assert result == {}


class TestCandidateRewardCompositeRuleId:
    def test_candidate_history_uses_candidate_composite_rule_id(self):
        """Composite 'DIRECT:qa_simple' must read FW's rewards under 'FW:qa_simple'."""
        matched = [
            {
                "session_id": "s1", "rule_id": "DIRECT:qa_simple",
                "actual_role": "DIRECT", "candidate_role": "FW",
                "routing_role": "DIRECT", "reward_value": 0.5,
                "timestamp": "2026-08-12T18:00:00+00:00",
            },
        ]
        all_rewards = [
            _reward(session_id=f"h{i}", rule_id="FW:qa_simple",
                    routing_role="FW", reward_value=0.9)
            for i in range(12)
        ] + [
            _reward(session_id="x", rule_id="FW:other_intent",
                    routing_role="FW", reward_value=0.0),
        ]
        result = compute_rule_evaluation(matched, all_rewards)
        r = result["DIRECT:qa_simple"]
        assert r["candidate_expected_reward"] == pytest.approx(0.9)
        assert r["improvement_delta"] == pytest.approx(0.4)
        assert "selection_bias_warning" not in r

    def test_composite_without_candidate_rewards_stays_zero(self):
        matched = [
            {
                "session_id": "s1", "rule_id": "FO:qa_simple",
                "actual_role": "FO", "candidate_role": "RB",
                "routing_role": "FO", "reward_value": 1.0,
                "timestamp": "2026-08-12T18:00:00+00:00",
            },
        ]
        result = compute_rule_evaluation(matched, [_reward(rule_id="FO:qa_simple", routing_role="FO")])
        assert result["FO:qa_simple"]["candidate_expected_reward"] == pytest.approx(0.0)
        assert result["FO:qa_simple"]["selection_bias_warning"] is True


class TestLoadShadowRoutes:
    def test_loads_jsonl(self, tmp_path):
        shadow_file = tmp_path / "shadow_routes.jsonl"
        entries = [_shadow(session_id=f"s{i}") for i in range(3)]
        shadow_file.write_text(
            "\n".join(json.dumps(e) for e in entries),
            encoding="utf-8",
        )
        with patch("shadow_evaluator.DATA_DIR", tmp_path):
            result = load_shadow_routes()
        assert len(result) == 3

    def test_missing_file_returns_empty(self, tmp_path):
        with patch("shadow_evaluator.DATA_DIR", tmp_path):
            result = load_shadow_routes()
        assert result == []


class TestSelectionBiasWarning:
    def test_exactly_10_sessions_no_warning(self):
        matched = [
            {
                "session_id": "s1", "rule_id": "R1",
                "actual_role": "DIRECT", "candidate_role": "FW",
                "routing_role": "DIRECT", "reward_value": 0.5,
                "timestamp": "2026-08-12T18:00:00+00:00",
            },
        ]
        all_rewards = [
            _reward(session_id=f"h{i}", rule_id="R1",
                    routing_role="FW", reward_value=0.8)
            for i in range(10)
        ]
        result = compute_rule_evaluation(matched, all_rewards)
        assert "selection_bias_warning" not in result["R1"]

    def test_9_sessions_has_warning(self):
        matched = [
            {
                "session_id": "s1", "rule_id": "R1",
                "actual_role": "DIRECT", "candidate_role": "FW",
                "routing_role": "DIRECT", "reward_value": 0.5,
                "timestamp": "2026-08-12T18:00:00+00:00",
            },
        ]
        all_rewards = [
            _reward(session_id=f"h{i}", rule_id="R1",
                    routing_role="FW", reward_value=0.8)
            for i in range(9)
        ]
        result = compute_rule_evaluation(matched, all_rewards)
        assert result["R1"]["selection_bias_warning"] is True


class TestIntentExtractionFromRuleId:
    def test_intent_extraction_from_composite_rule_id(self):
        """Composite rule_id 'FO:agentic_action' -> intent='agentic_action'."""
        matched = [
            {
                "session_id": "s1", "rule_id": "FO:agentic_action",
                "actual_role": "FO", "candidate_role": "DIRECT",
                "routing_role": "FO", "reward_value": 0.5,
                "timestamp": "2026-08-12T18:00:00+00:00",
            },
        ]
        result = compute_rule_evaluation(matched, all_rewards=[])
        assert "FO:agentic_action" in result
        assert result["FO:agentic_action"]["intent"] == "agentic_action"

    def test_intent_fallback_for_bare_rule_id(self):
        """Bare rule_id 'DIRECT' (no colon) -> intent='unknown'."""
        matched = [
            {
                "session_id": "s1", "rule_id": "DIRECT",
                "actual_role": "DIRECT", "candidate_role": "FW",
                "routing_role": "DIRECT", "reward_value": 0.6,
                "timestamp": "2026-08-12T18:00:00+00:00",
            },
        ]
        result = compute_rule_evaluation(matched, all_rewards=[])
        assert "DIRECT" in result
        assert result["DIRECT"]["intent"] == "unknown"

    def test_grouping_by_composite_rule_id(self):
        """Two different composite rule_ids produce separate evaluation entries."""
        matched = [
            {
                "session_id": "s1", "rule_id": "FO:agentic_action",
                "actual_role": "FO", "candidate_role": "DIRECT",
                "routing_role": "FO", "reward_value": 0.5,
                "timestamp": "2026-08-12T18:00:00+00:00",
            },
            {
                "session_id": "s2", "rule_id": "FO:qa_simple",
                "actual_role": "FO", "candidate_role": "FW",
                "routing_role": "FO", "reward_value": 0.7,
                "timestamp": "2026-08-12T18:01:00+00:00",
            },
        ]
        result = compute_rule_evaluation(matched, all_rewards=[])
        assert "FO:agentic_action" in result
        assert "FO:qa_simple" in result
        assert len(result) == 2
        assert result["FO:agentic_action"]["intent"] == "agentic_action"
        assert result["FO:qa_simple"]["intent"] == "qa_simple"


# ===========================================================================
# --- from test_reward.py (reward.py-specific tests only) ---
# ===========================================================================

SID = "rw-test-session"


def _ev(
    session_id: str = SID,
    tier: str = "fast",
    tool: str = "Edit",
    ts: str = "2025-01-01T00:00:00",
    error: bool = False,
    est_tokens: int = 0,
) -> dict:
    e: dict = {
        "session_id": session_id,
        "type": "tool",
        "tier": tier,
        "tool_name": tool,
        "ts": ts,
        "est_tokens": est_tokens,
    }
    if error:
        e["error"] = True
    return e


def test_detect_override_fast_then_reasoning_without_task():
    """Fast event followed by reasoning event (no Task in between) -> override detected."""
    events = [
        _ev(tier="fast", ts="2025-01-01T00:00:01"),
        _ev(tier="reasoning", ts="2025-01-01T00:00:02", est_tokens=500),
    ]
    assert rw.detect_override(events, SID, None) is True


def test_detect_override_agent_dispatch_exemption():
    """Agent dispatch (like Task) exempts from override penalty: fast->reasoning with Agent in between."""
    events = [
        _ev(tier="fast", ts="2025-01-01T00:00:01", tool="Edit"),
        {"session_id": SID, "type": "tool", "tool_name": "Agent", "ts": "2025-01-01T00:00:01.5", "est_tokens": 0},
        _ev(tier="reasoning", ts="2025-01-01T00:00:02", est_tokens=500),
    ]
    assert rw.detect_override(events, SID, None) is False


def test_compute_reward_agent_dispatch_no_override_penalty():
    """Reward for fast->reasoning with Agent dispatch equals reward without penalty (not -0.5)."""
    events_with_agent = [
        _ev(tier="fast", ts="2025-01-01T00:00:01", tool="Edit"),
        {"session_id": SID, "type": "tool", "tool_name": "Agent", "ts": "2025-01-01T00:00:01.5", "est_tokens": 0},
        _ev(tier="reasoning", ts="2025-01-01T00:00:02", est_tokens=500),
    ]
    reward_with_agent = rw.compute_reward(events_with_agent, SID, window_start=None, est_cost=0.0)

    # Control: same sequence without Agent dispatch should detect override and incur -0.5 penalty
    events_without_agent = [
        _ev(tier="fast", ts="2025-01-01T00:00:01", tool="Edit"),
        _ev(tier="reasoning", ts="2025-01-01T00:00:02", est_tokens=500),
    ]
    reward_without_agent = rw.compute_reward(events_without_agent, SID, window_start=None, est_cost=0.0)

    # Agent dispatch should prevent the override penalty, making reward higher (0.5 difference)
    assert reward_with_agent > reward_without_agent
    assert reward_with_agent - reward_without_agent == pytest.approx(0.5)


def test_compute_reward_subagent_dispatch_exemption_zero_penalty():
    """Subagent dispatch exempts from escalation penalty: reward equals baseline without tier events."""
    # Case A: subagent_dispatch events (tier fast -> reasoning) should incur ZERO penalty.
    # Real dispatches are preceded by an Agent tool event, which exempts the override check.
    events_with_subagent = [
        _ev(tier="fast", ts="2025-01-01T00:00:01", tool="Edit"),
        {"session_id": SID, "type": "tool", "tool_name": "Agent", "ts": "2025-01-01T00:00:01.2", "est_tokens": 0},
        {"session_id": SID, "type": "subagent_dispatch", "tier": "fast", "agent_type": "Explore", "ts": "2025-01-01T00:00:01.5", "est_tokens": 0},
        {"session_id": SID, "type": "subagent_dispatch", "tier": "reasoning", "agent_type": "Plan", "ts": "2025-01-01T00:00:02", "est_tokens": 500},
    ]
    reward_with_subagent = rw.compute_reward(events_with_subagent, SID, window_start=None, est_cost=0.0)

    # Baseline: no tier events (no escalation, no penalty)
    events_baseline = [
        _ev(tool="Edit", ts="2025-01-01T00:00:01", est_tokens=0),
    ]
    reward_baseline = rw.compute_reward(events_baseline, SID, window_start=None, est_cost=0.0)

    # Subagent dispatch incurs zero penalty, so reward equals baseline
    assert reward_with_subagent == pytest.approx(reward_baseline)


def test_compute_reward_main_session_escalation_incurs_penalty():
    """Main-session tool escalation incurs penalty; strictly lower than subagent dispatch exemption."""
    # Case B: main-session tools with escalation (fast->reasoning) INCURS penalty
    events_main_session = [
        _ev(tier="fast", ts="2025-01-01T00:00:01", tool="Edit"),
        _ev(tier="reasoning", ts="2025-01-01T00:00:02", est_tokens=500),
    ]
    reward_main_session = rw.compute_reward(events_main_session, SID, window_start=None, est_cost=0.0)

    # Case A: subagent_dispatch with same tiers (zero penalty), preceded by an Agent tool event
    events_with_subagent = [
        _ev(tier="fast", ts="2025-01-01T00:00:01", tool="Edit"),
        {"session_id": SID, "type": "tool", "tool_name": "Agent", "ts": "2025-01-01T00:00:01.2", "est_tokens": 0},
        {"session_id": SID, "type": "subagent_dispatch", "tier": "fast", "agent_type": "Explore", "ts": "2025-01-01T00:00:01.5", "est_tokens": 0},
        {"session_id": SID, "type": "subagent_dispatch", "tier": "reasoning", "agent_type": "Plan", "ts": "2025-01-01T00:00:02", "est_tokens": 500},
    ]
    reward_with_subagent = rw.compute_reward(events_with_subagent, SID, window_start=None, est_cost=0.0)

    # Main-session escalation incurs penalty, so reward is strictly lower than Case A
    assert reward_main_session < reward_with_subagent


def test_detect_revert_from_events():
    """detect_reverts returns an integer count (>= 0) and must not crash."""
    events = [
        _ev(),
        {"session_id": SID, "type": "tool", "tier": "fast", "tool_name": "Bash",
         "ts": "2025-01-01T00:00:03", "est_tokens": 0,
         "tool_input": {"command": "git revert HEAD"}},
    ]
    result = rw.detect_reverts(events, SID, window_start=None)
    assert isinstance(result, int)
    assert result >= 0


def test_compute_reward_clean_session_positive():
    """A clean fast session with no errors or reverts -> positive reward."""
    events = [_ev(tier="fast", tool="Edit")]
    score = rw.compute_reward(events, SID, window_start=None, est_cost=0.0)
    assert isinstance(score, float)
    assert score > 0, f"clean session reward should be positive, got {score}"


# ===========================================================================
# --- from test_reward_signal_quality.py ---
# ===========================================================================

def _make_verdict(role: str = "DIRECT", intent: str | None = None, model: str = "claude-haiku-4") -> dict:
    v = {"type": "routing_declared", "role": role, "model": model}
    if intent is not None:
        v["intent"] = intent
    return v


class TestQualityMultiplier:
    def test_no_quality_data_returns_defaults(self):
        mult, rate = _quality_multiplier("some_intent", {})
        assert mult == 1.0
        assert rate == 0.0

    def test_none_intent_returns_defaults(self):
        data = {"edit": {"revert_rate": 0.5}}
        mult, rate = _quality_multiplier(None, data)
        assert mult == 1.0
        assert rate == 0.0

    def test_revert_rate_half(self):
        data = {"edit": {"revert_rate": 0.5}}
        mult, rate = _quality_multiplier("edit", data)
        assert mult == pytest.approx(0.5)
        assert rate == pytest.approx(0.5)

    def test_revert_rate_zero(self):
        data = {"edit": {"revert_rate": 0.0}}
        mult, rate = _quality_multiplier("edit", data)
        assert mult == pytest.approx(1.0)
        assert rate == pytest.approx(0.0)

    def test_revert_rate_one_floors_to_point_one(self):
        data = {"edit": {"revert_rate": 1.0}}
        mult, rate = _quality_multiplier("edit", data)
        assert mult == pytest.approx(0.1)
        assert rate == pytest.approx(1.0)

    def test_revert_rate_above_one_still_floors(self):
        data = {"edit": {"revert_rate": 1.5}}
        mult, rate = _quality_multiplier("edit", data)
        assert mult == pytest.approx(0.1)
        assert rate == pytest.approx(1.5)

    def test_missing_revert_rate_key_defaults(self):
        data = {"edit": {"some_other_key": 99}}
        mult, rate = _quality_multiplier("edit", data)
        assert mult == pytest.approx(1.0)
        assert rate == pytest.approx(0.0)


class TestQualitySignalProducerConsumerShape:
    """quality_signal.py writes {"intents": [...]}; reward_signal must consume it."""

    def _roundtrip(self, tmp_path, monkeypatch, events):
        import reward_signal
        from reward_signal import _load_quality_signal, build_reward_entries
        monkeypatch.setattr(reward_signal, "DATA_DIR", tmp_path)
        (tmp_path / "quality_signal.json").write_text(
            json.dumps(compute_quality_signal(events)), encoding="utf-8"
        )
        return _load_quality_signal(), build_reward_entries

    def test_producer_output_yields_real_revert_rate(self, tmp_path, monkeypatch):
        events = [
            {"type": "intent", "session_id": "s1", "intent": "edit", "ts": "2026-01-01T00:00:01"},
            {"type": "tool", "session_id": "s1", "tool_name": "Edit", "intent": "edit", "ts": "2026-01-01T00:00:02"},
            {"type": "reward_signal", "session_id": "s1", "signal": "quality_failure", "intent": "edit", "ts": "2026-01-01T00:00:03"},
            {"type": "tool", "session_id": "s2", "tool_name": "Edit", "intent": "edit", "ts": "2026-01-01T00:01:00"},
        ]
        qdata, build = self._roundtrip(tmp_path, monkeypatch, events)
        assert qdata["edit"]["revert_rate"] == pytest.approx(0.5)
        entries = build([_make_verdict("FW", intent="edit")], "sx", False, qdata)
        assert entries[0]["revert_rate_at_compute"] == pytest.approx(0.5)
        assert entries[0]["quality_multiplier"] == pytest.approx(0.5)

    def test_intent_absent_from_producer_output_falls_back(self, tmp_path, monkeypatch):
        events = [{"type": "tool", "session_id": "s1", "tool_name": "Edit", "intent": "edit", "ts": "2026-01-01T00:00:02"}]
        qdata, build = self._roundtrip(tmp_path, monkeypatch, events)
        entries = build([_make_verdict("FW", intent="other")], "sx", False, qdata)
        assert entries[0]["quality_multiplier"] == 1.0
        assert entries[0]["revert_rate_at_compute"] == 0.0


class TestFilterGateOnlyQualityAware:
    def test_dampened_positive_not_discarded(self):
        entries = [
            {"session_id": "s1", "reward_value": 0.1, "base_reward_value": 1.0, "quality_multiplier": 0.1},
            {"session_id": "s1", "reward_value": 0.1, "base_reward_value": 1.0, "quality_multiplier": 0.1},
            {"session_id": "s1", "reward_value": 0.1, "base_reward_value": 1.0, "quality_multiplier": 0.1},
        ]
        result = filter_gate_only(entries)
        assert len(result) == 3

    def test_truly_negative_session_discarded_when_others_exist(self):
        entries = [
            {"session_id": "bad", "reward_value": -0.5, "base_reward_value": -1.0},
            {"session_id": "bad", "reward_value": -0.5, "base_reward_value": -1.0},
            {"session_id": "bad", "reward_value": -0.5, "base_reward_value": -1.0},
            {"session_id": "good", "reward_value": 0.5, "base_reward_value": 1.0},
            {"session_id": "good", "reward_value": 0.5, "base_reward_value": 1.0},
            {"session_id": "good", "reward_value": 0.5, "base_reward_value": 1.0},
        ]
        result = filter_gate_only(entries)
        session_ids = {e["session_id"] for e in result}
        assert "good" in session_ids
        assert "bad" not in session_ids

    def test_no_base_reward_falls_back_to_reward_value(self):
        entries = [
            {"session_id": "s1", "reward_value": -1.0},
            {"session_id": "s1", "reward_value": -1.0},
            {"session_id": "s1", "reward_value": -1.0},
            {"session_id": "s2", "reward_value": 1.0},
            {"session_id": "s2", "reward_value": 1.0},
            {"session_id": "s2", "reward_value": 1.0},
        ]
        result = filter_gate_only(entries)
        session_ids = {e["session_id"] for e in result}
        assert "s2" in session_ids
        assert "s1" not in session_ids


# ===========================================================================
# --- from test_quality_signal.py ---
# ===========================================================================

def _edit_event(session_id: str, intent: str = "", ts: str = "2026-09-01T10:00:00+00:00") -> dict:
    return {
        "type": "tool",
        "session_id": session_id,
        "tool_name": "Edit",
        "intent": intent,
        "ts": ts,
    }


def _revert_event(session_id: str, intent: str = "", ts: str = "2026-09-01T11:00:00+00:00") -> dict:
    return {
        "type": "reward_signal",
        "session_id": session_id,
        "signal": "quality_failure",
        "intent": intent,
        "ts": ts,
    }


class TestEmptyInput:
    def test_empty_events_returns_structure(self):
        result = compute_quality_signal(events=[])
        assert "updated_at" in result
        assert "window" in result
        assert result["window"] == DEFAULT_WINDOW
        assert result["intents"] == []

    def test_empty_events_window_matches_config(self):
        with patch("quality_signal._load_window", return_value=15):
            result = compute_quality_signal(events=[])
        assert result["window"] == 15


class TestNoFailures:
    def test_sessions_with_edits_but_no_reverts(self):
        events = [
            _edit_event("s1", intent="mechanical", ts="2026-09-01T10:00:00+00:00"),
            _edit_event("s2", intent="additive", ts="2026-09-01T10:01:00+00:00"),
            _edit_event("s3", intent="mechanical", ts="2026-09-01T10:02:00+00:00"),
        ]
        result = compute_quality_signal(events=events)
        assert len(result["intents"]) > 0
        for entry in result["intents"]:
            assert entry["reverted"] == 0
            assert entry["revert_rate"] == 0.0

    def test_revert_rate_zero_for_all_intents(self):
        events = [
            _edit_event("s1", intent="debug"),
            _edit_event("s2", intent="debug"),
        ]
        result = compute_quality_signal(events=events)
        debug_entry = next(e for e in result["intents"] if e["intent"] == "debug")
        assert debug_entry["sessions"] == 2
        assert debug_entry["reverted"] == 0
        assert debug_entry["revert_rate"] == 0.0


class TestFullRevertEdgeCases:
    def test_all_sessions_reverted(self):
        events = [
            _edit_event("s1", intent="mechanical"),
            _revert_event("s1", intent="mechanical"),
            _edit_event("s2", intent="mechanical"),
            _revert_event("s2", intent="mechanical"),
        ]
        result = compute_quality_signal(events=events)
        mech = next(e for e in result["intents"] if e["intent"] == "mechanical")
        assert mech["sessions"] == 2
        assert mech["reverted"] == 2
        assert mech["revert_rate"] == 1.0

    def test_single_session_full_revert(self):
        events = [
            _edit_event("s1", intent="debug"),
            _revert_event("s1", intent="debug"),
        ]
        result = compute_quality_signal(events=events)
        debug_entry = next(e for e in result["intents"] if e["intent"] == "debug")
        assert debug_entry["revert_rate"] == 1.0

    def test_partial_revert_rate(self):
        events = [
            _edit_event("s1", intent="additive", ts="2026-09-01T10:00:00+00:00"),
            _revert_event("s1", intent="additive", ts="2026-09-01T10:01:00+00:00"),
            _edit_event("s2", intent="additive", ts="2026-09-01T10:02:00+00:00"),
            _edit_event("s3", intent="additive", ts="2026-09-01T10:03:00+00:00"),
            _edit_event("s4", intent="additive", ts="2026-09-01T10:04:00+00:00"),
        ]
        result = compute_quality_signal(events=events)
        additive = next(e for e in result["intents"] if e["intent"] == "additive")
        assert additive["sessions"] == 4
        assert additive["reverted"] == 1
        assert additive["revert_rate"] == pytest.approx(0.25)


class TestRollingWindow:
    def test_window_limits_sessions(self):
        events = [
            _edit_event("s0", intent="mechanical", ts="2026-09-01T10:00:00+00:00"),
            _revert_event("s0", intent="mechanical", ts="2026-09-01T10:00:30+00:00"),
            _edit_event("s1", intent="mechanical", ts="2026-09-01T10:01:00+00:00"),
            _revert_event("s1", intent="mechanical", ts="2026-09-01T10:01:30+00:00"),
            _edit_event("s2", intent="mechanical", ts="2026-09-01T10:02:00+00:00"),
            _edit_event("s3", intent="mechanical", ts="2026-09-01T10:03:00+00:00"),
            _edit_event("s4", intent="mechanical", ts="2026-09-01T10:04:00+00:00"),
        ]
        with patch("quality_signal._load_window", return_value=3):
            result = compute_quality_signal(events=events)
        mech = next(e for e in result["intents"] if e["intent"] == "mechanical")
        assert mech["sessions"] == 3
        assert mech["reverted"] == 0

    def test_window_size_in_output(self):
        with patch("quality_signal._load_window", return_value=10):
            result = compute_quality_signal(events=[])
        assert result["window"] == 10


class TestIntentGrouping:
    def test_multiple_intents_grouped_separately(self):
        events = [
            _edit_event("s1", intent="mechanical"),
            _edit_event("s2", intent="additive"),
            _edit_event("s3", intent="mechanical"),
            _revert_event("s1", intent="mechanical"),
        ]
        result = compute_quality_signal(events=events)
        intent_map = {e["intent"]: e for e in result["intents"]}
        assert "mechanical" in intent_map
        assert "additive" in intent_map
        assert intent_map["mechanical"]["sessions"] == 2
        assert intent_map["mechanical"]["reverted"] == 1
        assert intent_map["additive"]["sessions"] == 1
        assert intent_map["additive"]["reverted"] == 0

    def test_no_intent_falls_back_to_unknown(self):
        events = [
            _edit_event("s1", intent=""),
            _revert_event("s1", intent=""),
        ]
        result = compute_quality_signal(events=events)
        intent_map = {e["intent"]: e for e in result["intents"]}
        assert "unknown" in intent_map
        assert intent_map["unknown"]["reverted"] == 1

    def test_intent_taken_from_classifier_intent_events(self):
        """Prod tool events carry no intent; the session's `intent` event supplies it."""
        events = [
            {"type": "intent", "session_id": "s1", "intent": "qa_simple",
             "ts": "2026-09-01T09:00:00+00:00"},
            {"type": "intent", "session_id": "s2", "intent": "agentic_action",
             "ts": "2026-09-01T09:00:00+00:00"},
            {"type": "intent", "session_id": "s2", "intent": "qa_simple",
             "ts": "2026-09-01T09:30:00+00:00"},
            _edit_event("s1"),
            _edit_event("s2"),
            _revert_event("s2"),
        ]
        result = compute_quality_signal(events=events)
        intent_map = {e["intent"]: e for e in result["intents"]}
        assert "unknown" not in intent_map
        assert intent_map["qa_simple"]["sessions"] == 2
        assert intent_map["qa_simple"]["reverted"] == 1

    def test_tool_event_intent_takes_priority_over_intent_event(self):
        events = [
            {"type": "intent", "session_id": "s1", "intent": "qa_simple",
             "ts": "2026-09-01T09:00:00+00:00"},
            _edit_event("s1", intent="mechanical"),
        ]
        result = compute_quality_signal(events=events)
        assert [e["intent"] for e in result["intents"]] == ["mechanical"]


class TestOutputSchema:
    def test_output_has_required_keys(self):
        result = compute_quality_signal(events=[])
        assert set(result.keys()) >= {"updated_at", "window", "intents"}

    def test_intent_entries_have_required_keys(self):
        events = [_edit_event("s1", intent="mechanical")]
        result = compute_quality_signal(events=events)
        for entry in result["intents"]:
            assert set(entry.keys()) >= {"intent", "sessions", "reverted", "revert_rate"}


# ===========================================================================
# --- from test_signal_filter_rules.py ---
# ===========================================================================

def _entry(session_id="s1", rule_id="r1", routing_role="DIRECT",
           reward_value=0.5, timestamp="2026-01-01T00:00:00+00:00"):
    return {
        "session_id": session_id,
        "rule_id": rule_id,
        "routing_role": routing_role,
        "actual_model": "sonnet",
        "reward_value": reward_value,
        "timestamp": timestamp,
    }


class TestFilterShortSessions:
    def test_excludes_sessions_under_3_turns(self):
        entries = [
            _entry(session_id="short", timestamp="2026-01-01T00:00:00+00:00"),
            _entry(session_id="short", timestamp="2026-01-01T00:00:01+00:00"),
            _entry(session_id="ok", timestamp="2026-01-01T00:00:00+00:00"),
            _entry(session_id="ok", timestamp="2026-01-01T00:00:01+00:00"),
            _entry(session_id="ok", timestamp="2026-01-01T00:00:02+00:00"),
        ]
        result = filter_short_sessions(entries)
        assert len(result) == 3
        assert all(e["session_id"] == "ok" for e in result)

    def test_keeps_sessions_with_exactly_3_turns(self):
        entries = [_entry(session_id="s1") for _ in range(3)]
        result = filter_short_sessions(entries)
        assert len(result) == 3


class TestFilterRetryStorms:
    def test_storm_of_3_keeps_only_last(self):
        entries = [
            _entry(session_id="s1", rule_id="r1", timestamp="2026-01-01T00:00:00+00:00"),
            _entry(session_id="s1", rule_id="r1", timestamp="2026-01-01T00:00:10+00:00"),
            _entry(session_id="s1", rule_id="r1", timestamp="2026-01-01T00:00:20+00:00"),
        ]
        result = filter_retry_storms(entries)
        assert len(result) == 1
        assert result[0]["timestamp"] == "2026-01-01T00:00:20+00:00"

    def test_non_storm_entries_kept(self):
        entries = [
            _entry(session_id="s1", rule_id="r1", timestamp="2026-01-01T00:00:00+00:00"),
            _entry(session_id="s1", rule_id="r1", timestamp="2026-01-01T00:05:00+00:00"),
        ]
        result = filter_retry_storms(entries)
        assert len(result) == 2

    def test_different_rule_ids_not_grouped(self):
        entries = [
            _entry(session_id="s1", rule_id="r1", timestamp="2026-01-01T00:00:00+00:00"),
            _entry(session_id="s1", rule_id="r2", timestamp="2026-01-01T00:00:10+00:00"),
            _entry(session_id="s1", rule_id="r3", timestamp="2026-01-01T00:00:20+00:00"),
        ]
        result = filter_retry_storms(entries)
        assert len(result) == 3


class TestFilterAccidentalOverrides:
    def test_revert_pattern_discards_middle(self):
        entries = [
            _entry(session_id="s1", routing_role="DIRECT", timestamp="2026-01-01T00:00:00+00:00"),
            _entry(session_id="s1", routing_role="FO", timestamp="2026-01-01T00:00:10+00:00"),
            _entry(session_id="s1", routing_role="DIRECT", timestamp="2026-01-01T00:00:20+00:00"),
        ]
        result = filter_accidental_overrides(entries)
        assert len(result) == 2
        roles = [e["routing_role"] for e in result]
        assert "FO" not in roles

    def test_no_revert_keeps_all(self):
        entries = [
            _entry(session_id="s1", routing_role="DIRECT", timestamp="2026-01-01T00:00:00+00:00"),
            _entry(session_id="s1", routing_role="FO", timestamp="2026-01-01T00:00:10+00:00"),
            _entry(session_id="s1", routing_role="FW", timestamp="2026-01-01T00:00:20+00:00"),
        ]
        result = filter_accidental_overrides(entries)
        assert len(result) == 3


class TestFilterGateOnly:
    def test_all_negative_session_excluded(self):
        entries = [
            _entry(session_id="bad", reward_value=-1.0),
            _entry(session_id="bad", reward_value=0.0),
            _entry(session_id="bad", reward_value=-0.5),
            _entry(session_id="good", reward_value=0.5),
            _entry(session_id="good", reward_value=-0.2),
            _entry(session_id="good", reward_value=0.3),
        ]
        result = filter_gate_only(entries)
        assert len(result) == 3
        assert all(e["session_id"] == "good" for e in result)

    def test_session_with_one_positive_kept(self):
        entries = [
            _entry(session_id="s1", reward_value=-1.0),
            _entry(session_id="s1", reward_value=0.1),
        ]
        result = filter_gate_only(entries)
        assert len(result) == 2


class TestApplyAllFilters:
    def test_mixed_valid_and_noise(self):
        valid = [
            _entry(session_id="good", rule_id="r1", routing_role="DIRECT",
                   reward_value=0.5, timestamp="2026-01-01T00:00:00+00:00"),
            _entry(session_id="good", rule_id="r2", routing_role="DIRECT",
                   reward_value=0.3, timestamp="2026-01-01T00:01:00+00:00"),
            _entry(session_id="good", rule_id="r3", routing_role="DIRECT",
                   reward_value=0.7, timestamp="2026-01-01T00:02:00+00:00"),
        ]
        noise = [
            _entry(session_id="short", reward_value=0.5, timestamp="2026-01-01T00:00:00+00:00"),
            _entry(session_id="short", reward_value=0.5, timestamp="2026-01-01T00:00:01+00:00"),
        ]
        clean, stats = apply_all_filters(valid + noise)
        assert len(clean) == 3
        assert all(e["session_id"] == "good" for e in clean)
        assert stats["total_entries"] == 5
        assert stats["noise_entries_filtered"] == 2
        assert stats["noise_rate"] == 0.4

    def test_noise_rate_zero_when_all_clean(self):
        entries = [
            _entry(session_id="s1", reward_value=0.5, timestamp=f"2026-01-01T00:0{i}:00+00:00")
            for i in range(3)
        ]
        clean, stats = apply_all_filters(entries)
        assert stats["noise_rate"] == 0.0
        assert stats["noise_entries_filtered"] == 0

    def test_empty_input(self):
        clean, stats = apply_all_filters([])
        assert clean == []
        assert stats["total_entries"] == 0
        assert stats["noise_rate"] == 0.0

    def test_noise_rate_calculation(self):
        entries = [
            _entry(session_id="ok", reward_value=0.5, timestamp=f"2026-01-01T00:0{i}:00+00:00")
            for i in range(3)
        ] + [
            _entry(session_id="tiny", reward_value=0.5, timestamp="2026-01-01T00:00:00+00:00"),
        ]
        clean, stats = apply_all_filters(entries)
        assert stats["noise_entries_filtered"] == 1
        assert stats["noise_rate"] == 0.25
        assert "timestamp" in stats
