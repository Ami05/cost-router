"""Tests for DGC-006b stop telemetry outcome/model/startup_estimate derivation.

Tests _build_subagent_stop_telemetry_row outcome derivation from payload["status"],
result_status, and stall_killed; model and startup_estimate read from dispatch registry;
missing registry data returns None (fail-open).
"""
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_routing import _build_subagent_stop_telemetry_row, _subagent_usage_record  # noqa: E402
from context_estimator import (  # noqa: E402
    _session_agents,
    load_dispatch_registry,
    mark_registry_busy,
    save_json_state,
)
from session_state import _get_session  # noqa: E402


def _make_subagent_stop_payload(
    session_id="test-session-123",
    agent_id="agent-456",
    status="success",
):
    """Build a minimal SubagentStop payload."""
    payload = {
        "hook_event_name": "SubagentStop",
        "session_id": session_id,
        "agent_id": agent_id,
        "status": status,
        "model": "claude-haiku-4-5-20251001",
    }
    return payload


def _make_state_with_subagent(
    agent_id, conversation_id="test-session-123", stall_count=0, stall_killed=False
):
    """Build a state dict with subagent_progress and session entry."""
    progress_entry = {"stall_count": stall_count, "last_marker_ts": time.time()}
    if stall_killed:
        progress_entry["stall_killed"] = True

    return {
        "sessions": {
            conversation_id: {
                "continuation_manifest": {"depth": 2},
            }
        },
        "subagents": {},
        "subagent_progress": {agent_id: progress_entry},
        "counters": {},
    }


def _make_registry_with_agent(
    session_id="test-session-123",
    agent_id="agent-456",
    model="claude-haiku-4-5-20251001",
    startup_estimate=12000,
    result_status="UNKNOWN",
    stall_killed=False,
):
    """Build a dispatch registry with agent entry."""
    return _real_shape_registry(
        session_id,
        agent_id,
        agent_id=agent_id,
        subagent_id=agent_id,
        status="idle",
        model=model,
        dispatch_context_estimate=startup_estimate,
        result_status=result_status,
        stall_killed=stall_killed,
    )


def _real_shape_registry(session_id, agent_id, **fields):
    """Registry in the production layout, laid out by the production helper.

    Hand-written dict literals here once drifted from the real
    sessions[sid]["agents"][id] layout and hid a dead registry lookup in the
    stop-row builder; going through _session_agents keeps the shape honest.
    """
    registry = {}
    _session_agents(registry, session_id)[agent_id] = dict(fields)
    return registry


class TestOutcomeFromPayloadStatus:
    """Test outcome derivation from payload["status"]."""

    def test_outcome_from_payload_status_success(self, monkeypatch, tmp_path):
        """Assert outcome='success' when payload['status']='success'."""
        registry_file = tmp_path / "dispatch_registry.jsonl"
        registry_file.write_text("")

        payload = _make_subagent_stop_payload(
            session_id="conv-001",
            agent_id="agent-001",
            status="success",
        )
        state = _make_state_with_subagent("agent-001", "conv-001")

        # Mock load_dispatch_registry to return empty registry (no entry)
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: {"sessions": {}},
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-001", state)

        assert result["outcome"] == "success"
        assert result["outcome_source"] == "payload"
        assert result["status"] == "success"

    def test_outcome_from_payload_status_partial(self, monkeypatch, tmp_path):
        """Assert outcome='partial' when payload['status']='partial'."""
        payload = _make_subagent_stop_payload(
            session_id="conv-002",
            agent_id="agent-002",
            status="partial",
        )
        state = _make_state_with_subagent("agent-002", "conv-002")

        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: {"sessions": {}},
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-002", state)

        assert result["outcome"] == "partial"
        assert result["outcome_source"] == "payload"
        assert result["status"] == "partial"

    def test_outcome_from_payload_status_failed(self, monkeypatch):
        """Assert outcome='failed' when payload['status']='failed'."""
        payload = _make_subagent_stop_payload(
            session_id="conv-003",
            agent_id="agent-003",
            status="failed",
        )
        state = _make_state_with_subagent("agent-003", "conv-003")

        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: {"sessions": {}},
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-003", state)

        assert result["outcome"] == "failed"
        assert result["outcome_source"] == "payload"
        assert result["status"] == "failed"

    def test_outcome_from_payload_status_retried(self, monkeypatch):
        """Assert outcome='retried' when payload['status']='retried'."""
        payload = _make_subagent_stop_payload(
            session_id="conv-004",
            agent_id="agent-004",
            status="retried",
        )
        state = _make_state_with_subagent("agent-004", "conv-004")

        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: {"sessions": {}},
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-004", state)

        assert result["outcome"] == "retried"
        assert result["outcome_source"] == "payload"
        assert result["status"] == "retried"

    def test_payload_status_invalid_falls_through(self, monkeypatch):
        """Assert invalid payload status falls through to other sources."""
        payload = _make_subagent_stop_payload(
            session_id="conv-005",
            agent_id="agent-005",
            status="invalid_status",  # not in {success, partial, failed, retried}
        )
        state = _make_state_with_subagent("agent-005", "conv-005")

        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: {"sessions": {}},
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-005", state)

        # Falls through to "unknown" since no result_status or stall_killed
        assert result["outcome"] == "unknown"
        assert result["outcome_source"] == "none"


