"""RC-7: live main-session context gate (_read_live_context_tokens + _context_pressure_check)."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import _config  # noqa: E402
import routing_resolver as rr  # noqa: E402
import session_state as ss  # noqa: E402
from transcript_scanner import _read_live_context_tokens, _read_session_output_tokens  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "context_pressure_transcript.jsonl"
# Last main-session assistant record: input 2 + cache_creation 857 + cache_read 138547.
PINNED_TOKENS = 139_406
PINNED_MODEL = "claude-sonnet-5-5"
WINDOW = 1_000_000
RATIO = PINNED_TOKENS / WINDOW


def _records() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def _write(tmp_path, records, name="t.jsonl") -> str:
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(p)


def _assistant(msg_id: str, inp: int, model: str = PINNED_MODEL, **extra) -> dict:
    rec = {
        "type": "assistant", "isSidechain": False,
        "message": {"id": msg_id, "role": "assistant", "model": model, "content": [],
                    "usage": {"input_tokens": inp, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0, "output_tokens": 9}},
    }
    rec.update(extra)
    return rec


@pytest.fixture
def set_cfg(monkeypatch, tmp_path):
    base = copy.deepcopy(_config.load_plugin_config())
    monkeypatch.setattr(ss, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
    monkeypatch.setattr(ss, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
    monkeypatch.setattr(ss, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")

    def _set(**cp):
        cfg = copy.deepcopy(base)
        cfg["context_pressure"] = cp
        monkeypatch.setattr(_config, "_config_cache", cfg)
    yield _set
    _config._invalidate_config_cache()


class TestReadLiveContextTokens:
    def test_known_answer(self):
        assert _read_live_context_tokens(str(FIXTURE)) == {"tokens": PINNED_TOKENS, "model": PINNED_MODEL}

    def test_invariant_to_sidechain_agentid_and_duplicates(self, tmp_path):
        recs = _records()
        recs.insert(3, _assistant("sc", 900_000, isSidechain=True))
        recs.append(_assistant("ag", 800_000, agentId="abc"))
        recs.append(copy.deepcopy(recs[-3]))  # duplicate of an earlier id
        # Duplicate of the final main record's id must not change the answer.
        last_main = [r for r in _records() if r["type"] == "assistant"][-1]
        recs.append(copy.deepcopy(last_main))
        assert _read_live_context_tokens(_write(tmp_path, recs))["tokens"] == PINNED_TOKENS

    def test_post_compact_drop(self, tmp_path):
        recs = _records() + [_assistant("after_compact", 12_345)]
        assert _read_live_context_tokens(_write(tmp_path, recs)) == {"tokens": 12_345, "model": PINNED_MODEL}

    def test_missing_empty_malformed_none(self, tmp_path):
        assert _read_live_context_tokens(str(tmp_path / "nope.jsonl")) is None
        assert _read_live_context_tokens("") is None
        empty = tmp_path / "e.jsonl"
        empty.write_text("")
        assert _read_live_context_tokens(str(empty)) is None
        bad = tmp_path / "b.jsonl"
        bad.write_text("not json\n{broken\n")
        assert _read_live_context_tokens(str(bad)) is None
        only_side = _write(tmp_path, [_assistant("s", 5, isSidechain=True)], "s.jsonl")
        assert _read_live_context_tokens(only_side) is None


class TestContextPressureCheck:
    def test_unknown_model_fails_open_and_bumps(self, tmp_path, set_cfg):
        set_cfg(mode="enforce", threshold=0.0001)
        path = _write(tmp_path, [_assistant("x", 500_000, model="mystery-model-9")])
        res = rr._context_pressure_check("sid", path)
        assert res["should_deny"] is False and res["would_deny"] is False
        assert ss._load_state()["counters"].get("context_pressure_default_window", 0) >= 1

    def test_warn_never_denies_but_flags(self, set_cfg):
        set_cfg(mode="warn", threshold=0.10)
        res = rr._context_pressure_check("sid", str(FIXTURE))
        assert res["should_deny"] is False and res["would_deny"] is True
        assert "RC-7" in res["message"]

    def test_invalid_or_missing_mode_is_warn(self, set_cfg):
        set_cfg(mode="bogus", threshold=0.10)
        res = rr._context_pressure_check("sid", str(FIXTURE))
        assert res["should_deny"] is False and res["would_deny"] is True
        set_cfg(threshold=0.10)
        res = rr._context_pressure_check("sid", str(FIXTURE))
        assert res["should_deny"] is False and res["would_deny"] is True

    def test_enforce_denies_at_and_above_threshold(self, set_cfg):
        set_cfg(mode="enforce", threshold=RATIO)
        res = rr._context_pressure_check("sid", str(FIXTURE))
        assert res["should_deny"] is True
        assert res["message"].startswith("RC-7: Orchestrator context at 14% of the model window")
        set_cfg(mode="enforce", threshold=0.10)
        assert rr._context_pressure_check("sid", str(FIXTURE))["should_deny"] is True

    def test_enforce_allows_below_threshold(self, set_cfg):
        set_cfg(mode="enforce", threshold=0.15)
        res = rr._context_pressure_check("sid", str(FIXTURE))
        assert res["should_deny"] is False and res["would_deny"] is False

    def test_mode_off_is_noop(self, set_cfg):
        set_cfg(mode="off", threshold=0.0001)
        res = rr._context_pressure_check("sid", str(FIXTURE))
        assert res == {"should_deny": False, "would_deny": False}

    def test_missing_transcript_fails_open(self, set_cfg):
        set_cfg(mode="enforce", threshold=0.0001)
        assert rr._context_pressure_check("sid", None)["should_deny"] is False


class TestReadSessionOutputTokens:
    def test_sums_output_tokens_from_fixture(self):
        # Fixture has 8 assistant records with output_tokens:
        # 688 + 456 + 67 + 2631 + 2631 + 2631 + 446 + 520 = 10070
        expected = 688 + 456 + 67 + 2631 * 3 + 446 + 520
        assert _read_session_output_tokens(str(FIXTURE)) == expected

    def test_missing_path_returns_zero(self):
        assert _read_session_output_tokens(str(Path("/nonexistent/path.jsonl"))) == 0

    def test_empty_path_returns_zero(self):
        assert _read_session_output_tokens("") == 0

    def test_malformed_lines_skipped(self, tmp_path):
        bad = tmp_path / "malformed.jsonl"
        # Mix valid and malformed lines; malformed should be skipped
        bad.write_text(
            '{"type": "assistant", "message": {"usage": {"output_tokens": 100}}}\n'
            'not json\n'
            '{"type": "assistant", "message": {"usage": {"output_tokens": 200}}}\n'
            '{broken json\n'
            '{"type": "assistant", "message": {"usage": {"output_tokens": 50}}}\n'
        )
        # Should sum only the three valid records: 100 + 200 + 50 = 350
        assert _read_session_output_tokens(str(bad)) == 350
