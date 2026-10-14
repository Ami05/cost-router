"""E2E tests: routing gate verdict enforcement (scripts/check_routing.py).

Exercises the full reset → scan → gate cycle through the real hook event
functions.  File I/O is isolated via monkeypatching ROUTING_STATE_FILE.
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

_TESTS_DIR = Path(__file__).resolve().parent.parent  # tests/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path / "routing_state.json"
    monkeypatch.setattr(cr, "ROUTING_STATE_FILE", state)
    monkeypatch.setattr(cr, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
    monkeypatch.setattr(cr, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")
    monkeypatch.setattr(cr, "RESUME_STATE_FILE", tmp_path / "resume_state.json")
    # check_routing's _load_state/_save_state are re-exported from
    # session_state.py and close over session_state's OWN module globals,
    # not check_routing's copy of the names -- patch both so state I/O is
    # actually isolated instead of silently falling through to prod files.
    monkeypatch.setattr(ss, "ROUTING_STATE_FILE", state)
    monkeypatch.setattr(ss, "GATE_COUNTERS_FILE", tmp_path / "gate_counters.json")
    monkeypatch.setattr(ss, "SUBAGENT_STATE_FILE", tmp_path / "subagent_state.json")
    # path_range_utils._project_root() falls back to Path.cwd(); pin it to the
    # repo root so project-relative path checks (resolve_safe_path) don't depend
    # on the directory pytest was launched from (e.g. `cd tests`).
    monkeypatch.delenv("CURSOR_PROJECT_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(_PROJECT_ROOT))
    return state


def _reset(sid: str, prompt: str = "Refactor auth module across files") -> None:
    cr.event_reset({"session_id": sid, "prompt": prompt}, False)


def _scan(sid: str, verdict: str = "DIRECT") -> None:
    cr.event_scan({"session_id": sid, "text": f"Routing: {verdict} — task"}, False)


def _gate(sid: str, tool: str = "Edit", **extra) -> dict:
    return cr.event_gate({"session_id": sid, "tool_name": tool, **extra}, False)


# 1 ──────────────────────────────────────────────────────────────────────────
# test_direct_verdict_allows_edit removed (TEST-006): exact duplicate of
# tests/e2e/test_journey_dispatch_lifecycle.py::test_full_turn_dispatch_lifecycle
# step 2 (DIRECT verdict -> Edit allowed).

# 2 ──────────────────────────────────────────────────────────────────────────

def test_no_verdict_blocks_write():
    """No routing verdict + no transcript → first call nudges (allow), repeat
    call in the same turn (same prompt_id) is denied with a Routing: hint."""
    nonexistent = str(_TESTS_DIR / "nonexistent.jsonl")
    prompt_id = "g2-prompt-1"

    first = cr.event_gate(
        {
            "session_id": "g2",
            "tool_name": "Write",
            "transcript_path": nonexistent,
            "prompt_id": prompt_id,
        },
        False,
    )
    assert first.get("permission") == "allow"
    assert "Routing:" in first.get("agent_message", "")

    second = cr.event_gate(
        {
            "session_id": "g2",
            "tool_name": "Write",
            "transcript_path": nonexistent,
            "prompt_id": prompt_id,
        },
        False,
    )
    assert second.get("permission") == "deny"
    assert "Routing:" in second.get("agent_message", "")


# 3 ──────────────────────────────────────────────────────────────────────────

def test_malformed_role_rejected():
    """A novel all-caps role not in the decision table must be rejected."""
    assert not cr.has_routing_verdict("Routing: FASTW — unknown role")


# 4 ──────────────────────────────────────────────────────────────────────────

def test_all_valid_roles_accepted():
    """All four decision-table roles (DIRECT, FO, FW, RB) must be accepted."""
    for role in ("DIRECT", "FO", "FW", "RB"):
        assert cr.has_routing_verdict(f"Routing: {role} — reason"), f"{role} should be valid"


# 6 ──────────────────────────────────────────────────────────────────────────

def test_qa_prompt_fast_path_allows_without_verdict():
    """Pure Q&A prompt → fast-path exemption; Write allowed with no verdict."""
    cr.event_reset({"session_id": "g6", "prompt": "How does check_routing work?"}, False)
    assert _gate("g6", "Write").get("permission") == "allow"


# 8 ──────────────────────────────────────────────────────────────────────────

def test_verdict_resets_on_new_turn():
    """After event_reset the old verdict no longer covers the new turn.
    The first no-verdict call on the new turn nudges (allow); a repeat call
    with the same prompt_id is denied."""
    _reset("g8")
    _scan("g8", "DIRECT")
    assert _gate("g8", "Edit").get("permission") == "allow"

    # New turn — no new verdict → nudge then deny
    cr.event_reset({"session_id": "g8", "prompt": "Another refactor needed"}, False)
    nonexistent = str(_TESTS_DIR / "nonexistent.jsonl")
    prompt_id = "g8-prompt-2"

    first = cr.event_gate(
        {
            "session_id": "g8",
            "tool_name": "Write",
            "transcript_path": nonexistent,
            "prompt_id": prompt_id,
        },
        False,
    )
    assert first.get("permission") == "allow"

    second = cr.event_gate(
        {
            "session_id": "g8",
            "tool_name": "Write",
            "transcript_path": nonexistent,
            "prompt_id": prompt_id,
        },
        False,
    )
    assert second.get("permission") == "deny"


# 9 ──────────────────────────────────────────────────────────────────────────

def test_subagent_call_exempt_from_gate():
    """Subagent context (agent_id in payload) bypasses verdict requirement."""
    out = cr.event_gate(
        {"session_id": "g9", "tool_name": "Edit", "agent_id": "worker-subagent-abc"},
        False,
    )
    assert out.get("permission") == "allow"


# 10 ─────────────────────────────────────────────────────────────────────────

def test_task_dispatch_resets_inline_mutation_counter():
    """Task dispatch resets the inline-mutation counter; next edit is allowed."""
    sid = "g10"
    _reset(sid, "Refactor parser across multiple files")

    # Drive up the counter with edits
    for _ in range(3):
        _scan(sid, "DIRECT")
        _gate(sid, "Edit")

    # Dispatch a Task — counter must reset
    _scan(sid, "FO")
    assert _gate(sid, "Task").get("permission") == "allow"

    # Next edit after reset must still allow
    _scan(sid, "DIRECT")
    assert _gate(sid, "Edit").get("permission") == "allow"


# dispatch_worker correlation (PreToolUse Task/Agent -> SubagentStart) ──────

# test_dispatch_worker_correlation_sequential_match removed (TEST-006): exact
# duplicate of tests/e2e/test_journey_dispatch_lifecycle.py::
# test_full_turn_dispatch_lifecycle steps 3-4 (single mutation-worker Task
# dispatch -> SubagentStart -> dispatch_worker_declared correlation).


def test_dispatch_worker_correlation_out_of_order_with_resume_id(monkeypatch):
    """Two dispatches queued (one plain, one via resume with a known
    agent_id) whose SubagentStart events arrive in reverse order must not
    silently mispair: the resume-tagged one always matches by exact id
    regardless of arrival order."""
    captured: list = []
    monkeypatch.setattr(cr, "append_event", lambda rec: captured.append(rec))
    sid = "dw-concurrent-1"
    _reset(sid)
    _scan(sid, "FO")

    prompt_a = (
        '{"worker":"mutation","intent":"task A",'
        '"files":["scripts/a.py:1"],"pattern":"x","output":"targeted_edits"}'
    )
    prompt_b = '{"worker":"shell","intent":"task B","command":"pytest -x -q","expect":"exit_code"}'

    assert _gate(sid, "Task", tool_input={"prompt": prompt_a}).get("permission") == "allow"
    assert _gate(
        sid, "Task", tool_input={"prompt": prompt_b, "resume": "sub-B"}
    ).get("permission") == "allow"

    # Out-of-order: the resumed agent (sub-B) starts BEFORE the fresh one.
    cr.event_subagent_start({"session_id": sid, "agent_id": "sub-B"}, False)
    cr.event_subagent_start({"session_id": sid, "agent_id": "sub-A"}, False)

    decls = {r["subagent_id"]: r for r in captured if r["type"] == "dispatch_worker_declared"}
    assert decls["sub-B"]["worker"] == "shell"
    assert decls["sub-B"]["intent"] == "task B"
    assert decls["sub-A"]["worker"] == "mutation"
    assert decls["sub-A"]["intent"] == "task A"


def test_dispatch_worker_correlation_ttl_expiry_falls_back(monkeypatch):
    """A queued worker/intent entry older than the TTL must not be matched
    to a later SubagentStart — it must fall back to the sentinel rather
    than pairing stale, likely-wrong data."""
    captured: list = []
    monkeypatch.setattr(cr, "append_event", lambda rec: captured.append(rec))
    sid = "dw-ttl-1"
    _reset(sid)
    _scan(sid, "FO")
    prompt = (
        '{"worker":"mutation","intent":"old task",'
        '"files":["scripts/x.py:1"],"pattern":"x","output":"targeted_edits"}'
    )
    assert _gate(sid, "Task", tool_input={"prompt": prompt}).get("permission") == "allow"

    # Age the queued entry past the TTL.
    state = cr._load_state()
    entry = cr._get_session(state["sessions"], sid)
    for q in entry["pending_dispatch_worker_intent"]:
        q["ts"] -= (cr._DISPATCH_WORKER_QUEUE_TTL_SECONDS + 30)
    cr._save_state(state)

    cr.event_subagent_start({"session_id": sid, "agent_id": "sub-stale"}, False)

    decl = [r for r in captured if r["type"] == "dispatch_worker_declared"][0]
    assert decl["worker"] == cr._DISPATCH_WORKER_NOT_APPLICABLE
    assert decl["intent"] is None


# RD-6 ──────────────────────────────────────────────────────────────────────

def test_fw_scope_deny_at_limit(monkeypatch):
    """FW with edits to >file_limit files triggers GATE-RD-6 hard deny."""
    import _config
    monkeypatch.setattr(_config, "_config_cache", {
        "routing": {"fw_scope_file_limit": 5}
    })

    sid = "rd6a"
    _reset(sid, "Fix typos across the codebase")

    # Edit 5 different files under FW — all should be allowed
    for i in range(5):
        _scan(sid, "FW")
        result = _gate(sid, "Edit", tool_input={"file_path": f"/tmp/file_{i}.py", "old_string": "old", "new_string": "new"})
        assert result.get("permission") == "allow"

    # 6th file should trigger hard deny
    _scan(sid, "FW")
    result = _gate(sid, "Edit", tool_input={"file_path": f"/tmp/file_5.py", "old_string": "old", "new_string": "new"})
    assert result.get("permission") == "deny"
    msg = result.get("agent_message", "") or ""
    assert "blocked" in msg.lower()
    assert "FO" in msg or "split" in msg.lower()


def test_bulk_write_counter_survives_redeclaration(monkeypatch):
    """Re-declaring DIRECT before each write must NOT reset the bulk-write counter."""
    import _config
    monkeypatch.setattr(_config, "_config_cache", {
        "routing": {"direct_bulk_write_limit": 3}
    })

    sid = "rd7"
    _reset(sid, "Write test files for auth module")

    # Write 3 .py files, each with a fresh DIRECT declaration
    for i in range(3):
        _scan(sid, "DIRECT")
        result = _gate(sid, "Write", tool_input={"file_path": f"/tmp/test_auth_{i}.py", "content": "# test"})
        if i < 2:
            assert result.get("permission") == "allow", f"Write {i+1} should be allowed"
        else:
            # 3rd write of same type should be denied
            assert result.get("permission") == "deny", "3rd same-type write should be denied"


def test_fix_loop_nudge_after_3_cycles(monkeypatch):
    """3 edit-test-edit cycles on same file → nudge."""
    # Increase inline mutation thresholds so they don't interfere with this test
    monkeypatch.setattr(cr, "INLINE_MUTATION_NUDGE_THRESHOLD", 20)
    monkeypatch.setattr(cr, "INLINE_MUTATION_DENY_THRESHOLD", 30)

    sid = "rd5a"
    _reset(sid, "Fix failing tests in auth module")

    for cycle in range(3):
        _scan(sid, "DIRECT — fixing bugs in code")
        _gate(sid, "Edit", tool_input={"file_path": "/tmp/auth.txt", "old_string": "old", "new_string": f"new{cycle}"})
        _gate(sid, "Bash", tool_input={"command": "python -m pytest tests/"})

    # Next edit to same file should nudge (fix-loop counter at 3)
    _scan(sid, "DIRECT — continuing repairs")
    result = _gate(sid, "Edit", tool_input={"file_path": "/tmp/auth.txt", "old_string": "x", "new_string": "y"})
    msg = result.get("agent_message", "") or ""
    assert "fix-loop" in msg.lower() or "cycle" in msg.lower(), f"Expected fix-loop message, got: {msg}"


def test_fix_loop_deny_after_5_cycles(monkeypatch):
    """5 edit-test-edit cycles on same file → deny."""
    # Increase inline mutation thresholds so they don't interfere
    monkeypatch.setattr(cr, "INLINE_MUTATION_NUDGE_THRESHOLD", 20)
    monkeypatch.setattr(cr, "INLINE_MUTATION_DENY_THRESHOLD", 30)

    sid = "rd5b"
    _reset(sid, "Fix failing tests in auth module")

    for cycle in range(5):
        _scan(sid, "DIRECT — fixing bugs in code")
        _gate(sid, "Edit", tool_input={"file_path": "/tmp/auth.txt", "old_string": "old", "new_string": f"new{cycle}"})
        _gate(sid, "Bash", tool_input={"command": "pytest tests/"})

    # Next edit to same file should deny (fix-loop counter at 5)
    _scan(sid, "DIRECT — continuing repairs")
    result = _gate(sid, "Edit", tool_input={"file_path": "/tmp/auth.txt", "old_string": "x", "new_string": "y"})
    assert result.get("permission") == "deny", f"Expected deny, got: {result}"


def test_mechanical_intent_lowers_bulk_write_threshold(monkeypatch):
    """When work unit is classified as mechanical, bulk-write threshold is lowered."""
    import _config
    monkeypatch.setattr(_config, "_config_cache", {
        "routing": {"direct_bulk_write_limit": 3}
    })

    sid = "rd3a"
    # Agentic prompt (mechanical signal)
    cr.event_reset({"session_id": sid, "prompt": "Write test files for all modules"}, False)

    # Simulate reads (spec loaded = mechanical signal)
    cr.event_read({"session_id": sid, "tool_name": "Read", "file_path": "/tmp/auth.py"}, False)
    cr.event_read({"session_id": sid, "tool_name": "Read", "file_path": "/tmp/utils.py"}, False)

    # Write 2 test files — with mechanical intent, effective limit is 2 (3-1)
    _scan(sid, "DIRECT")
    result1 = _gate(sid, "Write", tool_input={"file_path": "/tmp/test_auth.py", "content": "# test"})
    assert result1.get("permission") == "allow"

    _scan(sid, "DIRECT")
    result2 = _gate(sid, "Write", tool_input={"file_path": "/tmp/test_utils.py", "content": "# test"})
    # Should deny at effective limit 2 (lowered from 3 due to mechanical intent)
    assert result2.get("permission") == "deny"
    msg = result2.get("agent_message", "") or ""
    assert "mechanical" in msg.lower()


def test_oc1_mechanical_opus_nudge(monkeypatch):
    """OC-1: mechanical work + fast-tier session → nudge from matrix.

    The OC-1 gate compares tier_of_model(session) vs tier_of_model(matrix.orchestrator).
    For mechanical intent the matrix sets orchestrator=sonnet (reasoning tier).
    Using a haiku (fast) session triggers the tier mismatch → nudge fires.

    check_routing.py binds model_of via `from _model_utils import model_of`,
    so we patch the name in check_routing's own namespace.
    """
    monkeypatch.setattr(cr, "model_of", lambda payload: "claude-haiku-3-5")

    sid = "oc1a"
    cr.event_reset({"session_id": sid, "prompt": "Rename all snake_case methods across the codebase"}, False)
    cr.event_read({"session_id": sid, "tool_name": "Read", "file_path": "/tmp/auth.py"}, False)
    cr.event_read({"session_id": sid, "tool_name": "Read", "file_path": "/tmp/utils.py"}, False)

    # Inject mechanical intent directly into routing state
    state = cr._load_state()
    state.setdefault("sessions", {}).setdefault(sid, {})["work_unit_intent"] = "mechanical"
    cr._save_state(state)

    _scan(sid, "DIRECT")
    result = _gate(sid, "Edit", tool_input={"file_path": "/tmp/auth.py", "old_string": "foo", "new_string": "bar"})

    msg = result.get("agent_message", "") or ""
    # OC-1 fires as allow+nudge: fast session vs matrix orchestrator=sonnet (reasoning)
    assert "cost-router" in msg.lower()
    assert result.get("permission") == "allow"


def test_cross_turn_file_overlap_denied(monkeypatch, tmp_path):
    """New Task with file overlap to idle worker from previous turn → denied with resume.

    Uses project-relative paths so resolve_safe_path() accepts them (it rejects
    paths outside the project root such as /tmp).
    """
    import json as _json
    import estimate_dispatch_context as edc
    import _common
    from datetime import datetime, timezone, timedelta

    # Use paths inside the project root so resolve_safe_path accepts them
    shared_path = str(_PROJECT_ROOT / "scripts" / "_common.py")
    auth_path = str(_PROJECT_ROOT / "scripts" / "check_routing.py")

    warm_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    registry = {
        "sessions": {
            "ct1": {
                "agents": {
                    "worker-abc": {
                        "agent_id": "worker-abc",
                        "status": "idle",
                        "warm_until": warm_until,
                        "files_touched": [shared_path, auth_path],
                        "subagent_type": "general-purpose",
                    }
                }
            }
        }
    }
    registry_file = tmp_path / "dispatch_registry.json"
    registry_file.write_text(_json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)

    # New Task referencing the same shared_path → overlaps with idle worker
    tool_input = {
        "prompt": f"Fix auth logic in @{shared_path} and @{_PROJECT_ROOT}/scripts/gates.py",
        "model": "claude-sonnet-4-5",
    }
    result = edc.check_grouping_enforcement(tool_input)

    assert result.get("should_deny") is True
    assert result.get("resume") == "worker-abc"
    reason = result.get("reason", "")
    assert reason in ("cross_turn_file_overlap", "same_turn_shared_files") or "overlap" in reason
    # SPO-006: message must follow the new hard-deny format
    msg = result.get("message", "") or ""
    assert "[dispatch-group]" in msg
    assert "worker-abc" in msg
    assert "Task(resume=" in msg


def test_cross_turn_no_overlap_allowed(monkeypatch, tmp_path):
    """No file overlap → cold-start allowed."""
    import json as _json
    import estimate_dispatch_context as edc
    import _common
    from datetime import datetime, timezone, timedelta

    worker_path = str(_PROJECT_ROOT / "scripts" / "_common.py")
    task_path = str(_PROJECT_ROOT / "scripts" / "gates.py")

    warm_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    registry = {
        "sessions": {
            "ct3": {
                "agents": {
                    "worker-disjoint": {
                        "agent_id": "worker-disjoint",
                        "status": "idle",
                        "warm_until": warm_until,
                        "files_touched": [worker_path],
                        "subagent_type": "general-purpose",
                    }
                }
            }
        }
    }
    registry_file = tmp_path / "dispatch_registry.json"
    registry_file.write_text(_json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)

    # Task references a non-overlapping file
    tool_input = {"prompt": f"Refactor @{task_path}", "model": "claude-sonnet-4-5"}
    result = edc.check_grouping_enforcement(tool_input)

    assert result.get("should_deny") is False


def test_region_aware_disjoint_ranges_allowed(monkeypatch, tmp_path):
    """BWS-T1-001: disjoint line ranges in the same file are independent under
    region-aware overlap, so a new Task referencing a non-overlapping range
    must not be denied.
    """
    import json as _json
    import estimate_dispatch_context as edc
    import _config
    import _common
    from datetime import datetime, timezone, timedelta

    target = str(_PROJECT_ROOT / "scripts" / "_common.py")

    monkeypatch.setattr(
        _config,
        "_config_cache",
        {"dispatch_context": {"region_aware_overlap": True}},
    )

    warm_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    registry = {
        "sessions": {
            "ct4": {
                "agents": {
                    "worker-region": {
                        "agent_id": "worker-region",
                        "status": "idle",
                        "warm_until": warm_until,
                        "files_touched": [f"{target}:1-50"],
                        "subagent_type": "general-purpose",
                    }
                }
            }
        }
    }
    registry_file = tmp_path / "dispatch_registry.json"
    registry_file.write_text(_json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)

    # Task references a disjoint line range in the same file
    tool_input = {"prompt": f"Fix @{target}:100-150", "model": "claude-sonnet-4-5"}
    result = edc.check_grouping_enforcement(tool_input)

    assert result.get("should_deny") is False


def test_region_aware_overlapping_ranges_denied(monkeypatch, tmp_path):
    """BWS-T1-001: overlapping line ranges in the same file still serialize
    under region-aware overlap, so a new Task referencing an overlapping
    range must be denied and resumed onto the existing warm worker.
    """
    import json as _json
    import estimate_dispatch_context as edc
    import _config
    import _common
    from datetime import datetime, timezone, timedelta

    target = str(_PROJECT_ROOT / "scripts" / "_common.py")

    monkeypatch.setattr(
        _config,
        "_config_cache",
        {"dispatch_context": {"region_aware_overlap": True}},
    )

    warm_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    registry = {
        "sessions": {
            "ct4": {
                "agents": {
                    "worker-region": {
                        "agent_id": "worker-region",
                        "status": "idle",
                        "warm_until": warm_until,
                        "files_touched": [f"{target}:100-150"],
                        "subagent_type": "general-purpose",
                    }
                }
            }
        }
    }
    registry_file = tmp_path / "dispatch_registry.json"
    registry_file.write_text(_json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)

    # Task references a truly overlapping line range in the same file
    tool_input = {"prompt": f"Fix @{target}:120-160", "model": "claude-sonnet-4-5"}
    result = edc.check_grouping_enforcement(tool_input)

    assert result.get("should_deny") is True
    assert result.get("resume") == "worker-region"


def test_region_aware_bare_path_fails_closed(monkeypatch, tmp_path):
    """BWS-T1-001: a bare path reference with no line range must be treated as
    whole-file under region-aware overlap (fail closed), so it still overlaps
    an existing warm worker's touched range and gets denied.
    """
    import json as _json
    import estimate_dispatch_context as edc
    import _config
    import _common
    from datetime import datetime, timezone, timedelta

    target = str(_PROJECT_ROOT / "scripts" / "_common.py")

    monkeypatch.setattr(
        _config,
        "_config_cache",
        {"dispatch_context": {"region_aware_overlap": True}},
    )

    warm_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    registry = {
        "sessions": {
            "ct4": {
                "agents": {
                    "worker-region": {
                        "agent_id": "worker-region",
                        "status": "idle",
                        "warm_until": warm_until,
                        "files_touched": [f"{target}:100-150"],
                        "subagent_type": "general-purpose",
                    }
                }
            }
        }
    }
    registry_file = tmp_path / "dispatch_registry.json"
    registry_file.write_text(_json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)

    # Task references a bare path with no range - must be treated as whole-file
    tool_input = {"prompt": f"Fix @{target}", "model": "claude-sonnet-4-5"}
    result = edc.check_grouping_enforcement(tool_input)

    assert result.get("should_deny") is True
    assert result.get("resume") == "worker-region"


def test_region_aware_flag_off_preserves_prior_behavior(monkeypatch, tmp_path):
    """BWS-T1-001: with region_aware_overlap disabled, the gate must reproduce
    byte-identical prior behavior, where differently-specified line ranges on
    the same file never collide (even when the ranges actually overlap).
    """
    import json as _json
    import estimate_dispatch_context as edc
    import _config
    import _common
    from datetime import datetime, timezone, timedelta

    target = str(_PROJECT_ROOT / "scripts" / "_common.py")

    monkeypatch.setattr(
        _config,
        "_config_cache",
        {"dispatch_context": {"region_aware_overlap": False}},
    )

    warm_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    registry = {
        "sessions": {
            "ct4": {
                "agents": {
                    "worker-region": {
                        "agent_id": "worker-region",
                        "status": "idle",
                        "warm_until": warm_until,
                        "files_touched": [f"{target}:100-150"],
                        "subagent_type": "general-purpose",
                    }
                }
            }
        }
    }
    registry_file = tmp_path / "dispatch_registry.json"
    registry_file.write_text(_json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)

    # Task references a truly overlapping line range in the same file
    tool_input = {"prompt": f"Fix @{target}:120-160", "model": "claude-sonnet-4-5"}
    result = edc.check_grouping_enforcement(tool_input)

    assert result.get("should_deny") is False


# SPO-006: dispatch-group gate hard-deny ─────────────────────────────────────

def test_spo006_gate_blocks_cold_start_when_warm_worker_exists(monkeypatch, tmp_path):
    """SPO-006: gate must hard-deny a cold-start Task when a warm idle worker matches.

    Verifies should_deny=True, resume ID propagated, and the new [dispatch-group]
    error message format is used (worker ID + Task(resume=...) hint).
    """
    import json as _json
    import estimate_dispatch_context as edc
    import _common
    from datetime import datetime, timezone, timedelta

    shared_path = str(_PROJECT_ROOT / "scripts" / "_common.py")
    other_path = str(_PROJECT_ROOT / "scripts" / "gates.py")

    warm_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    registry = {
        "sessions": {
            "spo006-s1": {
                "agents": {
                    "warm-worker-99": {
                        "agent_id": "warm-worker-99",
                        "status": "idle",
                        "warm_until": warm_until,
                        "files_touched": [shared_path, other_path],
                        "subagent_type": "general-purpose",
                    }
                }
            }
        }
    }
    registry_file = tmp_path / "dispatch_registry.json"
    registry_file.write_text(_json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)

    tool_input = {
        "prompt": f"Update logging in @{shared_path}",
        "model": "claude-haiku-4-5",
    }
    result = edc.check_grouping_enforcement(tool_input)

    # Gate must deny
    assert result.get("should_deny") is True, f"Expected deny, got: {result}"
    # Resume ID must be the warm worker
    assert result.get("resume") == "warm-worker-99"
    # Message must include [dispatch-group] prefix, worker ID, and Task(resume=...) hint
    msg = result.get("message", "") or ""
    assert "[dispatch-group]" in msg, f"Missing [dispatch-group] prefix in: {msg!r}"
    assert "warm-worker-99" in msg, f"Missing worker ID in: {msg!r}"
    assert "Task(resume=" in msg, f"Missing Task(resume=...) hint in: {msg!r}"


def test_spo006_gate_passes_when_no_warm_worker_exists(monkeypatch, tmp_path):
    """SPO-006: gate must allow cold-start when no warm idle worker matches.

    An empty registry means there is nothing to resume — the dispatch must proceed.
    """
    import json as _json
    import estimate_dispatch_context as edc
    import _common

    task_path = str(_PROJECT_ROOT / "scripts" / "gates.py")

    # Empty registry — no warm workers at all
    registry: dict = {"sessions": {}}
    registry_file = tmp_path / "dispatch_registry.json"
    registry_file.write_text(_json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)

    tool_input = {
        "prompt": f"Add type hints to @{task_path}",
        "model": "claude-haiku-4-5",
    }
    result = edc.check_grouping_enforcement(tool_input)

    assert result.get("should_deny") is False, f"Expected allow, got: {result}"


# Bulk-edit worker downgrade ─────────────────────────────────────────────────

def test_bulk_edit_classification():
    """Prompts with >=5 files + mechanical signals → classified as bulk-edit."""
    result = cr.classify_dispatch_work(
        "Fix lint in these files: a.py b.py c.py d.py e.py f.py"
    )
    assert result == "bulk-edit"


# Mid-session cost feedback ─────────────────────────────────────────────────

def test_cost_warning_at_50_pct(tmp_path, monkeypatch):
    """Nudge when cumulative cost crosses 50% of budget."""
    import agent_call_tracker as act
    import spawn_tracker
    import _config
    import _model_utils

    stats_file = tmp_path / "stats.json"
    # spawn_tracker owns the actual read/write path for STATS_FILE (its own
    # module-level binding); agent_call_tracker only re-exports it for
    # display/messages, so that copy must be patched too to affect I/O.
    monkeypatch.setattr(spawn_tracker, "STATS_FILE", stats_file)
    monkeypatch.setattr(act, "STATS_FILE", stats_file)
    monkeypatch.setattr(act, "ROUTING_STATE_FILE", tmp_path / "routing.json")
    # Set low budget so we cross threshold easily
    monkeypatch.setattr(_config, "_config_cache", {
        "session_cost_budget": {"budget_usd": 1.0, "warning_pcts": [50, 80]},
        "routing": {"subagent_ttl_seconds": 300},
        "session_limits": {"agent_call_tracker": 50},
    })
    # Mock est_cold_write_cost_usd to return a known value (0.60 > 50% of $1.00)
    monkeypatch.setattr(_model_utils, "est_cold_write_cost_usd", lambda m, infra=None: 0.60)
    import dispatch_budget_enforcer as dbe
    monkeypatch.setattr(dbe, "est_cold_write_cost_usd", lambda m, infra=None: 0.60)

    payload = {
        "session_id": "cost-test",
        "tool_name": "Agent",
        "tool_input": {"prompt": "test", "model": "sonnet", "subagent_type": "general-purpose"},
    }
    r = act.event_preuse(payload, False)
    msg = (r or {}).get("agent_message", "") or ""
    assert "50%" in msg or "cost" in msg.lower()


def test_explore_overlap_nudge(tmp_path, monkeypatch):
    """Second Explore with >50% file overlap → nudge."""
    import agent_call_tracker as act
    import _config

    monkeypatch.setattr(act, "STATS_FILE", tmp_path / "stats.json")
    monkeypatch.setattr(act, "ROUTING_STATE_FILE", tmp_path / "routing.json")
    monkeypatch.setattr(_config, "_config_cache", {
        "session_cost_budget": {"budget_usd": 100.0, "warning_pcts": [50, 80]},
        "routing": {"subagent_ttl_seconds": 300},
        "session_limits": {"agent_call_tracker": 50},
    })

    # First explore — reads two files using @path syntax so extract_paths_from_prompt picks them up
    p1 = {
        "session_id": "explore-test",
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "Read @scripts/check_routing.py and @scripts/gates.py",
            "model": "haiku",
            "subagent_type": "Explore",
        },
    }
    act.event_preuse(p1, False)

    # Second explore — one of the same files (1/1 = 100% overlap, > 50%)
    p2 = {
        "session_id": "explore-test",
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "Read @scripts/check_routing.py",
            "model": "haiku",
            "subagent_type": "Explore",
        },
    }
    r2 = act.event_preuse(p2, False)
    msg = (r2 or {}).get("agent_message", "") or ""
    assert "already read" in msg.lower() or "overlap" in msg.lower() or "previous" in msg.lower()


# --- merged from test_enforce_role_gates.py ---

from enforce_role_gates import check_role_gate  # noqa: E402


def _role_payload(tool: str, role: str | None = None, limit: int | None = None) -> dict:
    p: dict = {"tool_name": tool, "tool_input": {}}
    if role:
        p["text"] = f"Routing: {role} — doing work"
    if limit is not None:
        p["tool_input"]["limit"] = limit
    return p


class TestRoleGates:

    def test_fo_edit_denied(self):
        result = check_role_gate(_role_payload("Edit", role="FO"))
        assert result.decision == "deny"
        assert "FO" in result.message

    def test_direct_edit_allowed(self):
        result = check_role_gate(_role_payload("Edit", role="DIRECT"))
        assert result.decision == "allow"

    def test_fo_task_not_blocked(self):
        result = check_role_gate(_role_payload("Task", role="FO"))
        assert result.decision == "allow"


# --- merged from test_enforce_explicit_model.py ---

from enforce_explicit_model import check_explicit_model  # noqa: E402


class TestExplicitModelGate:
    """Unit tests for the explicit-model gate."""

    def test_task_without_model_denied(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": {"prompt": "Do something"}})
        assert result.decision == "deny"
        assert "explicit model required" in result.message

    def test_task_with_model_field_allowed(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": {"model": "haiku", "prompt": "Do it"}})
        assert result.decision == "allow"


# --- merged from test_agent_role_scoping.py ---

import json as _json_scoping

from check_routing import _scan_role_backward  # noqa: E402


def _make_transcript_lines(records: list[dict]) -> list[str]:
    return [_json_scoping.dumps(r) for r in records]


def _assistant_record(text: str, agent_id: str = "") -> dict:
    record = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    if agent_id:
        record["agentId"] = agent_id
    return record


def _user_prompt_record(text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
    }


# --- Phase 3: negative-path and combined gate interaction tests ---

def _is_deny(result: dict) -> bool:
    """Return True when the gate result represents a deny verdict."""
    return result.get("permission") == "deny"


# --- TestDirectNewFileGate: check_direct_new_file_gate() and event_pre_write() wiring ---

from enforce_role_gates import check_direct_new_file_gate, event_pre_write  # noqa: E402


def _direct_new_file_payload(tool: str, role: str, file_path: str | None) -> dict:
    """Build a minimal payload for check_direct_new_file_gate tests.

    Role is conveyed via payload["text"] following the existing _role_payload pattern.
    File path is placed in payload["tool_input"]["path"].
    """
    p: dict = {
        "tool_name": tool,
        "text": f"Routing: {role} — doing work",
        "tool_input": {},
    }
    if file_path is not None:
        p["tool_input"]["path"] = file_path
    return p


class TestDirectNewFileGate:
    """Unit tests for check_direct_new_file_gate() and its wiring in event_pre_write()."""

    # --- check_direct_new_file_gate direct calls ---

    def test_direct_write_nonexistent_denied(self, tmp_path):
        """DIRECT + Write + non-existent file → DENY with 'Routing: FW' hint."""
        nonexistent = str(tmp_path / "new_nonexistent_file.py")
        payload = _direct_new_file_payload("Write", "DIRECT", nonexistent)
        result = check_direct_new_file_gate(payload)
        assert result.decision == "deny"
        assert "FW" in result.message

    def test_direct_edit_nonexistent_denied(self, tmp_path):
        """DIRECT + Edit + non-existent file → DENY."""
        nonexistent = str(tmp_path / "new_nonexistent_file.py")
        payload = _direct_new_file_payload("Edit", "DIRECT", nonexistent)
        result = check_direct_new_file_gate(payload)
        assert result.decision == "deny"

    def test_direct_write_existing_allowed(self, tmp_path):
        """DIRECT + Write + existing file → ALLOW."""
        existing = tmp_path / "existing_file.py"
        existing.write_text("# existing\n")
        payload = _direct_new_file_payload("Write", "DIRECT", str(existing))
        result = check_direct_new_file_gate(payload)
        assert result.decision == "allow"

    def test_direct_edit_existing_allowed(self, tmp_path):
        """DIRECT + Edit + existing file → ALLOW."""
        existing = tmp_path / "existing_file.py"
        existing.write_text("# existing\n")
        payload = _direct_new_file_payload("Edit", "DIRECT", str(existing))
        result = check_direct_new_file_gate(payload)
        assert result.decision == "allow"

    def test_fw_write_nonexistent_allowed(self, tmp_path):
        """FW + Write + non-existent file → ALLOW (gate only targets DIRECT)."""
        nonexistent = str(tmp_path / "new_nonexistent_file.py")
        payload = _direct_new_file_payload("Write", "FW", nonexistent)
        result = check_direct_new_file_gate(payload)
        assert result.decision == "allow"

    def test_direct_write_no_file_path_allow_fail_open(self):
        """DIRECT + Write + no file_path in payload → ALLOW (fail-open)."""
        payload = _direct_new_file_payload("Write", "DIRECT", None)
        result = check_direct_new_file_gate(payload)
        assert result.decision == "allow"

    # --- file_path also readable from payload["tool_input"]["file_path"] key ---

    def test_direct_write_nonexistent_file_path_key_denied(self, tmp_path):
        """DIRECT + Write + non-existent file via 'file_path' key → DENY."""
        nonexistent = str(tmp_path / "new_nonexistent_file.py")
        payload = {
            "tool_name": "Write",
            "text": "Routing: DIRECT — doing work",
            "tool_input": {"file_path": nonexistent},
        }
        result = check_direct_new_file_gate(payload)
        assert result.decision == "deny"

    # --- event_pre_write() wiring: check_direct_new_file_gate called after check_role_gate ---

    def test_event_pre_write_direct_nonexistent_denied(self, tmp_path):
        """event_pre_write: DIRECT + Write + non-existent file → deny permission."""
        nonexistent = str(tmp_path / "new_nonexistent_file.py")
        payload = _direct_new_file_payload("Write", "DIRECT", nonexistent)
        result = event_pre_write(payload, False)
        assert result.get("permission") == "deny"
        assert "FW" in result.get("agent_message", "")

    def test_event_pre_write_direct_existing_allowed(self, tmp_path):
        """event_pre_write: DIRECT + Write + existing file → allow permission."""
        existing = tmp_path / "existing_file.py"
        existing.write_text("# existing\n")
        payload = _direct_new_file_payload("Write", "DIRECT", str(existing))
        result = event_pre_write(payload, False)
        assert result.get("permission") == "allow"

    def test_event_pre_write_fo_write_nonexistent_denied_by_role_gate(self, tmp_path):
        """event_pre_write: FO + Write → denied by check_role_gate (not check_direct_new_file_gate)."""
        nonexistent = str(tmp_path / "new_nonexistent_file.py")
        payload = _direct_new_file_payload("Write", "FO", nonexistent)
        result = event_pre_write(payload, False)
        assert result.get("permission") == "deny"

    def test_event_pre_write_fw_write_nonexistent_allowed(self, tmp_path):
        """event_pre_write: FW + Write + non-existent file → allow (FW is codegen role)."""
        nonexistent = str(tmp_path / "new_nonexistent_file.py")
        payload = _direct_new_file_payload("Write", "FW", nonexistent)
        result = event_pre_write(payload, False)
        assert result.get("permission") == "allow"

    def test_gate_name_override_included_in_event_pre_write(self, tmp_path):
        """event_pre_write returns _gate_name_override for run_gate to pop and use for logging."""
        nonexistent = str(tmp_path / "new_nonexistent_file.py")
        payload = _direct_new_file_payload("Write", "DIRECT", nonexistent)
        result = event_pre_write(payload, False)
        assert result.get("permission") == "deny"
        assert result.get("_gate_name_override") == "direct_new_file_gate", (
            "_gate_name_override should be present for run_gate to pop"
        )

    def test_run_gate_uses_direct_new_file_gate_name(self, tmp_path, monkeypatch):
        """run_gate must log gate_name='direct_new_file_gate' (not 'enforce_role_gate_write') when direct_new_file_gate denies."""
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../scripts"))
        import gates as gates_mod

        logged: list[dict] = []

        def _capture(gate_chain, gate_name, action, result, payload):
            logged.append({"gate_chain": gate_chain, "gate_name": gate_name, "action": action})

        monkeypatch.setattr(gates_mod, "_log_gate_event", _capture)

        nonexistent = str(tmp_path / "new_nonexistent_file.py")
        payload = _direct_new_file_payload("Write", "DIRECT", nonexistent)
        result = gates_mod.run_gate("pre_write", payload, False)

        assert result.get("permission") == "deny"
        assert "_gate_name_override" not in result

        deny_logs = [e for e in logged if e["action"] == "deny"]
        assert deny_logs, "Expected at least one deny log entry"
        assert deny_logs[0]["gate_name"] == "direct_new_file_gate", (
            f"Expected gate_name='direct_new_file_gate', got '{deny_logs[0]['gate_name']}'"
        )


# ── GATE-BUG-001: worker-tool-allowlist transcript resolution ────────────────
#
# Root cause (confirmed 2026-09-09 via live PreToolUse payload capture): on
# Claude Code CLI, payload["transcript_path"] always points at the PARENT
# session's top-level <session_id>.jsonl file — even for a dispatched
# subagent's own tool calls, whose agent_id IS present in that same payload.
# The subagent's own dispatch-turn record (the JSON worker-schema payload
# from Task()) is written to a SEPARATE file at
# <session_dir>/subagents/agent-<agentId>.jsonl, which transcript_path never
# references. extract_declared_worker() used to scan only transcript_path,
# so it always fell through to "no dispatch turn found" -> None, and
# check_worker_tool_allowlist() silently allowed every tool for every
# worker type. Fixed by resolving and preferring the per-agent transcript
# file (_subagent_transcript_path()) before falling back to transcript_path.
#
# These fixtures reproduce the exact on-disk shape observed in the live
# capture, not a hand-wavy simulated one: a real-shaped PreToolUse payload
# (snake_case "agent_id", "transcript_path" pointing at the main session
# file) plus a real-shaped subagents/agent-<id>.jsonl dispatch-turn record.

from enforce_role_gates import check_worker_tool_allowlist, extract_declared_worker  # noqa: E402


class TestGateBug001WorkerTranscriptResolution:
    """Regression coverage for GATE-BUG-001."""

    _SESSION_ID = "92a0435f-3638-4e9a-b2f9-6ab1d42828a4"

    def _make_transcript_tree(self, tmp_path: Path, agent_id: str, dispatch_json: dict) -> str:
        """Build main session file (left empty, as real ones are for a
        subagent's own records) + subagents/agent-<id>.jsonl (real shape)."""
        main_file = tmp_path / f"{self._SESSION_ID}.jsonl"
        main_file.write_text("")
        subagents_dir = tmp_path / self._SESSION_ID / "subagents"
        subagents_dir.mkdir(parents=True)
        record = {
            "parentUuid": None,
            "isSidechain": True,
            "promptId": "43c75917-0362-4eaa-a0a0-1abe1181f7da",
            "agentId": agent_id,
            "type": "user",
            "message": {"role": "user", "content": json.dumps(dispatch_json)},
            "uuid": "f11ed119-f727-408d-b984-82ed2108d06a",
            "timestamp": "2026-09-09T06:20:50.515Z",
            "sessionId": self._SESSION_ID,
        }
        (subagents_dir / f"agent-{agent_id}.jsonl").write_text(json.dumps(record) + "\n")
        return str(main_file)

    def _real_shape_payload(self, tool_name: str, transcript_path: str, agent_id: str) -> dict:
        """Matches the exact PreToolUse payload keys captured live for a
        dispatched worker's own tool call (see GATE-BUG-001 root-cause note)."""
        return {
            "session_id": self._SESSION_ID,
            "transcript_path": transcript_path,
            "cwd": str(Path(__file__).resolve().parents[2]),
            "prompt_id": "dc301711-9e10-4f9c-8745-4d49219ea94d",
            "permission_mode": "default",
            "agent_id": agent_id,
            "agent_type": "general-purpose",
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_use_id": "toolu_test",
        }

    def test_shell_worker_write_denied(self, tmp_path):
        transcript_path = self._make_transcript_tree(
            tmp_path, "shellagent1",
            {"worker": "shell", "intent": "run tests", "command": "pytest", "expect": "exit_code"},
        )
        payload = self._real_shape_payload("Write", transcript_path, "shellagent1")
        result = check_worker_tool_allowlist(payload)
        assert result.decision == "deny"
        assert "shell" in result.message

    def test_read_worker_write_denied(self, tmp_path):
        # BUG-read-worker-tool-allowlist: Bash is now allowed for read
        # workers (Grep/Glob aren't provided by this environment), so this
        # regression check uses Write — still outside the read allowlist —
        # to keep covering the transcript-resolution path GATE-BUG-001 fixed.
        transcript_path = self._make_transcript_tree(
            tmp_path, "readagent1",
            {"worker": "read", "goal": "find X", "scope": ["scripts/"], "return": ["signatures"]},
        )
        payload = self._real_shape_payload("Write", transcript_path, "readagent1")
        result = check_worker_tool_allowlist(payload)
        assert result.decision == "deny"
        assert "read" in result.message

    @pytest.mark.parametrize("tool_name", ["Read", "Edit", "Write", "Bash"])
    def test_mutation_worker_allowed_all_tools(self, tmp_path, tool_name):
        transcript_path = self._make_transcript_tree(
            tmp_path, "mutagent1",
            {"worker": "mutation", "intent": "fix bug", "files": ["a.py:1"], "pattern": "fix it", "output": "summary"},
        )
        payload = self._real_shape_payload(tool_name, transcript_path, "mutagent1")
        result = check_worker_tool_allowlist(payload)
        assert result.decision == "allow"

    @pytest.mark.parametrize("tool_name", ["Read", "Write", "Edit"])
    def test_create_worker_allowed_edit(self, tmp_path, tool_name):
        """GATE-BUG-002: create worker must allow Edit (not just Write) so a
        small field-level fix doesn't force a full-file rewrite."""
        transcript_path = self._make_transcript_tree(
            tmp_path, "createagent1",
            {
                "worker": "create",
                "intent": "new config schema",
                "file_path": "scripts/docs/example.json",
                "template_ref": "json_config",
            },
        )
        payload = self._real_shape_payload(tool_name, transcript_path, "createagent1")
        result = check_worker_tool_allowlist(payload)
        assert result.decision == "allow"

    def test_create_worker_bash_denied(self, tmp_path):
        transcript_path = self._make_transcript_tree(
            tmp_path, "createagent2",
            {
                "worker": "create",
                "intent": "new config schema",
                "file_path": "scripts/docs/example.json",
                "template_ref": "json_config",
            },
        )
        payload = self._real_shape_payload("Bash", transcript_path, "createagent2")
        result = check_worker_tool_allowlist(payload)
        assert result.decision == "deny"
        assert "create" in result.message

    def test_extract_declared_worker_resolves_from_subagent_file_not_main(self, tmp_path):
        """Main transcript_path file is left empty — proves resolution came
        from the per-agent subagents/ file, not a lucky fallback scan."""
        transcript_path = self._make_transcript_tree(
            tmp_path, "shellagent2",
            {"worker": "shell", "intent": "x", "command": "y", "expect": "exit_code"},
        )
        assert Path(transcript_path).read_text() == ""  # sanity: main file truly empty
        payload = self._real_shape_payload("Bash", transcript_path, "shellagent2")
        assert extract_declared_worker(payload) == "shell"

    def _make_raw_transcript_tree(self, tmp_path: Path, agent_id: str, raw_content: str) -> str:
        """Build main session file (left empty, as real ones are for a
        subagent's own records) + subagents/agent-<id>.jsonl (real shape),
        with the message content field set to *raw_content* directly rather
        than json.dumps of a dict — for thin non-JSON Run dispatch fixtures."""
        main_file = tmp_path / f"{self._SESSION_ID}.jsonl"
        main_file.write_text("")
        subagents_dir = tmp_path / self._SESSION_ID / "subagents"
        subagents_dir.mkdir(parents=True)
        record = {
            "parentUuid": None,
            "isSidechain": True,
            "promptId": "43c75917-0362-4eaa-a0a0-1abe1181f7da",
            "agentId": agent_id,
            "type": "user",
            "message": {"role": "user", "content": raw_content},
            "uuid": "f11ed119-f727-408d-b984-82ed2108d06a",
            "timestamp": "2026-09-09T06:20:50.515Z",
            "sessionId": self._SESSION_ID,
        }
        (subagents_dir / f"agent-{agent_id}.jsonl").write_text(json.dumps(record) + "\n")
        return str(main_file)


# ── BWS-T3-002: Bounded Wave Scheduler gates (concurrency cap, serialize_verify,
# progress-ledger stall counter) — event_gate/_subagent_progress_gate coverage.

class TestBoundedWaveSchedulerGates:
    """Regression coverage for BWS-T3-002.

    Exercises three config-driven gates inside `check_routing.event_gate`:
    - `concurrency.max_inflight_subagents` — hard cap on concurrent Task
      dispatches, backed by the TTL-aware `_effective_subagent_count`.
    - `concurrency.serialize_verify` (BWS-T2-002) — denies a zero-handoff
      (pure shell-command) Task dispatch while a sibling subagent is active.
    - `progress_ledger.max_stall_markers` (BWS-T2-003) — denies a dispatched
      worker's own tool call once it accumulates too many consecutive calls
      with no progress marker in its most recent assistant text.

    All three gates are absent/null-safe by construction (no-op when the
    config key is missing), which is asserted explicitly below.
    """

    _AGENT_ID = "stall-agent-1"

    # -- shared fixture helpers ------------------------------------------

    def _make_subagent_transcript(
        self, tmp_path: Path, session_id: str, agent_id: str, text: str
    ) -> str:
        """Per-agent subagents/agent-<id>.jsonl file holding one assistant
        record with *text*, matching the real GATE-BUG-001 transcript shape
        (see TestGateBug001WorkerTranscriptResolution._make_transcript_tree)."""
        main_file = tmp_path / f"{session_id}.jsonl"
        if not main_file.exists():
            main_file.write_text("")
        subagents_dir = tmp_path / session_id / "subagents"
        subagents_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "parentUuid": None,
            "isSidechain": True,
            "agentId": agent_id,
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            "uuid": "assistant-record",
            "sessionId": session_id,
        }
        (subagents_dir / f"agent-{agent_id}.jsonl").write_text(json.dumps(record) + "\n")
        return str(main_file)

    def _subagent_payload(self, sid: str, transcript_path: str, agent_id: str, tool: str = "Edit") -> dict:
        return {
            "session_id": sid,
            "tool_name": tool,
            "transcript_path": transcript_path,
            "agent_id": agent_id,
            "agent_type": "general-purpose",
        }

    # -- concurrency cap ---------------------------------------------------

    def test_fourth_concurrent_dispatch_at_cap_denied(self, monkeypatch):
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "concurrency": {"max_inflight_subagents": 3}
        })
        sid = "bws-cap-1"
        for _ in range(3):
            cr.event_subagent_start({"session_id": sid}, False)

        _reset(sid)
        _scan(sid, "FO")
        result = _gate(sid, "Task")
        assert result.get("permission") == "deny"
        msg = result.get("agent_message", "") or ""
        assert "3/3" in msg
        assert "slot frees up" in msg

    def test_stale_subagent_count_releases_cap(self, monkeypatch):
        import time as _time
        import _config
        import session_state
        import subagent_tracker
        monkeypatch.setattr(_config, "_config_cache", {
            "concurrency": {"max_inflight_subagents": 3}
        })
        # ACTIVE_SUBAGENT_STALE_TTL_SECONDS is imported via `from _config import ...`
        # into cr, session_state, and subagent_tracker independently — each module
        # holds its own frozen binding. cr._save_state is actually
        # session_state._save_state (purges stale entries there) and
        # cr._effective_subagent_count is subagent_tracker._effective_subagent_count
        # (computes freshness there), so all three bindings must be patched for the
        # patched TTL to actually take effect.
        monkeypatch.setattr(cr, "ACTIVE_SUBAGENT_STALE_TTL_SECONDS", 5)
        monkeypatch.setattr(session_state, "ACTIVE_SUBAGENT_STALE_TTL_SECONDS", 5)
        monkeypatch.setattr(subagent_tracker, "ACTIVE_SUBAGENT_STALE_TTL_SECONDS", 5)

        sid = "bws-cap-stale"
        state = cr._load_state()
        state["subagents"][sid] = {"count": 3, "ts": _time.time() - 30}
        cr._save_state(state)  # purges the entry: 30s old > patched 5s TTL

        _reset(sid)
        _scan(sid, "FO")
        result = _gate(sid, "Task")
        assert result.get("permission") == "allow"

    # -- serialize_verify (BWS-T2-002) -------------------------------------

    def test_serialize_verify_denies_zero_handoff_while_active(self, monkeypatch):
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "concurrency": {"max_inflight_subagents": 3, "serialize_verify": True}
        })
        sid = "bws-serialize-1"
        cr.event_subagent_start({"session_id": sid}, False)

        _reset(sid)
        _scan(sid, "FO")
        result = _gate(sid, "Task", tool_input={"prompt": "Run: pytest tests/ -x -q"})
        assert result.get("permission") == "deny"
        assert "BWS-T2-002" in (result.get("agent_message", "") or "")

    def test_serialize_verify_allows_zero_handoff_when_none_active(self, monkeypatch):
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "concurrency": {"max_inflight_subagents": 3, "serialize_verify": True}
        })
        sid = "bws-serialize-2"

        _reset(sid)
        _scan(sid, "FO")
        result = _gate(sid, "Task", tool_input={"prompt": "Run: pytest tests/ -x -q"})
        assert result.get("permission") == "allow"

    def test_serialize_verify_absent_config_preserves_prior_behavior(self, monkeypatch):
        """No `concurrency.serialize_verify` key at all → config_get default
        False → the pre-BWS-T2-002 behavior (zero-handoff Task allowed even
        with an active sibling subagent) must be unchanged."""
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "concurrency": {"max_inflight_subagents": 3}
        })
        sid = "bws-serialize-absent"
        cr.event_subagent_start({"session_id": sid}, False)

        _reset(sid)
        _scan(sid, "FO")
        result = _gate(sid, "Task", tool_input={"prompt": "Run: pytest tests/ -x -q"})
        assert result.get("permission") == "allow"

    # -- stall / progress ledger (BWS-T2-003) ------------------------------

    def test_stall_counter_denies_after_max_consecutive_non_progress(self, monkeypatch, tmp_path):
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "progress_ledger": {"max_stall_markers": 3}
        })
        sid = "bws-stall-1"
        transcript_path = self._make_subagent_transcript(
            tmp_path, sid, self._AGENT_ID, "still working on it, nothing notable yet"
        )
        payload = self._subagent_payload(sid, transcript_path, self._AGENT_ID)

        assert cr.event_gate(payload, False).get("permission") == "allow"
        assert cr.event_gate(payload, False).get("permission") == "allow"
        result = cr.event_gate(payload, False)
        assert result.get("permission") == "deny"
        assert "partial report" in (result.get("agent_message", "") or "").lower()

    def test_stall_counter_resets_on_progress_marker(self, monkeypatch, tmp_path):
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "progress_ledger": {"max_stall_markers": 3}
        })
        sid = "bws-stall-2"
        stall_path = self._make_subagent_transcript(
            tmp_path, sid, self._AGENT_ID, "still working, nothing notable"
        )
        payload = self._subagent_payload(sid, stall_path, self._AGENT_ID)

        assert cr.event_gate(payload, False).get("permission") == "allow"
        assert cr.event_gate(payload, False).get("permission") == "allow"

        # A progress marker on what would be the 3rd (deny-triggering) call
        # resets the counter to 0 instead of denying.
        self._make_subagent_transcript(
            tmp_path, sid, self._AGENT_ID, "Done: fixed the bug, remaining: none"
        )
        assert cr.event_gate(payload, False).get("permission") == "allow"

        # Two more non-progress calls stay under the freshly-reset threshold.
        self._make_subagent_transcript(
            tmp_path, sid, self._AGENT_ID, "still working, nothing notable"
        )
        assert cr.event_gate(payload, False).get("permission") == "allow"
        assert cr.event_gate(payload, False).get("permission") == "allow"

    def test_stall_counter_resets_on_task_resume(self, monkeypatch, tmp_path):
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "progress_ledger": {"max_stall_markers": 3},
            "concurrency": {"max_inflight_subagents": 3, "serialize_verify": False},
        })
        sid = "bws-stall-resume"
        stall_path = self._make_subagent_transcript(
            tmp_path, sid, self._AGENT_ID, "still working, nothing notable"
        )
        payload = self._subagent_payload(sid, stall_path, self._AGENT_ID)

        assert cr.event_gate(payload, False).get("permission") == "allow"
        assert cr.event_gate(payload, False).get("permission") == "allow"

        # Parent resumes the same worker — resets the stall counter, mirroring
        # a fresh dispatch's reset semantics.
        _reset(sid)
        _scan(sid, "FO")
        resume_result = _gate(sid, "Task", tool_input={"resume": self._AGENT_ID})
        assert resume_result.get("permission") == "allow"

        # Two more non-progress calls must NOT deny — the counter restarted at 0.
        assert cr.event_gate(payload, False).get("permission") == "allow"
        assert cr.event_gate(payload, False).get("permission") == "allow"

    def test_stall_counter_absent_config_no_op(self, tmp_path, monkeypatch):
        """No `progress_ledger.max_stall_markers` configured → the gate is a
        no-op, preserving prior (pre-BWS-T2-003) unlimited-stall behavior."""
        import _config
        monkeypatch.setattr(_config, "_config_cache", {})
        sid = "bws-stall-absent"
        stall_path = self._make_subagent_transcript(
            tmp_path, sid, self._AGENT_ID, "still working, nothing notable"
        )
        payload = self._subagent_payload(sid, stall_path, self._AGENT_ID)
        for _ in range(10):
            assert cr.event_gate(payload, False).get("permission") == "allow"

    # -- wall-clock stall check (TICKET-037 / progress_ledger.max_stall_seconds) --

    def test_stall_time_based_denies_after_max_stall_seconds(self, monkeypatch, tmp_path):
        """A design/refactor-style orchestrator step that makes only one tool
        call but leaves it stale past max_stall_seconds must deny, even
        though the call-count check (max_stall_markers) never fires."""
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "progress_ledger": {"max_stall_markers": 6, "max_stall_seconds": 120}
        })
        sid = "bws-stall-time-1"
        transcript_path = self._make_subagent_transcript(
            tmp_path, sid, self._AGENT_ID, "still working on the redesign, nothing notable yet"
        )
        payload = self._subagent_payload(sid, transcript_path, self._AGENT_ID)

        # First call establishes last_marker_ts and is allowed (well under
        # both thresholds).
        assert cr.event_gate(payload, False).get("permission") == "allow"

        # Simulate 130s of wall-clock time elapsing with no marker emitted,
        # without sleeping in the test.
        state = cr._load_state()
        state["subagent_progress"][self._AGENT_ID]["last_marker_ts"] -= 130
        cr._save_state(state)

        result = cr.event_gate(payload, False)
        assert result.get("permission") == "deny"
        msg = (result.get("agent_message", "") or "").lower()
        assert "partial report" in msg
        assert "120" in msg or "2.0 min" in msg

    def test_stall_time_based_resets_on_progress_marker(self, monkeypatch, tmp_path):
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "progress_ledger": {"max_stall_markers": 6, "max_stall_seconds": 120}
        })
        sid = "bws-stall-time-2"
        transcript_path = self._make_subagent_transcript(
            tmp_path, sid, self._AGENT_ID, "still designing, nothing notable yet"
        )
        payload = self._subagent_payload(sid, transcript_path, self._AGENT_ID)
        assert cr.event_gate(payload, False).get("permission") == "allow"

        state = cr._load_state()
        state["subagent_progress"][self._AGENT_ID]["last_marker_ts"] -= 130
        cr._save_state(state)

        # A progress marker refreshes last_marker_ts instead of denying.
        self._make_subagent_transcript(
            tmp_path, sid, self._AGENT_ID, "progress: finished the interface draft, remaining: wiring"
        )
        assert cr.event_gate(payload, False).get("permission") == "allow"

        # Immediately after, elapsed time is ~0s, so it stays allowed.
        self._make_subagent_transcript(
            tmp_path, sid, self._AGENT_ID, "still designing, nothing notable yet"
        )
        assert cr.event_gate(payload, False).get("permission") == "allow"

    def test_stall_time_based_absent_config_no_op(self, monkeypatch, tmp_path):
        """No `progress_ledger.max_stall_seconds` configured → the time-based
        check is a no-op; only the pre-existing count-based check applies."""
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "progress_ledger": {"max_stall_markers": 6}
        })
        sid = "bws-stall-time-absent"
        transcript_path = self._make_subagent_transcript(
            tmp_path, sid, self._AGENT_ID, "still working, nothing notable"
        )
        payload = self._subagent_payload(sid, transcript_path, self._AGENT_ID)
        assert cr.event_gate(payload, False).get("permission") == "allow"

        state = cr._load_state()
        state["subagent_progress"][self._AGENT_ID]["last_marker_ts"] -= 10_000
        cr._save_state(state)

        # No time-based ceiling configured, so a huge elapsed gap alone must
        # not deny (count-based check is well under its threshold of 6).
        assert cr.event_gate(payload, False).get("permission") == "allow"

    # -- TICKET-039: per-worker-type overrides (mutation/read) --------------

    def _register_dispatch_worker(self, tmp_path, monkeypatch, sid: str, agent_id: str, worker: str) -> None:
        """Writes dispatch_registry.json so `_dispatch_worker_type_for` can
        resolve *worker* (mutation/read/shell/orchestrator) for *agent_id*,
        matching what mark_registry_busy records at real SubagentStart time
        (see check_routing.py ~L976)."""
        import _common
        registry = {
            "sessions": {
                sid: {
                    "agents": {
                        agent_id: {
                            "agent_id": agent_id,
                            "status": "busy",
                            "dispatch_worker": worker,
                        }
                    }
                }
            }
        }
        (tmp_path / "dispatch_registry.json").write_text(json.dumps(registry), encoding="utf-8")
        monkeypatch.setattr(_common, "DATA_DIR", tmp_path)

    def test_mutation_worker_uses_longer_stall_seconds_override(self, monkeypatch, tmp_path):
        """A mutation-worker dispatch gets the longer worker_overrides
        wall-clock ceiling (600s) instead of the tighter top-level default
        (180s) — 300s elapsed must NOT deny under the override."""
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "progress_ledger": {
                "max_stall_markers": 6,
                "max_stall_seconds": 180,
                "worker_overrides": {"mutation": {"max_stall_seconds": 600}},
            }
        })
        sid = "bws-stall-mutation-1"
        agent_id = "mutation-agent-1"
        self._register_dispatch_worker(tmp_path, monkeypatch, sid, agent_id, "mutation")
        transcript_path = self._make_subagent_transcript(
            tmp_path, sid, agent_id, "still applying the edit, nothing notable yet"
        )
        payload = self._subagent_payload(sid, transcript_path, agent_id)

        assert cr.event_gate(payload, False).get("permission") == "allow"

        state = cr._load_state()
        state["subagent_progress"][agent_id]["last_marker_ts"] -= 300
        cr._save_state(state)

        # 300s < the mutation override's 600s ceiling → still allowed, even
        # though 300s would have denied under the plain 180s default.
        assert cr.event_gate(payload, False).get("permission") == "allow"

        state = cr._load_state()
        state["subagent_progress"][agent_id]["last_marker_ts"] -= 650
        cr._save_state(state)

        # Past the 600s override → denies.
        result = cr.event_gate(payload, False)
        assert result.get("permission") == "deny"

    def test_read_worker_uses_longer_stall_seconds_override(self, monkeypatch, tmp_path):
        """Same override behavior for the read worker type."""
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "progress_ledger": {
                "max_stall_markers": 6,
                "max_stall_seconds": 180,
                "worker_overrides": {"read": {"max_stall_seconds": 600}},
            }
        })
        sid = "bws-stall-read-1"
        agent_id = "read-agent-1"
        self._register_dispatch_worker(tmp_path, monkeypatch, sid, agent_id, "read")
        transcript_path = self._make_subagent_transcript(
            tmp_path, sid, agent_id, "still searching the codebase, nothing notable yet"
        )
        payload = self._subagent_payload(sid, transcript_path, agent_id)

        assert cr.event_gate(payload, False).get("permission") == "allow"

        state = cr._load_state()
        state["subagent_progress"][agent_id]["last_marker_ts"] -= 300
        cr._save_state(state)
        assert cr.event_gate(payload, False).get("permission") == "allow"

    def test_shell_worker_without_override_keeps_default_180s(self, monkeypatch, tmp_path):
        """A registered worker type with no matching override entry
        (shell) falls back unchanged to the top-level 180s default."""
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "progress_ledger": {
                "max_stall_markers": 6,
                "max_stall_seconds": 180,
                "worker_overrides": {"mutation": {"max_stall_seconds": 600}},
            }
        })
        sid = "bws-stall-shell-1"
        agent_id = "shell-agent-1"
        self._register_dispatch_worker(tmp_path, monkeypatch, sid, agent_id, "shell")
        transcript_path = self._make_subagent_transcript(
            tmp_path, sid, agent_id, "still running the command, nothing notable yet"
        )
        payload = self._subagent_payload(sid, transcript_path, agent_id)

        assert cr.event_gate(payload, False).get("permission") == "allow"

        state = cr._load_state()
        state["subagent_progress"][agent_id]["last_marker_ts"] -= 200
        cr._save_state(state)

        # 200s > the un-overridden 180s default → denies, same as pre-TICKET-039.
        result = cr.event_gate(payload, False)
        assert result.get("permission") == "deny"

    def test_unresolvable_worker_type_falls_back_to_default(self, monkeypatch, tmp_path):
        """No dispatch_registry.json entry at all for this agent (registry
        lookup returns None) — must behave exactly like before TICKET-039:
        plain top-level thresholds, no crash."""
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "progress_ledger": {
                "max_stall_markers": 6,
                "max_stall_seconds": 180,
                "worker_overrides": {"mutation": {"max_stall_seconds": 600}},
            }
        })
        sid = "bws-stall-unknown-1"
        agent_id = "unknown-agent-1"
        transcript_path = self._make_subagent_transcript(
            tmp_path, sid, agent_id, "still working, nothing notable yet"
        )
        payload = self._subagent_payload(sid, transcript_path, agent_id)

        assert cr.event_gate(payload, False).get("permission") == "allow"

        state = cr._load_state()
        state["subagent_progress"][agent_id]["last_marker_ts"] -= 200
        cr._save_state(state)

        result = cr.event_gate(payload, False)
        assert result.get("permission") == "deny"


