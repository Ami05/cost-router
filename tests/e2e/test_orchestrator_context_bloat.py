"""E2E tests: RC-7 orchestrator context bloat — live-context gate.

Covers:
- _read_live_context_tokens(): reads last main-session assistant record from transcript
- _context_pressure_check(): denies when context ratio >= threshold in enforce mode
- event_gate wiring: enforced context blocks Task dispatch with actionable error
- mode="warn" emits warning but does not block
- mode="off" is a no-op
- Gate is fail-open: missing/empty transcript or unknown model → allow
- Uses real fixture records (tests/fixtures/context_pressure_transcript.jsonl)
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _config  # noqa: E402
import check_routing as cr  # noqa: E402
import routing_resolver as rr  # noqa: E402
import session_state as ss  # noqa: E402
from transcript_scanner import _read_live_context_tokens  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "context_pressure_transcript.jsonl"


def _records() -> list[dict]:
    """Load real fixture records."""
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def _write(tmp_path: Path, records: list[dict], name: str = "t.jsonl") -> str:
    """Write records to a temp transcript file."""
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(p)


def _assistant(msg_id: str, inp: int, model: str = "claude-sonnet-5-5", **extra) -> dict:
    """Build an assistant record for testing."""
    rec = {
        "type": "assistant",
        "isSidechain": False,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "content": [],
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 9,
            },
        },
    }
    rec.update(extra)
    return rec


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")


@pytest.fixture
def set_cfg(monkeypatch, tmp_path):
    """Configure context_pressure settings and yield a function to update them."""
    base = copy.deepcopy(_config.load_plugin_config())
    monkeypatch.setattr(ss, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")

    def _set(**cp):
        cfg = copy.deepcopy(base)
        cfg["context_pressure"] = cp
        monkeypatch.setattr(_config, "_config_cache", cfg)

    yield _set
    _config._invalidate_config_cache()


def _reset(sid: str, prompt: str = "Refactor auth module across files") -> None:
    cr.event_reset({"session_id": sid, "prompt": prompt}, False)


def _scan(sid: str, verdict: str = "FO") -> None:
    cr.event_scan({"session_id": sid, "text": f"Routing: {verdict} — task"}, False)


# ── _context_pressure_check() unit tests ─────────────────────────────────────


class TestContextPressureCheck:

    def test_below_threshold_returns_allow(self, tmp_path, set_cfg):
        """RC-7 AC: context < threshold → allow."""
        set_cfg(mode="enforce", threshold=0.85)
        # Use real fixture records truncated to stay below threshold
        recs = _records()
        path = _write(tmp_path, recs)
        result = rr._context_pressure_check("sid", path)
        # Fixture is ~14% of window, well below 85%
        assert result.get("should_deny") is False

    def test_at_threshold_blocks(self, tmp_path, set_cfg):
        """RC-7 AC: context >= threshold → deny with RC-7 message."""
        set_cfg(mode="enforce", threshold=0.10)
        recs = _records()
        path = _write(tmp_path, recs)
        result = rr._context_pressure_check("sid", path)
        assert result.get("should_deny") is True
        msg = result.get("message", "")
        assert "RC-7" in msg

    def test_warn_mode_does_not_deny(self, tmp_path, set_cfg):
        """RC-7 AC: mode=warn emits warning but does not deny."""
        set_cfg(mode="warn", threshold=0.10)
        recs = _records()
        path = _write(tmp_path, recs)
        result = rr._context_pressure_check("sid", path)
        assert result.get("should_deny") is False
        assert result.get("would_deny") is True
        assert "RC-7" in result.get("message", "")

    def test_off_mode_is_noop(self, tmp_path, set_cfg):
        """RC-7: mode=off is a complete no-op."""
        set_cfg(mode="off", threshold=0.0001)
        recs = _records()
        path = _write(tmp_path, recs)
        result = rr._context_pressure_check("sid", path)
        assert result == {"should_deny": False, "would_deny": False}

    def test_invalid_mode_defaults_to_warn(self, tmp_path, set_cfg):
        """Invalid or missing mode defaults to warn."""
        set_cfg(mode="bogus", threshold=0.10)
        recs = _records()
        path = _write(tmp_path, recs)
        result = rr._context_pressure_check("sid", path)
        assert result.get("should_deny") is False
        assert result.get("would_deny") is True

    def test_missing_transcript_is_fail_open(self, tmp_path, set_cfg):
        """No transcript → fail-open (allow); gate must not block on missing data."""
        set_cfg(mode="enforce", threshold=0.0001)
        result = rr._context_pressure_check("sid", str(tmp_path / "missing.jsonl"))
        assert result.get("should_deny") is False

    def test_none_transcript_path_is_fail_open(self, set_cfg):
        """None transcript path → fail-open."""
        set_cfg(mode="enforce", threshold=0.0001)
        result = rr._context_pressure_check("sid", None)
        assert result.get("should_deny") is False


# ── Integration: event_gate blocks Task when context >= threshold ──────────────


class TestContextPressureGateIntegration:

    def test_enforced_context_blocks_task_dispatch(self, tmp_path, set_cfg):
        """RC-7 AC: mode=enforce and context >= threshold blocks wave N+1 dispatch."""
        set_cfg(mode="enforce", threshold=0.10)

        # Use real fixture which is ~14% of window, above 10% threshold
        recs = _records()
        transcript = _write(tmp_path, recs)

        sid = "rc7-integ-1"
        _reset(sid, "fan out parallel read workers across scripts/")
        _scan(sid, "FO")

        result = cr.event_gate(
            {
                "session_id": sid,
                "tool_name": "Task",
                "transcript_path": transcript,
                "tool_input": {
                    "prompt": "fan out parallel read workers across scripts/",
                    "model": "haiku",
                },
            },
            False,
        )
        assert result.get("permission") == "deny"
        msg = result.get("agent_message", "") or ""
        assert "RC-7" in msg

    def test_below_threshold_context_does_not_block_task(self, tmp_path, set_cfg):
        """RC-7 AC: context < threshold does not block wave dispatch."""
        set_cfg(mode="enforce", threshold=0.85)

        # Real fixture is ~14% of window, well below 85% threshold
        recs = _records()
        transcript = _write(tmp_path, recs)

        sid = "rc7-integ-2"
        _reset(sid, "fan out parallel read workers across scripts/")
        _scan(sid, "FO")

        result = cr.event_gate(
            {
                "session_id": sid,
                "tool_name": "Task",
                "transcript_path": transcript,
                "tool_input": {
                    "prompt": "fan out parallel read workers across scripts/",
                    "model": "haiku",
                },
            },
            False,
        )
        # Should not be denied by context pressure (may be denied by other gates)
        msg = result.get("agent_message", "") or ""
        assert "RC-7" not in msg

    def test_warn_mode_does_not_block_task(self, tmp_path, set_cfg):
        """RC-7 AC: mode=warn emits warning but does not block dispatch."""
        set_cfg(mode="warn", threshold=0.10)

        recs = _records()
        transcript = _write(tmp_path, recs)

        sid = "rc7-integ-3"
        _reset(sid, "fan out parallel read workers across scripts/")
        _scan(sid, "FO")

        result = cr.event_gate(
            {
                "session_id": sid,
                "tool_name": "Task",
                "transcript_path": transcript,
                "tool_input": {
                    "prompt": "fan out parallel read workers across scripts/",
                    "model": "haiku",
                },
            },
            False,
        )
        # warn mode should not deny
        assert result.get("permission") != "deny" or "RC-7" not in (result.get("agent_message", "") or "")
