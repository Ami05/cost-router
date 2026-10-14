"""Dispatch cost accounting replayed from real session fixtures.

Fixture: tests/fixtures/data/accounting_session_excerpts.json, excerpts of real
sessions from the production agent_call_stats.json / dispatch_registry.json
(see its _provenance field). No synthetic cost numbers: every expected value is
computed from the fixture's recorded models with the nominal cold-write formula
(infra tokens / 1000 * cache_write_per_1k, read straight from config.json and
scripts/pricing.json by tests/e2e/_accounting_fixture.py).

Everything runs through the real hook entry point, agent_call_tracker.main():
  - PreToolUse(Agent)      -> event_preuse   (no --event flag)
  - SubagentStop           -> finalize_dispatch_charge (--event subagent_stop)

Contract under test (Claude Code):
  - spawn_tracker.finalize_dispatch_charge is the sole cumulative_cost_usd writer;
  - exactly one charge per DISPATCHED agent_id (registry entry with dispatch
    metadata), at the registry model's rate;
  - stop-only ids and repeated stops charge nothing;
  - Cursor registers no stop hook, so Cursor payloads still charge at dispatch;
  - a finalize write landing between event_preuse's stats load and save is not
    lost (spawn_tracker._save_stats keeps on-disk cost fields).
"""
from __future__ import annotations

import copy
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _accounting_fixture as acct  # noqa: E402
import _config  # noqa: E402
import agent_call_tracker as act  # noqa: E402
import bws_enforcer  # noqa: E402
import dispatch_budget_enforcer as dbe  # noqa: E402
import spawn_tracker as st  # noqa: E402
from _common import now_iso  # noqa: E402

pytestmark = pytest.mark.e2e