class TestOutcomeFromResultStatus:
    """Test outcome derivation from registry result_status."""

    def test_outcome_from_result_status_complete(self, monkeypatch):
        """Assert outcome='success' when result_status='COMPLETE'."""
        payload = _make_subagent_stop_payload(
            session_id="conv-010",
            agent_id="agent-010",
            status="unknown",  # Invalid, so check result_status
        )
        state = _make_state_with_subagent("agent-010", "conv-010")

        registry = _real_shape_registry(
            "conv-010",
            "agent-010",
            result_status="COMPLETE",
            model="claude-sonnet-4-20250514",
            dispatch_context_estimate=8000,
        )
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-010", state)

        assert result["outcome"] == "success"
        assert result["outcome_source"] == "result_status"
        assert result["result_status"] == "COMPLETE"

    def test_outcome_from_result_status_partial(self, monkeypatch):
        """Assert outcome='partial' when result_status='PARTIAL'."""
        payload = _make_subagent_stop_payload(
            session_id="conv-011",
            agent_id="agent-011",
            status="unknown",
        )
        state = _make_state_with_subagent("agent-011", "conv-011")

        registry = _real_shape_registry(
            "conv-011",
            "agent-011",
            result_status="PARTIAL",
            model="claude-opus-4-1-20250805",
        )
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-011", state)

        assert result["outcome"] == "partial"
        assert result["outcome_source"] == "result_status"
        assert result["result_status"] == "PARTIAL"


class TestOutcomeFromStallKilled:
    """Test outcome derivation from stall_killed."""

    def test_outcome_from_stall_killed(self, monkeypatch):
        """Assert outcome='failed' when stall_killed=True."""
        payload = _make_subagent_stop_payload(
            session_id="conv-020",
            agent_id="agent-020",
            status="unknown",
        )
        state = _make_state_with_subagent("agent-020", "conv-020", stall_killed=True)

        registry = _real_shape_registry(
            "conv-020",
            "agent-020",
            stall_killed=True,
        )
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-020", state)

        assert result["outcome"] == "failed"
        assert result["outcome_source"] == "stall"
        assert result["result_status"] is None


