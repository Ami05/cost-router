"""E2E tests for context_warn.py — main-session context-size warning hook."""
from __future__ import annotations

import copy
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import context_warn as cw  # noqa: E402
from _fileio import load_json_state, save_json_state  # noqa: E402


def _records() -> list[dict]:
    """Base assistant records for transcript construction."""
    return [
        {
            "type": "assistant",
            "isSidechain": False,
            "message": {
                "id": f"msg_{i}",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [],
                "usage": {
                    "input_tokens": 100_000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 9,
                },
            },
        }
        for i in range(2)
    ]


def _write(tmp_path, records, name="t.jsonl") -> str:
    """Write records to a JSONL file and return its path as string."""
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(p)


def _assistant(msg_id: str, inp: int, model: str = "claude-sonnet-5-5", **extra) -> dict:
    """Create an assistant record with specified input tokens."""
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


@pytest.fixture
def state_isolation(monkeypatch, tmp_path):
    """Isolate context_warn state file to tmp_path."""
    state_file = tmp_path / "context_warn_state.json"
    monkeypatch.setattr(cw, "_STATE_FILE", state_file)
    yield tmp_path
    # Clean up after test
    if state_file.exists():
        state_file.unlink()


def _payload(session_id="s1", transcript_path="", hook_event_name="PostToolUse", **extra) -> dict:
    """Create a hook payload."""
    p = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "hook_event_name": hook_event_name,
    }
    p.update(extra)
    return p


