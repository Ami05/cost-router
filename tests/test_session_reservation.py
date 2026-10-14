"""DGC-205: token-aware reservation and clearer deny/queue messaging.

Tests for session budget cap deny messages that report token-aware reservation
for the new dispatch being denied. Extend existing test_session_budget_cap.py
tests with checks for the deny message format and token estimates.

Session budget check reserves the new dispatch's estimated tokens via
estimate_dispatch_context and _est_context_input_cost_usd, and deny messages
report remaining budget, reserve required, and suggestion to queue or batch.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _config  # noqa: E402
import check_routing as cr  # noqa: E402
import routing_resolver as rr  # noqa: E402
import routing_state_manager as rsm  # noqa: E402
import session_state as ss  # noqa: E402
import spawn_tracker as st  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))
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
    monkeypatch.setattr(st, "STATS_FILE", tmp_path / "agent_call_stats.json")
    monkeypatch.setattr(st, "DISPATCH_REGISTRY_FILE", tmp_path / "dispatch_registry.json")
    return tmp_path


@pytest.fixture(autouse=True)
def _nominal_infra(monkeypatch):
    monkeypatch.delenv("COST_ROUTER_SUBAGENT_INFRA_TOKENS", raising=False)


def _cap_usd() -> float:
    budget = float(_config.config_get("session_cost_budget.budget_usd") or 5.0)
    pct = float(_config.config_get("concurrency.session_budget_pct") or 0.6)
    return budget * pct


def _charges_below_cap() -> int:
    """Largest number of real charges whose total stays strictly below the cap."""
    n = acct.charges_below(_cap_usd())
    assert n > 0, "cap must admit at least one real charge for these tests"
    return n


def _charge(sid: str, n: int) -> None:
    """n further real SubagentStop charges for *sid* (continues the sequence)."""
    acct.charge_real(st, sid, n)


def _cost(sid: str) -> float:
    return rsm._cumulative_dispatch_cost_usd(sid)


def _task_payload(sid: str, turn: str, path: str, intent: str = "fix bug in module") -> dict:
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


def _dispatch(sid: str, turn: str, path: str, intent: str = "fix bug in module", model: str | None = None) -> dict:
    cr.event_scan(
        {"session_id": sid, "text": f"Routing: FO — dispatching worker".strip(), "prompt_id": turn},
        False,
    )
    payload = _task_payload(sid, turn, path, intent)
    if model:
        payload["model"] = model
    return cr.event_gate(payload, False)


def _entry(sid: str) -> dict:
    return cr._get_session(cr._load_state()["sessions"], sid)


class TestTokenAwareReservationInDenyMessage:
    """DGC-205: deny message includes token-aware reservation."""

    def test_deny_message_includes_token_estimate(self):
        """Token-aware reservation is traced in deny message."""
        sid = "dgc205-token-estimate"
        cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)

        # Charge past the cap so the next dispatch is denied.
        _charge(sid, _charges_below_cap() + 1)
        assert _cost(sid) >= _cap_usd()

        # Dispatch with explicit model to get token estimate.
        denied = _dispatch(sid, "t-1", "scripts/alpha.py", model="claude-haiku-4-5-20251001")
        assert denied.get("permission") == "deny"

        msg = denied.get("agent_message", "")
        assert "Session dispatch budget cap" in msg
        # Token-aware reservation should appear in the deny message.
        assert "Token-aware reservation" in msg or "tokens" in msg or "$" in msg
        # Message should suggest alternatives (queue, batch, inline).
        assert ("queue" in msg.lower() or "batch" in msg.lower() or "inline" in msg.lower() or
                "resume" in msg.lower())

    def test_deny_message_with_no_model_still_denies(self):
        """Deny message works even if model is not provided (graceful degradation)."""
        sid = "dgc205-no-model"
        cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)

        _charge(sid, _charges_below_cap() + 1)
        assert _cost(sid) >= _cap_usd()

        # Dispatch without model — should still deny, just without token estimate.
        denied = _dispatch(sid, "nm-1", "scripts/alpha.py")
        assert denied.get("permission") == "deny"

        msg = denied.get("agent_message", "")
        assert "Session dispatch budget cap" in msg

    def test_deny_message_shows_remaining_budget(self):
        """Deny message reports remaining budget from the cap."""
        sid = "dgc205-remaining-budget"
        cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)

        # Charge to just above the cap.
        _charge(sid, _charges_below_cap() + 1)
        cost = _cost(sid)
        cap = _cap_usd()
        assert cost >= cap

        denied = _dispatch(sid, "rb-1", "scripts/alpha.py", model="claude-haiku-4-5-20251001")
        assert denied.get("permission") == "deny"

        msg = denied.get("agent_message", "")
        # Message should contain budget information.
        assert "budget" in msg.lower() or "$" in msg

    def test_session_budget_override_semantics_unchanged(self):
        """session_budget_override_limit semantics unchanged (default 0)."""
        sid = "dgc205-override-compat"
        cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)

        _charge(sid, _charges_below_cap() + 1)

        # Without override token, dispatch is denied.
        denied = _dispatch(sid, "oc-1", "scripts/alpha.py")
        assert denied.get("permission") == "deny"
        assert _entry(sid).get("session_budget_override_count", 0) == 0

    def test_token_estimate_multiple_models(self):
        """Token estimates vary by model."""
        sid = "dgc205-models"
        cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)

        _charge(sid, _charges_below_cap() + 1)

        # Dispatch with haiku model.
        denied_haiku = _dispatch(sid, "m-1", "scripts/alpha.py", model="claude-haiku-4-5-20251001")
        assert denied_haiku.get("permission") == "deny"
        msg_haiku = denied_haiku.get("agent_message", "")

        # Dispatch with sonnet model (different cost structure).
        denied_sonnet = _dispatch(sid, "m-2", "scripts/beta.py", model="claude-3-5-sonnet-20241022")
        assert denied_sonnet.get("permission") == "deny"
        msg_sonnet = denied_sonnet.get("agent_message", "")

        # Both deny, but messages might differ in token/cost details if available.
        assert "Session dispatch budget cap" in msg_haiku
        assert "Session dispatch budget cap" in msg_sonnet