# ── SPO-007: validate_dispatch_schema gate ────────────────────────────────────

import validate_dispatch_schema as _vds  # noqa: E402
import _validate_shared as _vs  # noqa: E402
from validate_dispatch_schema import validate_dispatch_schema  # noqa: E402


def test_spo007_templates_match_schema_json():
    """Templates loaded at module-init must equal the values in dispatch-schemas.json.

    This test is the automated drift guard: if someone edits the JSON without
    updating the fallback (or vice-versa), this will catch it immediately.
    """
    schema_file = _PROJECT_ROOT / "scripts" / "docs" / "dispatch-schemas.json"
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    expected_mutation = schema["gate_templates"]["mutation"]
    expected_read = schema["gate_templates"]["read"]
    expected_create = schema["gate_templates"]["create"]
    expected_shell = schema["gate_templates"]["shell"]

    assert _vds._MUTATION_TEMPLATE == expected_mutation, (
        f"_MUTATION_TEMPLATE diverged from dispatch-schemas.json.\n"
        f"  Module : {_vds._MUTATION_TEMPLATE!r}\n"
        f"  JSON   : {expected_mutation!r}"
    )
    assert _vds._READ_TEMPLATE == expected_read, (
        f"_READ_TEMPLATE diverged from dispatch-schemas.json.\n"
        f"  Module : {_vds._READ_TEMPLATE!r}\n"
        f"  JSON   : {expected_read!r}"
    )
    assert _vds._CREATE_TEMPLATE == expected_create, (
        f"_CREATE_TEMPLATE diverged from dispatch-schemas.json.\n"
        f"  Module : {_vds._CREATE_TEMPLATE!r}\n"
        f"  JSON   : {expected_create!r}"
    )
    assert _vds._SHELL_TEMPLATE == expected_shell, (
        f"_SHELL_TEMPLATE diverged from dispatch-schemas.json.\n"
        f"  Module : {_vds._SHELL_TEMPLATE!r}\n"
        f"  JSON   : {expected_shell!r}"
    )


