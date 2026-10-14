"""Journey: the Bounded Wave Scheduler (BWS) superstep barrier and stall-marker
counter -- the dispatch -> gate handoff detail for a subagent's OWN Task/Agent
fan-out (TEST-001, journey 7). Documented in CLAUDE.md under "Bounded Wave
Scheduler (BWS)": the RC-3 wave barrier is "subagent-inclusive" (a dispatched
worker fanning out its own Task/Agent calls is held to the same concurrency
cap as the orchestrator) and BWS-T2-003's stall-marker counter denies once
progress_ledger.max_stall_markers consecutive tool calls carry no done:/
completed:/remaining:/progress: marker.

Chains through REAL entrypoints only, no mocks of the system under test:
  check_routing.event_subagent_start (real per-conversation subagent count)
  -> check_routing.event_reset / event_scan / event_gate (real Task/Edit
     gate flow, where _wave_barrier_check and _subagent_progress_gate are
     invoked internally at the subagent-context block, check_routing.py:3127-3141)
  -> real transcript-file read-back for the stall marker text.

Shadowing finding (same TICKET-022 pattern already documented in
test_journey_spawn_worthiness.py for _check_dispatch_threshold /
_spawn_worthiness_check / score_complexity): check_routing.py defines its
OWN local _wave_barrier_check (L2929), _subagent_progress_gate (L2882), and
_expire_dead_subagents (L2912) -- but a later `from dispatch_enforcer import
(...)` block (check_routing.py:4129-4180, `# noqa: E402,F811`) re-binds all
three names in check_routing's module namespace to dispatch_enforcer.py's
copies. The LIVE callables (verified below by identity) are dispatch_enforcer's:

  - dispatch_enforcer._wave_barrier_check's cap check uses
    `len(state.get("subagents", {}))` (count of conversation-key entries),
    NOT check_routing's shadowed-out `_effective_subagent_count(...)` per-key
    lookup -- a real behavioral divergence between the dead local def and the
    live imported one, not just a cosmetic duplicate.
  - dispatch_enforcer's barrier-deny path does NOT call `_bump(...)` on deny
    (contrast check_routing.py:3200-3203's main-session `gate_deny_concurrency_cap`
    bump, which subagent-context dispatches never reach) -- pinned in Step 3.
  - Both copies' `_expire_dead_subagents` are logic-identical; no divergence there.

Deliberately does NOT re-assert every _expire_dead_subagents/timeout-expiry
case (wave_timeout_seconds default, exact-boundary expiry, resume_ttl_seconds
non-conflict) -- those are pure-function unit cases already covered by
tests/e2e/test_wave_partial_failure.py and stay there. This journey is the
gate-integration shape one layer up: the real event_gate dispatch chain
hitting the barrier and stall-counter checks together, on the LIVE shadowed
functions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_routing as cr  # noqa: E402
import session_state as ss  # noqa: E402
import dispatch_enforcer as de  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    for mod in (cr, ss):
        monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(mod, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
        monkeypatch.setattr(mod, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")
    monkeypatch.setattr(cr, "RESUME_STATE_FILE", tmp_path / "resume_state.json")


def test_bws_wave_barrier_and_stall_counter_journey(tmp_path, monkeypatch):
    # TICKET-026 candidate: config_get/config_int/_config_cache are truly
    # defined in _config.py, not _common.py (confirmed by reading source).
    # Patching _common._config_cache (the pattern used elsewhere in the
    # legacy suite, e.g. test_wave_partial_failure.py) is a no-op for the
    # real cache those functions read -- patch _config._config_cache directly.
    import _config
    monkeypatch.setattr(_config, "_config_cache", {
        "concurrency": {"max_inflight_subagents": 3, "wave_timeout_seconds": 180},
        "progress_ledger": {"max_stall_markers": 2},
    })

    # Step 1 (shadowing pin): the names check_routing calls are identically the
    # same function objects dispatch_enforcer.py defines -- the local defs at
    # check_routing.py:2882/2912/2929 are dead code, shadowed by the later
    # `noqa: F811` re-import. Everything below exercises the LIVE (imported) copy.
    assert cr._wave_barrier_check is de._wave_barrier_check
    assert cr._subagent_progress_gate is de._subagent_progress_gate
    assert cr._expire_dead_subagents is de._expire_dead_subagents

    sid = "journey-bws-1"
    cr.event_reset({"session_id": sid, "prompt": "fan out parallel read workers"}, False)
    cr.event_scan({"session_id": sid, "text": "Routing: FO — dispatching workers"}, False)

    # Step 2: register 3 live subagents under 3 DIFFERENT conversation keys
    # (session_id doubles as _conversation_key when no parent_conversation_id
    # is present). The live _wave_barrier_check's cap check is
    # `len(state.get("subagents", {}))` -- a count of conversation-key
    # entries -- so 3 distinct keys reach the max_inflight_subagents=3 cap,
    # even though no single conversation has 3 inflight workers itself.
    for i in range(3):
        cr.event_subagent_start({"session_id": f"journey-bws-conv-{i}"}, False)

    state_before = cr._load_state()
    conversation_keys = [k for k in state_before.get("subagents", {}) if not k.startswith("__")]
    assert len(conversation_keys) == 3

    # Step 3: a subagent-context Task dispatch (agent_id present makes
    # _is_subagent_context True) must be denied by the RC-3 wave barrier --
    # check_routing.py:3127-3133, reached only via the subagent-context
    # branch, never the main-session concurrency-cap block at L3194-3203.
    barrier_result = cr.event_gate(
        {
            "session_id": sid,
            "agent_id": "worker-bws-1",
            "tool_name": "Task",
            "tool_input": {"prompt": "spawn a grandchild worker", "model": "haiku"},
        },
        False,
    )
    assert barrier_result.get("permission") == "deny"
    assert "3/3" in (barrier_result.get("agent_message") or "")

    # Step 4: the live dispatch_enforcer._wave_barrier_check deny path does
    # NOT call _bump -- unlike the main-session concurrency-cap deny at
    # check_routing.py:3200-3203, which bumps gate_deny_concurrency_cap.
    # Neither counter should have moved.
    state_after_barrier = cr._load_state()
    assert state_after_barrier.get("counters", {}).get("gate_deny_wave_barrier", 0) == 0
    assert state_after_barrier.get("counters", {}).get("gate_deny_concurrency_cap", 0) == 0

    # Step 5: BWS-T2-003 stall counter -- a real transcript file with an
    # assistant turn carrying no done:/completed:/remaining:/progress: marker,
    # read back via the real chain (_subagent_progress_gate ->
    # _subagent_recent_assistant_text -> transcript_scanner._transcript_path /
    # _read_transcript_tail / _record_role / _record_content / _text_from_content).
    # tool_name is "Edit" (a MUTATING_TOOLS member, unlike "Read") so the call
    # reaches the subagent-context block at all -- "Task"/"Agent" would also
    # additionally hit the wave barrier above, which this step isn't testing.
    transcript_path = tmp_path / "parent-session.jsonl"
    transcript_path.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": "still working, no marker yet"},
    }) + "\n")

    stall_payload = {
        "session_id": sid,
        "agent_id": "worker-bws-2",
        "tool_name": "Edit",
        "transcript_path": str(transcript_path),
    }

    # max_stall_markers=2: the first no-marker call raises stall_count to 1
    # (1 < 2, allowed); the second raises it to 2 (2 >= 2, denied on this
    # same call -- not a third).
    first = cr.event_gate(stall_payload, False)
    assert first.get("permission") == "allow"

    second = cr.event_gate(stall_payload, False)
    assert second.get("permission") == "deny"
    assert "BWS-T2-003" in (second.get("agent_message") or "")

    # Step 6: the deny path resets subagent_progress[agent_id].stall_count
    # back to 0 in the real persisted state -- CLAUDE.md: "A fresh dispatch
    # or a resume clears the counter."
    state_stalled = cr._load_state()
    assert state_stalled.get("subagent_progress", {}).get("worker-bws-2", {}).get("stall_count") == 0

    # Step 7: a progress marker in the transcript resets/avoids the stall
    # count and allows the call through, even immediately after a deny.
    transcript_path.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": "done: fixed the target module"},
    }) + "\n")
    third = cr.event_gate(stall_payload, False)
    assert third.get("permission") == "allow"
    state_recovered = cr._load_state()
    assert state_recovered.get("subagent_progress", {}).get("worker-bws-2", {}).get("stall_count") == 0
