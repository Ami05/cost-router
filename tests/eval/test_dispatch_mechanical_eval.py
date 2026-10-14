#!/usr/bin/env python3
"""Offline eval for deny-mechanical-on-reasoning-tier feature.

Tests:
  - classify_dispatch_work() accuracy across mechanical / judgment / bulk-edit / ambiguous
  - event_explore_model_gate() enforces haiku for Explore subagents

No API key, no subprocess — direct function imports only.

Usage:
  pytest tests/eval/test_dispatch_mechanical_eval.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import check_routing as cr  # noqa: E402

# ---------------------------------------------------------------------------
# classify_dispatch_work — classification accuracy
# ---------------------------------------------------------------------------

_MECHANICAL_PROMPTS = [
    "rename symbol foo to bar in utils.py",
    "delete unused imports from scripts/gates.py",
    "add one line to config.json for the new flag",
    "fix the typo in the error message",
    "refactor the helper function into _common.py",
]

_JUDGMENT_PROMPTS = [
    "design the authentication architecture for the new service",
    "evaluate tradeoffs between approach A and approach B",
    "brainstorm solutions for the caching strategy",
    "recommend which model tier to use for this task",
    "decide on the right abstraction for the gate chain",
]

_BULK_EDIT_PROMPTS = [
    "delete all test files from the suite",
    "update imports in every file after the refactor",
]

_AMBIGUOUS_PROMPTS = [
    "review the system behavior",
    "this needs attention",
]


def test_mechanical_classification():
    """Mechanical prompts must classify as mechanical."""
    for prompt in _MECHANICAL_PROMPTS:
        result = cr.classify_dispatch_work(prompt)
        assert result == "mechanical", (
            f"Expected mechanical, got {result!r} for: {prompt!r}"
        )


def test_judgment_classification():
    """Judgment prompts must classify as judgment."""
    for prompt in _JUDGMENT_PROMPTS:
        result = cr.classify_dispatch_work(prompt)
        assert result == "judgment", (
            f"Expected judgment, got {result!r} for: {prompt!r}"
        )


def test_bulk_edit_classification():
    """Bulk-edit prompts must classify as bulk-edit."""
    for prompt in _BULK_EDIT_PROMPTS:
        result = cr.classify_dispatch_work(prompt)
        assert result == "bulk-edit", (
            f"Expected bulk-edit, got {result!r} for: {prompt!r}"
        )


def test_ambiguous_classification():
    """Ambiguous prompts must not classify as mechanical or judgment."""
    for prompt in _AMBIGUOUS_PROMPTS:
        result = cr.classify_dispatch_work(prompt)
        assert result == "ambiguous", (
            f"Expected ambiguous, got {result!r} for: {prompt!r}"
        )


# ---------------------------------------------------------------------------
# event_explore_model_gate — haiku enforcement for Explore subagents
# ---------------------------------------------------------------------------

def _explore_payload(model: str) -> dict:
    return {
        "tool_input": {"subagent_type": "Explore", "model": model},
        "session_id": "test-dispatch-mechanical-eval",
    }


def _non_explore_payload(model: str) -> dict:
    return {
        "tool_input": {"subagent_type": "general-purpose", "model": model},
        "session_id": "test-dispatch-mechanical-eval",
    }


def test_explore_gate_denies_non_haiku():
    """Explore subagent with sonnet model must be denied."""
    result = cr.event_explore_model_gate(_explore_payload("sonnet"), False)
    decision = result.get("permission") or result.get(
        "hookSpecificOutput", {}
    ).get("permissionDecision", "")
    assert decision == "deny", f"Expected deny for Explore+sonnet, got: {result}"


def test_explore_gate_allows_haiku():
    """Explore subagent with haiku model must be allowed."""
    result = cr.event_explore_model_gate(_explore_payload("haiku"), False)
    decision = result.get("permission") or result.get(
        "hookSpecificOutput", {}
    ).get("permissionDecision", "allow")
    assert decision != "deny", f"Expected allow for Explore+haiku, got: {result}"


def test_explore_gate_ignores_non_explore():
    """Non-Explore subagent type must always be allowed regardless of model."""
    result = cr.event_explore_model_gate(_non_explore_payload("sonnet"), False)
    decision = result.get("permission") or result.get(
        "hookSpecificOutput", {}
    ).get("permissionDecision", "allow")
    assert decision != "deny", f"Expected allow for non-Explore, got: {result}"
