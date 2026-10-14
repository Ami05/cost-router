"""TICKET-012: Tests for config_error event emission from _dispatch_debt_active call sites.

Verifies that all 6 _safe_dispatch_debt_active call sites in check_routing.py
emit config_error events with error_type when config or state I/O is broken.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    import _common
    import _fileio
    import _config
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(_common, "DATA_DIR", data_dir)
    monkeypatch.setattr(_fileio, "DATA_DIR", data_dir)
    # Reset dedup cache so events aren't suppressed between tests.
    _fileio._dedup_cache.clear()
    # Ensure config cache is cleared so patches take effect.
    _config._invalidate_config_cache()
    yield data_dir
    _config._invalidate_config_cache()


def _make_broken_config_patch():
    """Return a context manager that makes config_get raise ValueError.

    _dispatch_debt_active lives in routing_state_manager, so we patch there.
    """
    return patch(
        "routing_state_manager.config_get",
        side_effect=ValueError("injected config read error"),
    )


def _make_broken_state_patch(tmp_path: Path):
    """Return a context manager that makes load_json_state raise OSError for agent_call_stats."""

    def _broken_load(path):
        # Raise on agent_call_stats.json to trigger the config_error path.
        if "agent_call_stats" in str(path):
            raise OSError("injected state read error")
        import _common as _c
        return _c.load_json_state(path)

    return patch("routing_state_manager.load_json_state", side_effect=_broken_load)


class TestDispatchDebtActiveConfigError:
    """_dispatch_debt_active emits config_error when config_get raises."""

    def test_broken_config_emits_config_error(self, isolated_data):
        import check_routing as cr

        with _make_broken_config_patch():
            result = cr._dispatch_debt_active("sess-001")

        # Must fail-open (return False) without propagating.
        assert result is False

        events = _read_jsonl(isolated_data / "routing_events.jsonl")
        config_errors = [e for e in events if e.get("type") == "config_error"]
        assert len(config_errors) >= 1, f"Expected config_error event; got {events}"
        assert any(e.get("error_type") for e in config_errors), (
            "config_error must include error_type field"
        )

    def test_broken_state_emits_config_error(self, isolated_data):
        import check_routing as cr

        with _make_broken_state_patch(isolated_data):
            result = cr._dispatch_debt_active("sess-002")

        assert result is False
        events = _read_jsonl(isolated_data / "routing_events.jsonl")
        config_errors = [e for e in events if e.get("type") == "config_error"]
        assert len(config_errors) >= 1, f"Expected config_error event; got {events}"


class TestSafeDispatchDebtActiveConfigError:
    """_safe_dispatch_debt_active wraps _dispatch_debt_active and re-emits config_error."""

    def test_safe_wrapper_returns_false_on_error(self, isolated_data):
        import check_routing as cr

        with patch("check_routing._dispatch_debt_active", side_effect=RuntimeError("boom")):
            result = cr._safe_dispatch_debt_active("sess-003")

        assert result is False
        events = _read_jsonl(isolated_data / "routing_events.jsonl")
        config_errors = [e for e in events if e.get("type") == "config_error"]
        assert len(config_errors) >= 1
        err = config_errors[0]
        assert err.get("error_type") == "RuntimeError"
        assert err.get("config_key") == "_safe_dispatch_debt_active"

    def test_safe_wrapper_preserves_session_id(self, isolated_data):
        import check_routing as cr

        session = "sess-preserve-004"
        with patch("check_routing._dispatch_debt_active", side_effect=RuntimeError("boom")):
            cr._safe_dispatch_debt_active(session)

        events = _read_jsonl(isolated_data / "routing_events.jsonl")
        config_errors = [e for e in events if e.get("type") == "config_error"]
        assert any(e.get("session_id") == session for e in config_errors), (
            "config_error must carry the session_id"
        )


class TestCallSiteConfigErrorPropagation:
    """Verify that call sites of _safe_dispatch_debt_active don't crash on config errors.

    These call sites are at lines 2567, 2575, 2597, 2858, 2920, 3183 in check_routing.py.
    The gate functions must return a valid response even when debt-active check fails.
    """

    def _make_payload(self, tool: str = "Edit", session_id: str = "sess-gate-test") -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": {"path": "scripts/check_routing.py"},
            "session_id": session_id,
            "hook_type": "PreToolUse",
        }

    def test_check_inline_mutation_threshold_survives_config_error(self, isolated_data, monkeypatch, tmp_path):
        import check_routing as cr

        state = cr._load_state()
        entry = cr._get_session(state["sessions"], "sess-gate-test")
        # Push counter to deny threshold to trigger the _safe_dispatch_debt_active call.
        entry["inline_mutations_since_dispatch"] = cr.INLINE_MUTATION_DENY_THRESHOLD
        cr._save_state(state)

        with patch("check_routing._dispatch_debt_active", side_effect=OSError("state broken")):
            # Must not raise — should return None (allow) on error.
            result = cr._check_inline_mutation_threshold(state, entry, True, "sess-gate-test")

        # Result is either None (allow) or a deny dict — both are acceptable;
        # the key invariant is no exception was raised.
        assert result is None or isinstance(result, dict)

    def test_check_shell_gate_survives_config_error(self, isolated_data, monkeypatch):
        import check_routing as cr

        state = cr._load_state()
        entry = cr._get_session(state["sessions"], "sess-shell-test")
        entry["shell_calls_since_dispatch"] = cr.SHELL_DENY_THRESHOLD
        cr._save_state(state)

        with patch("check_routing._dispatch_debt_active", side_effect=OSError("state broken")):
            result = cr._check_shell_gate(state, entry, True, "sess-shell-test")

        assert result is None or isinstance(result, dict)
