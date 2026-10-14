"""TEST-004: Gate-parity contract test between routing_resolver.py and check_routing.py.

Investigation finding (see docstrings below for detail): the premise "check_routing.py
is the tested source of truth, routing_resolver.py has an untested duplicate" is
backwards for both functions in scope.

_check_shell_gate:
  - routing_resolver.py:697-726 defines the only live body.
  - check_routing.py:2739-2762 defines a byte-for-byte copy, but it is dead code:
    `from routing_resolver import (..., _check_shell_gate, ...)` near the bottom of
    check_routing.py rebinds the module-level name, so `check_routing._check_shell_gate`
    resolves to routing_resolver's function object at runtime, not check_routing's own
    body. There is only one live implementation to test, not two — a parity comparison
    would just be comparing an object with itself.

_dispatch_debt_active:
  - routing_resolver.py:82-115 defines its own copy (no config_error event logging on
    exception) and is what routing_resolver's own internal callers (e.g. its
    _check_shell_gate) invoke via routing_resolver's module globals.
  - check_routing.py:163-207 defines a third copy, but it too is dead code, shadowed by
    `from routing_state_manager import (..., _dispatch_debt_active, ...)` near the
    bottom of check_routing.py. `check_routing._dispatch_debt_active` resolves to
    routing_state_manager's copy at runtime.
  - routing_state_manager.py:142-184 defines the copy actually reached through
    check_routing.py's call paths (it logs a config_error event on exception).

So the one genuinely independent, both-live pair — reached through two different call
chains and never unified — is routing_resolver._dispatch_debt_active vs
routing_state_manager._dispatch_debt_active. That is the real gate-parity risk (a
behavioral fix or drift applied to one copy silently does not reach the other), and is
what this suite tests. The shadowing itself is pinned down with identity assertions so
regressions in the shim wiring are caught too.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

pytestmark = pytest.mark.e2e


def test_check_shell_gate_is_shadowed_not_duplicated():
    """check_routing._check_shell_gate IS routing_resolver._check_shell_gate (same
    object, via import shim) — not a second implementation. No parity test applies;
    this pins the shadowing down so it's visible if the shim wiring ever changes."""
    import check_routing
    import routing_resolver

    assert check_routing._check_shell_gate is routing_resolver._check_shell_gate


def test_dispatch_debt_active_is_shadowed_by_routing_state_manager():
    """check_routing._dispatch_debt_active resolves to routing_state_manager's copy
    (via import shim), not check_routing's own dead 163-207 body."""
    import check_routing
    import routing_state_manager

    assert check_routing._dispatch_debt_active is routing_state_manager._dispatch_debt_active


