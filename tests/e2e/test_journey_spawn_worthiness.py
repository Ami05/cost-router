"""Journey: the dispatch-stage spawn-worthiness check in the classify ->
dispatch -> gate -> reward -> report pipeline (TEST-001, journey 6).

Chains through REAL entrypoints only, no mocks of the system under test:
  routing_resolver.score_complexity (pure keyword/file-ref scoring)
  -> routing_resolver._spawn_worthiness_check (FO downgrade decision)
  -> check_routing.event_reset / event_scan / event_gate (real Task-tool
     gate flow, where _spawn_worthiness_check is invoked internally at the
     "RC-5" block, imported into check_routing's namespace from
     routing_resolver -- check_routing.py:4070-4097 re-imports and shadows
     its own earlier same-named definitions with noqa: F811).

Not covered by test_journey_classify_intent.py (classify stage) or
test_journey_reward_pipeline.py (reward stage) -- this journey is the
dispatch-decision stage in between: whether a declared FO verdict is worth
spawning a subagent for, given task complexity.

Deliberately narrower than the legacy tests/e2e/test_spawn_worthiness.py:
  - Does NOT re-assert every score_complexity boundary case (long text,
    2-file-ref-not-low, empty-string fail-open, etc.) -- those are pure-
    function unit cases, not journey shape, and stay in the legacy file.
  - Does NOT re-assert the gate_deny_spawn_worthiness counter's exact
    increment-by-one arithmetic across repeated calls -- one real
    counter-incremented assertion (Step 6) is kept as the field-completeness-
    style check; repeated-call arithmetic is legacy-file territory.
  - Does NOT call check_routing._spawn_worthiness_check directly as a second
    "unit" layer on top of the real gate chain -- Step 2 below calls
    routing_resolver's copy directly (the same object check_routing imports),
    which is enough to pin the pure-decision contract before chaining into
    the real gate.

Real (non-mocked) behaviors preserved from the legacy suite:
  - tests/e2e/test_spawn_worthiness.py: low-complexity FO -> should_deny=True
    with "RC-5" and "DIRECT" in the message (Step 2).
  - tests/e2e/test_spawn_worthiness.py: verification commands ("Run: ...")
    are exempt from the gate even at FO + low complexity (Step 5).
  - tests/e2e/test_spawn_worthiness.py: DIRECT role is a no-op for this gate
    -- Edit still allowed (Step 7).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_routing as cr  # noqa: E402
import session_state as ss  # noqa: E402
import routing_resolver as rr  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    for mod in (cr, ss):
        monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(mod, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
        monkeypatch.setattr(mod, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")
    monkeypatch.setattr(cr, "RESUME_STATE_FILE", tmp_path / "resume_state.json")


def test_spawn_worthiness_journey():
    sid = "journey-spawn-1"

    # Step 1: real score_complexity call -- a short, single-file, purely
    # mechanical intent must score 'low' (the precondition for downgrade).
    low_text = "rename symbol in one file"
    assert rr.score_complexity(low_text) == "low"

    # Step 2: real _spawn_worthiness_check call (the same function object
    # check_routing imports and calls internally) -- FO + low complexity
    # must be denied with an actionable RC-5/DIRECT message.
    direct_check = rr._spawn_worthiness_check(low_text, "Routing: FO — dispatching worker")
    assert direct_check.get("should_deny") is True
    assert "RC-5" in direct_check.get("message", "")
    assert "DIRECT" in direct_check.get("message", "")

    # Step 3: chain into the real gate flow -- turn begins with the same
    # low-complexity intent as the declared prompt.
    cr.event_reset({"session_id": sid, "prompt": low_text}, False)

    # Step 4: real event_scan records the FO verdict for this turn, exactly
    # as the assistant's "Routing: FO — ..." line would.
    cr.event_scan({"session_id": sid, "text": "Routing: FO — dispatching worker"}, False)

    # Step 5: real event_gate on a Task dispatch -- must be denied by the
    # RC-5 spawn-worthiness block before it ever reaches concurrency checks.
    gate_result = cr.event_gate(
        {"session_id": sid, "tool_name": "Task", "tool_input": {"prompt": low_text, "model": "haiku"}},
        False,
    )
    assert gate_result.get("permission") == "deny"
    assert "RC-5" in (gate_result.get("agent_message") or "")

    # Step 6: the real gate_deny_spawn_worthiness counter, read back off the
    # actual persisted state file, must reflect this denial.
    state = cr._load_state()
    assert state.get("counters", {}).get("gate_deny_spawn_worthiness", 0) >= 1

    # Step 7: a verification command ("Run: ...") is exempt even at FO +
    # low complexity -- must not be denied by the spawn-worthiness message.
    verify_sid = "journey-spawn-2"
    cr.event_reset({"session_id": verify_sid, "prompt": "Run: pytest tests/ -x -q"}, False)
    cr.event_scan({"session_id": verify_sid, "text": "Routing: FO — verification"}, False)
    verify_gate = cr.event_gate(
        {
            "session_id": verify_sid,
            "tool_name": "Task",
            "tool_input": {"prompt": "Run: pytest tests/ -x -q", "model": "haiku"},
        },
        False,
    )
    assert "RC-5" not in (verify_gate.get("agent_message") or "")

    # Step 8: DIRECT role is a no-op for this gate -- a plain Edit on the
    # same low-complexity turn must still be allowed through unaffected.
    direct_sid = "journey-spawn-3"
    cr.event_reset({"session_id": direct_sid, "prompt": low_text}, False)
    cr.event_scan({"session_id": direct_sid, "text": "Routing: DIRECT — simple edit"}, False)
    direct_result = cr.event_gate({"session_id": direct_sid, "tool_name": "Edit"}, False)
    assert direct_result.get("permission") == "allow"