class TestOutcomeUnknown:
    """Test outcome='unknown' when no status sources."""

    def test_outcome_unknown_no_status_data(self, monkeypatch):
        """Assert outcome='unknown' when all status sources are None."""
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "conv-030",
            "agent_id": "agent-030",
            "status": "unknown",
            # No model in payload
        }
        state = {
            "sessions": {
                "conv-030": {
                    "continuation_manifest": {"depth": 2},
                    # No last_dispatch_context_estimate
                }
            },
            "subagents": {},
            "subagent_progress": {"agent-030": {"stall_count": 0}},
            "counters": {},
        }

        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: {"sessions": {}},
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-030", state)

        assert result["outcome"] == "unknown"
        assert result["outcome_source"] == "none"
        assert result["result_status"] is None
        assert result["model"] is None


class TestModelAndStartupEstimateFromRegistry:
    """Test model and startup_estimate read from registry."""

    def test_model_from_registry(self, monkeypatch):
        """Assert model is read from dispatch registry entry."""
        payload = _make_subagent_stop_payload(
            session_id="conv-040",
            agent_id="agent-040",
        )
        state = _make_state_with_subagent("agent-040", "conv-040")

        registry = _real_shape_registry(
            "conv-040",
            "agent-040",
            model="claude-haiku-4-5-20251001",
        )
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-040", state)

        assert result["model"] == "claude-haiku-4-5-20251001"

    def test_startup_estimate_from_registry(self, monkeypatch):
        """Assert startup_estimate is read from dispatch registry entry."""
        payload = _make_subagent_stop_payload(
            session_id="conv-041",
            agent_id="agent-041",
        )
        state = _make_state_with_subagent("agent-041", "conv-041")

        registry = _real_shape_registry(
            "conv-041",
            "agent-041",
            dispatch_context_estimate=25000,
        )
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-041", state)

        assert result["startup_estimate"] == 25000

    def test_model_and_estimate_both_from_registry(self, monkeypatch):
        """Assert both model and startup_estimate read from registry."""
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "conv-042",
            "agent_id": "agent-042",
            "status": "success",
        }
        state = _make_state_with_subagent("agent-042", "conv-042")

        registry = _real_shape_registry(
            "conv-042",
            "agent-042",
            model="claude-sonnet-4-20250514",
            dispatch_context_estimate=18500,
        )
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-042", state)

        assert result["model"] == "claude-sonnet-4-20250514"
        assert result["startup_estimate"] == 18500


