"""Tests for DGC-106 manifest persistence at SubagentStop.

Tests event_subagent_stop manifest parsing, mark_registry_idle manifest fields,
manifest_stats tracking, and helper functions (get_last_manifest, get_manifest_stats,
reconcile_stuck_busy).
"""
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_routing import event_subagent_stop  # noqa: E402
from context_estimator import (  # noqa: E402
    get_last_manifest,
    get_manifest_stats,
    load_dispatch_registry,
    mark_registry_idle,
    reconcile_stuck_busy,
    save_json_state,
)
from _fileio import DATA_DIR  # noqa: E402
from session_state import ROUTING_STATE_FILE, SUBAGENT_STATE_FILE  # noqa: E402


def _make_subagent_stop_payload(
    session_id="test-session-123",
    agent_id="agent-456",
    status="success",
    agent_transcript_path=None,
    last_assistant_message=None,
):
    """Build a minimal SubagentStop payload."""
    payload = {
        "hook_event_name": "SubagentStop",
        "session_id": session_id,
        "agent_id": agent_id,
        "status": status,
        "model": "claude-haiku-4-5-20251001",
    }
    if agent_transcript_path:
        payload["agent_transcript_path"] = agent_transcript_path
    if last_assistant_message:
        payload["last_assistant_message"] = last_assistant_message
    return payload


def _make_state_with_subagent(
    agent_id, stall_count=0, depth=2, stall_killed=False
):
    """Build a state dict with subagent_progress and session entry."""
    progress_entry = {"stall_count": stall_count, "last_marker_ts": time.time()}
    if stall_killed:
        progress_entry["stall_killed"] = True

    return {
        "sessions": {
            "test-session-123": {
                "last_dispatch_context_estimate": {"total_tokens": 15000},
                "continuation_manifest": {"depth": depth},
            }
        },
        "subagents": {},
        "subagent_progress": {agent_id: progress_entry},
    }