SESSION_KEYS = ["sess-b11b", "sess-1f96", "sess-8ef3"]
PROMPT = "Summarize the prior step output."


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class Env:
    def __init__(self, tmp: Path) -> None:
        self.stats_file = tmp / "agent_call_stats.json"
        self.registry_file = tmp / "dispatch_registry.json"
        self.events: list[dict[str, Any]] = []
        self.fixture = acct.load_fixture()

    def load_registry(self, key: str) -> None:
        acct.write_registry(self.registry_file, key, self.fixture[key]["registry"]["agents"])

    def stats(self, sid: str) -> dict[str, Any]:
        data = json.loads(self.stats_file.read_text(encoding="utf-8"))
        return data["sessions"][sid]

    def events_of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if isinstance(e, dict) and e.get("type") == kind]

    # -- hook entry points -------------------------------------------------

    def _main(self, payload: dict[str, Any], *argv: str) -> dict[str, Any]:
        buf = io.StringIO()
        with patch.object(act, "read_stdin_json", return_value=payload), \
             patch.object(sys, "argv", ["agent_call_tracker.py", *argv]), \
             redirect_stdout(buf):
            rc = act.main()
        assert rc == 0
        return json.loads(buf.getvalue().strip().splitlines()[-1])

    def claude_dispatch(self, sid: str, agent: dict[str, Any]) -> dict[str, Any]:
        out = self._main({
            "hook_event_name": "PreToolUse",
            "session_id": sid,
            "tool_name": "Agent",
            "tool_input": {
                "subagent_type": agent["subagent_type"],
                "model": agent["model"],
                "prompt": PROMPT,
                "description": "accounting replay",
            },
        })
        decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
        assert decision == "allow", f"replayed dispatch must be allowed, got {out}"
        return out

    def cursor_dispatch(self, sid: str, agent: dict[str, Any]) -> dict[str, Any]:
        out = self._main({
            "hook_event_name": "preToolUse",
            "cursor_version": "1.7.0",
            "conversation_id": sid,
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": agent["subagent_type"],
                "model": agent["model"],
                "prompt": PROMPT,
                "description": "accounting replay",
            },
        })
        assert out.get("permission") == "allow", f"replayed dispatch must be allowed, got {out}"
        return out

    def stop(self, sid: str, agent_id: str) -> dict[str, Any]:
        return self._main(
            {"hook_event_name": "SubagentStop", "session_id": sid, "agent_id": agent_id},
            "--event", "subagent_stop",
        )

    def timeline(self, key: str) -> list[tuple[str, str, dict[str, Any]]]:
        """Recorded dispatch/stop events of a fixture session, in time order."""
        steps: list[tuple[str, int, str, str, dict[str, Any]]] = []
        for aid, entry in self.fixture[key]["registry"]["agents"].items():
            if acct.is_dispatched(entry):
                steps.append((entry["started_at"], 0, "dispatch", aid, entry))
            if entry.get("finished_at"):
                steps.append((entry["finished_at"], 1, "stop", aid, entry))
        steps.sort(key=lambda t: (t[0], t[1]))
        return [(kind, aid, entry) for _ts, _o, kind, aid, entry in steps]

    def replay_claude(self, key: str) -> None:
        for kind, aid, entry in self.timeline(key):
            if kind == "dispatch":
                self.claude_dispatch(key, entry)
            else:
                self.stop(key, aid)


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    e = Env(tmp_path)
    monkeypatch.delenv("COST_ROUTER_SUBAGENT_INFRA_TOKENS", raising=False)
    for mod in (st, act):
        monkeypatch.setattr(mod, "STATS_FILE", e.stats_file)
    monkeypatch.setattr(st, "DISPATCH_REGISTRY_FILE", e.registry_file)
    monkeypatch.setattr(bws_enforcer, "DISPATCH_REGISTRY_FILE", e.registry_file)
    # No orchestrator-turn cost: routing state does not exist under tmp.
    monkeypatch.setattr(dbe, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
    monkeypatch.setattr(act, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
    for mod in (st, dbe, act, bws_enforcer):
        if hasattr(mod, "append_event"):
            monkeypatch.setattr(mod, "append_event", e.events.append)
    return e


def _dispatched_ids(env: Env, key: str) -> list[str]:
    return [a["agent_id"] for a in acct.dispatched_entries(env.fixture[key])]


def _nominal_total(env: Env, key: str) -> float:
    return sum(acct.nominal_cost_usd(a["model"]) for a in acct.dispatched_entries(env.fixture[key]))


def _set_budget(monkeypatch, budget_usd: float) -> None:
    cfg = copy.deepcopy(_config.load_plugin_config())
    cfg.setdefault("session_cost_budget", {})["budget_usd"] = budget_usd
    monkeypatch.setattr(_config, "_config_cache", cfg)


# ---------------------------------------------------------------------------
# Fixture sanity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", SESSION_KEYS)
def test_fixture_is_real_shaped(env: Env, key: str) -> None:
    session = env.fixture[key]
    assert acct.dispatched_entries(session), "fixture must contain dispatched agents"
    assert acct.stop_only_ids(session), "fixture must contain stop-only ids"
    assert len(acct.dispatched_entries(session)) == len(session["stats"]["agents"])
    assert all(a.get("model") for a in acct.dispatched_entries(session))


# ---------------------------------------------------------------------------
# One charge per dispatched id; none for stop-only; nominal total
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", SESSION_KEYS)
def test_one_charge_per_dispatched_id_none_for_stop_only(env: Env, key: str) -> None:
    env.load_registry(key)
    env.replay_claude(key)

    stats = env.stats(key)
    dispatched = _dispatched_ids(env, key)
    charged = stats["charged_agent_ids"]
    assert len(charged) == len(set(charged)), "no agent_id may be charged twice"
    assert sorted(charged) == sorted(dispatched), "exactly the dispatched ids are charged"
    assert set(charged).isdisjoint(acct.stop_only_ids(env.fixture[key]))
    assert stats["total_agents"] == len(dispatched), "every replayed dispatch was recorded"
    assert not any(a.get("denied") for a in stats["agents"])

    nominal = _nominal_total(env, key)
    assert nominal > 0
    assert abs(stats["cumulative_cost_usd"] - nominal) <= 0.05 * nominal, (
        f"recorded {stats['cumulative_cost_usd']} vs nominal {nominal}"
    )


@pytest.mark.parametrize("key", SESSION_KEYS)
def test_stop_only_ids_alone_charge_nothing(env: Env, key: str) -> None:
    env.load_registry(key)
    for aid in acct.stop_only_ids(env.fixture[key]):
        env.stop(key, aid)
    if env.stats_file.exists():
        stats = env.stats_file.read_text(encoding="utf-8")
        sess = json.loads(stats).get("sessions", {}).get(key, {})
        assert float(sess.get("cumulative_cost_usd", 0.0)) == 0.0
        assert not sess.get("charged_agent_ids")


@pytest.mark.parametrize("key", SESSION_KEYS)
def test_repeated_stop_of_same_id_is_not_charged_again(env: Env, key: str) -> None:
    env.load_registry(key)
    env.replay_claude(key)
    before = env.stats(key)
    assert before["cumulative_cost_usd"] > 0

    for _ in range(2):
        for aid in env.fixture[key]["registry"]["agents"]:
            env.stop(key, aid)

    after = env.stats(key)
    assert after["cumulative_cost_usd"] == before["cumulative_cost_usd"]
    assert after["charged_agent_ids"] == before["charged_agent_ids"]


# ---------------------------------------------------------------------------
# Registry model rate
# ---------------------------------------------------------------------------

def test_charge_uses_registry_model_rate(env: Env) -> None:
    key = "sess-b11b"
    env.load_registry(key)
    agents = acct.dispatched_entries(env.fixture[key])
    models = {a["model"] for a in agents}
    assert {"haiku", "sonnet"} <= models, "fixture session must mix model tiers"

    seen_deltas: dict[str, float] = {}
    for agent in agents:
        env.claude_dispatch(key, agent)
        before = env.stats(key)["cumulative_cost_usd"]
        env.stop(key, agent["agent_id"])  # stop payload carries no model
        delta = env.stats(key)["cumulative_cost_usd"] - before
        assert delta == pytest.approx(acct.nominal_cost_usd(agent["model"]), abs=1e-6)
        seen_deltas[agent["model"]] = delta
    assert seen_deltas["sonnet"] > seen_deltas["haiku"]


def test_charge_follows_registry_model_not_dispatch_request(env: Env) -> None:
    """SubagentStop has no model; the rate must come from the registry entry."""
    key = "sess-1f96"
    env.load_registry(key)
    agent = acct.dispatched_entries(env.fixture[key])[0]
    assert agent["model"] == "haiku"
    # Dispatch asks for sonnet, registry (as recorded at SubagentStart) says haiku.
    env.claude_dispatch(key, {**agent, "model": "sonnet"})
    assert env.stats(key).get("cumulative_cost_usd", 0.0) == 0.0
    env.stop(key, agent["agent_id"])
    assert env.stats(key)["cumulative_cost_usd"] == pytest.approx(
        acct.nominal_cost_usd("haiku"), abs=1e-6
    )


def test_claude_dispatch_alone_does_not_charge(env: Env) -> None:
    key = "sess-8ef3"
    env.load_registry(key)
    for agent in acct.dispatched_entries(env.fixture[key]):
        env.claude_dispatch(key, agent)
    stats = env.stats(key)
    assert stats["total_agents"] == len(_dispatched_ids(env, key))
    assert stats["cumulative_cost_usd"] == 0.0
    assert not stats.get("charged_agent_ids")


# ---------------------------------------------------------------------------
# 50 / 80 percent warnings
# ---------------------------------------------------------------------------

def _expected_warning_points(models: list[str], budget: float, pcts: list[int]) -> dict[int, tuple[int, float]]:
    """pct -> (dispatch index, projected cumulative) at which it should first fire.

    Projection matches check_cost_warnings: cost charged so far (stop-time
    accounting, replayed sequentially) plus this dispatch's nominal cost.
    """
    fired: dict[int, tuple[int, float]] = {}
    cumulative = 0.0
    for i, model in enumerate(models):
        cost = acct.nominal_cost_usd(model)
        projected = cumulative + cost
        for pct in sorted(pcts):
            if pct not in fired and projected >= budget * pct / 100:
                fired[pct] = (i, projected)
        cumulative += cost
    return fired


def test_warnings_fire_at_the_right_dispatches(env: Env, monkeypatch) -> None:
    key = "sess-8ef3"
    env.load_registry(key)
    agents = acct.dispatched_entries(env.fixture[key])
    models = [a["model"] for a in agents]
    pcts = [int(p) for p in _config.config_get("session_cost_budget.warning_pcts") or [50, 80]]
    assert pcts == [50, 80]

    # Scale the budget knob to 90% of the real session's nominal total so the
    # replay crosses 50% and then 80% at different dispatches.
    budget = _nominal_total(env, key) * 0.9
    _set_budget(monkeypatch, budget)
    expected = _expected_warning_points(models, budget, pcts)
    assert set(expected) == {50, 80} and expected[50][0] < expected[80][0], (
        "fixture/budget must separate the two thresholds"
    )

    fired_at: dict[int, list[int]] = {}
    for i, agent in enumerate(agents):
        before = len(env.events_of("cost_budget_warning"))
        env.claude_dispatch(key, agent)
        for ev in env.events_of("cost_budget_warning")[before:]:
            fired_at.setdefault(ev["threshold_pct"], []).append(i)
            assert ev["cumulative_cost_usd"] == pytest.approx(expected[ev["threshold_pct"]][1], abs=1e-6)
            assert ev["budget_usd"] == pytest.approx(budget)
        env.stop(key, agent["agent_id"])

    assert fired_at == {pct: [expected[pct][0]] for pct in (50, 80)}, "each threshold fires once, at its point"
    assert env.stats(key)["cost_warnings_emitted"] == [50, 80]


def test_no_warning_below_threshold_on_default_budget(env: Env) -> None:
    key = "sess-8ef3"
    budget = float(_config.config_get("session_cost_budget.budget_usd") or 5.0)
    assert _nominal_total(env, key) < budget * 0.5, "real session is below 50% of the default budget"
    env.load_registry(key)
    env.replay_claude(key)
    assert env.events_of("cost_budget_warning") == []
    assert env.stats(key)["cost_warnings_emitted"] == []


# ---------------------------------------------------------------------------
# Cursor still charges at dispatch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", SESSION_KEYS)
def test_cursor_payloads_charge_at_dispatch(env: Env, key: str) -> None:
    cumulative = 0.0
    for agent in acct.dispatched_entries(env.fixture[key]):
        env.cursor_dispatch(key, agent)
        cumulative += acct.nominal_cost_usd(agent["model"])
        assert env.stats(key)["cumulative_cost_usd"] == pytest.approx(cumulative, abs=1e-6)
    nominal = _nominal_total(env, key)
    assert abs(env.stats(key)["cumulative_cost_usd"] - nominal) <= 0.05 * nominal
    assert not env.stats(key).get("charged_agent_ids"), "Cursor does not use the stop-time ledger"


# ---------------------------------------------------------------------------
# Stale-save race: finalize landing between event_preuse load and save
# ---------------------------------------------------------------------------

def test_finalize_between_preuse_load_and_save_is_not_lost(env: Env, monkeypatch) -> None:
    key = "sess-b11b"
    env.load_registry(key)
    agents = acct.dispatched_entries(env.fixture[key])
    dispatching, finishing = agents[0], agents[1]
    assert finishing["model"] == "sonnet"

    real_load = act._load_session_stats
    state = {"raced": False}

    def racing_load(session_id: str):
        loaded = real_load(session_id)  # event_preuse now holds a stale whole-session dict
        if not state["raced"]:
            state["raced"] = True
            # Another hook process finishes a dispatched agent right now.
            st.finalize_dispatch_charge(
                {"session_id": session_id, "agent_id": finishing["agent_id"]}, True
            )
        return loaded

    monkeypatch.setattr(act, "_load_session_stats", racing_load)
    env.claude_dispatch(key, dispatching)
    monkeypatch.setattr(act, "_load_session_stats", real_load)
    assert state["raced"]

    stats = env.stats(key)
    assert stats["cumulative_cost_usd"] == pytest.approx(
        acct.nominal_cost_usd(finishing["model"]), abs=1e-6
    ), "event_preuse's stale save must not roll the charge back"
    assert stats["charged_agent_ids"] == [finishing["agent_id"]]
    assert stats["total_agents"] == 1, "event_preuse's own bookkeeping still lands"

    # The preserved ledger still prevents a double charge for that id...
    env.stop(key, finishing["agent_id"])
    assert env.stats(key)["cumulative_cost_usd"] == pytest.approx(
        acct.nominal_cost_usd("sonnet"), abs=1e-6
    )
    # ...and the remaining dispatched agent is charged normally on top.
    env.stop(key, dispatching["agent_id"])
    assert env.stats(key)["cumulative_cost_usd"] == pytest.approx(
        acct.nominal_cost_usd("sonnet") + acct.nominal_cost_usd(dispatching["model"]), abs=1e-6
    )


def test_save_stats_keeps_disk_cost_fields_and_unions_ids(env: Env) -> None:
    key = "sess-1f96"
    a, b, c = _dispatched_ids(env, key)
    big = acct.nominal_cost_usd("sonnet")
    small = acct.nominal_cost_usd("haiku")

    st._save_stats({key: {
        "cumulative_cost_usd": big, "charged_agent_ids": [a, b], "last_activity": now_iso(),
    }})
    # Stale caller: lower cost, different ids -> disk cost kept, ids unioned.
    st._save_stats({key: {
        "cumulative_cost_usd": small, "charged_agent_ids": [c], "last_activity": now_iso(),
    }})
    sess = env.stats(key)
    assert sess["cumulative_cost_usd"] == pytest.approx(big)
    assert sorted(sess["charged_agent_ids"]) == sorted([a, b, c])

    # Cost only grows: a higher caller value still wins.
    st._save_stats({key: {
        "cumulative_cost_usd": big + small, "charged_agent_ids": [c], "last_activity": now_iso(),
    }})
    sess = env.stats(key)
    assert sess["cumulative_cost_usd"] == pytest.approx(big + small)
    assert sorted(sess["charged_agent_ids"]) == sorted([a, b, c])