class TestSPO007DispatchSchema:
    """Unit tests for the SPO-007 JSON Structural Dispatch Protocol gate."""

    def test_spo007_thin_prompt_passes(self):
        """50-char prompt (< 200 tokens) is below the thin-dispatch threshold — must pass freely."""
        prompt = "Run: pytest tests/ -x -q"
        assert len(prompt) // 4 < 200
        result = validate_dispatch_schema(prompt)
        assert result.decision == "allow"

    def test_spo007_code_block_blocked(self):
        """Prompt with triple backticks (> 200 tokens) is hard-blocked with subtype=code_block."""
        # Craft a prompt that is >= 200 tokens (800 chars) and contains ```
        base = "x" * 797
        prompt = base + "```"
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-cb")
        assert result.decision == "deny"
        assert "code_block" in result.message

    def test_spo007_token_limit_blocked(self):
        """Valid mutation JSON > 560 tokens (no examples) is blocked with subtype=token_limit.

        Non-JSON input now hits not_json first (check order changed). This test uses
        valid mutation JSON padded past 560 tokens to exercise the token_limit path.
        """
        padding = "x" * 2200
        payload = {
            "worker": "mutation",
            "intent": "add bounds check in process batch",
            "files": ["scripts/gates.py:97,120"],
            "pattern": f"Insert early return when list is empty. {padding}",
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 > 560, f"Expected >560 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-tl")
        assert result.decision == "deny"
        assert "token_limit" in result.message

    def test_spo007_realistic_multiline_mutation_now_allowed(self):
        """GATE-BUG-003: realistic mutation prompt between 300-560 tokens is allowed.

        This previously exceeded the old 300-token cap and was wrongly rejected;
        the raised 560-token cap now allows it.
        """
        pattern = (
            "Insert four new helper functions immediately after the "
            "resolve_safe_path definition, before the existing overlap-detection "
            "block. First, add split_path_and_range(ref): parse a "
            "'path:start,end' or 'path:line' style reference string into a "
            "(path, start, end) tuple, returning (path, None, None) when no "
            "range suffix is present; reuse the existing comma/dash splitting "
            "logic rather than duplicating a new regex. Second, add "
            "ranges_overlap(a_start, a_end, b_start, b_end): a small "
            "integer-interval overlap check that treats a missing end as an "
            "open-ended range extending to infinity, returning False whenever "
            "either interval is entirely None. Third, add "
            "any_ref_overlap(new_ref, existing_refs): iterate the "
            "existing_refs list, call split_path_and_range on each entry, "
            "skip entries whose path does not match new_ref's path, and "
            "return True on the first overlapping range found via "
            "ranges_overlap; otherwise return False after the loop "
            "completes. Fourth, add region_aware_overlap_enabled(config): "
            "read the 'dispatch.region_aware_overlap' key from the "
            "already-loaded config dict, defaulting to False when the key "
            "is absent, and coerce the value to a bool before returning it, "
            "since some config sources store it as a string. Wire "
            "any_ref_overlap into the existing duplicate-file-reference "
            "check so that when region_aware_overlap_enabled(config) is "
            "True, overlapping ranges on the same path are treated as a "
            "real conflict instead of being silently allowed through, "
            "while non-overlapping ranges on the same path continue to "
            "pass. Keep all four functions pure (no I/O, no logging) and "
            "add short one-line docstrings to each describing the "
            "parameter types and return value so downstream callers do "
            "not need to re-derive the contract from the implementation."
        )
        payload = {
            "worker": "mutation",
            "intent": "add region-aware overlap helpers after resolve_safe_path",
            "files": ["scripts/estimate_dispatch_context.py:110-125"],
            "pattern": pattern,
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        tokens = len(prompt) // 4
        assert 300 < tokens < 560, f"Expected 300-560 tokens, got {tokens}"
        result = validate_dispatch_schema(prompt, session_id="spo007-gate-bug-003")
        assert result.decision == "allow"

    def test_spo007_not_json_blocked(self):
        """Prose prompt between 200-300 tokens (not valid JSON) is blocked with subtype=not_json."""
        # 840 chars = 210 tokens, within (200, 300] token window, no backticks
        prompt = ("This is a plain English task description. " * 21)[:840]
        tokens = len(prompt) // 4
        assert 200 <= tokens <= 300, f"Expected 200-300 tokens, got {tokens}"
        assert "```" not in prompt
        result = validate_dispatch_schema(prompt, session_id="spo007-nj")
        assert result.decision == "deny"
        assert "not_json" in result.message

    def test_spo007_valid_mutation_passes(self):
        """Well-formed mutation dispatch JSON passes all checks."""
        payload = {
            "worker": "mutation",
            "intent": "Add bounds check before list index access",
            "files": ["scripts/gates.py:97,120"],
            "pattern": "Insert an early return with a logged warning when the input list is empty.",
            "constraints": ["Do not change the function signature"],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        result = validate_dispatch_schema(prompt)
        assert result.decision == "allow", f"Expected allow, got deny: {result.message}"

    def test_spo007_valid_read_passes(self):
        """Well-formed read dispatch JSON passes all checks."""
        payload = {
            "worker": "read",
            "goal": "Find all call sites of enforce_role_gates",
            "scope": ["scripts/", "tests/e2e/"],
            "return": ["function signatures", "file:line references"],
            "max_files": 10,
        }
        prompt = json.dumps(payload)
        result = validate_dispatch_schema(prompt)
        assert result.decision == "allow", f"Expected allow, got deny: {result.message}"

    def test_spo007_valid_create_passes(self):
        """Well-formed create dispatch JSON passes all checks (padded to >= 200 tokens)."""
        padding = "x" * 800
        payload = {
            "worker": "create",
            "intent": f"Create ticket for task tracking {padding}",
            "file_path": "knowledge/plans/test.json",
        }
        prompt = json.dumps(payload)
        tokens = len(prompt) // 4
        assert tokens >= 200, f"Expected >=200 tokens, got {tokens}"
        result = validate_dispatch_schema(prompt)
        assert result.decision == "allow", f"Expected allow, got deny: {result.message}"

    def test_spo007_valid_shell_passes(self):
        """Well-formed shell dispatch JSON passes all checks (padded to >= 200 tokens).

        Command includes --timeout and -k per the bare-pytest safety block
        (S7 incident 3, scripts/schema_validators/shell_validator.py).
        """
        padding = "y" * 800
        payload = {
            "worker": "shell",
            "intent": f"Run tests for validation {padding}",
            "command": "pytest tests/ -x -q --timeout=30 -k test_smoke",
            "expect": "exit_code",
        }
        prompt = json.dumps(payload)
        tokens = len(prompt) // 4
        assert tokens >= 200, f"Expected >=200 tokens, got {tokens}"
        result = validate_dispatch_schema(prompt)
        assert result.decision == "allow", f"Expected allow, got deny: {result.message}"

    def test_spo007_create_missing_file_path_blocked(self):
        """Create JSON missing the 'file_path' field is blocked with subtype=missing_fields."""
        padding = "z" * 900
        payload = {
            "worker": "create",
            "intent": f"Create ticket for tracking {padding}",
            # 'file_path' is intentionally omitted
        }
        prompt = json.dumps(payload)
        tokens = len(prompt) // 4
        assert tokens >= 200, f"Expected >=200 tokens, got {tokens}"
        result = validate_dispatch_schema(prompt, session_id="spo007-create-mf")
        assert result.decision == "deny"
        assert "missing_fields" in result.message
        assert "file_path" in result.message

    def test_spo007_shell_missing_expect_blocked(self):
        """Shell JSON missing the 'expect' field is blocked with subtype=missing_fields."""
        padding = "w" * 900
        payload = {
            "worker": "shell",
            "intent": f"Run test command for validation {padding}",
            "command": "pytest tests/ -x -q",
            # 'expect' is intentionally omitted
        }
        prompt = json.dumps(payload)
        tokens = len(prompt) // 4
        assert tokens >= 200, f"Expected >=200 tokens, got {tokens}"
        result = validate_dispatch_schema(prompt, session_id="spo007-shell-mf")
        assert result.decision == "deny"
        assert "missing_fields" in result.message
        assert "expect" in result.message

    def test_spo007_missing_fields_blocked(self):
        """Mutation JSON missing the 'output' field is blocked with subtype=missing_fields."""
        # Pattern is padded to push total JSON length past 800 chars (200 tokens)
        padding = "x" * 700
        payload = {
            "worker": "mutation",
            "intent": "Add bounds check before list index access",
            "files": ["scripts/gates.py:97,120"],
            "pattern": f"Insert an early return when the input list is empty. {padding}",
            # 'output' is intentionally omitted
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-mf")
        assert result.decision == "deny"
        assert "missing_fields" in result.message
        assert "output" in result.message

    def test_spo007_schema_stuffing_blocked(self):
        """Mutation JSON with 6 constraints items is blocked with subtype=schema_stuffing."""
        # Pattern is padded to push total JSON length past 800 chars (200 tokens)
        padding = "y" * 500
        payload = {
            "worker": "mutation",
            "intent": "Add bounds check",
            "files": ["scripts/gates.py:97"],
            "pattern": f"Insert an early return when the input list is empty. {padding}",
            "constraints": [
                "Do not change the function signature",
                "Do not remove existing log statements",
                "Keep changes inside the function body only",
                "Preserve existing indentation style",
                "Do not add new imports",
                "This is the sixth constraint that pushes over the limit",
            ],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-ss")
        assert result.decision == "deny"
        assert "schema_stuffing" in result.message

    def test_spo007_multiple_content_violations_reported_together(self):
        """Multiple content violations (constraints, examples, why) are reported together in one denial."""
        payload = {
            "worker": "mutation",
            "intent": "fix bounds check",
            "files": ["scripts/gates.py:97,120"],
            "pattern": "Insert early return when input list is empty.",
            "constraints": ["Preserve existing output"],  # "output" not in allowlist
            "examples": [
                {"before": "for item in data:", "after": "x" * 450}  # exceeds 400 char limit
            ],
            "why": "y" * 200,  # exceeds 150 char limit
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        result = validate_dispatch_schema(prompt, session_id="spo007-multi")
        assert result.decision == "deny"
        # All three violations should be reported together
        assert "schema_stuffing" in result.message
        assert "invalid_examples" in result.message
        assert "invalid_field_value" in result.message
        assert "1)" in result.message
        assert "2)" in result.message
        assert "3)" in result.message
        assert "3 problems found" in result.message

    def test_spo007_single_content_violation_still_reports_singular(self):
        """Single content violation (why field) is reported with singular language."""
        padding = "x" * 700  # push intent field above 200-token thin-dispatch threshold
        payload = {
            "worker": "mutation",
            "intent": f"fix bounds check {padding}",
            "files": ["scripts/gates.py:97,120"],
            "pattern": "Insert early return when input list is empty.",
            "why": "y" * 200,  # exceeds 150 char limit
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-single")
        assert result.decision == "deny"
        assert "invalid_field_value" in result.message
        assert "1 problem found" in result.message

    # ── Per-worker token ceiling (added this session) ─────────────────────────

    def test_spo007_mutation_with_examples_within_500_allowed(self):
        """Mutation + examples field within 500-token ceiling must be allowed.

        Without examples the ceiling is 300. With examples it lifts to 500.
        This verifies the ceiling auto-selects based on examples presence.
        """
        padding = "x" * 700  # push above 200-token thin-dispatch threshold
        payload = {
            "worker": "mutation",
            "intent": f"fix bounds check {padding}",
            "files": ["scripts/gates.py:97,120"],
            "pattern": "Insert early return when input list is empty.",
            "examples": [
                {"before": "for item in data:", "after": "if not data:\n    return None\nfor item in data:"}
            ],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        tokens = len(prompt) // 4
        assert 200 < tokens <= 500, f"Expected 200-500 tokens, got {tokens}"
        result = validate_dispatch_schema(prompt, session_id="spo007-ex-ok")
        assert result.decision == "allow", f"Expected allow within 500-token ceiling, got: {result.message}"

    def test_spo007_mutation_with_examples_exceeds_500_blocked(self):
        """Mutation + examples exceeding 650 tokens must be blocked with token_limit."""
        padding = "x" * 2600
        payload = {
            "worker": "mutation",
            "intent": f"fix bounds check {padding}",
            "files": ["scripts/gates.py:97,120"],
            "pattern": "Insert early return when input list is empty.",
            "examples": [
                {"before": "for item in data:", "after": "if not data:\n    return None\nfor item in data:"}
            ],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 > 650, f"Expected >650 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-ex-over")
        assert result.decision == "deny"
        assert "token_limit" in result.message

    def test_spo007_shell_over_300_blocked(self):
        """Shell worker over 560 tokens must be blocked — shell keeps the 560 ceiling."""
        padding = "x" * 2200
        payload = {
            "worker": "shell",
            "intent": f"run verification {padding}",
            "command": "pytest tests/ -x -q",
            "expect": "exit_code",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 > 560, f"Expected >560 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-shell-over")
        assert result.decision == "deny"
        assert "too large" in result.message

    def test_spo007_read_over_500_blocked(self):
        """Read worker over 650 tokens must be blocked — read's ceiling was lifted to 650, not unlimited."""
        padding = "x" * 2600
        payload = {
            "worker": "read",
            "goal": f"find all call sites {padding}",
            "scope": ["scripts/"],
            "return": ["function signatures"],
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 > 650, f"Expected >650 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-read-over")
        assert result.decision == "deny"
        assert "token_limit" in result.message

    def test_spo007_read_under_500_allowed(self):
        """Read worker under the new 650 ceiling must be allowed."""
        padding = "x" * 2300
        payload = {
            "worker": "read",
            "goal": f"find all call sites {padding}",
            "scope": ["scripts/"],
            "return": ["function signatures"],
        }
        prompt = json.dumps(payload)
        assert 560 < len(prompt) // 4 <= 650, f"Expected 560-650 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-read-under")
        assert result.decision == "allow"

    # ── examples field validation (added this session) ────────────────────────

    def test_spo007_valid_mutation_with_all_new_fields_passes(self):
        """Mutation with where, why, and examples all valid must be allowed."""
        padding = "p" * 600
        payload = {
            "worker": "mutation",
            "intent": f"add bounds check {padding}",
            "files": ["scripts/gates.py:97,120"],
            "where": "process_batch, before the for-loop",
            "pattern": "Insert early return when input list is empty.",
            "why": "Callers expect None on empty input. Hot path.",
            "examples": [
                {"before": "for item in data:", "after": "if not data:\n    return None\nfor item in data:"}
            ],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-all-fields")
        assert result.decision == "allow", f"Expected allow, got: {result.message}"

    def test_spo007_examples_too_many_items_blocked(self):
        """examples with 4 items (max 3) must be blocked with invalid_examples."""
        padding = "q" * 600
        payload = {
            "worker": "mutation",
            "intent": f"fix all call sites {padding}",
            "files": ["scripts/gates.py:1"],
            "pattern": "Apply transformation.",
            "examples": [
                {"before": "a", "after": "b"},
                {"before": "c", "after": "d"},
                {"before": "e", "after": "f"},
                {"before": "g", "after": "h"},  # 4th item — over limit
            ],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-ex-many")
        assert result.decision == "deny"
        assert "invalid_examples" in result.message

    def test_spo007_examples_item_not_dict_blocked(self):
        """examples item that is a string (not dict) must be blocked with invalid_examples."""
        padding = "r" * 600
        payload = {
            "worker": "mutation",
            "intent": f"fix import order {padding}",
            "files": ["scripts/gates.py:1"],
            "pattern": "Move import outside try block.",
            "examples": ["this is a string, not a dict"],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-ex-str")
        assert result.decision == "deny"
        assert "invalid_examples" in result.message

    def test_spo007_examples_missing_key_blocked(self):
        """examples item missing 'after' key must be blocked with invalid_examples."""
        padding = "s" * 600
        payload = {
            "worker": "mutation",
            "intent": f"fix import order {padding}",
            "files": ["scripts/gates.py:1"],
            "pattern": "Move import outside try block.",
            "examples": [{"before": "try:\n    import x"}],  # missing 'after'
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-ex-noafter")
        assert result.decision == "deny"
        assert "invalid_examples" in result.message

    def test_spo007_examples_before_exceeds_400_blocked(self):
        """examples[0]['before'] > 400 chars must be blocked with invalid_examples."""
        padding = "t" * 600
        payload = {
            "worker": "mutation",
            "intent": f"fix bounds check {padding}",
            "files": ["scripts/gates.py:1"],
            "pattern": "Insert early return.",
            "examples": [{"before": "x" * 401, "after": "short after"}],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-ex-long-before")
        assert result.decision == "deny"
        assert "invalid_examples" in result.message

    def test_spo007_examples_after_exceeds_400_blocked(self):
        """examples[0]['after'] > 400 chars must be blocked with invalid_examples."""
        padding = "u" * 600
        payload = {
            "worker": "mutation",
            "intent": f"fix bounds check {padding}",
            "files": ["scripts/gates.py:1"],
            "pattern": "Insert early return.",
            "examples": [{"before": "short before", "after": "y" * 401}],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-ex-long-after")
        assert result.decision == "deny"
        assert "invalid_examples" in result.message

    def test_spo007_examples_backtick_in_before_blocked(self):
        """examples[0]['before'] containing a backtick must be blocked with invalid_examples."""
        padding = "v" * 600
        payload = {
            "worker": "mutation",
            "intent": f"fix import {padding}",
            "files": ["scripts/gates.py:1"],
            "pattern": "Move import outside try.",
            "examples": [{"before": "use `import x` here", "after": "import x"}],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-ex-backtick")
        assert result.decision == "deny"
        assert "invalid_examples" in result.message

    # ── where / why field validation (added this session) ────────────────────

    def test_spo007_where_exceeds_80_chars_blocked(self):
        """where field > 170 chars must be blocked with invalid_field_value."""
        padding = "w" * 600
        payload = {
            "worker": "mutation",
            "intent": f"fix bounds check {padding}",
            "files": ["scripts/gates.py:97"],
            "where": "a" * 171,  # one char over limit
            "pattern": "Insert early return.",
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-where-long")
        assert result.decision == "deny"
        assert "invalid_field_value" in result.message

    def test_spo007_where_valid_passes(self):
        """where field at exactly 170 chars must be allowed."""
        padding = "z" * 600
        payload = {
            "worker": "mutation",
            "intent": f"fix bounds check {padding}",
            "files": ["scripts/gates.py:97"],
            "where": "a" * 170,  # exactly at limit
            "pattern": "Insert early return.",
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-where-ok")
        assert result.decision == "allow", f"Expected allow, got: {result.message}"

    def test_spo007_why_exceeds_150_chars_blocked(self):
        """why field > 150 chars must be blocked with invalid_field_value."""
        padding = "m" * 600
        payload = {
            "worker": "mutation",
            "intent": f"fix bounds check {padding}",
            "files": ["scripts/gates.py:97"],
            "pattern": "Insert early return.",
            "why": "b" * 151,  # one char over limit
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-why-long")
        assert result.decision == "deny"
        assert "invalid_field_value" in result.message

    def test_spo007_why_valid_passes(self):
        """why field at exactly 150 chars must be allowed."""
        padding = "n" * 600
        payload = {
            "worker": "mutation",
            "intent": f"fix bounds check {padding}",
            "files": ["scripts/gates.py:97"],
            "pattern": "Insert early return.",
            "why": "b" * 150,  # exactly at limit
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-why-ok")
        assert result.decision == "allow", f"Expected allow, got: {result.message}"

    def test_spo007_where_why_only_on_mutation_worker(self):
        """where and why fields on a non-mutation worker must not trigger validation errors.

        The gate only validates these fields for mutation workers — other workers
        pass unknown optional fields through silently (gate checks required fields only).
        """
        padding = "o" * 600
        payload = {
            "worker": "shell",
            "intent": f"run tests {padding}",
            "command": "pytest tests/ -x -q --timeout=30 -k test_smoke",
            "expect": "exit_code",
            "where": "a" * 81,  # would fail on mutation — should be ignored here
            "why": "b" * 151,   # same
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-shell-extra")
        assert result.decision == "allow", f"Expected allow for shell with extra fields, got: {result.message}"


class TestSPO007DenialLog:
    """SPO-007 denial telemetry: data/dispatch_denials.jsonl records category only.

    A denial must never persist prompt content, tokens, or a rejected-value
    snippet — only {subtype, session_id, ts}, and dispatch_schema_violation
    records must no longer land in data/usage.jsonl at all.
    """

    def _lines(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def test_denial_writes_only_subtype_session_ts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_vds, "DATA_DIR", tmp_path / "data")
        prompt = ("This is a plain English task description. " * 21)[:840]
        result = validate_dispatch_schema(prompt, session_id="denial-log-not-json")
        assert result.decision == "deny"
        records = self._lines(tmp_path / "data" / "dispatch_denials.jsonl")
        assert len(records) == 1
        assert set(records[0].keys()) == {"subtype", "session_id", "ts"}
        assert records[0]["subtype"] == "not_json"
        assert records[0]["session_id"] == "denial-log-not-json"

    def test_no_dispatch_schema_violation_in_usage_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_vds, "DATA_DIR", tmp_path / "data")
        padding = "x" * 700
        payload = {
            "worker": "mutation",
            "intent": "Add bounds check before list index access",
            "files": ["scripts/gates.py:97,120"],
            "pattern": f"Insert an early return when the input list is empty. {padding}",
        }
        prompt = json.dumps(payload)
        result = validate_dispatch_schema(prompt, session_id="denial-log-missing-fields")
        assert result.decision == "deny"
        usage_log = tmp_path / "data" / "routing_events.jsonl"
        for record in self._lines(usage_log):
            assert record.get("type") != "dispatch_schema_violation"

    def test_no_rejected_item_snippet_on_long_constraint_denial(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_vds, "DATA_DIR", tmp_path / "data")
        padding = "y" * 700
        payload = {
            "worker": "mutation",
            "intent": "Add bounds check",
            "files": ["scripts/gates.py:97"],
            "pattern": f"Insert an early return when the input list is empty. {padding}",
            "constraints": [
                "This single constraint item is deliberately padded past sixty characters",
            ],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="denial-log-rejected-item")
        assert result.decision == "deny"
        assert "schema_stuffing" in result.message
        for path in (tmp_path / "data" / "dispatch_denials.jsonl", tmp_path / "data" / "routing_events.jsonl"):
            if path.exists():
                assert "rejected_item" not in path.read_text()
                assert "sixty characters" not in path.read_text()


    # ── orchestrator worker (added this session) ──────────────────────────────

    def test_spo007_orchestrator_valid_passes(self):
        """Valid orchestrator dispatch with intent + step passes the gate."""
        padding = "x" * 900
        payload = {
            "worker": "orchestrator",
            "intent": "implement sequential explore gate",
            "step": f"Phase 0: haiku read worker covering scripts/ and hooks/hooks.json for exploration. {padding}",
            "model": "sonnet",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-valid")
        assert result.decision == "allow", f"Expected allow, got: {result.message}"

    def test_spo007_orchestrator_missing_step_blocked(self):
        """Orchestrator missing required 'step' field is blocked with missing_fields."""
        padding = "x" * 900
        payload = {
            "worker": "orchestrator",
            "intent": f"implement something complex {padding}",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-no-step")
        assert result.decision == "deny"
        assert "missing_fields" in result.message
        assert "step" in result.message

    def test_spo007_orchestrator_missing_intent_blocked(self):
        """Orchestrator missing required 'intent' field is blocked with missing_fields."""
        padding = "x" * 900
        payload = {
            "worker": "orchestrator",
            "plan": f"Phase 0: read. Phase 1: implement. Phase 2: test. {padding}",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-no-intent")
        assert result.decision == "deny"
        assert "missing_fields" in result.message
        assert "intent" in result.message

    def test_spo007_orchestrator_constraints_allowlist_exempt(self):
        """Orchestrator constraints bypass the allowlist — behavioral rules use non-allowlisted words."""
        padding = "x" * 900
        payload = {
            "worker": "orchestrator",
            "intent": "implement gate",
            "step": f"read source code. {padding}",
            "constraints": ["test and fix are separate dispatches", "all workers need explicit model"],
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-constraints")
        assert result.decision == "allow", f"Orchestrator constraints should bypass allowlist, got: {result.message}"

    def test_spo007_orchestrator_over_1200_token_ceiling_blocked(self):
        """Orchestrator over 1200 tokens is blocked with token_limit."""
        padding = "x" * 4900
        payload = {
            "worker": "orchestrator",
            "intent": "implement gate",
            "step": f"Phase 0: read. Phase 1: implement. Phase 2: test. {padding}",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 > 1200, f"Expected >1200 tokens, got {len(prompt) // 4}"
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-over")
        assert result.decision == "deny"
        assert "token_limit" in result.message

    def test_spo007_orchestrator_under_200_thin_dispatch_passes(self):
        """Short orchestrator prompt (<200 tokens) passes as a thin dispatch regardless of fields."""
        payload = {"worker": "orchestrator", "intent": "x"}
        prompt = json.dumps(payload)
        assert len(prompt) // 4 < 200
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-thin")
        assert result.decision == "allow"

    # ── TICKET-036 gate-side ceiling check (oversized orchestrator scope) ─────

    def test_spo007_orchestrator_20plus_files_blocked(self):
        """Step implying 20+ files is blocked with orchestrator_oversized_scope."""
        padding = "x" * 700
        payload = {
            "worker": "orchestrator",
            "intent": "restructure test suite",
            "step": f"Restructure tests/e2e into 6-8 chained journey scripts across 28 files. {padding}",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-20files")
        assert result.decision == "deny"
        assert "orchestrator_oversized_scope" in result.message
        assert "28" in result.message or "files" in result.message

    def test_spo007_orchestrator_3plus_artifact_types_blocked(self):
        """Step spanning 3+ distinct artifact-type keyword buckets is blocked."""
        padding = "x" * 700
        payload = {
            "worker": "orchestrator",
            "intent": "update infra",
            "step": (
                "Update the gate config, add new tests, rewrite the schema "
                f"docs, and touch the module scripts. {padding}"
            ),
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-artifacts")
        assert result.decision == "deny"
        assert "orchestrator_oversized_scope" in result.message

    def test_spo007_orchestrator_combined_ticket_redesign_blocked(self):
        """Step referencing 2+ distinct TICKET IDs (combined redesign) is blocked."""
        padding = "x" * 700
        payload = {
            "worker": "orchestrator",
            "intent": "combine ticket work",
            "step": f"Implement TICKET-001 and also finish TICKET-005 in the same pass. {padding}",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-tickets")
        assert result.decision == "deny"
        assert "orchestrator_oversized_scope" in result.message

    def test_spo007_orchestrator_redesign_verb_repeated_blocked(self):
        """Step with 2+ redesign-shaped verbs (redesign/rewrite/restructure) is blocked."""
        padding = "x" * 700
        payload = {
            "worker": "orchestrator",
            "intent": "big change",
            "step": f"Redesign the routing logic, then rewrite the dispatch layer to match. {padding}",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-verbs")
        assert result.decision == "deny"
        assert "orchestrator_oversized_scope" in result.message

    # ── TICKET-041 scattered-site sizing signal (single host file) ────────────

    def test_spo007_orchestrator_scattered_sites_phrase_blocked(self):
        """Step claiming 15+ scattered/non-contiguous sites in one file is blocked."""
        padding = "x" * 700
        payload = {
            "worker": "orchestrator",
            "intent": "rename symbol everywhere",
            "step": (
                "Apply the rename across 17 scattered occurrences in "
                f"scripts/gates.py. {padding}"
            ),
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-scattered")
        assert result.decision == "deny"
        assert "orchestrator_oversized_scope" in result.message
        assert "17" in result.message or "scattered" in result.message

    def test_spo007_orchestrator_scattered_line_list_blocked(self):
        """Step listing 15+ comma-separated line numbers for one file is blocked."""
        padding = "x" * 700
        lines = ",".join(str(10 + i * 7) for i in range(16))
        payload = {
            "worker": "orchestrator",
            "intent": "patch scattered call sites",
            "step": f"Update scripts/gates.py:{lines} to the new signature. {padding}",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-linelist")
        assert result.decision == "deny"
        assert "orchestrator_oversized_scope" in result.message

    def test_spo007_orchestrator_scattered_sites_below_threshold_passes(self):
        """Fewer than 15 scattered sites in one file does not trip the signal."""
        padding = "x" * 900
        payload = {
            "worker": "orchestrator",
            "intent": "rename symbol a few places",
            "step": (
                "Apply the rename across 5 scattered occurrences in "
                f"scripts/gates.py. {padding}"
            ),
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-fewsites")
        assert result.decision == "allow", f"Expected allow, got: {result.message}"

    def test_spo007_orchestrator_small_scope_step_passes(self):
        """A normally-scoped single-phase step (no oversized-scope signals) still passes."""
        padding = "x" * 900
        payload = {
            "worker": "orchestrator",
            "intent": "fix one gate check",
            "step": f"Add a single validation check to scripts/validate_dispatch_schema.py. {padding}",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200
        result = validate_dispatch_schema(prompt, session_id="spo007-orch-small")
        assert result.decision == "allow", f"Expected allow, got: {result.message}"


# ---------------------------------------------------------------------------
# SPO-007 allowlist branch coverage: config/constraint_allowlist.json loading,
# the schema_stuffing/allowlist rejection path, and the deliberate fail-open
# behavior when the allowlist file is missing or malformed (validate_dispatch_
# schema.py:43-47).
# ---------------------------------------------------------------------------

class TestSPO007AllowlistBranch:
    """Coverage for the constraint-allowlist branch of the SPO-007 gate."""

    def _mutation_prompt(self, constraint: str) -> str:
        padding = "x" * 700
        payload = {
            "worker": "mutation",
            "intent": f"fix bounds check {padding}",
            "files": ["scripts/gates.py:97,120"],
            "pattern": "Insert an early return when the input list is empty.",
            "constraints": [constraint],
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        assert len(prompt) // 4 >= 200, f"Expected >=200 tokens, got {len(prompt) // 4}"
        return prompt

    # ── positive: only-allowlisted words pass ──────────────────────────────

    def test_allowlist_all_words_known_passes(self):
        """Constraint built entirely from config/constraint_allowlist.json tokens is allowed."""
        prompt = self._mutation_prompt("Do not use new imports in the module")
        result = validate_dispatch_schema(prompt)
        assert result.decision == "allow", f"Expected allow, got deny: {result.message}"

    # ── negative: unknown word(s) block, with and without a close match ───

    @pytest.mark.parametrize(
        "constraint,expected_unknown",
        [
            ("Do not testt the module", "testt"),
            ("Do not touch the xyzzyplugh subsystem", "xyzzyplugh"),
            ("Never recursively traverse directories", "recursively"),
        ],
    )
    def test_allowlist_unknown_word_blocks_ordinary_prose(self, constraint, expected_unknown):
        """Each of these ordinary-prose constraints contains a word outside the closed
        vocabulary and must be hard-blocked with subtype=schema_stuffing,
        sub_reason=allowlist — proving the allowlist branch actually rejects prose
        rather than silently passing it (GATE-BUG-001-style false-allow risk)."""
        prompt = self._mutation_prompt(constraint)
        result = validate_dispatch_schema(prompt, session_id="spo007-allow-neg")
        assert result.decision == "deny"
        assert "schema_stuffing" in result.message
        assert expected_unknown in result.message

    # ── message content: names the allowlist path, the rejected word(s), and a suggestion ──

    def test_allowlist_denial_message_names_path_words_and_suggestion(self):
        """Denial message must cite config/constraint_allowlist.json, the rejected
        word, and a nearest-match suggestion (via difflib) so a caller can self-correct."""
        prompt = self._mutation_prompt("Do not testt the module")
        result = validate_dispatch_schema(prompt, session_id="spo007-allow-msg")
        assert result.decision == "deny"
        assert "config/constraint_allowlist.json" in result.message
        assert "testt" in result.message
        # 'testt' is a close match (difflib ratio) to the allowlisted word 'test'.
        assert "Nearest allowed match" in result.message
        assert "'test'" in result.message

    def test_allowlist_denial_message_no_close_match_says_so(self):
        """When no allowlist word is a close match, the suggestion text says so explicitly
        rather than silently omitting the suggestion clause."""
        prompt = self._mutation_prompt("Do not touch the xyzzyplugh subsystem")
        result = validate_dispatch_schema(prompt, session_id="spo007-allow-msg-nomatch")
        assert result.decision == "deny"
        assert "no close match" in result.message

    # ── docs-match-code: docstring, CLAUDE.md, and gate_templates all reference the vocabulary ──

    def test_allowlist_vocabulary_referenced_in_docstring_claude_md_and_templates(self):
        """The allowlist mechanism must be documented in three places that stay in sync:
        the module docstring (check #7), scripts/docs/dispatch-protocol.md's constraints
        section, and the gate_templates field descriptions in scripts/docs/dispatch-schemas.json.
        This guards against the docs drifting from the code path they describe."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        vds_source = (repo_root / "scripts" / "validate_dispatch_schema.py").read_text()
        dispatch_protocol_md = (repo_root / "scripts" / "docs" / "dispatch-protocol.md").read_text()
        schemas_json = (repo_root / "scripts" / "docs" / "dispatch-schemas.json").read_text()

        assert "config/constraint_allowlist.json" in vds_source
        assert "config/constraint_allowlist.json" in dispatch_protocol_md
        assert "config/constraint_allowlist.json" in schemas_json

    # ── fail-open on malformed/missing allowlist file (lines 43-47) ────────

    def test_allowlist_load_failure_is_documented_fail_open(self, monkeypatch, tmp_path):
        """_validate_shared.py:21-27 wraps the allowlist load in try/except and
        sets _CONSTRAINT_ALLOWLIST = None on ANY failure (missing file, malformed JSON,
        missing 'allowed_tokens' key). This is a deliberate fail-open choice (see the
        'Deliberate fail-open' comment) so a broken config file never blocks
        every dispatch in the repo. This test locks that choice in as intentional
        behavior rather than an accidental side effect: with the allowlist unset,
        a constraint containing words absent from the vocabulary must still be
        allowed through (only the allowlist check is skipped; the other schema_stuffing
        sub-checks — length/char-class/item-count — still apply and are unaffected).

        check_constraints() (in _validate_shared.py) reads _validate_shared's own
        module-level _CONSTRAINT_ALLOWLIST, not the copy re-exported into
        validate_dispatch_schema (`_vds._CONSTRAINT_ALLOWLIST` is a separate binding
        created by `from _validate_shared import _CONSTRAINT_ALLOWLIST`) — so the
        patch target must be _validate_shared itself."""
        bad_allowlist = tmp_path / "constraint_allowlist.json"
        bad_allowlist.write_text("{not valid json")

        # monkeypatch.setattr captures the ORIGINAL value before any mutation, so
        # both attributes are guaranteed to be restored (even the second setattr,
        # whose "original" would otherwise be the already-broken first value) once
        # this test ends — regardless of pass/fail. No direct global assignment.
        monkeypatch.setattr(_vs, "_ALLOWLIST_PATH", bad_allowlist)
        try:
            loaded = set(json.loads(bad_allowlist.read_text())["allowed_tokens"])
        except Exception:
            loaded = None  # mirrors the module-level try/except exactly
        monkeypatch.setattr(_vs, "_CONSTRAINT_ALLOWLIST", loaded)

        assert _vs._CONSTRAINT_ALLOWLIST is None, (
            "A malformed allowlist file must fail open (None), matching the documented "
            "behavior at _validate_shared.py:21-27 — not raise, and not fail closed."
        )

        prompt = self._mutation_prompt("Do not xyzzyplugh the recursively frobnicate module")
        result = validate_dispatch_schema(prompt, session_id="spo007-allow-failopen")
        assert result.decision == "allow", (
            "With the allowlist fail-open (None), unknown words must NOT block the "
            f"dispatch — got deny instead: {result.message}"
        )


# ---------------------------------------------------------------------------
# ENRICH-001: routing_declared enrichment fields
# ---------------------------------------------------------------------------

class TestRoutingDeclaredEnrichment:
    """Verify that event_scan emits routing_declared events with model/intent/rule_id."""

    def test_routing_declared_contains_enrichment_fields(self, isolated_data_dir, monkeypatch):
        """PostToolUse event_scan emits routing_declared with model, intent, and rule_id."""
        import _common

        sid = "enrich-001-a"
        # Reset to record prompt_intent in routing state
        cr.event_reset({"session_id": sid, "prompt": "Refactor auth module"}, False)
        # Inject a known prompt_intent into state
        state = cr._load_state()
        state.setdefault("sessions", {}).setdefault(sid, {})["prompt_intent"] = "mechanical"
        cr._save_state(state)

        # Patch USAGE_LOG on cr's _common reference and on _common itself
        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        # Simulate PostToolUse: payload has text (inline assistant text path)
        payload = {
            "session_id": sid,
            "text": "Routing: DIRECT — refactor auth",
            "tool_name": "Edit",
            "model": "claude-sonnet-4-5",
        }
        cr.event_scan(payload, False)

        # Read emitted events
        events = [
            json.loads(line)
            for line in (isolated_data_dir / "routing_events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        declared = [e for e in events if e.get("type") == "routing_declared"]
        assert declared, "Expected at least one routing_declared event"
        ev = declared[-1]
        assert "model" in ev, f"Missing 'model' in routing_declared: {ev}"
        assert "intent" in ev, f"Missing 'intent' in routing_declared: {ev}"
        assert "rule_id" in ev, f"Missing 'rule_id' in routing_declared: {ev}"

    def test_routing_declared_rule_id_format(self, isolated_data_dir, monkeypatch):
        """rule_id in routing_declared matches '{role}:{intent}' format."""
        import _common

        sid = "enrich-001-b"
        cr.event_reset({"session_id": sid, "prompt": "Refactor auth module"}, False)
        state = cr._load_state()
        state.setdefault("sessions", {}).setdefault(sid, {})["prompt_intent"] = "mechanical"
        cr._save_state(state)

        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        payload = {
            "session_id": sid,
            "text": "Routing: FW — additive changes",
            "tool_name": "Write",
            "model": "claude-haiku-3-5",
        }
        cr.event_scan(payload, False)

        events = [
            json.loads(line)
            for line in (isolated_data_dir / "routing_events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        declared = [e for e in events if e.get("type") == "routing_declared"]
        assert declared
        ev = declared[-1]
        role = ev.get("role", "")
        intent = ev.get("intent", "")
        expected_rule_id = f"{role}:{intent}"
        assert ev.get("rule_id") == expected_rule_id, (
            f"rule_id {ev.get('rule_id')!r} != '{expected_rule_id}'"
        )

    def test_routing_declared_model_fallback(self, isolated_data_dir, monkeypatch):
        """When payload has no model field, routing_declared falls back to the
        routing-matrix's model for the declared intent (TICKET-034: the old
        expectation of a bare 'unknown' predates this fallback and was stale)."""
        import _common

        sid = "enrich-001-c"
        cr.event_reset({"session_id": sid, "prompt": "Refactor auth module"}, False)
        state = cr._load_state()
        state.setdefault("sessions", {}).setdefault(sid, {})["prompt_intent"] = "agentic_action"
        cr._save_state(state)

        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        # Payload without 'model' key
        payload = {
            "session_id": sid,
            "text": "Routing: DIRECT — inline fix",
            "tool_name": "Edit",
            # no 'model' field
        }
        cr.event_scan(payload, False)

        events = [
            json.loads(line)
            for line in (isolated_data_dir / "routing_events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        declared = [e for e in events if e.get("type") == "routing_declared"]
        assert declared
        ev = declared[-1]
        expected_model = cr.get_routing_for_intent("agentic_action").get("orchestrator")
        assert ev.get("model") == expected_model, (
            f"Expected routing-matrix fallback model {expected_model!r} when payload "
            f"has no model, got: {ev.get('model')!r}"
        )

    def test_routing_declared_quantitative_model_presence(self, isolated_data_dir, monkeypatch):
        """Verify that 10 routing_declared events with real models have zero model='unknown'."""
        import _common

        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        valid_models = ["haiku", "sonnet", "opus", "gpt-4", "claude-3"]
        for i in range(10):
            sid = f"quantitative-{i:03d}"
            cr.event_reset({"session_id": sid, "prompt": f"Task {i}"}, False)
            state = cr._load_state()
            state.setdefault("sessions", {}).setdefault(sid, {})["prompt_intent"] = "mechanical"
            cr._save_state(state)

            payload = {
                "session_id": sid,
                "text": "Routing: DIRECT — mechanical work",
                "tool_name": "Edit",
                "model": valid_models[i % len(valid_models)],
            }
            cr.event_scan(payload, False)

        events = [
            json.loads(line)
            for line in (isolated_data_dir / "routing_events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        declared = [e for e in events if e.get("type") == "routing_declared"]
        assert len(declared) >= 10, f"Expected ≥10 routing_declared events, got {len(declared)}"

        unknown_count = sum(1 for e in declared if e.get("model") == "unknown")
        assert unknown_count == 0, (
            f"Expected zero model='unknown' with real models in payload, got {unknown_count}/{len(declared)}"
        )

    def test_routing_declared_intent_alignment(self, isolated_data_dir, monkeypatch):
        """Verify that routing_declared event has valid intent from valid set."""
        import _common

        sid = "intent-align-001"
        cr.event_reset({"session_id": sid, "prompt": "Design system"}, False)
        state = cr._load_state()
        state.setdefault("sessions", {}).setdefault(sid, {})["prompt_intent"] = "design"
        cr._save_state(state)

        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        payload = {
            "session_id": sid,
            "text": "Routing: DIRECT — architecture work",
            "tool_name": "Edit",
            "model": "opus",
        }
        cr.event_scan(payload, False)

        events = [
            json.loads(line)
            for line in (isolated_data_dir / "routing_events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        declared = [e for e in events if e.get("type") == "routing_declared"]
        assert declared
        ev = declared[-1]

        intent = ev.get("intent")
        valid_intents = {
            "agentic_action",
            "qa_simple",
            "qa_with_reads",
            "qa_architecture",
            "mechanical",
            "additive",
            "design",
            "ambiguous",
            "debug",
        }
        assert intent is not None, "intent field must be present"
        assert intent != "unknown", f"intent must not be 'unknown', got {intent!r}"
        assert intent in valid_intents, (
            f"intent {intent!r} not in valid set: {valid_intents}"
        )


# ---------------------------------------------------------------------------
# Role-vs-dispatched-tier cross-check (next to the PROD-19 nudges in
# check_routing.py's Task/Agent branch of event_gate). A declared FO/RB role
# is self-reported and was never checked against the tier of the model
# actually dispatched, so role analytics (routing_declared / role_tier_mismatch)
# could silently drift from reality. This records a `role_tier_mismatch`
# event on every mismatching dispatch and nudges (allow, never deny) once per
# session — a nudge that silently stops firing is worse than none, and the
# event itself is the input to role analytics, so both must be covered.
# ---------------------------------------------------------------------------

_RTM_PROMPT = "Summarize the current state of session tracking"


class TestRoleTierMismatchCrossCheck:
    """Regression coverage for the role-vs-dispatched-tier cross-check."""

    @staticmethod
    def _dispatch(sid: str, role: str, model: str, **extra_tool_input) -> dict:
        """One reset->scan->Task-dispatch cycle. `_RTM_PROMPT` classifies as
        dispatch-work label 'ambiguous' (no agentic-verb/judgment signal), so
        the earlier PROD-19 mechanical/judgment tier nudges never fire and
        can't mask the role_tier_mismatch check under test."""
        cr.event_reset({"session_id": sid, "prompt": _RTM_PROMPT}, False)
        _scan(sid, role)
        tool_input = {"prompt": _RTM_PROMPT, "model": model, **extra_tool_input}
        return cr.event_gate(
            {"session_id": sid, "tool_name": "Task", "tool_input": tool_input}, False
        )

    @staticmethod
    def _mismatch_events(isolated_data_dir) -> list[dict]:
        log = isolated_data_dir / "routing_events.jsonl"
        if not log.exists():
            return []
        events = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
        return [e for e in events if e.get("type") == "role_tier_mismatch"]

    def test_fo_with_sonnet_worker_records_mismatch_expecting_rb(self, isolated_data_dir, monkeypatch):
        """FO declared + sonnet (reasoning-tier) worker -> event with expected_role RB."""
        import _common
        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        result = self._dispatch("rtm-fo-sonnet", "FO", "claude-sonnet-4-5")

        assert result.get("permission") == "allow"
        events = self._mismatch_events(isolated_data_dir)
        assert events, "Expected a role_tier_mismatch event"
        assert events[-1].get("declared_role") == "FO"
        assert events[-1].get("expected_role") == "RB"

    def test_rb_with_haiku_worker_records_mismatch_expecting_fo(self, isolated_data_dir, monkeypatch):
        """RB declared + haiku (fast-tier) worker -> event with expected_role FO."""
        import _common
        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        result = self._dispatch("rtm-rb-haiku", "RB", "claude-haiku-4-5")

        assert result.get("permission") == "allow"
        events = self._mismatch_events(isolated_data_dir)
        assert events, "Expected a role_tier_mismatch event"
        assert events[-1].get("declared_role") == "RB"
        assert events[-1].get("expected_role") == "FO"

    def test_fo_with_haiku_worker_records_nothing(self, isolated_data_dir, monkeypatch):
        """FO + fast-tier worker is the expected pairing -> no mismatch event."""
        import _common
        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        result = self._dispatch("rtm-fo-haiku", "FO", "claude-haiku-4-5")

        assert result.get("permission") == "allow"
        assert self._mismatch_events(isolated_data_dir) == []

    def test_rb_with_sonnet_worker_records_nothing(self, isolated_data_dir, monkeypatch):
        """RB + reasoning-tier worker is the expected pairing -> no mismatch event."""
        import _common
        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        result = self._dispatch("rtm-rb-sonnet", "RB", "claude-sonnet-4-5")

        assert result.get("permission") == "allow"
        assert self._mismatch_events(isolated_data_dir) == []

    def test_second_mismatch_in_session_records_event_without_repeating_nudge(
        self, isolated_data_dir, monkeypatch
    ):
        """A second mismatching dispatch in the same session still gets its
        own role_tier_mismatch event, but the nudge is allow-once-per-session:
        the second dispatch's agent_message must not repeat the nudge text."""
        import _common
        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        sid = "rtm-repeat"
        first = self._dispatch(sid, "FO", "claude-sonnet-4-5")
        assert first.get("permission") == "allow"
        first_msg = first.get("agent_message", "") or ""
        assert "Routing: RB" in first_msg

        second = self._dispatch(sid, "FO", "claude-opus-4-5")
        assert second.get("permission") == "allow"
        second_msg = second.get("agent_message", "") or ""
        assert "Routing: RB" not in second_msg, (
            f"Nudge text must not repeat on the 2nd mismatch, got: {second_msg!r}"
        )

        events = self._mismatch_events(isolated_data_dir)
        assert len(events) == 2, f"Expected 2 role_tier_mismatch events, got {len(events)}"

    def test_mismatch_never_denies(self, isolated_data_dir, monkeypatch):
        """role_tier_mismatch is advisory only — repeated mismatches across a
        session must always allow, never deny."""
        import _common
        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        sid = "rtm-never-deny"
        for i, model in enumerate(["claude-sonnet-4-5", "claude-opus-4-5", "claude-sonnet-4-5"]):
            result = self._dispatch(sid, "FO", model)
            assert result.get("permission") == "allow", f"dispatch {i} unexpectedly denied: {result}"

    def test_no_declared_role_records_nothing(self, isolated_data_dir, monkeypatch):
        """DIRECT is not FO/RB -> the cross-check is skipped entirely."""
        import _common
        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        result = self._dispatch("rtm-no-role", "DIRECT", "claude-sonnet-4-5")

        assert result.get("permission") == "allow"
        assert self._mismatch_events(isolated_data_dir) == []

    def test_resume_dispatch_skips_check(self, isolated_data_dir, monkeypatch):
        """A resume dispatch reuses the original worker's tier by definition
        -> the cross-check is skipped, regardless of declared role/model."""
        import _common
        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        result = self._dispatch("rtm-resume", "FO", "claude-sonnet-4-5", resume="worker-abc")

        assert result.get("permission") == "allow"
        assert self._mismatch_events(isolated_data_dir) == []

    def test_explore_subagent_skips_check(self, isolated_data_dir, monkeypatch):
        """Explore/plan subagent_type dispatches are short-lived and
        fixed-tier by convention -> the cross-check is skipped."""
        import _common
        monkeypatch.setattr(_common, "USAGE_LOG", isolated_data_dir / "routing_events.jsonl")

        result = self._dispatch(
            "rtm-explore", "FO", "claude-sonnet-4-5", subagent_type="explore"
        )

        assert result.get("permission") == "allow"
        assert self._mismatch_events(isolated_data_dir) == []


# ---------------------------------------------------------------------------
# Path-overlap and resume_used tests
# ---------------------------------------------------------------------------

def test_same_turn_overlap_different_dirs(tmp_path, monkeypatch):
    """Two files named api.py in different directories must NOT be treated as the same file."""
    import agent_call_tracker as act
    import spawn_tracker
    import _config

    stats_file = tmp_path / "stats.json"
    # spawn_tracker owns the actual read/write path for STATS_FILE (its own
    # module-level binding); agent_call_tracker only re-exports it for
    # display/messages, so that copy must be patched too to affect I/O.
    monkeypatch.setattr(spawn_tracker, "STATS_FILE", stats_file)
    monkeypatch.setattr(act, "STATS_FILE", stats_file)
    monkeypatch.setattr(act, "ROUTING_STATE_FILE", tmp_path / "routing.json")
    monkeypatch.setattr(_config, "_config_cache", {
        "session_cost_budget": {"budget_usd": 100.0, "warning_pcts": [50, 80]},
        "routing": {"subagent_ttl_seconds": 300},
        "session_limits": {"agent_call_tracker": 50},
    })

    # First explore — reads api.py from scripts/
    p1 = {
        "session_id": "overlap-dirs-test",
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "Read @scripts/api.py",
            "model": "haiku",
            "subagent_type": "Explore",
        },
    }
    act.event_preuse(p1, False)

    # Second explore — reads api.py from a different directory (tests/)
    p2 = {
        "session_id": "overlap-dirs-test",
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "Read @tests/api.py",
            "model": "haiku",
            "subagent_type": "Explore",
        },
    }
    r2 = act.event_preuse(p2, False)
    msg = (r2 or {}).get("agent_message", "") or ""
    # Different full paths → no overlap nudge
    assert "already read" not in msg.lower()
    assert "overlap" not in msg.lower()


def test_resume_used_event_emitted(tmp_path, monkeypatch):
    """event_preuse with a resume key must emit a resume_used event via append_event."""
    import agent_call_tracker as act
    import _config
    import _common

    monkeypatch.setattr(act, "STATS_FILE", tmp_path / "stats.json")
    monkeypatch.setattr(act, "ROUTING_STATE_FILE", tmp_path / "routing.json")
    monkeypatch.setattr(_config, "_config_cache", {
        "session_cost_budget": {"budget_usd": 100.0, "warning_pcts": [50, 80]},
        "routing": {"subagent_ttl_seconds": 300},
        "session_limits": {"agent_call_tracker": 50},
    })

    emitted: list[dict] = []

    def _capture_event(record: dict) -> None:
        emitted.append(record)

    # Patch append_event in both the act module namespace and _common
    monkeypatch.setattr(act, "append_event", _capture_event)
    monkeypatch.setattr(_common, "append_event", _capture_event)

    payload = {
        "session_id": "resume-test",
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "Continue previous analysis",
            "model": "haiku",
            "subagent_type": "Explore",
            "resume": "worker-abc-123",
        },
    }
    act.event_preuse(payload, False)

    resume_events = [e for e in emitted if e.get("type") == "resume_used"]
    assert resume_events, f"Expected at least one resume_used event, got events: {emitted}"
    ev = resume_events[0]
    assert ev.get("resume_id") == "worker-abc-123", (
        f"resume_id mismatch: {ev.get('resume_id')!r}"
    )
    assert ev.get("session_id") == "resume-test", (
        f"session_id mismatch: {ev.get('session_id')!r}"
    )


def _counter(name: str) -> int:
    state = cr._load_state()
    return int(state.get("counters", {}).get(name, 0))


# ---------------------------------------------------------------------------
# PROD-19 Task-dispatch tier-mismatch: judgment-on-fast is advisory only.
#
# event_gate's Task/Agent branch used to return _deny(...) for BOTH the
# mechanical-on-reasoning case and the judgment-on-fast case, even though
# event_subagent_start's identical judgment-on-fast check (~line 2022) has
# always been advisory-only (appended to `nudges`, never blocks). That made
# the Task-dispatch path hard-block a dispatch its own sibling check treats
# as a quality suggestion, silently stopping the FO-to-haiku dispatch pattern.
# Fixed so judgment-on-fast now returns _allow(...) with the nudge message;
# mechanical-on-reasoning is unchanged (still a hard _deny).
# ---------------------------------------------------------------------------

_JUDGMENT_PROMPT = "Decide which approach to take for this trade-off, then brainstorm options"
_MECHANICAL_PROMPT = "Fix the typo in scripts/foo.py and rename the helper"


class TestDispatchTierMismatchAdvisory:
    """Regression coverage: judgment-on-fast nudges instead of blocking."""

    def test_judgment_on_fast_model_allows_dispatch_with_nudge(self):
        sid = "tier-judgment-fast"
        _reset(sid, _JUDGMENT_PROMPT)
        _scan(sid, "DIRECT")

        result = _gate(
            sid,
            "Task",
            tool_input={"prompt": _JUDGMENT_PROMPT, "model": "claude-haiku-4-5"},
        )

        assert result.get("permission") == "allow", (
            "judgment-on-fast must be advisory (allow), not a hard deny — "
            f"got {result!r}"
        )
        message = result.get("agent_message") or ""
        assert "judgment" in message.lower()

    def test_mechanical_on_reasoning_model_still_denies(self):
        """The sibling branch (mechanical work on a reasoning-tier model)
        must remain an unchanged hard deny."""
        sid = "tier-mechanical-reasoning"
        _reset(sid, _MECHANICAL_PROMPT)
        _scan(sid, "DIRECT")

        result = _gate(
            sid,
            "Task",
            tool_input={"prompt": _MECHANICAL_PROMPT, "model": "claude-sonnet-4-5"},
        )

        assert result.get("permission") == "deny", (
            f"mechanical-on-reasoning must still hard-deny — got {result!r}"
        )
        message = result.get("agent_message") or ""
        assert "mechanical" in message.lower()


# ---------------------------------------------------------------------------
# Session-scoped escalation: judgment-on-fast nudges 1-4 stay advisory
# (allow), but the 5th occurrence in one session escalates to a hard deny.
# This is tracked by a NEW session-scoped field
# (entry["judgment_on_fast_nudge_count_session"]), kept separate from the
# existing global observability counter state["counters"]
# ["dispatch_nudge_judgment_on_fast"], which must keep incrementing on every
# occurrence regardless of the escalation state.
# ---------------------------------------------------------------------------


class TestJudgmentOnFastEscalation:
    """Regression coverage: 5th judgment-on-fast nudge in a session blocks."""

    def test_first_four_nudges_allow_fifth_denies(self):
        sid = "tier-judgment-fast-escalate"
        _reset(sid, _JUDGMENT_PROMPT)
        _scan(sid, "DIRECT")

        for i in range(4):
            result = _gate(
                sid,
                "Task",
                tool_input={"prompt": _JUDGMENT_PROMPT, "model": "claude-haiku-4-5"},
            )
            assert result.get("permission") == "allow", (
                f"occurrence {i + 1} should still be an advisory allow — got {result!r}"
            )

        result = _gate(
            sid,
            "Task",
            tool_input={"prompt": _JUDGMENT_PROMPT, "model": "claude-haiku-4-5"},
        )
        assert result.get("permission") == "deny", (
            f"5th occurrence in one session must escalate to deny — got {result!r}"
        )
        message = result.get("agent_message") or ""
        assert "judgment" in message.lower()

    def test_global_observability_counter_still_increments_through_escalation(self):
        """state['counters']['dispatch_nudge_judgment_on_fast'] must keep
        incrementing on every occurrence, including the one that denies —
        it is a long-term tuning signal, independent of the session-scoped
        escalation counter."""
        sid = "tier-judgment-fast-escalate-counter"
        _reset(sid, _JUDGMENT_PROMPT)
        _scan(sid, "DIRECT")

        for _ in range(5):
            _gate(
                sid,
                "Task",
                tool_input={"prompt": _JUDGMENT_PROMPT, "model": "claude-haiku-4-5"},
            )

        state = cr._load_state()
        assert state["counters"].get("dispatch_nudge_judgment_on_fast", 0) == 5

    def test_escalation_counter_persists_across_turns_in_same_session(self):
        """A new turn (event_reset) must NOT clear the session-scoped
        escalation counter — it only starts at 0 when a session is first
        created (_default_session_entry). Four nudges followed by a new
        turn and one more nudge must still hit the 5th-occurrence deny,
        since all five occurred within the same session_id."""
        sid = "tier-judgment-fast-escalate-persist"
        _reset(sid, _JUDGMENT_PROMPT)
        _scan(sid, "DIRECT")

        for _ in range(4):
            _gate(
                sid,
                "Task",
                tool_input={"prompt": _JUDGMENT_PROMPT, "model": "claude-haiku-4-5"},
            )

        # New turn within the SAME session — escalation counter must carry
        # over, not reset, even though event_reset runs.
        _reset(sid, _JUDGMENT_PROMPT)
        _scan(sid, "DIRECT")

        result = _gate(
            sid,
            "Task",
            tool_input={"prompt": _JUDGMENT_PROMPT, "model": "claude-haiku-4-5"},
        )
        assert result.get("permission") == "deny", (
            f"escalation counter must persist across turns in the same "
            f"session — got {result!r}"
        )

    def test_escalation_counter_starts_at_zero_for_new_session(self):
        """A brand new session_id must start the escalation counter at 0
        (via _default_session_entry), independent of any other session's
        accumulated count."""
        sid = "tier-judgment-fast-escalate-newsession"
        _reset(sid, _JUDGMENT_PROMPT)
        _scan(sid, "DIRECT")

        result = _gate(
            sid,
            "Task",
            tool_input={"prompt": _JUDGMENT_PROMPT, "model": "claude-haiku-4-5"},
        )
        assert result.get("permission") == "allow", (
            f"first occurrence in a brand new session must be an advisory "
            f"allow — got {result!r}"
        )


# --- GATE-BUG-003: routing_matrix.test_runner enforcement + cost-aware round trips ---

from enforce_test_runner_model import check_test_runner_model  # noqa: E402


class TestGateBug003TestRunnerModelEnforcement:
    """Regression coverage for GATE-BUG-003."""

    _MUTATION_TEST_PROMPT = json.dumps({
        "worker": "mutation",
        "intent": "fix off-by-one bug",
        "files": ["scripts/foo.py:10"],
        "pattern": "Fix the bug, then run pytest tests/e2e/ -x -q and report the result.",
        "output": "targeted_edits",
    })

    _MUTATION_NO_TEST_PROMPT = json.dumps({
        "worker": "mutation",
        "intent": "rename variable",
        "files": ["scripts/foo.py:10"],
        "pattern": "Rename local variable x to y.",
        "output": "targeted_edits",
    })

    def _isolate_routing_state(self, monkeypatch, tmp_path):
        """Point agent_call_tracker's ROUTING_STATE_FILE (consulted by this
        gate via _get_work_unit_intent) at an empty tmp file, so resolution
        falls back to routing_matrix._default deterministically, independent
        of any real session state on disk.

        _get_work_unit_intent is actually defined in bws_enforcer.py and
        closes over bws_enforcer's OWN ROUTING_STATE_FILE global --
        agent_call_tracker just re-imports/re-exports the function, so
        patching act.ROUTING_STATE_FILE alone is a no-op for it. Patch
        bws_enforcer's copy too so isolation actually takes effect."""
        import agent_call_tracker as act
        import bws_enforcer
        monkeypatch.setattr(act, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")
        monkeypatch.setattr(bws_enforcer, "ROUTING_STATE_FILE", tmp_path / "routing_state.json")

    def test_sonnet_chained_pytest_pattern_denied_citing_test_runner(self, monkeypatch, tmp_path):
        """A mutation worker whose pattern chains a pytest run must be denied
        when its model isn't routing_matrix.test_runner for the intent — the
        real defect from GATE-BUG-003's RCA (7/7 dispatches ran pytest on
        sonnet with zero enforcement)."""
        self._isolate_routing_state(monkeypatch, tmp_path)
        result = check_test_runner_model({
            "tool_name": "Task",
            "session_id": "gb3-sonnet",
            "tool_input": {"model": "claude-sonnet-4-5", "prompt": self._MUTATION_TEST_PROMPT},
        })
        assert result.decision == "deny"
        assert "routing_matrix.test_runner" in result.message
        assert "haiku" in result.message

    def test_haiku_chained_pytest_pattern_allowed(self, monkeypatch, tmp_path):
        """The same chained-pytest prompt on the matrix's test_runner model
        (haiku for the default/_default intent) must be allowed."""
        self._isolate_routing_state(monkeypatch, tmp_path)
        result = check_test_runner_model({
            "tool_name": "Task",
            "session_id": "gb3-haiku",
            "tool_input": {"model": "claude-haiku-4-5", "prompt": self._MUTATION_TEST_PROMPT},
        })
        assert result.decision == "allow"

    def test_non_test_prompt_unaffected_on_sonnet(self, monkeypatch, tmp_path):
        """A mutation prompt that never mentions running tests is untouched
        by this gate regardless of model."""
        self._isolate_routing_state(monkeypatch, tmp_path)
        result = check_test_runner_model({
            "tool_name": "Task",
            "session_id": "gb3-notest",
            "tool_input": {"model": "claude-sonnet-4-5", "prompt": self._MUTATION_NO_TEST_PROMPT},
        })
        assert result.decision == "allow"

    def test_unstructured_prompt_fails_open(self, monkeypatch, tmp_path):
        """Unstructured (non-JSON) prompts are out of scope for this gate —
        it only judges the structured worker schema."""
        self._isolate_routing_state(monkeypatch, tmp_path)
        result = check_test_runner_model({
            "tool_name": "Task",
            "session_id": "gb3-unstructured",
            "tool_input": {"model": "claude-sonnet-4-5", "prompt": "Run pytest and report back."},
        })
        assert result.decision == "allow"


from enforce_worker_model import check_worker_model  # noqa: E402


class TestGateBug004WorkerOrchestratorEnforcement:
    """Regression coverage for GATE-BUG-004.

    Correction: the gate classifies bulk-edit purely from the dispatch's own
    prompt text via check_routing.classify_dispatch_work() (same function
    PROD-19 already uses) -- NOT from session-stored work_unit_intent, which
    can only ever be "mechanical"/"reasoning" (see classify_work_unit_intent)
    and can never be "bulk-edit". Prompts below are crafted to actually
    trigger classify_dispatch_work's bulk-edit signals (a mechanical verb
    plus either a bulk-scope phrase like "across all" or 5+ file refs).
    """

    _BULK_EDIT_SHELL_PROMPT = json.dumps({
        "worker": "shell",
        "intent": "delete stale generated files across all build directories",
        "command": "rm -f build/*.tmp",
        "expect": "exit_code",
    })

    _BULK_EDIT_MUTATION_PROMPT = json.dumps({
        "worker": "mutation",
        "intent": "rename variable across all call sites",
        "files": [
            "scripts/foo.py:10", "scripts/bar.py:5", "scripts/baz.py:20",
            "scripts/qux.py:1", "scripts/quux.py:30",
        ],
        "pattern": "Rename local variable x to y.",
        "output": "targeted_edits",
    })

    _MECHANICAL_MUTATION_PROMPT = json.dumps({
        "worker": "mutation",
        "intent": "rename local variable in one file",
        "files": ["scripts/foo.py:10"],
        "pattern": "Rename local variable x to y.",
        "output": "targeted_edits",
    })

    _READ_PROMPT = json.dumps({
        "worker": "read",
        "goal": "find all callers of foo() across the codebase",
        "scope": ["scripts/"],
        "return": ["file:line references"],
    })

    def test_sonnet_shell_worker_denied_when_matrix_requires_haiku(self):
        """A shell worker dispatched on sonnet is denied when the dispatch's
        own prompt classifies as bulk-edit, whose routing_matrix.worker is
        haiku."""
        result = check_worker_model({
            "tool_name": "Task",
            "session_id": "gb4-sonnet-shell",
            "tool_input": {"model": "claude-sonnet-4-5", "prompt": self._BULK_EDIT_SHELL_PROMPT},
        })
        assert result.decision == "deny"
        assert "fast-tier" in result.message
        assert "composer-2.5-fast" in result.message

    def test_haiku_shell_worker_allowed_when_matrix_requires_haiku(self):
        """The same dispatch on haiku is allowed."""
        result = check_worker_model({
            "tool_name": "Task",
            "session_id": "gb4-haiku-shell",
            "tool_input": {"model": "claude-haiku-4-5", "prompt": self._BULK_EDIT_SHELL_PROMPT},
        })
        assert result.decision == "allow"

    def test_sonnet_mutation_worker_denied_when_prompt_classifies_bulk_edit(self):
        """A mutation worker whose own prompt trips classify_dispatch_work's
        bulk-edit signals (mechanical verb + bulk-scope phrase/5+ files) is
        denied on a non-haiku model."""
        result = check_worker_model({
            "tool_name": "Task",
            "session_id": "gb4-sonnet-mutation",
            "tool_input": {"model": "claude-sonnet-4-5", "prompt": self._BULK_EDIT_MUTATION_PROMPT},
        })
        assert result.decision == "deny"
        assert "fast-tier" in result.message
        assert "composer-2.5-fast" in result.message

    def test_composer_shell_worker_allowed_when_matrix_requires_haiku(self):
        """composer-2.5-fast is the Cursor fast-tier equivalent of haiku."""
        result = check_worker_model({
            "tool_name": "Task",
            "session_id": "gb4-composer-shell",
            "tool_input": {"model": "composer-2.5-fast", "prompt": self._BULK_EDIT_SHELL_PROMPT},
        })
        assert result.decision == "allow"

    def test_sonnet_mutation_worker_denied_for_mechanical_intent(self):
        """A single-file rename classifies as "mechanical"; routing_matrix
        .mechanical.worker is haiku, so a sonnet worker is denied and a
        haiku worker is allowed."""
        denied = check_worker_model({
            "tool_name": "Task",
            "session_id": "gb4-mechanical-mutation",
            "tool_input": {"model": "claude-sonnet-4-5", "prompt": self._MECHANICAL_MUTATION_PROMPT},
        })
        assert denied.decision == "deny"
        allowed = check_worker_model({
            "tool_name": "Task",
            "session_id": "gb4-mechanical-mutation-haiku",
            "tool_input": {"model": "claude-haiku-4-5", "prompt": self._MECHANICAL_MUTATION_PROMPT},
        })
        assert allowed.decision == "allow"

    def test_read_worker_exempt_even_under_bulk_edit_prompt(self):
        """A `read` worker type is never enforced by this gate, regardless
        of the prompt's classification -- it's governed by `explorer` instead."""
        result = check_worker_model({
            "tool_name": "Task",
            "session_id": "gb4-read-exempt",
            "tool_input": {"model": "claude-sonnet-4-5", "prompt": self._READ_PROMPT},
        })
        assert result.decision == "allow"

    def test_oc1_mismatch_escalates_to_deny_on_third_recurrence(self, monkeypatch):
        """OC-1's one-shot nudge is replaced by a counter: the first two
        orchestrator/matrix mismatches nudge (allow), and the third (matching
        config.json's routing.orchestrator_nudge_deny_after=3) denies."""
        sid = "gb4-oc1-escalate"
        cr.event_reset({"session_id": sid, "prompt": "Rename all snake_case methods across the codebase"}, False)
        cr.event_read({"session_id": sid, "tool_name": "Read", "file_path": "/tmp/auth.py"}, False)
        cr.event_read({"session_id": sid, "tool_name": "Read", "file_path": "/tmp/utils.py"}, False)

        state = cr._load_state()
        state.setdefault("sessions", {}).setdefault(sid, {})["work_unit_intent"] = "mechanical"
        cr._save_state(state)

        results = []
        for i in range(3):
            _scan(sid, "DIRECT")
            # Real extraction path: OC-1 reads model_of(payload), which checks
            # top-level payload keys model/model_name/modelName (not tool_input.model).
            results.append(_gate(sid, "Edit", model="claude-haiku-3-5", tool_input={
                "file_path": "/tmp/auth.py", "old_string": f"foo{i}", "new_string": f"bar{i}",
            }))

        assert results[0].get("permission") == "allow"
        assert results[1].get("permission") == "allow"
        assert results[2].get("permission") == "deny", (
            f"Expected the 3rd recurring mismatch to deny, got: {results[2]}"
        )
        # Ruling: the fast-path same-file gate (routing.fast_path_same_file_limit=2,
        # unconditional, fires before OC-1's own verdict-bypass check) now wins this
        # race on the 3rd same-file call before OC-1's orchestrator-mismatch escalation
        # is reached — deliberately, per its "no override, fires before verdict bypass"
        # spec. This assertion covers that priority rather than OC-1's own message.
        # Cost if wrong: OC-1's escalation-on-recurrence path would go untested when the
        # mismatch and same-file conditions co-occur.
        assert "batch all remaining edits" in (results[2].get("agent_message") or "")


import gates as _gates  # noqa: E402
import _common as _common_mod  # noqa: E402
import _fileio as _fileio_mod  # noqa: E402


class TestGateBug005SessionLogEventLabeling:
    """Regression coverage for GATE-BUG-005.

    OC-1's deny previously shared gate_name="routing_gate" with RD-7's
    bulk_write_denied, so it was mislabeled in session_log.jsonl. It now sets
    _gate_name_override="orchestrator_ceiling" on its output (popped by
    gates.run_gate before the result reaches the hook caller), which gives it
    a distinct _GATE_TO_SESSION_EVENT entry. Separately, enforce_worker_model
    and enforce_test_runner_model pre_task denials previously had no entry at
    all and never reached session_log.jsonl.
    """

    def _read_session_log(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_oc1_deny_labeled_distinctly_from_bulk_write_denied(self, monkeypatch, tmp_path):
        session_log = tmp_path / "session_log.jsonl"
        monkeypatch.setattr(_common_mod, "SESSION_LOG", session_log)
        monkeypatch.setattr(_fileio_mod, "SESSION_LOG", session_log)
        monkeypatch.setattr(cr, "model_of", lambda payload: "claude-haiku-3-5")

        sid = "gb5-oc1-deny"
        # NOTE: intentionally not the "Rename all..." prompt used elsewhere in this
        # file — that phrasing is fast-path exempt (is_fast_path_exempt), which would
        # let the (unrelated) fast-path same-file gate preempt OC-1's own deny on the
        # 3rd same-file call and defeat this test's GATE-BUG-005 labeling regression.
        cr.event_reset({"session_id": sid, "prompt": "Refactor auth module across files"}, False)
        cr.event_read({"session_id": sid, "tool_name": "Read", "file_path": "/tmp/auth.py"}, False)
        cr.event_read({"session_id": sid, "tool_name": "Read", "file_path": "/tmp/utils.py"}, False)

        state = cr._load_state()
        state.setdefault("sessions", {}).setdefault(sid, {})["work_unit_intent"] = "mechanical"
        cr._save_state(state)

        result = None
        for i in range(3):
            _scan(sid, "DIRECT")
            result = _gates.run_gate("pre_write", {
                "session_id": sid, "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/auth.py", "old_string": f"foo{i}", "new_string": f"bar{i}"},
            }, False)

        assert result.get("permission") == "deny"

        event_types = [e["event_type"] for e in self._read_session_log(session_log)]
        assert "orchestrator_deny" in event_types, event_types
        assert "bulk_write_denied" not in event_types, event_types

    def test_worker_model_deny_reaches_session_log(self, monkeypatch, tmp_path):
        session_log = tmp_path / "session_log.jsonl"
        monkeypatch.setattr(_common_mod, "SESSION_LOG", session_log)
        monkeypatch.setattr(_fileio_mod, "SESSION_LOG", session_log)

        sid = "gb5-worker-model"
        # Initialize session with a routing verdict so _routing_gate allows through
        cr.event_reset({"session_id": sid, "prompt": "Delete stale files"}, False)
        _scan(sid, "DIRECT")

        bulk_edit_shell_prompt = json.dumps({
            "worker": "shell",
            "intent": "delete stale generated files across all build directories",
            "command": "rm -f build/*.tmp",
            "expect": "exit_code",
        })
        result = _gates.run_gate("pre_task", {
            "session_id": sid,
            "tool_name": "Task",
            "tool_input": {"model": "claude-sonnet-4-5", "prompt": bulk_edit_shell_prompt},
        }, False)

        assert result.get("permission") == "deny"
        event_types = [e["event_type"] for e in self._read_session_log(session_log)]
        assert "worker_model_deny" in event_types, event_types


class TestOpusSubagentBan:
    """Regression coverage for P4: permanent opus subagent ban.

    enforce_explicit_model permanently denies model='opus' on Task/Agent
    dispatches (no prompt override). Denials set
    _gate_name_override='opus_subagent' so session_log.jsonl records
    opus_subagent_deny (GATE-BUG-005 mapping).
    """

    _READ_PROMPT = json.dumps({
        "worker": "read",
        "goal": "find gate registration in pre_task chain",
        "scope": ["scripts/gates.py"],
        "return": ["function names"],
    })

    _SHELL_PROMPT = json.dumps({
        "worker": "shell",
        "intent": "list scripts directory",
        "command": "ls scripts/",
        "expect": "exit_code",
    })

    def _read_session_log(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_opus_subagent_denied_via_pretask_chain(self):
        sid = "p4-opus-deny"
        cr.event_reset({"session_id": sid, "prompt": "Explore routing gates"}, False)
        _scan(sid, "DIRECT")

        result = _gates.run_gate("pre_task", {
            "session_id": sid,
            "tool_name": "Task",
            "tool_input": {"model": "opus", "prompt": self._SHELL_PROMPT},
        }, False)

        assert result.get("permission") == "deny"
        msg = result.get("agent_message", "") or ""
        assert "opus" in msg.lower()
        assert "not permitted" in msg.lower()

    def test_opus_subagent_deny_reaches_session_log(self, monkeypatch, tmp_path):
        session_log = tmp_path / "session_log.jsonl"
        monkeypatch.setattr(_common_mod, "SESSION_LOG", session_log)
        monkeypatch.setattr(_fileio_mod, "SESSION_LOG", session_log)

        sid = "p4-opus-session-log"
        cr.event_reset({"session_id": sid, "prompt": "Explore routing gates"}, False)
        _scan(sid, "DIRECT")

        result = _gates.run_gate("pre_task", {
            "session_id": sid,
            "tool_name": "Task",
            "tool_input": {"model": "opus", "prompt": self._SHELL_PROMPT},
        }, False)

        assert result.get("permission") == "deny"
        event_types = [e["event_type"] for e in self._read_session_log(session_log)]
        assert "opus_subagent_deny" in event_types, event_types

    def test_sonnet_subagent_allowed_via_pretask_chain(self):
        sid = "p4-sonnet-allow"
        cr.event_reset({"session_id": sid, "prompt": "Read gate registration"}, False)
        _scan(sid, "DIRECT")

        result = _gates.run_gate("pre_task", {
            "session_id": sid,
            "tool_name": "Task",
            "tool_input": {"model": "sonnet", "prompt": self._READ_PROMPT},
        }, False)

        assert result.get("permission") == "allow", (
            f"Sonnet read-worker dispatch should pass pre_task chain; got: {result}"
        )


class TestGateBug006StatsIntegrityCheck:
    """Regression coverage for GATE-BUG-006.

    The stats file (_checksum field on agent_call_stats.json) detects tampering
    (out-of-band edits to dispatch_count). A valid checksum allows dispatch;
    a mismatched checksum blocks dispatch hard (not warn-only, since it could
    silently bypass cost limits). Legacy files without the _checksum field are
    not treated as tampering.
    """

    def test_tampered_checksum_denies_dispatch(self, monkeypatch, tmp_path):
        """A stats file with a checksum that does not match its contents denies dispatch."""
        import agent_call_tracker as act
        import spawn_tracker

        stats_file = tmp_path / "agent_call_stats.json"
        # spawn_tracker owns the actual read/write path for STATS_FILE (its own
        # module-level binding); agent_call_tracker only re-exports it for
        # display/messages, so that copy must be patched too to affect I/O.
        monkeypatch.setattr(spawn_tracker, "STATS_FILE", stats_file)
        monkeypatch.setattr(act, "STATS_FILE", stats_file)
        monkeypatch.setattr(act, "ROUTING_STATE_FILE", cr.ROUTING_STATE_FILE)

        sid = "gb6-tamper"

        # Create a valid stats file via the real write path.
        result_1 = act.event_preuse(
            {
                "session_id": sid,
                "tool_name": "Task",
                "tool_input": {"prompt": "Initial dispatch", "model": "claude-haiku-4-5"},
            },
            False,
        )
        assert result_1.get("permission") == "allow", "Initial dispatch should be allowed"

        # Verify the stats file was created with a checksum.
        stats_data = json.loads(stats_file.read_text())
        assert "_checksum" in stats_data, "Stats file should have a _checksum field"
        original_checksum = stats_data["_checksum"]

        # Tamper: mutate dispatch_count without recomputing _checksum.
        stats_data["sessions"][sid]["dispatch_count"] = 999
        stats_file.write_text(json.dumps(stats_data))

        # Call event_preuse again — the checksum mismatch should be detected.
        result_2 = act.event_preuse(
            {
                "session_id": sid,
                "tool_name": "Task",
                "tool_input": {"prompt": "Second dispatch after tamper", "model": "claude-haiku-4-5"},
            },
            False,
        )

        assert result_2.get("permission") == "deny", "Dispatch should be denied after tampering"
        msg = result_2.get("agent_message", "") or ""
        assert "integrity" in msg.lower(), (
            f"Deny message should mention integrity check. Got: {msg}"
        )

    def test_missing_checksum_is_not_treated_as_tampering(self, monkeypatch, tmp_path):
        """A stats file without a _checksum field (legacy) is not treated as tampering."""
        import agent_call_tracker as act

        stats_file = tmp_path / "agent_call_stats.json"
        monkeypatch.setattr(act, "STATS_FILE", stats_file)
        monkeypatch.setattr(act, "ROUTING_STATE_FILE", cr.ROUTING_STATE_FILE)

        # Write a legacy stats file with no _checksum field.
        legacy_stats = {
            "sessions": {
                "gb6-legacy": {
                    "dispatch_count": 2.0,
                    "agents": [],
                    "explore_without_haiku": 0,
                    "total_agents": 0,
                }
            }
        }
        stats_file.write_text(json.dumps(legacy_stats))

        # check_stats_integrity should return True (no tampering detected).
        result = act.check_stats_integrity()
        assert result is True, "Legacy file without _checksum should not be flagged as tampering"

    def test_valid_checksum_allows_dispatch(self, monkeypatch, tmp_path):
        """A stats file with a valid checksum allows dispatch."""
        import agent_call_tracker as act

        stats_file = tmp_path / "agent_call_stats.json"
        monkeypatch.setattr(act, "STATS_FILE", stats_file)
        monkeypatch.setattr(act, "ROUTING_STATE_FILE", cr.ROUTING_STATE_FILE)

        sid = "gb6-valid"

        # Create a valid stats file via the real write path.
        result_1 = act.event_preuse(
            {
                "session_id": sid,
                "tool_name": "Task",
                "tool_input": {"prompt": "First dispatch", "model": "claude-haiku-4-5"},
            },
            False,
        )
        assert result_1.get("permission") == "allow", "First dispatch should be allowed"

        # Call check_stats_integrity directly — should return True.
        integrity_ok = act.check_stats_integrity()
        assert integrity_ok is True, "Valid stats file should pass integrity check"

        # Dispatch again — should also be allowed.
        result_2 = act.event_preuse(
            {
                "session_id": sid,
                "tool_name": "Task",
                "tool_input": {"prompt": "Second dispatch with valid checksum", "model": "claude-haiku-4-5"},
            },
            False,
        )
        assert result_2.get("permission") == "allow", "Dispatch with valid checksum should be allowed"


class TestGateBug007PreTaskRoutingGateWiring:
    """Regression coverage for GATE-BUG-007.

    check_routing.event_gate() holds all of BWS-T1/T2's Task/Agent-dispatch
    enforcement (concurrency cap, grouping, serialize_verify, stall counter),
    but gates.py's GATE_REGISTRY wired it (via _routing_gate) into "pre_write"
    only -- never into "pre_task", the chain that actually fires for real
    PreToolUse:Task/Agent events. TestBoundedWaveSchedulerGates (BWS-T3-002)
    calls cr.event_gate(...) directly, which always passed and masked this:
    it never exercised gates.run_gate("pre_task", ...), the real dispatch
    path a live Task/Agent hook invocation actually takes.
    """

    def test_routing_gate_registered_in_pre_task_chain(self):
        assert _gates._routing_gate in _gates.GATE_REGISTRY["pre_task"], (
            "check_routing.event_gate (via _routing_gate) must be reachable "
            "from the pre_task chain, or BWS concurrency-cap/grouping/"
            "serialize_verify/stall-counter enforcement is dead code for "
            "real Task/Agent dispatches."
        )

    def test_fourth_concurrent_dispatch_denied_via_real_pretask_chain(self, monkeypatch):
        """Same scenario as TestBoundedWaveSchedulerGates's cap test, but driven
        through gates.run_gate("pre_task", ...) -- the real dispatcher path --
        instead of calling cr.event_gate() directly."""
        import _config
        monkeypatch.setattr(_config, "_config_cache", {
            "concurrency": {"max_inflight_subagents": 3}
        })
        sid = "gb7-cap-real-pretask"
        for _ in range(3):
            cr.event_subagent_start({"session_id": sid}, False)

        result = _gates.run_gate("pre_task", {
            "session_id": sid,
            "tool_name": "Task",
            "tool_input": {"model": "claude-haiku-4-5"},
        }, False)

        assert result.get("permission") == "deny", (
            f"4th concurrent Task dispatch at cap=3 must be denied by the "
            f"real pre_task chain; got: {result}"
        )
        msg = result.get("agent_message", "") or ""
        assert "3/3" in msg


# worker_response ─────────────────────────────────────────────────────────────

import validate_worker_response as vwr  # noqa: E402


def _make_stop_payload(tmp_path, dispatch_prompt: str, return_text: str) -> dict:
    """Build a minimal SubagentStop payload with an inline transcript file."""
    transcript = tmp_path / "subagent_transcript.jsonl"
    entry = {
        "message": {
            "role": "assistant",
            "content": return_text,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    }
    transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return {
        "session_id": "wr-test",
        "agent_id": "agent-abc",
        "tool_input": {"prompt": dispatch_prompt},
        "agent_transcript_path": str(transcript),
    }


def test_worker_response_shell_valid_passes(tmp_path):
    """Shell worker return matching schema emits no advisory."""
    dispatch = json.dumps({"worker": "shell", "intent": "run tests", "command": "pytest", "expect": "exit_code"})
    return_json = json.dumps({"exit_code": 0, "stdout": "2 passed", "stderr": ""})
    payload = _make_stop_payload(tmp_path, dispatch, return_json)
    result = vwr.event_subagent_stop(payload, is_claude=False)
    assert result.get("continue") is True
    assert "agent_message" not in result, f"No advisory expected for valid shell response; got: {result}"


def test_worker_response_shell_missing_exit_code_emits_advisory(tmp_path):
    """Shell worker return missing exit_code emits an advisory nudge."""
    dispatch = json.dumps({"worker": "shell", "intent": "run tests", "command": "pytest", "expect": "exit_code"})
    return_json = json.dumps({"stdout": "2 passed", "stderr": ""})  # exit_code missing
    payload = _make_stop_payload(tmp_path, dispatch, return_json)
    result = vwr.event_subagent_stop(payload, is_claude=False)
    assert result.get("continue") is True
    msg = result.get("agent_message", "") or ""
    assert "exit_code" in msg, f"Expected advisory mentioning exit_code; got: {msg}"
    assert "advisory" in msg.lower(), f"Expected [advisory] prefix; got: {msg}"


def test_worker_response_read_valid_passes(tmp_path):
    """Read worker return with valid findings passes without advisory."""
    dispatch = json.dumps({"worker": "read", "goal": "find symbol", "scope": ["scripts/"], "return": ["file:line"]})
    return_json = json.dumps({"findings": [{"file": "scripts/gates.py", "line": 42, "summary": "GATE_REGISTRY defined here"}]})
    payload = _make_stop_payload(tmp_path, dispatch, return_json)
    result = vwr.event_subagent_stop(payload, is_claude=False)
    assert result.get("continue") is True
    assert "agent_message" not in result, f"No advisory expected for valid read response; got: {result}"


def test_worker_response_read_free_text_emits_advisory(tmp_path):
    """Read worker returning free text (not JSON) emits an advisory."""
    dispatch = json.dumps({"worker": "read", "goal": "find symbol", "scope": ["scripts/"], "return": ["file:line"]})
    return_text = "I found the symbol in scripts/gates.py at line 42. It does X and Y."
    payload = _make_stop_payload(tmp_path, dispatch, return_text)
    result = vwr.event_subagent_stop(payload, is_claude=False)
    assert result.get("continue") is True
    msg = result.get("agent_message", "") or ""
    assert "advisory" in msg.lower(), f"Expected [advisory] prefix; got: {msg}"
    assert "read" in msg.lower() or "findings" in msg.lower(), f"Expected read/findings mention; got: {msg}"


# ---------------------------------------------------------------------------
# TICKET-007 — Adversarial extraction tests (missing-key / empty-string variants)
# ---------------------------------------------------------------------------
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent.parent.parent / "scripts"))
from _model_utils import model_of, tier_of_model
from _session import session_id_of


@pytest.mark.parametrize("payload,expected", [
    ({}, None),
    ({"model": ""}, None),
    ({"model": None}, None),
    ({"model": "claude-sonnet-4-6"}, "claude-sonnet-4-6"),
    ({"model_name": "claude-sonnet-4-6"}, "claude-sonnet-4-6"),
    ({"modelName": "claude-sonnet-4-6"}, "claude-sonnet-4-6"),
    ({"other_key": "claude-sonnet-4-6"}, None),
])
def test_model_of_adversarial(payload, expected):
    assert model_of(payload) == expected


@pytest.mark.parametrize("tier_input,expected", [
    (None, "unknown"),
    ("", "unknown"),
    ("claude-haiku-4-5", "fast"),
    ("claude-sonnet-4-6", "reasoning"),
    ("claude-opus-4-8", "reasoning"),
    ("unrecognized-model-xyz", "unknown"),
])
def test_tier_of_model_adversarial(tier_input, expected):
    assert tier_of_model(tier_input) == expected


@pytest.mark.parametrize("payload,is_generated", [
    ({}, True),  # no session_id generates a UUID
    ({"session_id": ""}, True),  # empty string generates a UUID
    ({"session_id": "   "}, True),  # whitespace generates a UUID
    ({"session_id": "abc-123"}, False),  # provided value used as-is
    ({"sessionId": "abc-123"}, False),  # camelCase variant
    ({"conversation_id": "abc-123"}, False),  # conversation_id alias
    ({"conversationId": "abc-123"}, False),  # camelCase conversation_id
])
def test_session_id_of_adversarial(monkeypatch, payload, is_generated):
    import uuid
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CURSOR_SESSION_ID", raising=False)
    result = session_id_of(payload)
    if is_generated:
        # Verify it's a valid UUID string
        uuid.UUID(result)
    else:
        # Verify it matches the provided value
        assert result == (payload.get("session_id") or payload.get("sessionId") or
                         payload.get("conversation_id") or payload.get("conversationId"))


@pytest.mark.parametrize("payload,expected", [
    ({}, ""),
    ({"prompt": ""}, ""),
    ({"prompt": "hello"}, "hello"),
    ({"text": "hello"}, "hello"),
    ({"message": "hello"}, "hello"),
    ({"other": "hello"}, ""),
])
def test_text_for_prompt_adversarial(payload, expected):
    import check_routing as cr
    assert cr._text_for_prompt(payload) == expected


def test_resume_ttl_clamped_to_300(monkeypatch):
    import _model_utils as mu
    monkeypatch.setenv("COST_ROUTER_RESUME_TTL_SECONDS", "900")
    assert mu.resume_ttl_seconds() == 300
    monkeypatch.setenv("COST_ROUTER_RESUME_TTL_SECONDS", "120")
    assert mu.resume_ttl_seconds() == 120


def test_get_thresholds_reads_config_block():
    import _config
    t = _config.get_thresholds()
    assert t["read_full_max_lines"] == 300
    assert t["edit_dispatch_min_lines"] == 500
    assert t["write_existing_max_lines"] == 150
    assert t["write_existing_max_kb"] == 5
    assert t["role_read_max_lines"] == 50
