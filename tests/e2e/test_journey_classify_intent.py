"""Journey: the classify stage of the classify -> dispatch -> gate -> reward
-> report pipeline (TEST-001, journey 3).

Chains through REAL entrypoints only, no mocks of the system under test:
  classify_intent.classify_intent_detailed / classify_intent (keyword scoring)
  -> classify_intent.classify_work_unit_intent (mechanical vs reasoning signal)
  -> check_routing._tier_of_model_with_event (model/tier extraction fallback
     telemetry, real event emission)

Deliberately scoped to the deterministic keyword-only paths. The semantic
scorer's threshold/fallback behavior (test_intent_classification.py's
SEM-102/103 cases) depends on an embedding model and is NOT re-asserted here
to avoid a flaky e2e journey -- that coverage stays in the legacy file until
TEST-006 migrates it with its own isolation strategy.

Real (non-mocked) behaviors preserved from the legacy suite:
  - tests/e2e/test_extraction_fallback.py: model_none vs tier_unrecognized
    event emission are distinct types, never conflated (Steps 6-7).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_routing as cr  # noqa: E402
import session_state as ss  # noqa: E402
import classify_intent as ci  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    for mod in (cr, ss):
        monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(mod, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
        monkeypatch.setattr(mod, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")
    monkeypatch.setattr(cr, "RESUME_STATE_FILE", tmp_path / "resume_state.json")
    monkeypatch.setattr(cr, "DATA_DIR", tmp_path)
    return tmp_path


def test_classify_stage_journey(monkeypatch):
    sid = "journey-classify-1"
    events: list[dict] = []
    monkeypatch.setattr(cr, "append_event", lambda rec: events.append(rec))

    # Step 1: turn begins on a clear agentic-action prompt.
    cr.event_reset({"session_id": sid, "prompt": "Fix the auth bug in gates.py"}, False)

    # Step 2: real classify_intent_detailed call -- a keyword hit for a
    # non-qa_simple class must return immediately via the "keyword" source,
    # never touching the semantic scorer.
    detailed = ci.classify_intent_detailed("Fix the auth bug in gates.py")
    assert detailed["source"] == "keyword"
    assert detailed["intent"] != "qa_simple"
    assert 0.0 < detailed["confidence"] <= 1.0

    # Step 3: the plain classify_intent entrypoint must agree with
    # classify_intent_detailed's intent for the same text -- both real
    # entrypoints, no shared mutable state between them.
    assert ci.classify_intent("Fix the auth bug in gates.py") == detailed["intent"]

    # Step 4: classify_work_unit_intent -- heavy mechanical signal (reads +
    # writes already observed this turn) must classify as "mechanical".
    mechanical_entry = {
        "prompt_intent": detailed["intent"],
        "read_count": 2,
        "inline_mutations_since_dispatch": 1,
    }
    assert ci.classify_work_unit_intent(mechanical_entry) == "mechanical"

    # Step 5: same function, reasoning-shaped signal (qa intent, no
    # reads/writes yet) must classify as "reasoning".
    reasoning_entry = {
        "prompt_intent": "qa_architecture",
        "read_count": 0,
        "inline_mutations_since_dispatch": 0,
    }
    assert ci.classify_work_unit_intent(reasoning_entry) == "reasoning"

    # Step 6: model extraction fallback -- no model in this call at all ->
    # tier "unknown" and a real extraction_fallback event, reason model_none.
    tier = cr._tier_of_model_with_event(None, session_id=sid)
    assert tier == "unknown"
    assert any(
        e.get("type") == "extraction_fallback" and e.get("reason") == "model_none"
        for e in events
    )

    # Step 7: a real but unrecognized model string must land on a distinct
    # event type (tier_unrecognized), never conflated with model_none.
    events.clear()
    tier2 = cr._tier_of_model_with_event("llama-4-scout-experimental", session_id=sid)
    assert tier2 == "unknown"
    assert any(
        e.get("type") == "tier_unrecognized" and e.get("model") == "llama-4-scout-experimental"
        for e in events
    )
    assert not any(e.get("type") == "extraction_fallback" for e in events)