def _make_transcript_with_manifest(tmp_path, manifest_dict):
    """Create a temporary JSONL transcript with last assistant message containing manifest.

    Returns the path to the transcript file.
    """
    manifest_json = json.dumps(manifest_dict)
    report_text = f"Work done. Final status:\n\n```continuation-manifest\n{manifest_json}\n```"

    transcript_path = tmp_path / "agent_transcript.jsonl"
    entries = [
        {"role": "user", "content": "Do some work"},
        {
            "role": "assistant",
            "content": report_text,
            "message": {"usage": {"output_tokens": 100, "input_tokens": 50, "cache_read_input_tokens": 0}},
        },
    ]

    with open(transcript_path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    return str(transcript_path)


class TestManifestParsingInStop:
    """Test event_subagent_stop parses manifest from agent_transcript_path."""

    def test_manifest_complete_from_transcript(self, tmp_path, monkeypatch):
        """Assert COMPLETE manifest parsed from agent_transcript_path."""
        manifest_dict = {
            "status": "COMPLETE",
            "depth": 1,
            "done": [],
            "remaining": [],
            "tokens_used": 5000,
        }
        transcript_path = _make_transcript_with_manifest(tmp_path, manifest_dict)

        payload = _make_subagent_stop_payload(
            session_id="test-session-123",
            agent_id="agent-001",
            agent_transcript_path=transcript_path,
        )
        state = _make_state_with_subagent("agent-001")

        # Mock _load_state and _save_state
        monkeypatch.setattr("check_routing._load_state", lambda: state)
        monkeypatch.setattr("check_routing._save_state", lambda s: None)
        monkeypatch.setattr("check_routing.append_event", lambda e: None)

        # Mock mark_registry_idle to capture arguments
        captured_args = {}
        def mock_mark_registry_idle(*args, **kwargs):
            captured_args.update(kwargs)
        monkeypatch.setattr("check_routing.mark_registry_idle", mock_mark_registry_idle)

        event_subagent_stop(payload, is_claude=True)

        assert captured_args.get("result_status") == "COMPLETE"
        assert captured_args.get("depth") == 1
        assert captured_args.get("manifest_source") == "agent_transcript_path"

    def test_manifest_partial_from_transcript(self, tmp_path, monkeypatch):
        """Assert PARTIAL manifest parsed from agent_transcript_path."""
        manifest_dict = {
            "status": "PARTIAL",
            "depth": 2,
            "done": [{"id": "task-1", "goal": "done"}],
            "remaining": [{"id": "task-2", "goal": "pending"}],
            "tokens_used": 3000,
        }
        transcript_path = _make_transcript_with_manifest(tmp_path, manifest_dict)

        payload = _make_subagent_stop_payload(
            session_id="test-session-123",
            agent_id="agent-002",
            agent_transcript_path=transcript_path,
        )
        state = _make_state_with_subagent("agent-002")

        monkeypatch.setattr("check_routing._load_state", lambda: state)
        monkeypatch.setattr("check_routing._save_state", lambda s: None)
        monkeypatch.setattr("check_routing.append_event", lambda e: None)

        captured_args = {}
        def mock_mark_registry_idle(*args, **kwargs):
            captured_args.update(kwargs)
        monkeypatch.setattr("check_routing.mark_registry_idle", mock_mark_registry_idle)

        event_subagent_stop(payload, is_claude=True)

        assert captured_args.get("result_status") == "PARTIAL"
        assert captured_args.get("depth") == 2

    def test_manifest_fallback_to_payload(self, monkeypatch):
        """Assert manifest parsed from payload.last_assistant_message on transcript miss."""
        manifest_dict = {
            "status": "PARTIAL",
            "depth": 0,
            "done": [],
            "remaining": [],
        }
        manifest_json = json.dumps(manifest_dict)
        report_text = f"Fallback: ```continuation-manifest\n{manifest_json}\n```"

        payload = _make_subagent_stop_payload(
            session_id="test-session-123",
            agent_id="agent-003",
            agent_transcript_path="/nonexistent/path.jsonl",
            last_assistant_message=report_text,
        )
        state = _make_state_with_subagent("agent-003")

        monkeypatch.setattr("check_routing._load_state", lambda: state)
        monkeypatch.setattr("check_routing._save_state", lambda s: None)
        monkeypatch.setattr("check_routing.append_event", lambda e: None)

        captured_args = {}
        def mock_mark_registry_idle(*args, **kwargs):
            captured_args.update(kwargs)
        monkeypatch.setattr("check_routing.mark_registry_idle", mock_mark_registry_idle)

        event_subagent_stop(payload, is_claude=True)

        assert captured_args.get("result_status") == "PARTIAL"
        assert captured_args.get("manifest_source") == "payload"

    def test_no_manifest_result_status_unknown(self, monkeypatch):
        """Assert result_status=UNKNOWN when no manifest found."""
        payload = _make_subagent_stop_payload(
            session_id="test-session-123",
            agent_id="agent-004",
        )
        state = _make_state_with_subagent("agent-004")

        monkeypatch.setattr("check_routing._load_state", lambda: state)
        monkeypatch.setattr("check_routing._save_state", lambda s: None)
        monkeypatch.setattr("check_routing.append_event", lambda e: None)

        captured_args = {}
        def mock_mark_registry_idle(*args, **kwargs):
            captured_args.update(kwargs)
        monkeypatch.setattr("check_routing.mark_registry_idle", mock_mark_registry_idle)

        event_subagent_stop(payload, is_claude=True)

        assert captured_args.get("result_status") == "UNKNOWN"
        assert captured_args.get("manifest_source") == "none"

    def test_stall_killed_with_manifest_partial(self, tmp_path, monkeypatch):
        """Assert result_status=PARTIAL when stall_killed + valid manifest."""
        manifest_dict = {"status": "COMPLETE", "depth": 1, "done": [], "remaining": []}
        transcript_path = _make_transcript_with_manifest(tmp_path, manifest_dict)

        payload = _make_subagent_stop_payload(
            session_id="test-session-123",
            agent_id="agent-005",
            agent_transcript_path=transcript_path,
        )
        state = _make_state_with_subagent("agent-005", stall_killed=True)

        monkeypatch.setattr("check_routing._load_state", lambda: state)
        monkeypatch.setattr("check_routing._save_state", lambda s: None)
        monkeypatch.setattr("check_routing.append_event", lambda e: None)

        captured_args = {}
        def mock_mark_registry_idle(*args, **kwargs):
            captured_args.update(kwargs)
        monkeypatch.setattr("check_routing.mark_registry_idle", mock_mark_registry_idle)

        event_subagent_stop(payload, is_claude=True)

        assert captured_args.get("result_status") == "PARTIAL"
        assert captured_args.get("stall_killed") is True

    def test_stall_killed_without_manifest_unknown(self, monkeypatch):
        """Assert result_status=UNKNOWN when stall_killed without valid manifest."""
        payload = _make_subagent_stop_payload(
            session_id="test-session-123",
            agent_id="agent-006",
        )
        state = _make_state_with_subagent("agent-006", stall_killed=True)

        monkeypatch.setattr("check_routing._load_state", lambda: state)
        monkeypatch.setattr("check_routing._save_state", lambda s: None)
        monkeypatch.setattr("check_routing.append_event", lambda e: None)

        captured_args = {}
        def mock_mark_registry_idle(*args, **kwargs):
            captured_args.update(kwargs)
        monkeypatch.setattr("check_routing.mark_registry_idle", mock_mark_registry_idle)

        event_subagent_stop(payload, is_claude=True)

        assert captured_args.get("result_status") == "UNKNOWN"
        assert captured_args.get("stall_killed") is True


class TestMarkRegistryIdleManifest:
    """Test mark_registry_idle persists manifest fields."""

    def test_mark_idle_result_status_complete(self, tmp_path, monkeypatch):
        """Assert result_status=COMPLETE persisted in registry entry."""
        registry_path = tmp_path / "dispatch_registry.json"
        registry_path.write_text("{}")

        monkeypatch.setattr(
            "context_estimator._dispatch_registry_file",
            lambda: registry_path,
        )

        mark_registry_idle(
            "session-1",
            "agent-001",
            result_status="COMPLETE",
            depth=2,
            manifest_source="agent_transcript_path",
        )

        registry = load_dispatch_registry()
        entry = registry["sessions"]["session-1"]["agents"]["agent-001"]

        assert entry["status"] == "idle"
        assert entry["result_status"] == "COMPLETE"
        assert entry["depth"] == 2
        assert entry["manifest_source"] == "agent_transcript_path"
        assert entry["manifest_schema_version"] == 1

    def test_mark_idle_manifest_stats_updated(self, tmp_path, monkeypatch):
        """Assert manifest_stats incremented on mark_registry_idle."""
        registry_path = tmp_path / "dispatch_registry.json"
        registry_path.write_text("{}")

        monkeypatch.setattr(
            "context_estimator._dispatch_registry_file",
            lambda: registry_path,
        )

        mark_registry_idle("session-1", "agent-001", result_status="COMPLETE")
        mark_registry_idle("session-1", "agent-002", result_status="PARTIAL")
        mark_registry_idle("session-1", "agent-003", result_status="UNKNOWN")

        registry = load_dispatch_registry()
        stats = registry.get("manifest_stats", {})

        assert stats["total"] == 3
        assert stats["unknown"] == 1
        assert stats["by_status"]["COMPLETE"] == 1
        assert stats["by_status"]["PARTIAL"] == 1
        assert stats["by_status"]["UNKNOWN"] == 1

    def test_mark_idle_remaining_capped(self, tmp_path, monkeypatch):
        """Assert remaining items capped to 20."""
        registry_path = tmp_path / "dispatch_registry.json"
        registry_path.write_text("{}")

        monkeypatch.setattr(
            "context_estimator._dispatch_registry_file",
            lambda: registry_path,
        )

        remaining = [{"id": f"task-{i}", "goal": f"goal-{i}"} for i in range(30)]
        mark_registry_idle("session-1", "agent-001", remaining=remaining)

        registry = load_dispatch_registry()
        entry = registry["sessions"]["session-1"]["agents"]["agent-001"]

        assert len(entry["remaining"]) == 20
        assert entry["remaining"][0]["id"] == "task-0"
        assert entry["remaining"][19]["id"] == "task-19"


class TestGetLastManifest:
    """Test get_last_manifest reads manifest from registry."""

    def test_get_last_manifest_found(self, tmp_path, monkeypatch):
        """Assert get_last_manifest returns entry with schema version."""
        registry_path = tmp_path / "dispatch_registry.json"
        registry_path.write_text("{}")

        monkeypatch.setattr(
            "context_estimator._dispatch_registry_file",
            lambda: registry_path,
        )

        mark_registry_idle(
            "session-1",
            "agent-001",
            result_status="COMPLETE",
            depth=3,
        )

        manifest = get_last_manifest("agent-001")

        assert manifest is not None
        assert manifest["result_status"] == "COMPLETE"
        assert manifest["depth"] == 3
        assert manifest["manifest_schema_version"] == 1

    def test_get_last_manifest_not_found(self, tmp_path, monkeypatch):
        """Assert get_last_manifest returns None for unknown agent."""
        registry_path = tmp_path / "dispatch_registry.json"
        registry_path.write_text("{}")

        monkeypatch.setattr(
            "context_estimator._dispatch_registry_file",
            lambda: registry_path,
        )

        manifest = get_last_manifest("unknown-agent")

        assert manifest is None

    def test_get_last_manifest_legacy_excluded(self, tmp_path, monkeypatch):
        """Assert legacy entries (no schema version) return None."""
        registry_path = tmp_path / "dispatch_registry.json"
        legacy_registry = {
            "sessions": {
                "session-1": {
                    "agents": {
                        "agent-001": {
                            "status": "idle",
                            "result_status": "COMPLETE",
                            # No manifest_schema_version
                        }
                    }
                }
            }
        }
        registry_path.write_text(json.dumps(legacy_registry))

        monkeypatch.setattr(
            "context_estimator._dispatch_registry_file",
            lambda: registry_path,
        )

        manifest = get_last_manifest("agent-001")

        assert manifest is None


class TestGetManifestStats:
    """Test get_manifest_stats returns aggregated stats."""

    def test_get_manifest_stats_counts(self, tmp_path, monkeypatch):
        """Assert get_manifest_stats returns correct totals and rates."""
        registry_path = tmp_path / "dispatch_registry.json"
        registry_path.write_text("{}")

        monkeypatch.setattr(
            "context_estimator._dispatch_registry_file",
            lambda: registry_path,
        )

        mark_registry_idle("session-1", "agent-001", result_status="COMPLETE")
        mark_registry_idle("session-1", "agent-002", result_status="UNKNOWN")
        mark_registry_idle("session-1", "agent-003", result_status="UNKNOWN")

        stats = get_manifest_stats()

        assert stats["total"] == 3
        assert stats["unknown"] == 2
        assert abs(stats["unknown_rate"] - (2/3 * 100)) < 0.01

    def test_get_manifest_stats_empty(self, tmp_path, monkeypatch):
        """Assert get_manifest_stats returns zeros on empty registry."""
        registry_path = tmp_path / "dispatch_registry.json"
        registry_path.write_text("{}")

        monkeypatch.setattr(
            "context_estimator._dispatch_registry_file",
            lambda: registry_path,
        )

        stats = get_manifest_stats()

        assert stats["total"] == 0
        assert stats["unknown"] == 0
        assert stats["unknown_rate"] == 0


class TestReconcileStuckBusy:
    """Test reconcile_stuck_busy marks old busy entries idle."""

    def test_reconcile_stuck_busy_age_threshold(self, tmp_path, monkeypatch):
        """Assert only entries older than min_age_hours are marked idle."""
        registry_path = tmp_path / "dispatch_registry.json"

        now = datetime.now(timezone.utc)
        old_ts = (now.timestamp() - 7 * 3600)  # 7 hours ago
        old_started = datetime.fromtimestamp(old_ts, tz=timezone.utc).isoformat(timespec="seconds")

        new_ts = (now.timestamp() - 1 * 3600)  # 1 hour ago
        new_started = datetime.fromtimestamp(new_ts, tz=timezone.utc).isoformat(timespec="seconds")

        registry = {
            "sessions": {
                "session-1": {
                    "agents": {
                        "agent-old": {
                            "status": "busy",
                            "started_at": old_started,
                        },
                        "agent-new": {
                            "status": "busy",
                            "started_at": new_started,
                        },
                    }
                }
            }
        }
        registry_path.write_text(json.dumps(registry))

        reconcile_stuck_busy(registry, now, exclude_ids=set(), min_age_hours=6)

        assert registry["sessions"]["session-1"]["agents"]["agent-old"]["status"] == "idle"
        assert registry["sessions"]["session-1"]["agents"]["agent-old"]["reconciled"] is True
        assert registry["sessions"]["session-1"]["agents"]["agent-new"]["status"] == "busy"

    def test_reconcile_stuck_busy_exclude_ids(self, tmp_path, monkeypatch):
        """Assert exclude_ids prevents reconciliation."""
        now = datetime.now(timezone.utc)
        old_ts = (now.timestamp() - 7 * 3600)
        old_started = datetime.fromtimestamp(old_ts, tz=timezone.utc).isoformat(timespec="seconds")

        registry = {
            "sessions": {
                "session-1": {
                    "agents": {
                        "agent-excluded": {
                            "status": "busy",
                            "started_at": old_started,
                        }
                    }
                }
            }
        }

        reconcile_stuck_busy(registry, now, exclude_ids={"agent-excluded"}, min_age_hours=6)

        assert registry["sessions"]["session-1"]["agents"]["agent-excluded"]["status"] == "busy"

    def test_reconcile_stuck_busy_sets_result_status(self, tmp_path, monkeypatch):
        """Assert reconciled entries get result_status=UNKNOWN."""
        now = datetime.now(timezone.utc)
        old_ts = (now.timestamp() - 7 * 3600)
        old_started = datetime.fromtimestamp(old_ts, tz=timezone.utc).isoformat(timespec="seconds")

        registry = {
            "sessions": {
                "session-1": {
                    "agents": {
                        "agent-001": {
                            "status": "busy",
                            "started_at": old_started,
                        }
                    }
                }
            }
        }

        reconcile_stuck_busy(registry, now, min_age_hours=6)

        entry = registry["sessions"]["session-1"]["agents"]["agent-001"]
        assert entry["status"] == "idle"
        assert entry["result_status"] == "UNKNOWN"
        assert entry["reconciled"] is True
        assert "warm_until" not in entry
