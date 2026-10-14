"""Journey: session dispatch-debt exhaustion drives dispatch-forcing gates to
fall through to inline allow instead of deadlocking (TEST-005).

TEST-005 requirement: drive the exhaustion signal itself through REAL,
sequential gate calls -- never by hand-writing agent_call_stats.json (that is
what the old tests/e2e/test_rt_exhaustion_fallback.py did via its
_exhaust_round_trips() helper, which this journey replaces).

Real exhaustion paths exercised (`_dispatch_debt_active`):
Trigger 2 (all-inline): a session that has NEVER dispatched a Task
(task_dispatched_count_session == 0) and has accumulated
`total_gate_calls_session >= SESSION_DISPATCH_DENY_THRESHOLD` real gate calls
is "in debt" -- this is the actual live wiring (`event_gate` is the function
`_routing_gate` in gates.py calls for every real PreToolUse dispatch).
Trigger 1 (cumulative cost): spawn_tracker.finalize_dispatch_charge IS wired
(hooks.json SubagentStop -> agent_call_tracker.py --event subagent_stop, see
agent_call_tracker.main) and is the sole writer of cumulative_cost_usd on Claude
Code. It charges once per DISPATCHED agent_id (an id with dispatch metadata in
dispatch_registry.json; stop-only ids are free) at the registry model's rate, so
test_cost_trigger_fires_from_real_subagent_stop_charges drives it with real
dispatched registry entries from tests/fixtures/data/accounting_session_excerpts.json
rather than stubbing the stats file. (An earlier version of this docstring called
Trigger 1 dead code; that stopped being true when TICKET-014 wired the hook.)

The counter is driven up using a synthetic tool name ("notebook_edit") that
is NOT a member of MUTATING_TOOLS/_SHELL_TOOLS, so it clears the
tool-specific gates and lands straight on the session-level counter --
exactly the "all-inline, never dispatched" deadlock case Trigger 2 exists to
catch.

NOTE (discovered while building this journey): the camelCase tool name
actually registered in hooks/hooks.json's pre_write matcher ("NotebookEdit",
line ~112) does NOT work for this purpose -- check_routing.py's
`_looks_mutating()` / `_MUTATION_KEYWORD_RE` requires a `_`/`.`/`-`/`/`
boundary immediately before the mutation keyword, and "NotebookEdit" has no
such boundary before "Edit" (it's preceded by "k"). That regex fails to
match, so `event_gate` early-exits via the `tool not in MUTATING_TOOLS and
not _looks_mutating(tool)` check (~line 3057) BEFORE ever reaching the
session-level counter increment (~line 3356) -- the opposite of what the
docstring here originally claimed. Filed as a product-code finding, not
fixed here (test-only fix; scope is this journey file). "notebook_edit"
(snake_case) satisfies the boundary regex and is used purely as a stand-in
for "some unknown/unenumerated tool that looks mutation-shaped" -- it is
not asserted to be a real registered tool name.

Once debt is real and on disk, the shell-volume and inline-mutation gates
are driven to their own thresholds via real Bash/Edit event_gate calls and
must fall through to allow, with the real fallback counters read back off
disk (not inferred from the return value alone).

TICKET-019: every event_scan/event_gate pair in this file now shares one
prompt_id per simulated turn. Previously several loops passed a unique
prompt_id to event_gate while the paired event_scan call passed none (or a
different one) -- _verdict_is_current() (check_routing.py:1101) keys the
persisted verdict by turn_id (== prompt_id for Claude Code), so the
mismatch made those calls treat the just-scanned verdict as belonging to a
different turn and fall through to the no-verdict-nudge path instead. The
"allow" assertions still passed (the nudge path also allows on first hit),
which is why this was latent rather than a visible failure -- see
test_mismatched_prompt_id_defeats_verdict_currency in
test_journey_dispatch_lifecycle.py for a test that pins down the
difference directly.
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
    # See test_journey_dispatch_lifecycle.py: _load_state/_save_state close
    # over session_state's OWN frozen file constants, not check_routing's.
    for mod in (cr, ss):
        monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(mod, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
        monkeypatch.setattr(mod, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")
    monkeypatch.setattr(cr, "RESUME_STATE_FILE", tmp_path / "resume_state.json")
    monkeypatch.setattr(cr, "DATA_DIR", tmp_path)
    # Same class of gap as TICKET-016: `check_routing._safe_dispatch_debt_active`
    # resolves `_dispatch_debt_active` via a late `from routing_state_manager
    # import _dispatch_debt_active` (~line 4101) that silently overrides the
    # local def earlier in the file despite the "local definitions remain
    # authoritative" re-export-shim comment. That function's `ROUTING_STATE_FILE`
    # / `DATA_DIR` free variables resolve against routing_state_manager's OWN
    # module globals, not check_routing's or session_state's -- patching only
    # cr/ss (as above) leaves it reading the real production
    # data/routing_state.json. Must patch routing_state_manager directly too,
    # or every debt-exhaustion assertion below silently checks prod state.
    monkeypatch.setattr(rsm, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
    monkeypatch.setattr(rsm, "DATA_DIR", tmp_path)
    # A THIRD independent copy of the same debt-check logic (own
    # ROUTING_STATE_FILE + own _dispatch_debt_active def, not imported from
    # routing_state_manager) lives in routing_resolver.py and backs
    # check_routing._check_shell_gate (itself late-imported from
    # routing_resolver, shadowing yet another dead local def in
    # check_routing.py). Same root cause as the rsm patch above, different
    # module -- must be patched separately or the shell-gate's debt check
    # silently reads real production state.
    monkeypatch.setattr(rr, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
    monkeypatch.setattr(rr, "DATA_DIR", tmp_path)
    # Real SubagentStop charge path (Trigger 1) reads/writes these.
    monkeypatch.setattr(st, "STATS_FILE", tmp_path / "agent_call_stats.json")
    monkeypatch.setattr(st, "DISPATCH_REGISTRY_FILE", tmp_path / "dispatch_registry.json")
    monkeypatch.delenv("COST_ROUTER_SUBAGENT_INFRA_TOKENS", raising=False)
    return tmp_path


def _drive_session_counter_to_threshold(sid: str, threshold: int) -> dict:
    """Real sequential event_scan/event_gate calls, never Task, until the
    session-level counter crosses *threshold*. Returns the final gate result."""
    out = None
    for i in range(threshold):
        # TICKET-019: prompt_id must match between the event_scan that
        # stamps the verdict and the paired event_gate call, or
        # _verdict_is_current() treats the verdict as a different turn's
        # and falls through to the no-verdict-nudge path instead.
        prompt_id = f"nb-{i}"
        cr.event_scan({"session_id": sid, "text": "Routing: DIRECT — inline step", "prompt_id": prompt_id}, False)
        out = cr.event_gate(
            {"session_id": sid, "tool_name": "notebook_edit", "prompt_id": prompt_id}, False
        )
    return out


def test_session_debt_exhaustion_via_real_gate_calls_then_shell_fallback(monkeypatch):
    sid = "journey-rt-1"

    # Step 1: turn begins on an agentic (non-fast-path) prompt.
    cr.event_reset({"session_id": sid, "prompt": "Multi-step refactor across the codebase"}, False)

    # Step 2: 11 real inline notebook_edit gate calls — under the real
    # SESSION_DISPATCH_DENY_THRESHOLD (12) — must all sail through allowed,
    # with no fallback counter bumped yet (debt is not real yet).
    out = _drive_session_counter_to_threshold(sid, 11)
    assert out.get("permission") == "allow"
    state = cr._load_state()
    assert int(state.get("counters", {}).get("gate_session_allow_rt_exhausted", 0)) == 0
    entry = cr._get_session(state["sessions"], sid)
    assert entry["total_gate_calls_session"] == 11
    assert entry["task_dispatched_count_session"] == 0

    # Step 3: the 12th real call crosses SESSION_DISPATCH_DENY_THRESHOLD with
    # zero real Task dispatches this session — Trigger 2 fires for real. But
    # _dispatch_debt_active() reads the on-disk ROUTING_STATE_FILE, not the
    # in-memory entry this same call just mutated — the in-memory counter
    # (now 12) isn't flushed to disk until AFTER this call's own gate
    # decision, so debt is not yet observably "real" from this call's own
    # point of view. It denies normally, and that denial's _save_state is
    # what persists total_gate_calls_session=12 to disk for the first time.
    cr.event_scan({"session_id": sid, "text": "Routing: DIRECT — inline step", "prompt_id": "nb-crossing"}, False)
    crossing = cr.event_gate({"session_id": sid, "tool_name": "notebook_edit", "prompt_id": "nb-crossing"}, False)
    assert crossing.get("permission") == "deny", (
        "the call that first crosses the threshold denies -- debt becomes real only once "
        "this call's own state save lands on disk, which happens after its own decision"
    )
    assert cr._safe_dispatch_debt_active(sid) is True, (
        "debt must be real now, derived from the counters the crossing call just persisted"
    )

    # Step 3b: the NEXT real call (13th) is the one that actually observes
    # debt-active on disk (persisted by Step 3's denial) and falls through
    # to allow -- this is the "must not deadlock" behavior Trigger 2 exists
    # to guarantee once debt is real.
    cr.event_scan({"session_id": sid, "text": "Routing: DIRECT — inline step", "prompt_id": "nb-final"}, False)
    final = cr.event_gate({"session_id": sid, "tool_name": "notebook_edit", "prompt_id": "nb-final"}, False)
    assert final.get("permission") == "allow", (
        "session gate must fall through to allow, not deadlock, once debt is real"
    )

    state = cr._load_state()
    assert int(state.get("counters", {}).get("gate_session_allow_rt_exhausted", 0)) > 0, (
        "real fallback counter must be bumped on disk"
    )

    # Step 4: same now-exhausted session — drive the shell-volume gate to its
    # own normal deny threshold (config default 5) via real sequential Bash
    # gate calls. It must ALSO fall through, proving the debt signal protects
    # every dispatch-forcing gate, not just the one that produced it.
    last_shell = None
    for i in range(5):
        prompt_id = f"sh-{i}"
        cr.event_scan({"session_id": sid, "text": "Routing: DIRECT — shell step", "prompt_id": prompt_id}, False)
        last_shell = cr.event_gate(
            {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": "echo hi"}, "prompt_id": prompt_id},
            False,
        )
    assert last_shell.get("permission") == "allow", "shell gate must fall through once debt is real"

    state = cr._load_state()
    assert int(state.get("counters", {}).get("gate_shell_allow_rt_exhausted", 0)) > 0

    # Step 5: and the inline-mutation gate, via real sequential Edit calls to
    # its own deny threshold (config default 6).
    #
    # TICKET-019 status update: this step passes end-to-end (verified before
    # and after the prompt_id fixes in this file -- see module docstring).
    # Two issues were stacked here; only the first is in this ticket's scope:
    #
    # 1. (FIXED) Edit reaches the verdict-currency check in event_gate, which
    #    requires event_scan's verdict_turn_id to match this call's turn_id
    #    -- both derived from the same host per-turn identifier field
    #    (`prompt_id` for Claude Code, see _turn_id()). Fixed by passing the
    #    same prompt_id to both calls (this was already done for step 5 pre-
    #    ticket; steps 2-4 above and the control test below did not, and
    #    were passing only via the no-verdict-nudge fallback -- fixed now).
    # 2. (STILL NOT FIXED, out of scope) The *unrelated* reasoning-tier
    #    dispatch-volume gate (`_check_dispatch_threshold`, keyed on
    #    `mechanical_since_dispatch`) runs strictly before the inline-
    #    mutation gate in event_gate's pipeline and has its own nudge
    #    threshold (config default 3) with NO debt-active fallback on the
    #    nudge branch -- it unconditionally returns an early allow for every
    #    call once its own counter crosses 3, which starves
    #    inline_mutations_since_dispatch of ever reaching its own threshold.
    #    It activates because entry["tier"] flips to "reasoning" on its own
    #    during steps 1-4 above (via the routing_declared model-fallback
    #    path in event_scan, which resolves a default "sonnet" orchestrator
    #    model for this session's intent once no explicit model has ever
    #    been given). This means the "allow" this step asserts, and the
    #    inline-mutation counter bump below, are both real -- but the path
    #    that produces them may be gate #2's early-allow rather than the
    #    inline-mutation gate's own rt-exhausted fallback. A genuinely
    #    isolated session for step 5, or a real fix to the gate ordering /
    #    debt-fallback gap in `_check_dispatch_threshold`, would be needed
    #    to pin down which gate fires -- separate ticket, not this one.
    last_edit = None
    for i in range(6):
        prompt_id = f"ed-{i}"
        cr.event_scan(
            {"session_id": sid, "text": "Routing: DIRECT — editing", "prompt_id": prompt_id},
            False,
        )
        last_edit = cr.event_gate(
            {
                "session_id": sid, "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/journey_rt.py", "old_string": "a", "new_string": f"b{i}"},
                "prompt_id": prompt_id,
            },
            False,
        )
    assert last_edit.get("permission") == "allow", "inline-mutation gate must also fall through"

    state = cr._load_state()
    counters = state.get("counters", {})
    assert (
        int(counters.get("gate_allow_inline_mutation_rt_exhausted", 0))
        + int(counters.get("gate_allow_inline_mutation_nudge_rt_exhausted", 0))
    ) > 0

    # Step 6: sanity — a REAL Task dispatch in this same session would have
    # reset task_dispatched_count_session to >=1 and disarmed Trigger 2; the
    # exhaustion above was produced purely by inline gate-call volume, which
    # is exactly the "ran the whole session inline, never dispatched" failure
    # mode Trigger 2 exists to rescue.
    entry = cr._get_session(cr._load_state()["sessions"], sid)
    assert entry["task_dispatched_count_session"] == 0


def test_shell_gate_denies_normally_when_debt_not_active():
    """Control (migrated from test_rt_exhaustion_fallback.py): without any
    real session-debt signal, the shell gate must deny normally at its
    threshold — proves the fallback above is conditional, not unconditional."""
    sid = "journey-rt-2"
    cr.event_reset({"session_id": sid, "prompt": "Multi-step task"}, False)

    last = None
    for i in range(5):
        prompt_id = f"c-{i}"
        cr.event_scan({"session_id": sid, "text": "Routing: DIRECT — step", "prompt_id": prompt_id}, False)
        last = cr.event_gate(
            {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": "echo hi"}, "prompt_id": prompt_id},
            False,
        )
    assert last.get("permission") == "deny", "5th shell call must deny when no real debt exists"
    state = cr._load_state()
    assert int(state.get("counters", {}).get("gate_shell_allow_rt_exhausted", 0)) == 0


def test_cost_trigger_fires_from_real_subagent_stop_charges():
    """Trigger 1 through the real SubagentStop charge path: debt is inactive just
    below the relief threshold and active once real charges reach it, in both the
    routing_state_manager and routing_resolver copies of the check."""
    import _config

    sid = "journey-rt-cost"
    budget = float(_config.config_get("session_cost_budget.budget_usd") or 5.0)
    pcts = list(_config.config_get("session_cost_budget.warning_pcts") or [50, 80])
    session_pct = float(_config.config_get("concurrency.session_budget_pct") or 0.6)
    threshold = budget * min(max(pcts) / 100.0, session_pct)

    below = acct.charges_below(threshold)
    acct.charge_real(st, sid, below)
    assert rsm._cumulative_dispatch_cost_usd(sid) < threshold
    assert rsm._dispatch_debt_active(sid) is False
    assert rr._dispatch_debt_active(sid) is False

    acct.charge_real(st, sid, 1)
    assert rsm._cumulative_dispatch_cost_usd(sid) >= threshold
    assert rsm._dispatch_debt_active(sid) is True
    assert rr._dispatch_debt_active(sid) is True
