"""Journey: BWS-T2-004 session dispatch budget cap and its bounded override.

Style follows tests/e2e/test_journey_rt_exhaustion.py: real event_scan /
event_gate calls against real state files under tmp_path, no gate mocks.

Cumulative dispatch cost is seeded through the real SubagentStop charge path
(spawn_tracker.finalize_dispatch_charge -> agent_call_stats.json), never by
hand-writing the stats file. finalize charges once per DISPATCHED agent_id (one
with dispatch metadata in dispatch_registry.json) at that registry entry's model
rate, so each charge here replays a real dispatched registry entry from
tests/fixtures/data/accounting_session_excerpts.json under a fresh agent id
(tests/e2e/_accounting_fixture.py). Only the file locations are redirected to tmp_path
and, for the override tests, the config cache is a copy of the real config with
concurrency.session_budget_override_limit changed.

Cases:
  1. Cap boundary: dispatch allowed below session_budget_pct, denied at it.
  2. No simultaneous deny: cost past the cap, inline mutations past the deny
     threshold -> inline work is allowed (rt_exhausted) while dispatch is denied.
  3. Override: limit 0 denies even with the token; limit > 0 grants up to the
     limit then denies; dispatch-budget-override does not waive the cap.
  4. An overridden dispatch still resets inline_mutations_since_dispatch.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _config  # noqa: E402
import check_routing as cr  # noqa: E402
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
    # Cap and debt checks live in rsm / rr with their own file constants; both
    # read agent_call_stats.json via DATA_DIR and routing state via ROUTING_STATE_FILE.
    for mod in (rsm, rr):
        monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    # Real SubagentStop charge path writes here.
    monkeypatch.setattr(st, "STATS_FILE", tmp_path / "agent_call_stats.json")
    monkeypatch.setattr(st, "DISPATCH_REGISTRY_FILE", tmp_path / "dispatch_registry.json")
    return tmp_path


@pytest.fixture(autouse=True)
def _nominal_infra(monkeypatch):
    monkeypatch.delenv("COST_ROUTER_SUBAGENT_INFRA_TOKENS", raising=False)


def _set_override_limit(monkeypatch, limit: int) -> None:
    cfg = copy.deepcopy(_config.load_plugin_config())
    cfg.setdefault("concurrency", {})["session_budget_override_limit"] = limit
    monkeypatch.setattr(_config, "_config_cache", cfg)


def _cap_usd() -> float:
    budget = float(_config.config_get("session_cost_budget.budget_usd") or 5.0)
    pct = float(_config.config_get("concurrency.session_budget_pct") or 0.6)
    return budget * pct


def _charges_below_cap() -> int:
    """Largest number of real charges whose total stays strictly below the cap.

    Charges are not uniform (the registry model sets the rate), so this walks
    the exact sequence charge_real will produce, priced by the nominal formula.
    """
    n = acct.charges_below(_cap_usd())
    assert n > 0, "cap must admit at least one real charge for these tests"
    return n


def _charge(sid: str, n: int) -> None:
    """n further real SubagentStop charges for *sid* (continues the sequence)."""
    acct.charge_real(st, sid, n)


def _cost(sid: str) -> float:
    return rsm._cumulative_dispatch_cost_usd(sid)


def _task_payload(sid: str, turn: str, path: str, intent: str = "fix bug in module") -> dict:
    # Split path into base and ext to create variants for complexity scoring
    base, ext = path.rsplit(".", 1) if "." in path else (path, "")
    ext_str = f".{ext}" if ext else ""
    files = [
        f"{path}:1",
        f"{base}_b{ext_str}:1",
        f"{base}_c{ext_str}:1",
    ]
    prompt = json.dumps({
        "worker": "mutation",
        "intent": intent,
        "files": files,
        "pattern": "do it",
        "output": "targeted_edits",
    })
    return {"session_id": sid, "tool_name": "Task", "tool_input": {"prompt": prompt}, "prompt_id": turn}


def _dispatch(sid: str, turn: str, path: str, token: str = "", intent: str = "fix bug in module") -> dict:
    cr.event_scan(
        {"session_id": sid, "text": f"Routing: FO — dispatching worker {token}".strip(), "prompt_id": turn},
        False,
    )
    return cr.event_gate(_task_payload(sid, turn, path, f"{intent} {token}".strip()), False)


def _edit(sid: str, turn: str) -> dict:
    cr.event_scan({"session_id": sid, "text": "Routing: DIRECT — editing", "prompt_id": turn}, False)
    return cr.event_gate(
        {
            "session_id": sid, "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/session_budget_cap.py", "old_string": "a", "new_string": turn},
            "prompt_id": turn,
        },
        False,
    )


def _counters() -> dict:
    return cr._load_state().get("counters", {})


def _entry(sid: str) -> dict:
    return cr._get_session(cr._load_state()["sessions"], sid)


def test_cap_boundary_allows_below_and_denies_at_cap():
    sid = "sbc-boundary"
    cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)

    _charge(sid, _charges_below_cap())
    assert _cost(sid) < _cap_usd()
    allowed = _dispatch(sid, "b-1", "scripts/alpha.py")
    assert allowed.get("permission") == "allow", "dispatch below the cap must be allowed"
    assert int(_counters().get("gate_deny_session_budget_cap", 0)) == 0

    _charge(sid, 1)
    assert _cost(sid) >= _cap_usd()
    denied = _dispatch(sid, "b-2", "scripts/beta.py")
    assert denied.get("permission") == "deny", "dispatch at the cap must be denied"
    assert "Session dispatch budget cap" in denied.get("agent_message", "")
    assert int(_counters().get("gate_deny_session_budget_cap", 0)) == 1


def test_cap_active_does_not_deadlock_inline_work():
    sid = "sbc-no-simul-deny"
    cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)

    # Past the cap (and below the 80% high-water mark, so only the cost trigger
    # aligned to the cap can be what relieves the inline gate).
    _charge(sid, _charges_below_cap() + 1)
    cost = _cost(sid)
    budget = float(_config.config_get("session_cost_budget.budget_usd") or 5.0)
    assert _cap_usd() <= cost < budget * 0.8

    deny_threshold = _config.INLINE_MUTATION_DENY_THRESHOLD
    last = None
    for i in range(deny_threshold + 1):
        last = _edit(sid, f"i-{i}")
        assert last.get("permission") == "allow", f"inline edit {i} must not be denied while the cap is active"
    assert _entry(sid)["inline_mutations_since_dispatch"] >= deny_threshold

    counters = _counters()
    assert int(counters.get("gate_deny_inline_mutation_limit", 0)) == 0
    assert (
        int(counters.get("gate_allow_inline_mutation_rt_exhausted", 0))
        + int(counters.get("gate_allow_inline_mutation_nudge_rt_exhausted", 0))
    ) > 0, "relief must be recorded on disk, not just inferred from the allow"

    denied = _dispatch(sid, "i-dispatch", "scripts/gamma.py")
    assert denied.get("permission") == "deny"
    assert int(_counters().get("gate_deny_session_budget_cap", 0)) == 1


def test_override_limit_zero_denies_even_with_token():
    sid = "sbc-limit-zero"
    cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)
    _charge(sid, _charges_below_cap() + 1)

    out = _dispatch(sid, "z-1", "scripts/alpha.py", token="session-budget-override")
    assert out.get("permission") == "deny", "default limit 0 disables the override"
    counters = _counters()
    assert int(counters.get("gate_deny_session_budget_cap", 0)) == 1
    assert int(counters.get("gate_allow_session_budget_override", 0)) == 0
    assert int(_entry(sid).get("session_budget_override_count", 0)) == 0


def test_override_grants_up_to_limit_then_denies(monkeypatch):
    _set_override_limit(monkeypatch, 2)
    sid = "sbc-limit-two"
    cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)
    _charge(sid, _charges_below_cap() + 1)

    for n, path in enumerate(("scripts/alpha.py", "scripts/beta.py"), start=1):
        out = _dispatch(sid, f"o-{n}", path, token="session-budget-override")
        assert out.get("permission") == "allow", f"override use {n} of 2 must be granted"
        assert int(_entry(sid)["session_budget_override_count"]) == n
        assert int(_counters().get("gate_allow_session_budget_override", 0)) == n

    out = _dispatch(sid, "o-3", "scripts/gamma.py", token="session-budget-override")
    assert out.get("permission") == "deny", "uses beyond the limit are denied"
    assert int(_entry(sid)["session_budget_override_count"]) == 2
    assert int(_counters().get("gate_deny_session_budget_cap", 0)) == 1


def test_dispatch_budget_override_does_not_waive_cap(monkeypatch):
    _set_override_limit(monkeypatch, 2)
    sid = "sbc-wrong-token"
    cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)
    _charge(sid, _charges_below_cap() + 1)

    out = _dispatch(sid, "w-1", "scripts/alpha.py", token="dispatch-budget-override")
    assert out.get("permission") == "deny"
    assert int(_counters().get("gate_deny_session_budget_cap", 0)) == 1
    assert int(_counters().get("gate_allow_session_budget_override", 0)) == 0
    assert int(_entry(sid).get("session_budget_override_count", 0)) == 0


def test_overridden_dispatch_still_resets_inline_mutation_counter(monkeypatch):
    _set_override_limit(monkeypatch, 1)
    sid = "sbc-reset"
    cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)
    _charge(sid, _charges_below_cap() + 1)

    for i in range(2):
        assert _edit(sid, f"r-{i}").get("permission") == "allow"
    before = _entry(sid)
    assert int(before["inline_mutations_since_dispatch"]) > 0
    dispatched_before = int(before.get("task_dispatched_count_session", 0))

    out = _dispatch(sid, "r-dispatch", "scripts/alpha.py", token="session-budget-override")
    assert out.get("permission") == "allow"

    after = _entry(sid)
    assert int(after["session_budget_override_count"]) == 1, "the cap was waived via the override"
    assert int(after["inline_mutations_since_dispatch"]) == 0, "only the cap is waived; bookkeeping still runs"
    assert int(after.get("task_dispatched_count_session", 0)) == dispatched_before + 1