class TestContextWarnCheck:
    """Tests for context_warn.check() function."""

    def test_100k_tokens_returns_none(self, tmp_path, state_isolation, monkeypatch):
        """Case 1: 100k tokens -> check returns None."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "warn")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,
        }.get(k, kw.get("default", 0)))

        records = [_assistant("msg", 100_000)]
        transcript_path = _write(tmp_path, records)
        payload = _payload(transcript_path=transcript_path)

        result = cw.check(payload)
        assert result is None

    def test_165k_tokens_warns(self, tmp_path, state_isolation, monkeypatch):
        """Case 2: 165k tokens -> returns text containing CONTEXT WARNING."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "warn")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,
        }.get(k, kw.get("default", 0)))

        records = [_assistant("msg", 165_000)]
        transcript_path = _write(tmp_path, records)
        payload = _payload(transcript_path=transcript_path)

        result = cw.check(payload)
        assert result is not None
        assert "CONTEXT WARNING" in result
        assert "165" in result

    def test_165k_tokens_twice_returns_none_second_time(self, tmp_path, state_isolation, monkeypatch):
        """Case 3: same 165k again -> None (already at that level)."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "warn")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,
        }.get(k, kw.get("default", 0)))

        records = [_assistant("msg", 165_000)]
        transcript_path = _write(tmp_path, records)
        payload = _payload(transcript_path=transcript_path)

        # First call: should warn
        result1 = cw.check(payload)
        assert result1 is not None and "CONTEXT WARNING" in result1

        # Second call with same level: should NOT warn
        result2 = cw.check(payload)
        assert result2 is None

    def test_185k_after_warn_returns_urgent(self, tmp_path, state_isolation, monkeypatch):
        """Case 4: 185k after warn -> text containing CONTEXT URGENT."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "warn")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,
        }.get(k, kw.get("default", 0)))

        # First: warn at 165k
        records = [_assistant("msg", 165_000)]
        transcript_path = _write(tmp_path, records)
        payload = _payload(transcript_path=transcript_path)
        result1 = cw.check(payload)
        assert "CONTEXT WARNING" in result1

        # Second: escalate to urgent at 185k
        records2 = [_assistant("msg2", 185_000)]
        transcript_path2 = _write(tmp_path, records2, name="t2.jsonl")
        payload2 = _payload(transcript_path=transcript_path2)
        result2 = cw.check(payload2)
        assert result2 is not None
        assert "CONTEXT URGENT" in result2
        assert "185" in result2

    def test_rewrite_to_100k_re_arms_warning(self, tmp_path, state_isolation, monkeypatch):
        """Case 5: rewrite to 100k re-arms warning; then 165k warns again."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "warn")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,
        }.get(k, kw.get("default", 0)))

        # Warn at 165k
        records = [_assistant("msg", 165_000)]
        transcript_path = _write(tmp_path, records)
        payload = _payload(transcript_path=transcript_path)
        result1 = cw.check(payload)
        assert "CONTEXT WARNING" in result1

        # Rewrite to 100k (below threshold)
        records_low = [_assistant("msg_low", 100_000)]
        transcript_path_low = _write(tmp_path, records_low, name="t_low.jsonl")
        payload_low = _payload(transcript_path=transcript_path_low)
        result2 = cw.check(payload_low)
        assert result2 is None

        # Verify state was reset to 0
        state = load_json_state(cw._STATE_FILE)
        assert int(state.get("s1", 0) or 0) == 0

        # Rewrite to 165k again - should warn again (re-armed)
        records_warn = [_assistant("msg_warn", 165_000)]
        transcript_path_warn = _write(tmp_path, records_warn, name="t_warn.jsonl")
        payload_warn = _payload(transcript_path=transcript_path_warn)
        result3 = cw.check(payload_warn)
        assert result3 is not None
        assert "CONTEXT WARNING" in result3

    def test_subagent_payload_with_agent_id_returns_none(self, tmp_path, state_isolation, monkeypatch):
        """Case 6: payload with agent_id set at 185k -> None (subagent detection)."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "warn")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,
        }.get(k, kw.get("default", 0)))

        records = [_assistant("msg", 185_000)]
        transcript_path = _write(tmp_path, records)
        payload = _payload(transcript_path=transcript_path, agent_id="subagent-123")

        result = cw.check(payload)
        assert result is None  # Subagents are never warned

    def test_tail_read_with_large_transcript(self, tmp_path, state_isolation, monkeypatch):
        """Case 7: tail read with 1MB prefix + 170k final record -> CONTEXT WARNING."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "warn")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,  # 512KB tail
        }.get(k, kw.get("default", 0)))

        # Build a large transcript: many 50k records followed by one 170k
        large_records = [_assistant(f"old_{i}", 50_000) for i in range(20)]
        large_records.append(_assistant("final", 170_000))
        transcript_path = _write(tmp_path, large_records)

        payload = _payload(transcript_path=transcript_path)
        result = cw.check(payload)
        assert result is not None
        assert "CONTEXT WARNING" in result

    def test_hook_mode_off_returns_none(self, tmp_path, state_isolation, monkeypatch):
        """Hook mode 'off' should not warn."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "off")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,
        }.get(k, kw.get("default", 0)))

        records = [_assistant("msg", 185_000)]
        transcript_path = _write(tmp_path, records)
        payload = _payload(transcript_path=transcript_path)

        result = cw.check(payload)
        assert result is None

    def test_missing_transcript_returns_none(self, tmp_path, state_isolation, monkeypatch):
        """Missing transcript should return None (fail open)."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "warn")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,
        }.get(k, kw.get("default", 0)))

        payload = _payload(transcript_path="/nonexistent/path.jsonl")
        result = cw.check(payload)
        assert result is None


class TestContextWarnMain:
    """Tests for context_warn.main() hook entry point."""

    def test_main_with_165k_claude_format(self, tmp_path, state_isolation, monkeypatch):
        """Case 8a: main() with 165k Claude Code hook format."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "warn")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,
        }.get(k, kw.get("default", 0)))

        records = [_assistant("msg", 165_000)]
        transcript_path = _write(tmp_path, records)
        payload = _payload(transcript_path=transcript_path)

        # Monkeypatch stdin and stdout
        stdin_data = io.StringIO(json.dumps(payload))
        stdout_capture = io.StringIO()

        monkeypatch.setattr("sys.stdin", stdin_data)
        monkeypatch.setattr("sys.stdout", stdout_capture)

        cw.main()

        output = stdout_capture.getvalue()
        response = json.loads(output)

        assert "hookSpecificOutput" in response
        assert response["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert "CONTEXT WARNING" in response["hookSpecificOutput"]["additionalContext"]

    def test_main_with_100k_returns_empty_json(self, tmp_path, state_isolation, monkeypatch):
        """Case 8b: main() with 100k returns empty JSON."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "warn")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,
        }.get(k, kw.get("default", 0)))

        records = [_assistant("msg", 100_000)]
        transcript_path = _write(tmp_path, records)
        payload = _payload(transcript_path=transcript_path)

        # Monkeypatch stdin and stdout
        stdin_data = io.StringIO(json.dumps(payload))
        stdout_capture = io.StringIO()

        monkeypatch.setattr("sys.stdin", stdin_data)
        monkeypatch.setattr("sys.stdout", stdout_capture)

        cw.main()

        output = stdout_capture.getvalue().strip()
        assert output == "{}"

    def test_main_with_invalid_json_fails_open(self, tmp_path, state_isolation, monkeypatch):
        """main() with invalid JSON should fail open and return empty JSON."""
        stdin_data = io.StringIO("not valid json")
        stdout_capture = io.StringIO()

        monkeypatch.setattr("sys.stdin", stdin_data)
        monkeypatch.setattr("sys.stdout", stdout_capture)

        cw.main()

        output = stdout_capture.getvalue().strip()
        assert output == "{}"

    def test_main_cursor_format(self, tmp_path, state_isolation, monkeypatch):
        """main() should handle Cursor hook format (with cursor_version)."""
        monkeypatch.setattr("context_warn.config_get", lambda *a, **k: "warn")
        monkeypatch.setattr("context_warn.config_int", lambda k, **kw: {
            "context_pressure.hook_warn_tokens": 160000,
            "context_pressure.hook_urgent_tokens": 180000,
            "context_pressure.hook_tail_bytes": 524288,
        }.get(k, kw.get("default", 0)))

        records = [_assistant("msg", 165_000)]
        transcript_path = _write(tmp_path, records)
        payload = _payload(transcript_path=transcript_path)
        payload["cursor_version"] = "1.0"  # Mark as Cursor, not Claude Code

        stdin_data = io.StringIO(json.dumps(payload))
        stdout_capture = io.StringIO()

        monkeypatch.setattr("sys.stdin", stdin_data)
        monkeypatch.setattr("sys.stdout", stdout_capture)

        cw.main()

        output = stdout_capture.getvalue()
        response = json.loads(output)

        assert "continue" in response
        assert response["continue"] is True
        assert "additional_context" in response
        assert "CONTEXT WARNING" in response["additional_context"]
