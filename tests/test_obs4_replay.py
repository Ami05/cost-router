"""Replay unboundedOrchestrator obs-4 (7 dispatches) through the REAL charge path.

Rows come from tests/fixtures/obs4_dispatch_table.json (measured data). Models
are documented assumptions (see fixture _provenance). Costs are never
hardcoded: nominal comes from _model_utils.est_cold_write_cost_usd, and the
cumulative is read back from what spawn_tracker.finalize_dispatch_charge wrote.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _config  # noqa: E402
import _fileio  # noqa: E402
import check_routing as cr  # noqa: E402
import routing_resolver as rr  # noqa: E402
import routing_state_manager as rsm  # noqa: E402
import session_state as ss  # noqa: E402
import spawn_tracker as st  # noqa: E402
from _model_utils import est_cold_write_cost_usd  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "obs4_dispatch_table.json").read_text("utf-8"))
ROWS = FIXTURE["rows"]
SCENARIOS = FIXTURE["scenarios"]


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    for mod in (cr, ss):
        monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(mod, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
        monkeypatch.setattr(mod, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")
    monkeypatch.setattr(cr, "RESUME_STATE_FILE", tmp_path / "resume_state.json")
    monkeypatch.setattr(cr, "DATA_DIR", tmp_path)
    for mod in (rsm, rr):
        monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_fileio, "DATA_DIR", tmp_path)  # events
    monkeypatch.setattr(st, "STATS_FILE", tmp_path / "agent_call_stats.json")
    monkeypatch.setattr(st, "DISPATCH_REGISTRY_FILE", tmp_path / "dispatch_registry.json")
    monkeypatch.delenv("COST_ROUTER_SUBAGENT_INFRA_TOKENS", raising=False)
    return tmp_path


def _register(sid: str, aid: str, model: str, i: int) -> None:
    reg_file = st.DISPATCH_REGISTRY_FILE
    try:
        reg = json.loads(reg_file.read_text("utf-8"))
    except (OSError, ValueError):
        reg = {}
    reg.setdefault("sessions", {}).setdefault(sid, {}).setdefault("agents", {})[aid] = {
        "agent_id": aid,
        "subagent_id": aid,
        "model": model,
        "started_at": f"2026-09-28T00:00:{i:02d}+00:00",
    }
    reg_file.write_text(json.dumps(reg), encoding="utf-8")


def _cumulative(sid: str) -> float:
    return float(json.loads(st.STATS_FILE.read_text("utf-8"))["sessions"][sid]["cumulative_cost_usd"])


def _replay(scenario: str):
    sid = f"obs4-{scenario}"
    models = SCENARIOS[scenario]
    assert len(models) == len(ROWS) == 7
    cumulatives: list[float] = []
    active: list[bool] = []
    for i, (row, model) in enumerate(zip(ROWS, models)):
        aid = f"obs4-{scenario}-{i}"
        _register(sid, aid, model, i)
        st.finalize_dispatch_charge({"session_id": sid, "agent_id": aid}, True)
        cumulatives.append(_cumulative(sid))
        active.append(cr._session_dispatch_cap_active(sid))
    first_active = next((i for i, a in enumerate(active) if a), None)
    print(f"\n[{scenario}] per-dispatch cumulative: {cumulatives}; first-active index: {first_active}")
    return sid, models, cumulatives, active


def test_default_config_values():
    assert float(_config.config_get("concurrency.session_budget_pct")) == 0.6
    assert float(_config.config_get("session_cost_budget.budget_usd")) == 5.0


def test_all_sonnet_scenario():
    _, _, cum, active = _replay("all_sonnet")
    expected = 7 * est_cold_write_cost_usd("sonnet")
    assert cum[-1] == pytest.approx(expected, abs=1e-6)
    assert not any(active)


def test_mixed_scenario():
    _, _, cum, active = _replay("mixed")
    expected = 3 * est_cold_write_cost_usd("haiku") + 4 * est_cold_write_cost_usd("sonnet")
    assert cum[-1] == pytest.approx(expected, abs=1e-6)
    assert not any(active)


def test_same_agent_id_charged_once():
    sid = "obs4-dup"
    _register(sid, "dup-1", "sonnet", 0)
    payload = {"session_id": sid, "agent_id": "dup-1"}
    st.finalize_dispatch_charge(payload, True)
    first = _cumulative(sid)
    st.finalize_dispatch_charge(payload, True)
    assert _cumulative(sid) == first
    assert first == pytest.approx(est_cold_write_cost_usd("sonnet"), abs=1e-6)