class TestRegistryRoundTrip:
    """Rows must read what the real writer (mark_registry_busy) stored; no mocked registry."""

    def _isolate(self, monkeypatch, tmp_path):
        import _common

        monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
        monkeypatch.setattr(_common, "DISPATCH_REGISTRY_FILE", tmp_path / "dispatch_registry.json", raising=False)

    def _stop_payload(self, **extra):
        # Deliberately no "model": SubagentStop does not carry it on Claude Code.
        payload = {"hook_event_name": "SubagentStop", "session_id": "conv-rt", "agent_id": "agent-rt", "status": "unknown"}
        payload.update(extra)
        return payload

    def test_stop_row_reads_model_and_estimate_written_by_mark_registry_busy(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        mark_registry_busy("conv-rt", "agent-rt", subagent_type="Explore", model="haiku", dispatch_context_estimate=7777)
        state = _make_state_with_subagent("agent-rt", "conv-rt")

        row = _build_subagent_stop_telemetry_row(self._stop_payload(), "conv-rt", state)

        assert row["model"] == "haiku"
        assert row["startup_estimate"] == 7777

    def test_usage_record_falls_back_to_registry_model(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        mark_registry_busy("conv-rt", "agent-rt", subagent_type="Explore", model="haiku")

        record = _subagent_usage_record(self._stop_payload(), "conv-rt")

        assert record["model"] == "haiku"

    def test_agent_type_recorded_from_payload_on_both_events(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        state = _make_state_with_subagent("agent-rt", "conv-rt")
        payload = self._stop_payload(agent_type="Explore")

        assert _build_subagent_stop_telemetry_row(payload, "conv-rt", state)["agent_type"] == "Explore"
        assert _subagent_usage_record(payload, "conv-rt")["agent_type"] == "Explore"
        assert _subagent_usage_record(self._stop_payload(), "conv-rt")["agent_type"] is None


class TestMissingRegistryDataFailsOpen:
    """Test missing registry data returns None (fail-open)."""

    def test_missing_registry_entry_model_null(self, monkeypatch):
        """Assert model=None when agent entry missing from registry."""
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "conv-050",
            "agent_id": "agent-050",
            "status": "success",
            # No model in payload
        }
        state = {
            "sessions": {
                "conv-050": {
                    "continuation_manifest": {"depth": 2},
                    # No last_dispatch_context_estimate
                }
            },
            "subagents": {},
            "subagent_progress": {"agent-050": {"stall_count": 0}},
            "counters": {},
        }

        # Empty registry, no agent entry
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: {"sessions": {}},
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-050", state)

        assert result["model"] is None

    def test_missing_registry_entry_estimate_null(self, monkeypatch):
        """Assert startup_estimate=None when agent entry missing."""
        payload = _make_subagent_stop_payload(
            session_id="conv-051",
            agent_id="agent-051",
        )
        state = _make_state_with_subagent("agent-051", "conv-051")

        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: {"sessions": {}},
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-051", state)

        assert result["startup_estimate"] is None

    def test_missing_model_field_null(self, monkeypatch):
        """Assert model=None when model field missing from entry."""
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "conv-052",
            "agent_id": "agent-052",
            "status": "success",
        }
        state = _make_state_with_subagent("agent-052", "conv-052")

        registry = _real_shape_registry(
            "conv-052",
            "agent-052",
            dispatch_context_estimate=10000,
            # model field missing
        )
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-052", state)

        assert result["model"] is None
        assert result["startup_estimate"] == 10000

    def test_missing_estimate_field_null(self, monkeypatch):
        """Assert startup_estimate=None when field missing from entry."""
        payload = _make_subagent_stop_payload(
            session_id="conv-053",
            agent_id="agent-053",
        )
        state = _make_state_with_subagent("agent-053", "conv-053")

        registry = _real_shape_registry(
            "conv-053",
            "agent-053",
            model="claude-haiku-4-5-20251001",
            # dispatch_context_estimate field missing
        )
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-053", state)

        assert result["model"] == "claude-haiku-4-5-20251001"
        assert result["startup_estimate"] is None

    def test_registry_load_exception_fails_open(self, monkeypatch):
        """Assert exceptions in registry load are caught (fail-open)."""
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "conv-054",
            "agent_id": "agent-054",
            "status": None,
            # No model in payload
        }
        state = {
            "sessions": {
                "conv-054": {
                    "continuation_manifest": {"depth": 2},
                    # No last_dispatch_context_estimate
                }
            },
            "subagents": {},
            "subagent_progress": {"agent-054": {"stall_count": 0}},
            "counters": {},
        }

        # Simulate registry load failure
        def mock_load_fail():
            raise OSError("Registry file locked")

        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            mock_load_fail,
        )

        # Should not raise, returns None for model/estimate
        result = _build_subagent_stop_telemetry_row(payload, "conv-054", state)

        assert result["model"] is None
        assert result["startup_estimate"] is None
        assert result["outcome"] == "unknown"


