"""Tests for scripts/proactive_dispatch_hook.py.

Covers _check() (pure unit tests) and main() (subprocess integration tests).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
REPO_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import proactive_dispatch_hook as pdh
from proactive_dispatch_hook import _check, _tokenize_constraint


# ── helpers ───────────────────────────────────────────────────────────────────

def _dispatch(worker: str = "mutation", **extra) -> str:
    """Build a minimal valid JSON dispatch string for a given worker."""
    base: dict = {"worker": worker, "intent": "test intent"}
    base.update(extra)
    return json.dumps(base)


def _run_main(payload: dict) -> dict:
    """Run proactive_dispatch_hook.py as a subprocess and return parsed output."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "proactive_dispatch_hook.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def _permission(out: dict) -> str | None:
    """Extract permissionDecision from either Claude Code or Cursor output shape."""
    hso = out.get("hookSpecificOutput", {})
    if hso:
        return hso.get("permissionDecision")
    return out.get("permission")


def _advisory(out: dict) -> str:
    """Extract additionalContext regardless of output shape."""
    hso = out.get("hookSpecificOutput", {})
    if hso:
        return hso.get("additionalContext", "")
    return out.get("agent_message", "")


def _task_payload(prompt: str) -> dict:
    return {"tool_name": "Task", "tool_input": {"prompt": prompt}}


def _agent_payload(prompt: str) -> dict:
    return {"tool_name": "Agent", "tool_input": {"prompt": prompt}}


# ── _tokenize_constraint ──────────────────────────────────────────────────────

class TestTokenize:
    def test_splits_on_space(self):
        assert _tokenize_constraint("no side effects") == ["no", "side", "effects"]

    def test_splits_on_hyphen(self):
        assert _tokenize_constraint("no-side-effects") == ["no", "side", "effects"]

    def test_mixed(self):
        assert _tokenize_constraint("targeted-edits only") == ["targeted", "edits", "only"]

    def test_empty(self):
        assert _tokenize_constraint("") == []

    def test_lowercases(self):
        assert _tokenize_constraint("NoSideEffects") == ["nosideeffects"]


# ── _check — pass-through cases ───────────────────────────────────────────────

class TestCheckPassthrough:
    def test_plain_text_returns_empty(self):
        assert _check("Run: pytest tests/ -x -q") == []

    def test_empty_string_returns_empty(self):
        assert _check("") == []

    def test_json_array_returns_empty(self):
        assert _check(json.dumps([1, 2, 3])) == []

    def test_clean_mutation_dispatch_returns_empty(self):
        prompt = _dispatch(
            "mutation",
            files=["scripts/gates.py:10"],
            pattern="Add null check before processing.",
            output="targeted_edits",
        )
        assert _check(prompt) == []

    def test_clean_shell_dispatch_returns_empty(self):
        prompt = _dispatch("shell", command="pytest tests/ -x -q", expect="exit_code")
        assert _check(prompt) == []

    def test_unknown_worker_no_ceiling_violation(self):
        # unknown worker → no token ceiling applied
        prompt = _dispatch("unknown_worker")
        assert _check(prompt) == []


# ── _check — field char limits ────────────────────────────────────────────────

class TestCheckFieldLimits:
    def test_pattern_at_limit_ok(self):
        prompt = _dispatch(pattern="x" * 200)
        assert _check(prompt) == []

    def test_pattern_over_limit(self):
        prompt = _dispatch(pattern="x" * 201)
        violations = _check(prompt)
        assert len(violations) == 1
        assert "pattern" in violations[0]
        assert "201" in violations[0]

    def test_where_at_limit_ok(self):
        prompt = _dispatch(where="x" * 170)
        assert _check(prompt) == []

    def test_where_over_limit(self):
        prompt = _dispatch(where="x" * 171)
        violations = _check(prompt)
        assert any("where" in v for v in violations)

    def test_why_at_limit_ok(self):
        prompt = _dispatch(why="x" * 150)
        assert _check(prompt) == []

    def test_why_over_limit(self):
        prompt = _dispatch(why="x" * 151)
        violations = _check(prompt)
        assert any("why" in v for v in violations)

    def test_multiple_field_violations(self):
        prompt = _dispatch(pattern="x" * 210, where="y" * 180)
        violations = _check(prompt)
        assert len(violations) == 2


# ── _check — constraints ──────────────────────────────────────────────────────

