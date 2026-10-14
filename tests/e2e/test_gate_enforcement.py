"""Consolidated gate enforcement tests.

Sources:
- test_enforce_role_gates.py
- test_shell_gate.py
- test_read_file_gates.py
- test_enforce_explicit_model.py
- test_file_graph_gate.py
- test_read_gate_false_positive.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from enforce_role_gates import check_role_gate
from enforce_explicit_model import check_explicit_model
from _model_utils import tier_of_model
from file_graph_gate import _top_symbols, _symbol_line_ranges, gate_fn
import check_routing as cr
import check_read_repeat as crr
import read_gate
import session_state as ss

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_TESTS_DIR = Path(__file__).resolve().parent.parent


# ===========================================================================
# --- from test_enforce_role_gates.py ---
# ===========================================================================

def _payload(tool: str, role: str | None = None, limit: int | None = None) -> dict:
    p: dict = {"tool_name": tool, "tool_input": {}}
    if role:
        p["text"] = f"Routing: {role} — doing work"
    if limit is not None:
        p["tool_input"]["limit"] = limit
    return p


class TestRoleGates:

    def test_fo_edit_denied(self):
        result = check_role_gate(_payload("Edit", role="FO"))
        assert result.decision == "deny"
        assert "direct mutations are blocked" in result.message

    def test_fo_write_denied(self):
        result = check_role_gate(_payload("Write", role="FO"))
        assert result.decision == "deny"

    def test_rb_edit_denied(self):
        result = check_role_gate(_payload("Edit", role="RB"))
        assert result.decision == "deny"

    def test_rb_write_denied(self):
        result = check_role_gate(_payload("Write", role="RB"))
        assert result.decision == "deny"

    def test_fo_bash_denied(self):
        result = check_role_gate(_payload("Bash", role="FO"))
        assert result.decision == "deny"

    def test_fo_read_small_allowed(self):
        result = check_role_gate(_payload("Read", role="FO", limit=20))
        assert result.decision == "allow"

    def test_fo_read_unlimited_denied(self):
        result = check_role_gate(_payload("Read", role="FO"))
        assert result.decision == "deny"
        assert "Explore agent" in result.message

    def test_fo_read_large_denied(self):
        result = check_role_gate(_payload("Read", role="FO", limit=100))
        assert result.decision == "deny"

    def test_fo_read_at_threshold_allowed(self):
        result = check_role_gate(_payload("Read", role="FO", limit=50))
        assert result.decision == "allow"

    def test_direct_edit_allowed(self):
        result = check_role_gate(_payload("Edit", role="DIRECT"))
        assert result.decision == "allow"

    def test_fw_edit_allowed(self):
        result = check_role_gate(_payload("Edit", role="FW"))
        assert result.decision == "allow"

    def test_no_routing_line_allowed(self):
        result = check_role_gate(_payload("Edit"))
        assert result.decision == "allow"

    def test_fo_task_not_blocked(self):
        result = check_role_gate(_payload("Task", role="FO"))
        assert result.decision == "allow"

    def test_rb_agent_not_blocked(self):
        result = check_role_gate(_payload("Agent", role="RB"))
        assert result.decision == "allow"


# ===========================================================================
# --- from test_shell_gate.py ---
# ===========================================================================

class _ShellBase(unittest.TestCase):
    def setUp(self) -> None:
        _DATA_DIR.mkdir(exist_ok=True)
        self._orig = cr.ROUTING_STATE_FILE
        self._orig_ss = ss.ROUTING_STATE_FILE
        self._state = _DATA_DIR / f".rstate_sg_{self.__class__.__name__}_{self._testMethodName}.json"
        if self._state.exists():
            self._state.unlink()
        cr.ROUTING_STATE_FILE = self._state
        ss.ROUTING_STATE_FILE = self._state

    def tearDown(self) -> None:
        if self._state.exists():
            self._state.unlink()
        cr.ROUTING_STATE_FILE = self._orig
        ss.ROUTING_STATE_FILE = self._orig_ss

    def _setup(self, sid: str) -> None:
        cr.event_reset({"session_id": sid, "prompt": "Run diagnostics across multiple services"}, False)
        cr.event_scan({"session_id": sid, "text": "Routing: DIRECT — running diagnostics"}, False)

    def _shell(self, sid: str) -> dict:
        return cr.event_gate(
            {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": "echo hello"}},
            False,
        )

    def _shell_with_verdict(self, sid: str) -> dict:
        cr.event_scan({"session_id": sid, "text": "Routing: DIRECT — step"}, False)
        return self._shell(sid)


class TestNudgeAt3(_ShellBase):
    def test_nudge_at_three_shell_commands(self) -> None:
        sid = "sg-nudge"
        self._setup(sid)

        for i in range(1, 3):
            out = self._shell_with_verdict(sid)
            self.assertEqual(out.get("permission"), "allow", f"call {i} must allow")
            self.assertFalse(out.get("agent_message"), f"call {i} must have no nudge")

        out3 = self._shell_with_verdict(sid)
        self.assertEqual(out3.get("permission"), "allow", "3rd call must still allow")
        self.assertIn("shell-gate", out3.get("agent_message", ""), "nudge must mention shell-gate")
        self.assertIn("Task", out3.get("agent_message", ""), "nudge must mention Task")


class TestDenyAt5(_ShellBase):
    def test_deny_at_five_shell_commands(self) -> None:
        sid = "sg-deny"
        self._setup(sid)

        for i in range(1, 5):
            out = self._shell_with_verdict(sid)
            self.assertEqual(out.get("permission"), "allow", f"call {i} must allow")

        out5 = self._shell_with_verdict(sid)
        self.assertEqual(out5.get("permission"), "deny", "5th call must be denied")
        self.assertIn("shell-gate", out5.get("agent_message", ""), "deny must mention shell-gate")


class TestResetOnDispatch(_ShellBase):
    def test_shell_gate_resets_after_task_dispatch(self) -> None:
        sid = "sg-reset"
        self._setup(sid)

        for _ in range(3):
            self._shell_with_verdict(sid)

        state = cr._load_state()
        counter = int(state.get("sessions", {}).get(sid, {}).get("shell_calls_since_dispatch", 0))
        self.assertEqual(counter, 3)

        cr.event_scan({"session_id": sid, "text": "Routing: FO — dispatch"}, False)
        task_out = cr.event_gate({"session_id": sid, "tool_name": "Task"}, False)
        self.assertEqual(task_out.get("permission"), "allow")

        state2 = cr._load_state()
        counter2 = int(state2.get("sessions", {}).get(sid, {}).get("shell_calls_since_dispatch", 0))
        self.assertEqual(counter2, 0, "counter must reset to 0 after Task dispatch")


# ===========================================================================
# --- from test_read_file_gates.py ---
# ===========================================================================

class _ReadRepeatBase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile, _common as _c
        self._tmp_dir = tempfile.mkdtemp()
        self._data_dir = Path(self._tmp_dir) / "data"
        self._data_dir.mkdir()
        self._orig_state = crr.READ_STATE_FILE
        self._orig_index = crr.FILE_GRAPH_INDEX
        self._orig_data_dir = _c.DATA_DIR
        self._state = self._data_dir / "read_state.json"
        self._index = self._data_dir / "file_graph_index.json"
        crr.READ_STATE_FILE = self._state
        crr.FILE_GRAPH_INDEX = self._index
        _c.DATA_DIR = self._data_dir
        crr.DATA_DIR = self._data_dir

    def tearDown(self) -> None:
        import shutil, _common as _c
        crr.READ_STATE_FILE = self._orig_state
        crr.FILE_GRAPH_INDEX = self._orig_index
        _c.DATA_DIR = self._orig_data_dir
        crr.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _read(self, path: str = "/tmp/example.py", sid: str = "rr-sess") -> dict:
        return crr.event_read(
            {"session_id": sid, "tool_name": "Read", "tool_input": {"path": path}},
            is_claude=False,
        )


class TestReadRepeatGate(_ReadRepeatBase):

    def test_first_read_allowed_no_nudge(self) -> None:
        out = self._read()
        self.assertEqual(out.get("permission"), "allow")
        self.assertNotIn("agent_message", out)

    def test_second_read_same_file_nudges(self) -> None:
        self._read()
        out = self._read()
        self.assertEqual(out.get("permission"), "allow")
        self.assertIn("re-read", out.get("agent_message", "").lower())

    def test_subagent_read_exempt(self) -> None:
        """Subagent reads (subagent_id present) must not trigger the repeat gate."""
        payload = {
            "session_id": "rr-subagent",
            "tool_name": "Read",
            "tool_input": {"path": "/tmp/example.py"},
            "subagent_id": "worker-123",
        }
        for _ in range(5):
            out = crr.event_read(payload, is_claude=False)
            self.assertEqual(out.get("permission"), "allow")
            self.assertNotIn("re-read", out.get("agent_message", "").lower())


# ===========================================================================
# --- from test_enforce_explicit_model.py ---
# ===========================================================================

class TestExplicitModelGate:

    def test_task_without_model_denied(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": {"prompt": "Do something"}})
        assert result.decision == "deny"
        assert "explicit model required" in result.message

    def test_task_with_model_field_allowed(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": {"model": "haiku", "prompt": "Do it"}})
        assert result.decision == "allow"

    def test_task_with_sonnet_allowed(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": {"model": "sonnet", "prompt": "Do it"}})
        assert result.decision == "allow"

    def test_task_with_opus_denied(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": {"model": "opus", "prompt": "Do it"}})
        assert result.decision == "deny"
        assert "opus" in result.message.lower()

    def test_agent_opus_denied(self):
        result = check_explicit_model({"tool_name": "Agent", "tool_input": {"model": "opus", "prompt": "Explore"}})
        assert result.decision == "deny"
        assert "opus" in result.message.lower()
        assert "not permitted" in result.message.lower()

    def test_denial_message_lists_platform_slugs(self):
        opus_result = check_explicit_model({
            "tool_name": "Task",
            "tool_input": {"model": "opus", "prompt": "Do it"},
        })
        assert opus_result.decision == "deny"
        assert "composer-2.5-fast" in opus_result.message
        assert "claude-4.6-sonnet-medium-thinking" in opus_result.message
        assert "|'opus'" not in opus_result.message

        missing_result = check_explicit_model({
            "tool_name": "Task",
            "tool_input": {"prompt": "Do something"},
        })
        assert missing_result.decision == "deny"
        assert "composer-2.5-fast" in missing_result.message
        assert "claude-4.6-sonnet-medium-thinking" in missing_result.message

    def test_create_worker_opus_denied(self):
        payload = {
            "worker": "create",
            "intent": "new module",
            "file_path": "scripts/new_module.py",
        }
        result = check_explicit_model({
            "tool_name": "Task",
            "tool_input": {"model": "opus", "prompt": json.dumps(payload)},
        })
        assert result.decision == "deny"
        assert "opus" in result.message.lower()

    def test_unstructured_input_with_opus_denied(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": "Run with model=opus"})
        assert result.decision == "deny"
        assert "opus" in result.message.lower()

    def test_opus_with_ruling_override_still_denied(self):
        payload = {
            "worker": "mutation",
            "intent": "critical cross-domain fix",
            "files": ["scripts/foo.py:1"],
            "pattern": "Apply judgment call fix.",
            "output": "targeted_edits",
        }
        prompt = "Ruling: opus required — rare cross-domain judgment.\n" + json.dumps(payload)
        result = check_explicit_model({
            "tool_name": "Task",
            "tool_input": {"model": "opus", "prompt": prompt},
        })
        assert result.decision == "deny"
        assert "opus" in result.message.lower()
        assert "Ruling" not in result.message

    def test_agent_without_model_denied(self):
        result = check_explicit_model({"tool_name": "Agent", "tool_input": {"prompt": "Explore codebase"}})
        assert result.decision == "deny"

    def test_agent_with_model_allowed(self):
        result = check_explicit_model({"tool_name": "Agent", "tool_input": {"model": "haiku", "prompt": "Explore"}})
        assert result.decision == "allow"

    def test_edit_not_intercepted(self):
        result = check_explicit_model({"tool_name": "Edit", "tool_input": {}})
        assert result.decision == "allow"

    def test_write_not_intercepted(self):
        result = check_explicit_model({"tool_name": "Write", "tool_input": {}})
        assert result.decision == "allow"

    def test_read_not_intercepted(self):
        result = check_explicit_model({"tool_name": "Read", "tool_input": {}})
        assert result.decision == "allow"

    def test_empty_model_string_denied(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": {"model": "", "prompt": "Do it"}})
        assert result.decision == "deny"

    def test_invalid_model_string_denied(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": {"model": "gpt4", "prompt": "Do it"}})
        assert result.decision == "deny"

    def test_unstructured_input_with_model_allowed(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": "Run with model=haiku"})
        assert result.decision == "allow"

    def test_unstructured_input_without_model_denied(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": "Just do the thing"})
        assert result.decision == "deny"


class TestDispatchSchemaHint:
    def test_long_prose_prompt_without_model_includes_schema_hint(self):
        prompt = ("Please refactor this function to add a bounds check. " * 20)[:900]
        result = check_explicit_model({"tool_name": "Task", "tool_input": {"prompt": prompt}})
        assert result.decision == "deny"
        assert "explicit model required" in result.message
        assert "valid JSON dispatch" in result.message

    def test_short_prompt_without_model_has_no_schema_hint(self):
        result = check_explicit_model({"tool_name": "Task", "tool_input": {"prompt": "Run: pytest tests/ -x -q"}})
        assert result.decision == "deny"
        assert "explicit model required" in result.message
        assert "valid JSON dispatch" not in result.message

    def test_long_valid_json_prompt_without_model_has_no_schema_hint(self):
        payload = {
            "worker": "mutation",
            "intent": "fix bounds check " + ("x" * 700),
            "files": ["scripts/gates.py:97,120"],
            "pattern": "Insert early return when input list is empty.",
            "output": "targeted_edits",
        }
        prompt = json.dumps(payload)
        result = check_explicit_model({"tool_name": "Task", "tool_input": {"prompt": prompt}})
        assert result.decision == "deny"
        assert "explicit model required" in result.message
        assert "valid JSON dispatch" not in result.message

    def test_unstructured_long_prose_input_without_model_includes_schema_hint(self):
        prompt = ("Please refactor this function to add a bounds check. " * 20)[:900]
        result = check_explicit_model({"tool_name": "Task", "tool_input": prompt})
        assert result.decision == "deny"
        assert "valid JSON dispatch" in result.message


class TestFastTierModels:
    def test_composer_fast_allowed(self):
        result = check_explicit_model({
            "tool_name": "Task",
            "tool_input": {"model": "composer-2.5-fast", "prompt": "Do something"},
        })
        assert result.decision == "allow"

    def test_claude_haiku_thinking_allowed(self):
        result = check_explicit_model({
            "tool_name": "Task",
            "tool_input": {"model": "claude-4.5-haiku-thinking", "prompt": "Do something"},
        })
        assert result.decision == "allow"

    def test_haiku_thinking_not_misclassified_as_reasoning(self):
        assert tier_of_model("claude-4.5-haiku-thinking") == "fast"
        result = check_explicit_model({
            "tool_name": "Task",
            "tool_input": {"model": "claude-4.5-haiku-thinking", "prompt": "Complex analysis"},
        })
        assert result.decision == "allow"

    def test_composer_in_unstructured_input_allowed(self):
        result = check_explicit_model({
            "tool_name": "Task",
            "tool_input": "Run with model=composer-2.5-fast to speed up",
        })
        assert result.decision == "allow"

    def test_fast_tier_models_listed(self):
        fast_models = [
            "haiku",
            "composer-2.5-fast",
            "claude-4.5-haiku-thinking",
            "mini",
        ]
        for model in fast_models:
            result = check_explicit_model({
                "tool_name": "Task",
                "tool_input": {"model": model, "prompt": "Task"},
            })
            assert result.decision == "allow", f"Model {model} should be allowed as fast-tier"


# ===========================================================================
# --- from test_file_graph_gate.py ---
# ===========================================================================

SAMPLE_GRAPH = {
    "path_hash": "abc123",
    "language": "python",
    "symbols": [
        {"name": "main", "kind": "function", "line_start": 10, "line_end": 45, "qualname": "main"},
        {"name": "helper", "kind": "function", "line_start": 50, "line_end": 70, "qualname": "helper"},
        {"name": "Config", "kind": "class", "line_start": 75, "line_end": 120, "qualname": "Config"},
        {"name": "validate", "kind": "function", "line_start": 125, "line_end": 140, "qualname": "validate"},
    ],
}

SAMPLE_GRAPH_NO_LINES = {
    "symbols": [
        {"name": "foo"},
        {"name": "bar"},
    ],
}


def test_top_symbols_returns_names():
    assert _top_symbols(SAMPLE_GRAPH, n=2) == ["main", "helper"]


def test_symbol_line_ranges_includes_offset_limit():
    result = _symbol_line_ranges(SAMPLE_GRAPH, n=2)
    assert result == ["main (offset=10, limit=36)", "helper (offset=50, limit=21)"]


def _make_claude_payload(file_path: str, offset=None, limit=None):
    tool_input = {"file_path": file_path}
    if offset is not None:
        tool_input["offset"] = offset
    if limit is not None:
        tool_input["limit"] = limit
    return {
        "tool_name": "Read",
        "tool_input": tool_input,
    }


@patch("file_graph_gate._is_subagent_context", return_value=False)
@patch("file_graph_gate._is_image_or_pdf", return_value=False)
@patch("file_graph_gate._is_small_file", return_value=False)
@patch("file_graph_gate._has_node_map", return_value=True)
@patch("file_graph_gate._load_graph", return_value=SAMPLE_GRAPH)
@patch("file_graph_gate.is_claude_payload", return_value=True)
def test_gate_nudges_not_blocks(mock_claude, mock_load, mock_has, mock_small, mock_img, mock_sub):
    """Gate should nudge (allow with message) not deny when node map exists."""
    payload = _make_claude_payload("/some/big_file.py")
    result = gate_fn(payload)
    err = result.get("error", "")
    assert not err, f"Gate blocked instead of nudging: {err}"


@patch("file_graph_gate._is_subagent_context", return_value=False)
@patch("file_graph_gate._is_image_or_pdf", return_value=False)
@patch("file_graph_gate._is_small_file", return_value=False)
@patch("file_graph_gate._has_node_map", return_value=True)
@patch("file_graph_gate._load_graph", return_value=SAMPLE_GRAPH)
@patch("file_graph_gate.is_claude_payload", return_value=True)
def test_gate_message_includes_line_ranges(mock_claude, mock_load, mock_has, mock_small, mock_img, mock_sub):
    payload = _make_claude_payload("/some/big_file.py")
    result = gate_fn(payload)
    msg = json.dumps(result)
    assert "offset=" in msg, f"Message missing offset: {result}"
    assert "limit=" in msg, f"Message missing limit: {result}"


@patch("file_graph_gate._is_subagent_context", return_value=False)
@patch("file_graph_gate._is_image_or_pdf", return_value=False)
@patch("file_graph_gate._is_small_file", return_value=False)
@patch("file_graph_gate._has_node_map", return_value=True)
@patch("file_graph_gate._load_graph", return_value=SAMPLE_GRAPH)
@patch("file_graph_gate.is_claude_payload", return_value=True)
def test_gate_allows_partial_read(mock_claude, mock_load, mock_has, mock_small, mock_img, mock_sub):
    """Partial reads (with offset/limit) should always be allowed."""
    payload = _make_claude_payload("/some/big_file.py", offset=10, limit=36)
    result = gate_fn(payload)
    err = result.get("error", "")
    assert not err


# ===========================================================================
# --- from test_read_gate_false_positive.py ---
# ===========================================================================

def _make_stale_routing_state(path: Path, session_id: str = "stale-sess") -> None:
    state = {
        "sessions": {
            session_id: {
                "turn": 99,
                "inline_mutations_since_dispatch": 3,
            }
        }
    }
    path.write_text(json.dumps(state), encoding="utf-8")


class TestLoadMutationCountUnknownSession(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = _TESTS_DIR / f".rg_data_{self._testMethodName}"
        self._tmp.mkdir(exist_ok=True)
        self._orig_data_dir = read_gate.DATA_DIR
        read_gate.DATA_DIR = self._tmp

    def tearDown(self) -> None:
        read_gate.DATA_DIR = self._orig_data_dir
        for f in self._tmp.iterdir():
            f.unlink()
        self._tmp.rmdir()

    def _routing_state_path(self) -> Path:
        return self._tmp / "routing_state.json"

    def test_returns_zero_when_no_routing_state(self) -> None:
        count = read_gate._load_mutation_count()
        self.assertEqual(count, 0)

    def test_returns_zero_when_no_session_env_var(self) -> None:
        _make_stale_routing_state(self._routing_state_path())
        for var in ("CLAUDE_SESSION_ID", "COST_ROUTER_SESSION_ID"):
            os.environ.pop(var, None)
        count = read_gate._load_mutation_count()
        self.assertEqual(count, 0)

    def test_returns_zero_when_session_id_not_in_state(self) -> None:
        _make_stale_routing_state(self._routing_state_path(), session_id="stale-sess")
        os.environ["CLAUDE_SESSION_ID"] = "different-session-id"
        try:
            count = read_gate._load_mutation_count()
            self.assertEqual(count, 0)
        finally:
            del os.environ["CLAUDE_SESSION_ID"]

    def test_returns_correct_count_when_session_matches(self) -> None:
        _make_stale_routing_state(self._routing_state_path(), session_id="my-sess")
        os.environ["CLAUDE_SESSION_ID"] = "my-sess"
        try:
            count = read_gate._load_mutation_count()
            self.assertEqual(count, 3)
        finally:
            del os.environ["CLAUDE_SESSION_ID"]


class TestEventResetClearsMutationCount(unittest.TestCase):
    def setUp(self) -> None:
        self._state_file = _TESTS_DIR / f".rg_routing_{self._testMethodName}.json"
        self._resume_file = _TESTS_DIR / f".rg_resume_{self._testMethodName}.json"
        self._counters_file = _TESTS_DIR / f".rg_counters_{self._testMethodName}.json"
        self._subagent_file = _TESTS_DIR / f".rg_subagent_{self._testMethodName}.json"
        for p in (self._state_file, self._resume_file, self._counters_file, self._subagent_file):
            if p.exists():
                p.unlink()
        self._orig_routing = cr.ROUTING_STATE_FILE
        self._orig_resume = cr.RESUME_STATE_FILE
        self._orig_counters = cr.GATE_COUNTERS_FILE
        self._orig_subagent = cr.SUBAGENT_STATE_FILE
        self._orig_routing_ss = ss.ROUTING_STATE_FILE
        self._orig_counters_ss = ss.GATE_COUNTERS_FILE
        self._orig_subagent_ss = ss.SUBAGENT_STATE_FILE
        cr.ROUTING_STATE_FILE = self._state_file
        cr.RESUME_STATE_FILE = self._resume_file
        cr.GATE_COUNTERS_FILE = self._counters_file
        cr.SUBAGENT_STATE_FILE = self._subagent_file
        ss.ROUTING_STATE_FILE = self._state_file
        ss.GATE_COUNTERS_FILE = self._counters_file
        ss.SUBAGENT_STATE_FILE = self._subagent_file

    def tearDown(self) -> None:
        cr.ROUTING_STATE_FILE = self._orig_routing
        cr.RESUME_STATE_FILE = self._orig_resume
        cr.GATE_COUNTERS_FILE = self._orig_counters
        cr.SUBAGENT_STATE_FILE = self._orig_subagent
        ss.ROUTING_STATE_FILE = self._orig_routing_ss
        ss.GATE_COUNTERS_FILE = self._orig_counters_ss
        ss.SUBAGENT_STATE_FILE = self._orig_subagent_ss
        for p in (self._state_file, self._resume_file, self._counters_file, self._subagent_file):
            if p.exists():
                p.unlink()

    def _reset(self, sid: str = "test-sess", prompt: str = "do something") -> dict:
        return cr.event_reset({"session_id": sid, "prompt": prompt}, False)

    def _load_entry(self, sid: str = "test-sess") -> dict:
        state = json.loads(self._state_file.read_text())
        return state["sessions"][sid]

    def test_event_reset_zeroes_inline_mutations(self) -> None:
        sid = "test-sess"
        self._reset(sid=sid)
        state = json.loads(self._state_file.read_text())
        state["sessions"][sid]["inline_mutations_since_dispatch"] = 5
        self._state_file.write_text(json.dumps(state))

        self._reset(sid=sid)

        entry = self._load_entry(sid)
        self.assertEqual(
            entry.get("inline_mutations_since_dispatch", 0),
            0,
        )

    def test_mutation_count_still_increments_within_turn(self) -> None:
        sid = "test-sess"
        self._reset(sid=sid)

        state = json.loads(self._state_file.read_text())
        state["sessions"][sid]["inline_mutations_since_dispatch"] = 2
        self._state_file.write_text(json.dumps(state))

        self._reset(sid=sid)
        self.assertEqual(self._load_entry(sid).get("inline_mutations_since_dispatch", 0), 0)

        state = json.loads(self._state_file.read_text())
        state["sessions"][sid]["inline_mutations_since_dispatch"] = 1
        self._state_file.write_text(json.dumps(state))
        self.assertEqual(self._load_entry(sid).get("inline_mutations_since_dispatch", 0), 1)


if __name__ == "__main__":
    unittest.main()