class TestRegistryDataTypesAndEdgeCases:
    """Test edge cases and data type handling."""

    def test_registry_session_not_dict(self, monkeypatch):
        """Assert agent lookup safe when session value is not dict."""
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "conv-060",
            "agent_id": "agent-060",
            "status": "success",
            # No model in payload
        }
        state = {
            "sessions": {
                "conv-060": {
                    "continuation_manifest": {"depth": 2},
                    # No last_dispatch_context_estimate
                }
            },
            "subagents": {},
            "subagent_progress": {"agent-060": {"stall_count": 0}},
            "counters": {},
        }

        registry = {
            "sessions": {
                "conv-060": "not_a_dict",  # Invalid, should be dict
            }
        }
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-060", state)

        assert result["model"] is None
        assert result["startup_estimate"] is None

    def test_registry_agent_not_dict(self, monkeypatch):
        """Assert safe handling when agent entry is not dict."""
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "conv-061",
            "agent_id": "agent-061",
            "status": "success",
            # No model in payload
        }
        state = {
            "sessions": {
                "conv-061": {
                    "continuation_manifest": {"depth": 2},
                    # No last_dispatch_context_estimate
                }
            },
            "subagents": {},
            "subagent_progress": {"agent-061": {"stall_count": 0}},
            "counters": {},
        }

        registry = {
            "sessions": {
                "conv-061": {
                    "agents": {
                        "agent-061": "not_a_dict",  # Invalid
                    }
                }
            }
        }
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-061", state)

        assert result["model"] is None
        assert result["startup_estimate"] is None

    def test_model_field_value_zero(self, monkeypatch):
        """Assert model=0 is preserved (falsy but valid)."""
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "conv-062",
            "agent_id": "agent-062",
            "status": "success",
        }
        state = _make_state_with_subagent("agent-062", "conv-062")

        registry = _real_shape_registry(
            "conv-062",
            "agent-062",
            model=0,  # Falsy value
        )
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-062", state)

        assert result["model"] == 0

    def test_startup_estimate_zero(self, monkeypatch):
        """Assert startup_estimate=0 is preserved."""
        payload = _make_subagent_stop_payload(
            session_id="conv-063",
            agent_id="agent-063",
        )
        state = _make_state_with_subagent("agent-063", "conv-063")

        registry = _real_shape_registry(
            "conv-063",
            "agent-063",
            dispatch_context_estimate=0,
        )
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-063", state)

        assert result["startup_estimate"] == 0


class TestTelemetryRowStructure:
    """Test telemetry row contains all required fields."""

    def test_telemetry_row_has_all_fields(self, monkeypatch):
        """Assert telemetry row has all DGC-006b fields."""
        payload = _make_subagent_stop_payload(
            session_id="conv-070",
            agent_id="agent-070",
            status="success",
        )
        state = _make_state_with_subagent("agent-070", "conv-070", stall_count=3)

        registry = _real_shape_registry(
            "conv-070",
            "agent-070",
            model="claude-sonnet-4-20250514",
            dispatch_context_estimate=30000,
            result_status="COMPLETE",
        )
        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: registry,
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-070", state)

        # All required fields present
        assert "type" in result
        assert result["type"] == "subagent_stop_telemetry"
        assert "session_id" in result
        assert "agent_id" in result
        assert "depth" in result
        assert "status" in result
        assert "model" in result
        assert "outcome" in result
        assert "outcome_source" in result
        assert "result_status" in result
        assert "startup_estimate" in result
        assert "stall_events" in result
        assert "would_deny_counters" in result

    def test_telemetry_row_depth_from_state(self, monkeypatch):
        """Assert depth is read from state continuation_manifest."""
        payload = _make_subagent_stop_payload(
            session_id="conv-071",
            agent_id="agent-071",
        )
        state = {
            "sessions": {
                "conv-071": {
                    "last_dispatch_context_estimate": {"total_tokens": 10000},
                    "continuation_manifest": {"depth": 5},
                }
            },
            "subagents": {},
            "subagent_progress": {"agent-071": {"stall_count": 0}},
            "counters": {},
        }

        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: {"sessions": {}},
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-071", state)

        assert result["depth"] == 5

    def test_telemetry_row_stall_events_count(self, monkeypatch):
        """Assert stall_events is read from subagent_progress stall_count."""
        payload = _make_subagent_stop_payload(
            session_id="conv-072",
            agent_id="agent-072",
        )
        state = _make_state_with_subagent("agent-072", "conv-072", stall_count=7)

        monkeypatch.setattr(
            "check_routing.load_dispatch_registry",
            lambda: {"sessions": {}},
        )

        result = _build_subagent_stop_telemetry_row(payload, "conv-072", state)

        assert result["stall_events"] == 7
