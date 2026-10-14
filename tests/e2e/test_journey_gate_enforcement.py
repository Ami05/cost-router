"""Journey: scope and orchestrator-tier gate enforcement in the
reset -> scan -> gate cycle (TEST-001, journey 8, final journey).

Chains through REAL entrypoints only, no mocks of the system under test:
  check_routing.event_reset / event_scan / event_gate (real hook event
  functions -- same objects the PreToolUse/UserPromptSubmit hooks call).

Covers three gate behaviors not exercised by journeys 1-7 (dispatch
lifecycle, rt-exhaustion, classify, reward, report, spawn-worthiness, BWS
wave barrier):
  (a) PROD-20 (break-020) out-of-scope mutation denial -- a mutation target
      not named anywhere in prompt_files/assistant text is denied.
  (b) RD-6 FW scope guard -- >fw_scope_file_limit files edited under a
      single FW declaration is denied.
  (c) OC-1 / orchestrator_ceiling gate (the gate filed as TICKET-021 this
      session) -- session model/routing_matrix.orchestrator tier mismatch
      nudges twice then hard-denies on the routing.orchestrator_nudge_deny_after
      (config.json default: 3) recurrence.

Real (non-mocked) behaviors confirmed by reading scripts/check_routing.py
and scripts/dispatch_enforcer.py directly (the latter re-imports and
shadows same-named local defs earlier in check_routing.py per the
TICKET-022 warning at check_routing.py:4028-4040 -- `_out_of_scope_deny_message`,
`_fw_scope_deny_message`, `_target_is_in_scope`, and `_mutation_target_file`
all have dead local copies above that block; the versions actually called
at runtime are dispatch_enforcer.py's, confirmed byte-for-byte identical to
the local copies for these four):
  - `_target_is_in_scope` (dispatch_enforcer.py:456) checks prompt_files
    exact/derived/stem-containment, then falls back to filenames named in
    current-turn assistant text -- Step 2 picks target/mentioned stems
    ("billing.py" vs "auth.py") that don't overlap under stem-containment,
    so the deny is deterministic.
  - RD-6's FW scope guard (check_routing.py:3652-3663) fires on
    `role == "FW"` and `tool in DISPATCH_MECHANICAL_TOOLS`, keyed off
    `config_int("routing.fw_scope_file_limit", 5)` -- config.json's
    committed value is also 5, exercised at the real default (Step 4-5).
  - OC-1 (check_routing.py:3599-3644) reads `normalize_payload(payload).model`
    -- a TOP-LEVEL `model` key on the hook payload (the session/orchestrator
    model the harness stamps on every call), NOT `tool_input.model` and NOT
    `model_of(payload)`. This is the TICKET-021 finding: the legacy unit
    test `test_oc1_mismatch_escalates_to_deny_on_third_recurrence` in
    test_routing_gate.py monkeypatches `cr.model_of`, which OC-1 never
    calls -- that monkeypatch is a no-op against the real code path. That
    test still passes today, but not because of the monkeypatch: its
    `_gate()` calls never set a top-level `model` key either, so
    `session_model` is `None` there and the OC-1 `if session_model:` guard
    is never entered on the real path -- meaning that legacy test currently
    exercises no OC-1 behavior at all despite asserting a deny. This journey
    deliberately supplies the top-level `model` field on the `event_gate`
    payload itself (Step 6-8) so the OC-1 branch is actually entered, per
    TICKET-021.

  (d) RC-7 / context-pressure gate -- a session transcript whose cumulative
      output_tokens exceeds 120% of `context_pressure.output_token_budget`
      hard-denies the next `Task` dispatch via the real `event_gate` path,
      naming RC-7 and `/compact` in the denial message and incrementing the
      real `gate_deny_context_pressure` counter.

Deliberately excluded (kept in the legacy/unit files, not journey shape):
  - RD-6 exact boundary arithmetic at exactly N vs N+1 files across repeated
    monkeypatched config values -- tests/e2e/test_routing_gate.py's
    `test_fw_scope_deny_at_limit` already pins that; this journey exercises
    one real allow-then-deny transition at the real default limit (5).
  - OC-1 nudge message field content / GATE-BUG-005 `_gate_name_override`
    session-log labeling -- tests/e2e/test_routing_gate.py's
    `TestGateBug005SessionLogEventLabeling` covers that; this journey only
    asserts permission + the recurrence-count text.
  - Every `_target_is_in_scope` precedence branch (test->impl derivation,
    stem containment against assistant text) -- this is journey shape, not
    a full precedence-table re-assertion; that stays in unit-test territory
    if/when it's written.
  - RC-7's pure-function edge cases (`_read_session_output_tokens` malformed
    lines / missing usage / empty file / empty path; `_context_pressure_check`
    custom budgets, exact-120% boundary, multi-wave accumulation, fail-open
    on missing/None transcript) -- these stay in
    tests/e2e/test_orchestrator_context_bloat.py; this journey exercises
    only the one real allow-then-deny transition through `event_gate`, same
    as the other gates above.
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
import _common  # noqa: E402


def _make_transcript(tmp_path: Path, output_tokens: int) -> str:
    """Write a minimal single-turn transcript JSONL with the given output_tokens."""
    path = tmp_path / "rc7_session.jsonl"
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "turn"}],
            "usage": {"input_tokens": 100, "output_tokens": output_tokens},
        },
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    for mod in (cr, ss):
        monkeypatch.setattr(mod, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(mod, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
        monkeypatch.setattr(mod, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")
    monkeypatch.setattr(cr, "RESUME_STATE_FILE", tmp_path / "resume_state.json")


def test_scope_and_orchestrator_ceiling_gate_journey(tmp_path, monkeypatch):
    # Step 1: real event_reset with a prompt naming exactly one file --
    # prompt_files scope is derived from this text via _files_mentioned_in_prompt.
    scope_sid = "journey-gate-scope"
    cr.event_reset({"session_id": scope_sid, "prompt": "Fix a bug in auth.py"}, False)
    cr.event_scan({"session_id": scope_sid, "text": "Routing: DIRECT — fixing auth.py"}, False)

    # Step 2: PROD-20 (break-020) -- a mutation target named nowhere in
    # prompt_files or assistant text (billing.py has no stem overlap with
    # auth.py) must be denied out-of-scope, not silently allowed.
    oos_result = cr.event_gate(
        {
            "session_id": scope_sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/billing.py", "old_string": "old", "new_string": "new"},
        },
        False,
    )
    assert oos_result.get("permission") == "deny"
    oos_message = oos_result.get("agent_message") or ""
    assert "billing.py" in oos_message
    assert "auth.py" in oos_message

    # Step 3: the real gate_deny_out_of_scope counter, read back off the
    # actual persisted state file, must reflect this denial.
    state = cr._load_state()
    assert state.get("counters", {}).get("gate_deny_out_of_scope", 0) >= 1

    # Step 4: RD-6 FW scope guard -- edits to files 1..5 under a single FW
    # declaration at the real config.json default (fw_scope_file_limit: 5)
    # must all be allowed.
    fw_sid = "journey-gate-fwscope"
    cr.event_reset({"session_id": fw_sid, "prompt": "Fix typos across the codebase"}, False)
    for i in range(5):
        cr.event_scan({"session_id": fw_sid, "text": "Routing: FW — targeted fix"}, False)
        fw_result = cr.event_gate(
            {
                "session_id": fw_sid,
                "tool_name": "Edit",
                "tool_input": {"file_path": f"/tmp/fwfile_{i}.py", "old_string": "old", "new_string": "new"},
            },
            False,
        )
        assert fw_result.get("permission") == "allow", f"file {i} should be allowed under the limit"

    # Step 5: the 6th distinct file under the same FW declaration must be
    # hard-denied by RD-6.
    cr.event_scan({"session_id": fw_sid, "text": "Routing: FW — targeted fix"}, False)
    fw_deny = cr.event_gate(
        {
            "session_id": fw_sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/fwfile_5.py", "old_string": "old", "new_string": "new"},
        },
        False,
    )
    assert fw_deny.get("permission") == "deny"
    assert "FO" in (fw_deny.get("agent_message") or "")

    # Step 6: OC-1 / orchestrator_ceiling -- a work unit declared DIRECT
    # under a fast-tier session model. classify_work_unit_intent(entry) is
    # recomputed fresh on every event_gate call (it is NOT read back from
    # any value pre-seeded into state -- entry["work_unit_intent"] is
    # overwritten unconditionally at check_routing.py:3581/3582 before the
    # OC-1 block runs), so this journey does not pre-seed it. Whether it
    # resolves to "mechanical" (config.json routing_matrix.mechanical
    # .orchestrator == "sonnet") or "reasoning" (not a routing_matrix key,
    # falls to routing_matrix._default.orchestrator == "sonnet"), the
    # expected orchestrator tier is "sonnet" either way -- so the haiku/
    # sonnet mismatch below is real and deterministic regardless of which
    # of the two the classifier lands on for this prompt.
    oc_sid = "journey-gate-oc1"
    # NOTE: "Rename all..." is fast-path exempt (is_fast_path_exempt), which would let
    # the unrelated fast-path same-file gate (routing.fast_path_same_file_limit, fires
    # unconditionally before OC-1's own check on repeated same-file edits) preempt OC-1's
    # deny on the 3rd call below. Use a non-fast-path-exempt phrasing instead so this
    # journey still exercises OC-1's own escalation/message, per the classification note
    # above (still lands on "mechanical" or the reasoning default -- same expected tier).
    cr.event_reset({"session_id": oc_sid, "prompt": "Refactor all snake_case methods across the codebase"}, False)

    results = []
    for i in range(3):
        cr.event_scan({"session_id": oc_sid, "text": "Routing: DIRECT — mechanical rename"}, False)
        results.append(
            cr.event_gate(
                {
                    "session_id": oc_sid,
                    "tool_name": "Edit",
                    # Real extraction path: OC-1 reads normalize_payload(payload).model
                    # (a top-level payload field), not tool_input.model / model_of().
                    "model": "claude-haiku-3-5",
                    "tool_input": {"file_path": "/tmp/auth.py", "old_string": f"foo{i}", "new_string": f"bar{i}"},
                },
                False,
            )
        )

    assert results[0].get("permission") == "allow"
    assert results[1].get("permission") == "allow"

    # Step 7: the third recurrence (matching config.json's
    # routing.orchestrator_nudge_deny_after == 3) hard-denies, naming the
    # routing_matrix.orchestrator recommendation in the message.
    assert results[2].get("permission") == "deny"
    oc_message = results[2].get("agent_message") or ""
    assert "routing_matrix.orchestrator" in oc_message

    # Step 8: the real gate_deny_orchestrator_ceiling counter, read back off
    # the actual persisted state file, must reflect this denial.
    final_state = cr._load_state()
    assert final_state.get("counters", {}).get("gate_deny_orchestrator_ceiling", 0) >= 1

    # Step 9: RC-7 / context-pressure -- a session transcript whose live-context
    # ratio exceeds the threshold hard-denies the next Task dispatch through the
    # real event_gate path (not a mocked _context_pressure_check call).
    import copy

    import _config

    _cfg = copy.deepcopy(_config.load_plugin_config())
    _cfg["context_pressure"] = {"mode": "enforce", "threshold": 0.10}
    monkeypatch.setattr(_config, "_config_cache", _cfg)
    rc7_sid = "journey-gate-rc7"
    cr.event_reset(
        {"session_id": rc7_sid, "prompt": "fan out parallel read workers across scripts/"},
        False,
    )
    cr.event_scan(
        {"session_id": rc7_sid, "text": "Routing: FO — fan out parallel read workers"},
        False,
    )
    saturated_transcript = str(Path(__file__).resolve().parent.parent / "fixtures" / "context_pressure_transcript.jsonl")
    rc7_result = cr.event_gate(
        {
            "session_id": rc7_sid,
            "tool_name": "Task",
            "transcript_path": saturated_transcript,
            "tool_input": {
                "prompt": "fan out parallel read workers across scripts/",
                "model": "haiku",
            },
        },
        False,
    )
    assert rc7_result.get("permission") == "deny"
    rc7_message = rc7_result.get("agent_message") or ""
    assert "RC-7" in rc7_message
    assert "compact" in rc7_message.lower()

    # Step 10: the real gate_deny_context_pressure counter, read back off
    # the actual persisted state file, must reflect this denial.
    rc7_state = cr._load_state()
    assert rc7_state.get("counters", {}).get("gate_deny_context_pressure", 0) >= 1