class TestDispatchDebtActiveParity:
    """Contract: routing_resolver._dispatch_debt_active and
    routing_state_manager._dispatch_debt_active are independent, copy-pasted bodies
    that must return the same fail-open boolean for the same session state. They
    differ only in exception-path logging (routing_state_manager's copy emits a
    config_error event; routing_resolver's copy fails silently) — success-path return
    values must match across the two trigger conditions (cost, all-inline deadlock).
    """

    def _make_fakes(self, session_id, cost, dispatched, gate_calls):
        def fake_config_get(key):
            if key == "session_cost_budget.budget_usd":
                return 5.0
            if key == "session_cost_budget.warning_pcts":
                return [50, 80]
            return None

        def fake_load_json_state(path):
            path_s = str(path)
            if "agent_call_stats" in path_s:
                return {"sessions": {session_id: {"cumulative_cost_usd": cost}}}
            if "routing_state" in path_s:
                return {
                    "sessions": {
                        session_id: {
                            "task_dispatched_count_session": dispatched,
                            "total_gate_calls_session": gate_calls,
                        }
                    }
                }
            return {}

        return fake_config_get, fake_load_json_state

    def _scenarios(self):
        import routing_resolver

        deny_threshold = routing_resolver.SESSION_DISPATCH_DENY_THRESHOLD
        return [
            ("no_debt_default_state", 0.0, 0, 0, False),
            ("cost_trigger_fires_at_high_water", 4.0, 0, 0, True),  # stubbed reader input; threshold = min(0.8, session_budget_pct 0.6) * 5.0 = 3.0
            ("cost_trigger_just_under", 2.99, 0, 0, False),
            ("cost_trigger_at_threshold", 3.0, 0, 0, True),
            ("cost_trigger_above_threshold", 3.99, 0, 0, True),
            ("all_inline_deadlock_fires", 0.0, 0, deny_threshold, True),
            ("all_inline_but_dispatch_happened", 0.0, 1, deny_threshold, False),
            ("both_triggers_clear", 0.0, 0, max(deny_threshold - 1, 0), False),
        ]

    def test_parity_across_scenarios(self):
        import routing_resolver
        import routing_state_manager

        for label, cost, dispatched, gate_calls, expected in self._scenarios():
            session_id = f"sess-{label}"
            fake_config_get, fake_load_json_state = self._make_fakes(
                session_id, cost, dispatched, gate_calls
            )

            with patch("routing_resolver.config_get", side_effect=fake_config_get), \
                 patch("routing_resolver.load_json_state", side_effect=fake_load_json_state), \
                 patch("routing_state_manager.config_get", side_effect=fake_config_get), \
                 patch("routing_state_manager.load_json_state", side_effect=fake_load_json_state):

                result_resolver = routing_resolver._dispatch_debt_active(session_id)
                result_state_mgr = routing_state_manager._dispatch_debt_active(session_id)

            assert result_resolver == expected, f"{label}: routing_resolver mismatch"
            assert result_state_mgr == expected, f"{label}: routing_state_manager mismatch"
            assert result_resolver == result_state_mgr, (
                f"{label}: parity broken — resolver={result_resolver} "
                f"state_manager={result_state_mgr}"
            )

    def test_parity_on_exception_fail_open(self):
        """Both copies must fail open (return False) when config_get raises, even
        though only routing_state_manager's copy logs a config_error event."""
        import routing_resolver
        import routing_state_manager

        with patch("routing_resolver.config_get", side_effect=ValueError("boom")), \
             patch("routing_state_manager.config_get", side_effect=ValueError("boom")):
            result_resolver = routing_resolver._dispatch_debt_active("sess-exc")
            result_state_mgr = routing_state_manager._dispatch_debt_active("sess-exc")

        assert result_resolver is False
        assert result_state_mgr is False


class TestDispatchDebtParityOnRealAccounting:
    """The stubbed scenarios above feed the cost trigger through a fake stats
    reader. Dispatch cost on Claude Code is written only by
    spawn_tracker.finalize_dispatch_charge (SubagentStop, once per dispatched
    agent_id, at the registry model's rate), so the same cost-trigger parity is
    also checked against stats produced by that real path, from real dispatched
    registry entries (tests/fixtures/data/accounting_session_excerpts.json)."""

    def test_cost_trigger_parity_on_real_charges(self, tmp_path, monkeypatch):
        import _config
        import routing_resolver
        import routing_state_manager
        import spawn_tracker

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _accounting_fixture as acct

        monkeypatch.delenv("COST_ROUTER_SUBAGENT_INFRA_TOKENS", raising=False)
        for mod in (routing_resolver, routing_state_manager):
            monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
            monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(spawn_tracker, "STATS_FILE", tmp_path / "agent_call_stats.json")
        monkeypatch.setattr(
            spawn_tracker, "DISPATCH_REGISTRY_FILE", tmp_path / "dispatch_registry.json"
        )

        budget = float(_config.config_get("session_cost_budget.budget_usd") or 5.0)
        pcts = list(_config.config_get("session_cost_budget.warning_pcts") or [50, 80])
        session_pct = float(_config.config_get("concurrency.session_budget_pct") or 0.6)
        threshold = budget * min(max(pcts) / 100.0, session_pct)

        sid = "sess-parity-real"
        below = acct.charges_below(threshold)
        acct.charge_real(spawn_tracker, sid, below)
        assert routing_resolver._dispatch_debt_active(sid) is False
        assert routing_state_manager._dispatch_debt_active(sid) is False

        for extra in range(1, 4):
            acct.charge_real(spawn_tracker, sid, 1)
            assert routing_resolver._dispatch_debt_active(sid) is True, extra
            assert routing_state_manager._dispatch_debt_active(sid) is True, extra