class TestCheckConstraints:
    def test_five_items_ok(self):
        prompt = _dispatch(constraints=["no side effects"] * 5)
        # allowlist may flag words; only check count doesn't trigger
        violations = _check(prompt)
        assert not any("exceeds max 5" in v for v in violations)

    def test_six_items_flagged(self):
        prompt = _dispatch(constraints=["no side effects"] * 6)
        violations = _check(prompt)
        assert any("exceeds max 5" in v for v in violations)

    def test_item_at_60_chars_ok(self):
        prompt = _dispatch(constraints=["a" * 60])
        violations = _check(prompt)
        assert not any("exceeds limit 60" in v for v in violations)

    def test_item_over_60_chars(self):
        prompt = _dispatch(constraints=["a" * 61])
        violations = _check(prompt)
        assert any("exceeds limit 60" in v for v in violations)

    def test_invalid_chars_flagged(self):
        prompt = _dispatch(constraints=["invalid@chars!"])
        violations = _check(prompt)
        assert any("disallowed characters" in v for v in violations)

    def test_valid_chars_not_flagged(self):
        with patch.object(pdh, "_load_allowlist", return_value=frozenset()):
            prompt = _dispatch(constraints=["no-side-effects"])
            violations = _check(prompt)
            # empty allowlist → no allowlist violation, only char check
            assert not any("disallowed characters" in v for v in violations)

    def test_allowlist_violation(self):
        with patch.object(pdh, "_load_allowlist", return_value=frozenset(["no", "side"])):
            prompt = _dispatch(constraints=["no side unauthorized"])
            violations = _check(prompt)
            assert any("unauthorized" in v for v in violations)

    def test_allowlist_all_valid(self):
        with patch.object(pdh, "_load_allowlist", return_value=frozenset(["no", "side", "effects"])):
            prompt = _dispatch(constraints=["no-side-effects"])
            violations = _check(prompt)
            assert not any("allowlist" in v for v in violations)

    def test_empty_allowlist_skips_word_check(self):
        with patch.object(pdh, "_load_allowlist", return_value=frozenset()):
            prompt = _dispatch(constraints=["anything goes here"])
            violations = _check(prompt)
            assert not any("allowlist" in v for v in violations)


# ── _check — examples ─────────────────────────────────────────────────────────

class TestCheckExamples:
    def test_examples_at_400_ok(self):
        ex = [{"before": "x" * 400, "after": "y" * 400}]
        prompt = _dispatch(examples=ex)
        assert not any("exceeds limit 400" in v for v in _check(prompt))

    def test_before_over_400(self):
        ex = [{"before": "x" * 401, "after": "ok"}]
        prompt = _dispatch(examples=ex)
        violations = _check(prompt)
        assert any("examples[0].before" in v and "exceeds limit 400" in v for v in violations)

    def test_after_over_400(self):
        ex = [{"before": "ok", "after": "y" * 401}]
        prompt = _dispatch(examples=ex)
        violations = _check(prompt)
        assert any("examples[0].after" in v and "exceeds limit 400" in v for v in violations)

    def test_backtick_in_before(self):
        ex = [{"before": "some `code`", "after": "fixed"}]
        prompt = _dispatch(examples=ex)
        violations = _check(prompt)
        assert any("backtick" in v and "before" in v for v in violations)

    def test_backtick_in_after(self):
        ex = [{"before": "ok", "after": "result `here`"}]
        prompt = _dispatch(examples=ex)
        violations = _check(prompt)
        assert any("backtick" in v and "after" in v for v in violations)

    def test_clean_examples_no_violation(self):
        ex = [{"before": "old code", "after": "new code"}]
        prompt = _dispatch(examples=ex)
        assert not any("examples" in v for v in _check(prompt))


# ── _check — token ceiling ────────────────────────────────────────────────────

