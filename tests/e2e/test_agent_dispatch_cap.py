"""Journey: the session dispatch budget cap applies to tool_name "Agent".

Claude Code dispatches subagents through the Agent tool (older payloads say
Task). tests/e2e/test_session_budget_cap.py drives the cap through
check_routing.event_gate with tool_name "Task"; this file drives the real
gates.run_gate("pre_task", ...) chain with tool_name "Agent", the path a live
Claude Code dispatch takes.

Cumulative dispatch cost is seeded through the real SubagentStop charge path
(spawn_tracker.finalize_dispatch_charge, via tests/e2e/_accounting_fixture.charge_real)
replaying real dispatched registry entries from
tests/fixtures/data/accounting_session_excerpts.json. No synthetic cost numbers
and no ANTHROPIC_API_KEY. Only file locations are redirected to tmp_path and,
for the override tests, the config cache is a copy of the real config with
concurrency.session_budget_override_limit changed.

Cases:
  1. Agent dispatch below the cap is allowed; no cap deny is counted.
  2. Agent dispatch past the cap is denied and gate_deny_session_budget_cap increments.
  3. session-budget-override (limit > 0) waives only the cap for an Agent dispatch;
     dispatch-budget-override does not.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _common  # noqa: E402
import _config  # noqa: E402
import _fileio  # noqa: E402
import check_routing as cr  # noqa: E402
import gates  # noqa: E402
import routing_resolver as rr  # noqa: E402
import routing_state_manager as rsm  # noqa: E402
import session_state as ss  # noqa: E402
import spawn_tracker as st  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _accounting_fixture as acct  # noqa: E402


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
    # Real SubagentStop charge path writes here.
    monkeypatch.setattr(st, "STATS_FILE", tmp_path / "agent_call_stats.json")
    monkeypatch.setattr(st, "DISPATCH_REGISTRY_FILE", tmp_path / "dispatch_registry.json")
    # The full pre_task chain also logs and tracks; keep all of it under tmp_path.
    monkeypatch.setattr(gates, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_common, "SESSION_LOG", tmp_path / "session_log.jsonl")
    monkeypatch.setattr(_fileio, "SESSION_LOG", tmp_path / "session_log.jsonl")
    monkeypatch.delenv("COST_ROUTER_SUBAGENT_INFRA_TOKENS", raising=False)
    return tmp_path


def _set_override_limit(monkeypatch, limit: int) -> None:
    cfg = copy.deepcopy(_config.load_plugin_config())
    cfg.setdefault("concurrency", {})["session_budget_override_limit"] = limit
    monkeypatch.setattr(_config, "_config_cache", cfg)


def _cap_usd() -> float:
    budget = float(_config.config_get("session_cost_budget.budget_usd") or 5.0)
    pct = float(_config.config_get("concurrency.session_budget_pct") or 0.6)
    return budget * pct


def _charges_below_cap() -> int:
    n = acct.charges_below(_cap_usd())
    assert n > 0, "cap must admit at least one real charge for these tests"
    return n


def _agent_dispatch(sid: str, turn: str, scope: str, token: str = "") -> dict:
    """Routing verdict, then a PreToolUse Agent payload through the pre_task chain."""
    cr.event_scan(
        {"session_id": sid, "text": f"Routing: FO — dispatching worker {token}".strip(), "prompt_id": turn},
        False,
    )
    prompt = json.dumps({
        "worker": "read",
        "goal": f"find gate registration in {scope} {token}".strip(),
        "scope": [scope],
        "return": ["function names"],
    })
    return gates.run_gate("pre_task", {
        "session_id": sid,
        "tool_name": "Agent",
        "tool_input": {"model": "sonnet", "subagent_type": "general-purpose", "prompt": prompt},
        "prompt_id": turn,
    }, False)


def _counters() -> dict:
    return cr._load_state().get("counters", {})


def _entry(sid: str) -> dict:
    return cr._get_session(cr._load_state()["sessions"], sid)


def test_agent_dispatch_below_cap_is_allowed():
    sid = "adc-below"
    cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)
    acct.charge_real(st, sid, _charges_below_cap())
    assert rsm._cumulative_dispatch_cost_usd(sid) < _cap_usd()

    out = _agent_dispatch(sid, "b-1", "scripts/gates.py")
    assert out.get("permission") == "allow", f"Agent dispatch below the cap must be allowed; got: {out}"
    assert int(_counters().get("gate_deny_session_budget_cap", 0)) == 0


def test_agent_dispatch_past_cap_is_denied_and_counted():
    sid = "adc-past"
    cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)
    acct.charge_real(st, sid, _charges_below_cap() + 1)
    assert rsm._cumulative_dispatch_cost_usd(sid) >= _cap_usd()

    out = _agent_dispatch(sid, "p-1", "scripts/gates.py")
    assert out.get("permission") == "deny", f"Agent dispatch past the cap must be denied; got: {out}"
    assert "Session dispatch budget cap" in out.get("agent_message", "")
    assert int(_counters().get("gate_deny_session_budget_cap", 0)) == 1

    out = _agent_dispatch(sid, "p-2", "scripts/check_routing.py")
    assert out.get("permission") == "deny"
    assert int(_counters().get("gate_deny_session_budget_cap", 0)) == 2


def test_session_budget_override_waives_only_the_cap_for_agent(monkeypatch):
    _set_override_limit(monkeypatch, 1)
    sid = "adc-override"
    cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)
    acct.charge_real(st, sid, _charges_below_cap() + 1)

    # dispatch-budget-override is a different waiver: the cap still denies.
    wrong = _agent_dispatch(sid, "o-1", "scripts/gates.py", token="dispatch-budget-override")
    assert wrong.get("permission") == "deny"
    assert int(_counters().get("gate_deny_session_budget_cap", 0)) == 1
    assert int(_counters().get("gate_allow_session_budget_override", 0)) == 0
    assert int(_entry(sid).get("session_budget_override_count", 0)) == 0

    # session-budget-override grants exactly one use at limit 1.
    granted = _agent_dispatch(sid, "o-2", "scripts/check_routing.py", token="session-budget-override")
    assert granted.get("permission") == "allow", f"override must waive the cap for Agent; got: {granted}"
    assert int(_entry(sid)["session_budget_override_count"]) == 1
    assert int(_counters().get("gate_allow_session_budget_override", 0)) == 1

    # The limit is spent: the next overridden Agent dispatch is denied again.
    spent = _agent_dispatch(sid, "o-3", "scripts/routing_resolver.py", token="session-budget-override")
    assert spent.get("permission") == "deny"
    assert int(_entry(sid)["session_budget_override_count"]) == 1
    assert int(_counters().get("gate_deny_session_budget_cap", 0)) == 2
