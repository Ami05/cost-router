"""Journey: a single orchestrator turn's full dispatch lifecycle.

Chains, through REAL entrypoints only (no mocks of the system under test):
  check_routing.event_reset -> event_scan -> event_gate(Edit) ->
  event_gate(Task, real worker-schema prompt) -> event_subagent_start
  (dispatch_worker_declared correlation) -> event_subagent_stop ->
  a second inline Edit proving the inline-mutation counter reset on dispatch.

Migrated from (TEST-001):
  - tests/e2e/test_routing_gate.py::test_direct_verdict_allows_edit
  - tests/e2e/test_routing_gate.py::test_task_dispatch_resets_inline_mutation_counter
  - tests/e2e/test_routing_gate.py::test_dispatch_worker_correlation_sequential_match
  - tests/e2e/test_routing_gate.py::test_verdict_resets_on_new_turn
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_routing as cr  # noqa: E402
import session_state as ss  # noqa: E402

_TESTS_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    # _load_state/_save_state (imported into check_routing's namespace from
    # session_state.py) close over session_state's OWN module-level file
    # constants, not check_routing's — both module sets must be patched or
    # the test silently falls through to the real on-disk state file.
    for mod in (cr, ss):
        monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(mod, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
        monkeypatch.setattr(mod, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")
    monkeypatch.setattr(cr, "RESUME_STATE_FILE", tmp_path / "resume_state.json")
    return tmp_path


def test_full_turn_dispatch_lifecycle(monkeypatch):
    """Step through one orchestrator turn end-to-end using only real event_* calls."""
    sid = "journey-dl-1"
    captured: list = []
    monkeypatch.setattr(cr, "append_event", lambda rec: captured.append(rec))

    # Step 1: new turn begins — real event_reset.
    cr.event_reset({"session_id": sid, "prompt": "Refactor auth module across files"}, False)

    # Step 2: orchestrator declares DIRECT and edits inline — real event_scan + event_gate.
    # TICKET-019: event_scan's verdict is keyed by turn_id (== prompt_id for
    # Claude Code, see _turn_id()) and is only "current" for a later
    # event_gate call carrying that SAME prompt_id (_verdict_is_current at
    # check_routing.py:1101). Passing prompt_id to event_gate but not the
    # paired event_scan made the two calls disagree on turn_id (None vs
    # "turn-1"), so the assertions below were coincidentally passing via the
    # no-verdict-nudge fallback (still an "allow", but not exercising the
    # verdict-currency path this journey claims to test) rather than a real
    # current-verdict match. Every event_scan/event_gate pair below now
    # shares one prompt_id per turn.
    cr.event_scan({"session_id": sid, "text": "Routing: DIRECT — quick edit", "prompt_id": "turn-1"}, False)
    out = cr.event_gate({"session_id": sid, "tool_name": "Edit", "prompt_id": "turn-1"}, False)
    assert out.get("permission") == "allow", "step 2: DIRECT edit must be allowed"

    # Step 3: work grows — orchestrator switches to FO and dispatches a real
    # mutation-worker Task with a real JSON dispatch prompt.
    cr.event_scan({"session_id": sid, "text": "Routing: FO — dispatching worker", "prompt_id": "turn-2"}, False)
    prompt = (
        '{"worker":"mutation","intent":"fix bug in gates.py",'
        '"files":["scripts/gates.py:1"],"pattern":"do it","output":"targeted_edits"}'
    )
    out = cr.event_gate(
        {"session_id": sid, "tool_name": "Task", "tool_input": {"prompt": prompt}, "prompt_id": "turn-2"},
        False,
    )
    assert out.get("permission") == "allow", "step 3: FO Task dispatch must be allowed"

    # Step 4: the real SubagentStart hook fires for the dispatched worker — the
    # worker/intent parsed at PreToolUse time must correlate to this agent_id.
    cr.event_subagent_start({"session_id": sid, "agent_id": "sub-dl-1"}, False)
    decl = [r for r in captured if r["type"] == "dispatch_worker_declared"][0]
    assert decl["worker"] == "mutation"
    assert decl["intent"] == "fix bug in gates.py"

    # Step 5: real SubagentStop closes the loop — must not raise, must fail open.
    stop_out = cr.event_subagent_stop({"session_id": sid, "agent_id": "sub-dl-1"}, False)
    assert isinstance(stop_out, dict)

    # Step 6: back in the orchestrator, the Task dispatch must have reset the
    # inline-mutation counter — next inline Edit under a fresh DIRECT is allowed.
    cr.event_scan({"session_id": sid, "text": "Routing: DIRECT — post-dispatch tidy", "prompt_id": "turn-3"}, False)
    out = cr.event_gate({"session_id": sid, "tool_name": "Edit", "prompt_id": "turn-3"}, False)
    assert out.get("permission") == "allow", "step 6: post-dispatch inline edit must be allowed"

    # Step 7: a genuinely new turn (event_reset) invalidates the stale verdict.
    # The real gate always gives a first-in-turn benefit-of-the-doubt nudge
    # (allow), then denies a REPEAT no-verdict call within that same host
    # turn (turn_id dedup) — exercise both calls to prove the escalation.
    cr.event_reset({"session_id": sid, "prompt": "Another refactor needed"}, False)
    nonexistent = str(_TESTS_DIR / "nonexistent.jsonl")
    first = cr.event_gate(
        {"session_id": sid, "tool_name": "Write", "transcript_path": nonexistent, "prompt_id": "turn-4"},
        False,
    )
    assert first.get("permission") == "allow", "step 7a: first no-verdict hit nudges, doesn't deny"
    out = cr.event_gate(
        {"session_id": sid, "tool_name": "Write", "transcript_path": nonexistent, "prompt_id": "turn-4"},
        False,
    )
    assert out.get("permission") == "deny", "step 7b: repeat no-verdict hit in same turn must deny"
    assert "Routing:" in out.get("agent_message", "")

    # Step 8: sanity — the real routing state on disk reflects the turn history
    # (task_dispatched_count_session bumped by the real Task dispatch in step 3).
    state = cr._load_state()
    entry = cr._get_session(state["sessions"], sid)
    assert int(entry.get("task_dispatched_count_session", 0)) >= 1

    # Step 9 (TICKET-019): steps 2/3/6 above must have been allowed via the
    # real current-verdict path (matching prompt_id on both calls) -- the
    # "gate_allow_routed" counter (check_routing.py:3771) only bumps once a
    # verdict was honored, never via the no-verdict-nudge/no-transcript
    # fallback that step 7 (deliberately, on a fresh turn with no verdict)
    # drives instead.
    counters = state.get("counters", {})
    assert int(counters.get("gate_allow_routed", 0)) == 3, (
        "steps 2, 3, and 6 must each be a real verdict-honored allow"
    )
    assert int(counters.get("gate_nudge_no_transcript", 0)) == 1, "step 7a's deliberate no-verdict hit"
    assert int(counters.get("gate_deny_no_transcript_repeat", 0)) == 1, "step 7b's deliberate repeat-deny"


def test_mismatched_prompt_id_defeats_verdict_currency(monkeypatch):
    """TICKET-019 regression guard: if a caller passes a prompt_id to
    event_gate that does NOT match the prompt_id the paired event_scan call
    stamped the verdict with, _verdict_is_current() (check_routing.py:1101)
    treats the verdict as stale for a DIFFERENT turn and falls through to
    the no-verdict-nudge path instead of honoring the real verdict -- the
    exact bug this ticket fixes call sites for. This proves the failure
    mode exists (so a future regression here is caught) and that matching
    prompt_id avoids it."""
    sid = "journey-dl-mismatch"
    cr.event_reset({"session_id": sid, "prompt": "Fix a small bug"}, False)

    # Mismatched: event_scan stamps verdict_turn_id via a different prompt_id
    # than the paired event_gate call uses.
    cr.event_scan({"session_id": sid, "text": "Routing: DIRECT — fix", "prompt_id": "scan-1"}, False)
    mismatched = cr.event_gate({"session_id": sid, "tool_name": "Edit", "prompt_id": "gate-1"}, False)
    assert mismatched.get("permission") == "allow", "still allowed, but only via the nudge fallback"
    state = cr._load_state()
    counters = state.get("counters", {})
    assert int(counters.get("gate_allow_routed", 0)) == 0, (
        "mismatched prompt_id must NOT be treated as a current verdict -- no verdict-honored allow"
    )
    assert int(counters.get("gate_nudge_no_transcript", 0)) == 1, (
        "must instead fall through to the no-verdict/no-transcript nudge fallback"
    )

    # Matched: same prompt_id on both calls -- real verdict-currency path.
    # Counters are shared/cumulative across this test function's calls
    # (same on-disk state file), so compare the fallback counter's delta
    # across just this second call, not its absolute value.
    nudge_before = int(counters.get("gate_nudge_no_transcript", 0))
    sid2 = "journey-dl-matched"
    cr.event_reset({"session_id": sid2, "prompt": "Fix a small bug"}, False)
    cr.event_scan({"session_id": sid2, "text": "Routing: DIRECT — fix", "prompt_id": "turn-x"}, False)
    matched = cr.event_gate({"session_id": sid2, "tool_name": "Edit", "prompt_id": "turn-x"}, False)
    assert matched.get("permission") == "allow"
    state2 = cr._load_state()
    counters2 = state2.get("counters", {})
    assert int(counters2.get("gate_allow_routed", 0)) == 1, (
        "matching prompt_id must carry the verdict over as a real verdict-honored allow"
    )
    assert int(counters2.get("gate_nudge_no_transcript", 0)) == nudge_before, (
        "matching prompt_id must not need the no-transcript/no-verdict fallback"
    )