class TestCheckTokenCeiling:
    def _pad_to_tokens(self, base: dict, target_tokens: int) -> str:
        """Pad the why field so len(json)//4 exceeds target_tokens."""
        base = dict(base)
        raw = json.dumps(base)
        needed_chars = (target_tokens * 4) - len(raw) + 10
        base["why"] = "x" * max(0, needed_chars)
        return json.dumps(base)

    def test_mutation_under_ceiling_ok(self):
        prompt = _dispatch("mutation", pattern="short", output="targeted_edits")
        assert not any("ceiling" in v for v in _check(prompt))

    def test_mutation_over_560_flagged(self):
        base = {"worker": "mutation", "intent": "x", "output": "targeted_edits"}
        prompt = self._pad_to_tokens(base, 561)
        violations = _check(prompt)
        assert any("ceiling" in v and "mutation" in v for v in violations)

    def test_mutation_with_examples_ceiling_650(self):
        # With examples present, mutation ceiling is 650 not 560
        base = {
            "worker": "mutation",
            "intent": "x",
            "output": "targeted_edits",
            "examples": [{"before": "a", "after": "b"}],
        }
        # 561-649 tokens → should NOT flag (ceiling raised to 650)
        prompt = self._pad_to_tokens(base, 561)
        violations = _check(prompt)
        assert not any("ceiling" in v for v in violations)

    def test_mutation_with_examples_over_650_flagged(self):
        base = {
            "worker": "mutation",
            "intent": "x",
            "output": "targeted_edits",
            "examples": [{"before": "a", "after": "b"}],
        }
        prompt = self._pad_to_tokens(base, 651)
        violations = _check(prompt)
        assert any("ceiling" in v for v in violations)

    def test_read_ceiling_650(self):
        base = {"worker": "read", "goal": "x", "scope": [], "return": []}
        prompt = self._pad_to_tokens(base, 651)
        violations = _check(prompt)
        assert any("ceiling" in v and "read" in v for v in violations)

    def test_shell_ceiling_560(self):
        base = {"worker": "shell", "intent": "x", "command": "echo", "expect": "exit_code"}
        prompt = self._pad_to_tokens(base, 561)
        violations = _check(prompt)
        assert any("ceiling" in v and "shell" in v for v in violations)

    def test_orchestrator_ceiling_1200(self):
        base = {"worker": "orchestrator", "intent": "x", "step": "do work", "model": "sonnet"}
        prompt = self._pad_to_tokens(base, 1201)
        violations = _check(prompt)
        assert any("ceiling" in v and "orchestrator" in v for v in violations)


# ── _check — file existence ───────────────────────────────────────────────────

class TestCheckFileExistence:
    def test_existing_file_no_violation(self):
        prompt = _dispatch(files=["scripts/gates.py:10"])
        assert not any("does not exist" in v for v in _check(prompt))

    def test_missing_file_flagged(self):
        prompt = _dispatch(files=["scripts/nonexistent_file_xyz.py:10"])
        violations = _check(prompt)
        assert any("does not exist" in v for v in violations)

    def test_path_with_range_parsed_correctly(self):
        prompt = _dispatch(files=["scripts/gates.py:10,50"])
        assert not any("does not exist" in v for v in _check(prompt))

    def test_multiple_files_one_missing(self):
        prompt = _dispatch(files=["scripts/gates.py:1", "scripts/ghost.py:1"])
        violations = _check(prompt)
        assert any("ghost.py" in v for v in violations)
        assert not any("gates.py" in v for v in violations)


# ── main() integration via subprocess ────────────────────────────────────────

class TestMain:
    def test_non_task_tool_returns_empty(self):
        out = _run_main({"tool_name": "Read", "tool_input": {"file_path": "x"}})
        assert out == {}

    def test_non_agent_tool_returns_empty(self):
        out = _run_main({"tool_name": "Edit", "tool_input": {}})
        assert out == {}

    def test_agent_tool_processed(self):
        prompt = _dispatch("shell", command="pytest", expect="exit_code")
        out = _run_main(_agent_payload(prompt))
        assert "hookSpecificOutput" in out or "permission" in out

    def test_clean_dispatch_allows_without_advisory(self):
        prompt = _dispatch("shell", command="pytest tests/ -x -q", expect="exit_code")
        out = _run_main(_task_payload(prompt))
        assert _permission(out) == "allow"
        assert _advisory(out) == ""

    def test_violation_allows_with_advisory(self):
        prompt = _dispatch(pattern="x" * 201)
        out = _run_main(_task_payload(prompt))
        assert _permission(out) == "allow"
        advisory = _advisory(out)
        assert advisory != ""
        assert "pattern" in advisory

    def test_string_tool_input_processed(self):
        # tool_input as bare string (not dict)
        prompt = _dispatch("shell", command="echo", expect="exit_code")
        out = _run_main({"tool_name": "Task", "tool_input": prompt})
        assert _permission(out) == "allow"

    def test_plain_text_prompt_passes_cleanly(self):
        out = _run_main(_task_payload("Run: pytest tests/ -x -q"))
        assert _permission(out) == "allow"
        assert _advisory(out) == ""

    def test_advisory_lists_all_violations(self):
        prompt = _dispatch(pattern="x" * 210, where="y" * 180, why="z" * 160)
        out = _run_main(_task_payload(prompt))
        advisory = _advisory(out)
        assert "pattern" in advisory
        assert "where" in advisory
        assert "why" in advisory
