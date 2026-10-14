"""Journey: shell total-cap and same-type denials are skipped when dispatch debt is active (TEST-006).

Shell batch-cap behavior (check_routing.py _check_shell_gate):
- Total inline cap: shell_calls_total_inline >= shell_batch_cap.total_inline_cap (default 4) denies
- Same-type cap: shell_same_type_counts[cmd_type] >= 2 denies

Both thresholds are skipped (fall through to allow and bump fallback counters)
when _safe_dispatch_debt_active(session_id) is True.

Three tests cover:
1. Without debt: 4+ distinct-type inline Bash calls trigger gate_deny_shell_total_cap
2. With debt: same calls are allowed and gate_shell_total_cap_allow_rt_exhausted is bumped
3. Same-type calls: 2+ identical-type calls deny without debt, allow with debt
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_routing as cr  # noqa: E402
import session_state as ss  # noqa: E402
import routing_state_manager as rsm  # noqa: E402
import routing_resolver as rr  # noqa: E402
import spawn_tracker as st  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _accounting_fixture as acct  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Isolated routing state (same pattern as test_journey_rt_exhaustion.py)."""
    for mod in (cr, ss):
        monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(mod, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
        monkeypatch.setattr(mod, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")
    monkeypatch.setattr(cr, "RESUME_STATE_FILE", tmp_path / "resume_state.json")
    monkeypatch.setattr(cr, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rsm, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
    monkeypatch.setattr(rsm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rr, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
    monkeypatch.setattr(rr, "DATA_DIR", tmp_path)
    monkeypatch.setattr(st, "STATS_FILE", tmp_path / "agent_call_stats.json")
    monkeypatch.setattr(st, "DISPATCH_REGISTRY_FILE", tmp_path / "dispatch_registry.json")
    monkeypatch.delenv("COST_ROUTER_SUBAGENT_INFRA_TOKENS", raising=False)
    return tmp_path


def test_shell_total_cap_denies_without_debt():
    """Without dispatch debt, 4+ distinct-type inline Bash calls trigger
    gate_deny_shell_total_cap on the 4th call (total_inline_cap=4)."""
    sid = "journey-shell-cap-1"
    cr.event_reset({"session_id": sid, "prompt": "Multi-step task"}, False)

    # Four different command types (each will be counted separately for
    # same-type gate, but all counted toward total_inline_cap)
    commands = ["echo hi", "find . -name test", "grep pattern file", "ls -la"]
    cmd_types = ["echo", "find", "grep", "ls"]

    results = []
    for i, command in enumerate(commands):
        payload = {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        result = cr.shell_same_type_gate(payload, False)
        results.append(result)

    # First 3 calls should allow (below total_inline_cap of 4)
    for i in range(3):
        assert results[i] is None, f"Call {i+1} should allow (below cap)"

    # 4th call should deny (at total_inline_cap of 4)
    assert results[3] is not None and results[3].get("permission") == "deny", "4th call should deny at total_inline_cap"

    # Verify counter was bumped
    state = ss._load_state()
    assert int(state.get("counters", {}).get("gate_deny_shell_total_cap", 0)) > 0
    # Fallback counter should NOT be bumped (no debt)
    assert int(state.get("counters", {}).get("gate_shell_total_cap_allow_rt_exhausted", 0)) == 0


def test_shell_total_cap_allows_with_debt():
    """With dispatch debt active, the same 4 distinct-type inline Bash calls
    are allowed and gate_shell_total_cap_allow_rt_exhausted is bumped."""
    import _config

    sid = "journey-shell-cap-2"
    budget = float(_config.config_get("session_cost_budget.budget_usd") or 5.0)
    pcts = list(_config.config_get("session_cost_budget.warning_pcts") or [50, 80])
    session_pct = float(_config.config_get("concurrency.session_budget_pct") or 0.6)
    threshold = budget * min(max(pcts) / 100.0, session_pct)

    # Charge the session to activate debt
    below = acct.charges_below(threshold)
    acct.charge_real(st, sid, below)
    # One more charge to cross the threshold
    acct.charge_real(st, sid, 1)
    assert rsm._dispatch_debt_active(sid) is True, "Debt must be active"

    cr.event_reset({"session_id": sid, "prompt": "Multi-step task with debt"}, False)

    # Same four different command types
    commands = ["echo hi", "find . -name test", "grep pattern file", "ls -la"]

    results = []
    for i, command in enumerate(commands):
        payload = {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        result = cr.shell_same_type_gate(payload, False)
        results.append(result)

    # All 4 calls should allow (debt relief overrides the cap)
    for i in range(4):
        assert results[i] is None, f"Call {i+1} should allow under debt"

    # Verify fallback counter was bumped
    state = ss._load_state()
    assert int(state.get("counters", {}).get("gate_shell_total_cap_allow_rt_exhausted", 0)) > 0
    # Normal deny counter should NOT be bumped (debt relief prevented denial)
    assert int(state.get("counters", {}).get("gate_deny_shell_total_cap", 0)) == 0


def test_shell_same_type_deny_without_debt_and_allow_with_debt():
    """Same-type calls: 2+ identical-type calls deny without debt,
    allow with debt and bump gate_shell_same_type_allow_rt_exhausted."""
    import _config

    # Part 1: Without debt, second identical-type call should deny
    sid_no_debt = "journey-shell-same-type-1"
    cr.event_reset({"session_id": sid_no_debt, "prompt": "No debt test"}, False)

    # Two identical echo commands
    no_debt_results = []
    for i in range(2):
        payload = {
            "session_id": sid_no_debt,
            "tool_name": "Bash",
            "tool_input": {"command": "echo test"},
        }
        result = cr.shell_same_type_gate(payload, False)
        no_debt_results.append(result)

    # First call allows
    assert no_debt_results[0] is None, "First same-type call should allow"
    # Second call denies (same_type_counts[echo] = 2)
    assert no_debt_results[1] is not None and no_debt_results[1].get("permission") == "deny", "Second same-type call should deny without debt"

    state = ss._load_state()
    assert int(state.get("counters", {}).get("gate_deny_shell_same_type", 0)) > 0
    assert int(state.get("counters", {}).get("gate_shell_same_type_allow_rt_exhausted", 0)) == 0

    # Part 2: With debt, same two identical-type calls should allow
    sid_with_debt = "journey-shell-same-type-2"
    budget = float(_config.config_get("session_cost_budget.budget_usd") or 5.0)
    pcts = list(_config.config_get("session_cost_budget.warning_pcts") or [50, 80])
    session_pct = float(_config.config_get("concurrency.session_budget_pct") or 0.6)
    threshold = budget * min(max(pcts) / 100.0, session_pct)

    # Charge the session to activate debt
    below = acct.charges_below(threshold)
    acct.charge_real(st, sid_with_debt, below)
    acct.charge_real(st, sid_with_debt, 1)
    assert rsm._dispatch_debt_active(sid_with_debt) is True, "Debt must be active for second session"

    cr.event_reset({"session_id": sid_with_debt, "prompt": "With debt test"}, False)

    # Two identical find commands
    with_debt_results = []
    for i in range(2):
        payload = {
            "session_id": sid_with_debt,
            "tool_name": "Bash",
            "tool_input": {"command": "find . -type f"},
        }
        result = cr.shell_same_type_gate(payload, False)
        with_debt_results.append(result)

    # Both calls should allow (debt relief overrides same-type cap)
    for i in range(2):
        assert with_debt_results[i] is None, f"Same-type call {i+1} should allow with debt"

    state = ss._load_state()
    assert int(state.get("counters", {}).get("gate_shell_same_type_allow_rt_exhausted", 0)) > 0
