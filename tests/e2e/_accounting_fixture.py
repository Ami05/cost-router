"""Shared helpers for dispatch-cost accounting tests (real-fixture only).

Fixture: tests/fixtures/data/accounting_session_excerpts.json, excerpts of real
sessions from production agent_call_stats.json / dispatch_registry.json.

On Claude Code spawn_tracker.finalize_dispatch_charge (SubagentStop) is the sole
writer of cumulative_cost_usd; it charges once per dispatched agent_id, at the
rate of the model recorded in the dispatch registry. These helpers drive that
path with real registry entries and compute the nominal expectation straight
from config.json and scripts/pricing.json, independent of the code under test.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "data" / "accounting_session_excerpts.json"


def load_fixture() -> dict[str, Any]:
    return copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8"))["sessions"])


def is_dispatched(entry: dict[str, Any]) -> bool:
    """Same notion finalize uses: dispatch metadata present."""
    return bool(entry.get("started_at") or entry.get("subagent_type"))


def dispatched_entries(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Dispatched registry entries in recorded start order."""
    agents = [a for a in session["registry"]["agents"].values() if is_dispatched(a)]
    return sorted(agents, key=lambda a: a["started_at"])


def stop_only_ids(session: dict[str, Any]) -> list[str]:
    return [aid for aid, a in session["registry"]["agents"].items() if not is_dispatched(a)]


def nominal_cost_usd(model: str | None) -> float:
    """Nominal cold-write cost: infra tokens/1000 * cache_write_per_1k.

    Read directly from config.json / pricing.json (tier: haiku -> fast,
    sonnet/opus -> reasoning, anything else -> default).
    """
    cfg = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
    tokens_by_tier = cfg["dispatch_context"]["subagent_infra_tokens"]
    pricing = json.loads((REPO / "scripts" / "pricing.json").read_text(encoding="utf-8"))
    name = (model or "").lower()
    if "haiku" in name:
        tier, price_key = "fast", "haiku"
    elif "sonnet" in name:
        tier, price_key = "reasoning", "sonnet"
    elif "opus" in name:
        tier, price_key = "reasoning", "opus"
    else:
        tier, price_key = "default", "default"
    return tokens_by_tier[tier] / 1000.0 * pricing[price_key]["cache_write_per_1k"]


def pooled_templates() -> list[dict[str, Any]]:
    """All real dispatched registry entries across fixture sessions, fixed order."""
    out: list[dict[str, Any]] = []
    for key in sorted(load_fixture()):
        out.extend(dispatched_entries(load_fixture()[key]))
    return out


def charge_unit_models() -> list[str | None]:
    """Model of the i-th charge made by charge_real (cycles through templates)."""
    return [t.get("model") for t in pooled_templates()]


def charge_costs(n: int) -> list[float]:
    models = charge_unit_models()
    return [nominal_cost_usd(models[i % len(models)]) for i in range(n)]


def charges_below(cap_usd: float) -> int:
    """Largest n such that the first n charge_real charges total strictly < cap."""
    total, n = 0.0, 0
    while True:
        nxt = charge_costs(n + 1)[-1]
        if round(total + nxt, 6) >= cap_usd:
            return n
        total = round(total + nxt, 6)  # finalize stores round(sum, 6)
        n += 1


def write_registry(registry_file: Path, sid: str, agents: dict[str, dict[str, Any]]) -> None:
    """Merge *agents* into the registry file under *sid* (creating it)."""
    try:
        reg = json.loads(registry_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        reg = {}
    reg.setdefault("sessions", {}).setdefault(sid, {}).setdefault("agents", {}).update(agents)
    registry_file.write_text(json.dumps(reg), encoding="utf-8")


def charge_real(spawn_tracker: Any, sid: str, n: int) -> None:
    """n further real SubagentStop charges for *sid*.

    Each charge uses a real dispatched registry entry (cycled, new unique id)
    and goes through spawn_tracker.finalize_dispatch_charge. Continues the
    sequence from what the session was already charged, so repeated calls never
    collide on agent ids (once-per-id accounting).
    """
    from _common import load_json_state, now_iso  # noqa: PLC0415

    stats_file: Path = spawn_tracker.STATS_FILE
    all_sessions = load_json_state(stats_file).get("sessions", {})
    if not isinstance(all_sessions, dict):
        all_sessions = {}
    this_session = all_sessions.get(sid)
    if not isinstance(this_session, dict):
        this_session = {}
    this_session["last_activity"] = now_iso()
    all_sessions[sid] = this_session
    spawn_tracker._save_stats(all_sessions)

    done = len(load_json_state(stats_file)["sessions"][sid].get("charged_agent_ids", []))
    templates = pooled_templates()
    new_agents: dict[str, dict[str, Any]] = {}
    ids: list[str] = []
    for i in range(done, done + n):
        aid = f"charge-{i}"
        entry = copy.deepcopy(templates[i % len(templates)])
        entry["agent_id"] = entry["subagent_id"] = aid
        new_agents[aid] = entry
        ids.append(aid)
    write_registry(spawn_tracker.DISPATCH_REGISTRY_FILE, sid, new_agents)
    for aid in ids:
        spawn_tracker.finalize_dispatch_charge({"session_id": sid, "agent_id": aid}, True)
