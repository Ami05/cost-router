"""Adversarial missing-key tests for normalize_payload() and NormalizedPayload fields.

V-4: one parametrized test per extraction function, exercising missing keys,
alternate key aliases, wrong types, and empty strings.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _payload import normalize_payload  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_session_env(monkeypatch):
    """Clear CURSOR_SESSION_ID and CLAUDE_SESSION_ID to prevent live env vars from breaking tests."""
    monkeypatch.delenv("CURSOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)


# ---------------------------------------------------------------------------
# session_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({}, "unknown-session"),
    ({"session_id": "s1"}, "s1"),
    ({"sessionId": "s2"}, "s2"),
    ({"conversation_id": "c1"}, "c1"),
    ({"conversationId": "c2"}, "c2"),
    ({"session_id": 123}, "unknown-session"),
    ({"session_id": ""}, "unknown-session"),
    ({"session_id": "  "}, "unknown-session"),
])
def test_session_id_extraction(raw, expected):
    assert normalize_payload(raw).session_id == expected


def test_session_id_env_fallback(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-sid")
    assert normalize_payload({}).session_id == "env-sid"


def test_session_id_cursor_env_wins(monkeypatch):
    monkeypatch.setenv("CURSOR_SESSION_ID", "cursor-sid")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-sid")
    assert normalize_payload({}).session_id == "cursor-sid"


# ---------------------------------------------------------------------------
# subagent_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({}, None),
    ({"agent_id": "a1"}, "a1"),
    ({"subagent_id": "a2"}, "a2"),
    ({"agentId": "a3"}, "a3"),
    ({"subagentId": "a4"}, "a4"),
    ({"agent_id": 42}, None),
    ({"agent_id": ""}, None),
    ({"agent_id": "  "}, None),
])
def test_subagent_id_extraction(raw, expected):
    assert normalize_payload(raw).subagent_id == expected


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({}, None),
    ({"model": "claude-sonnet"}, "claude-sonnet"),
    ({"model_name": "haiku"}, "haiku"),
    ({"modelName": "opus"}, "opus"),
    ({"model": 99}, None),
    ({"model": ""}, None),
])
def test_model_extraction(raw, expected):
    assert normalize_payload(raw).model == expected


# ---------------------------------------------------------------------------
# tool_input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({}, None),
    ({"tool_input": {"k": "v"}}, {"k": "v"}),
    ({"toolInput": {"k": "v"}}, {"k": "v"}),
    ({"tool_input": "not-a-dict"}, None),
    ({"tool_input": None}, None),
    ({"tool_input": []}, None),
])
def test_tool_input_extraction(raw, expected):
    assert normalize_payload(raw).tool_input == expected


# ---------------------------------------------------------------------------
# file_path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({}, None),
    ({"tool_input": {"path": "/a/b.py"}}, "/a/b.py"),
    ({"tool_input": {"file_path": "/c/d.py"}}, "/c/d.py"),
    ({"tool_input": {"target_file": "/e/f.py"}}, "/e/f.py"),
    ({"path": "/top.py"}, "/top.py"),
    ({"file_path": "/top2.py"}, "/top2.py"),
    ({"tool_input": {"path": 123}}, None),
    ({"tool_input": {"path": ""}}, None),
])
def test_file_path_extraction(raw, expected):
    assert normalize_payload(raw).file_path == expected


# ---------------------------------------------------------------------------
# hook_event_name / cursor_version (direct key access)
# ---------------------------------------------------------------------------

def test_hook_event_name_present():
    assert normalize_payload({"hook_event_name": "UserPromptSubmit"}).hook_event_name == "UserPromptSubmit"


def test_hook_event_name_missing():
    assert normalize_payload({}).hook_event_name is None


def test_cursor_version_present():
    assert normalize_payload({"cursor_version": "1.0"}).cursor_version == "1.0"


def test_cursor_version_missing():
    assert normalize_payload({}).cursor_version is None


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({}, None),
    ({"tool_input": {"command": "pytest tests/"}}, "pytest tests/"),
    ({"tool_input": {"command": "  ls  "}}, "ls"),
    ({"tool_input": {"command": "  "}}, None),
    ({"tool_input": {"command": ""}}, None),
    ({"tool_input": {"command": 42}}, None),
    ({"command": "ignored"}, None),
])
def test_command_extraction(raw, expected):
    assert normalize_payload(raw).command == expected


# ---------------------------------------------------------------------------
# prompt_text
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({}, ""),
    ({"prompt": "hello"}, "hello"),
    ({"text": "world"}, "world"),
    ({"message": "msg"}, "msg"),
    ({"prompt": 123}, ""),
    ({"prompt": ""}, ""),
])
def test_prompt_text_extraction(raw, expected):
    assert normalize_payload(raw).prompt_text == expected


def test_prompt_text_priority():
    raw = {"prompt": "p", "text": "t", "message": "m"}
    assert normalize_payload(raw).prompt_text == "p"


# ---------------------------------------------------------------------------
# assistant_text
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({}, ""),
    ({"text": "hi"}, "hi"),
    ({"last_assistant_message": "lam"}, "lam"),
    ({"thought": "th"}, "th"),
    ({"content": "co"}, "co"),
    ({"last_assistant_message": 42}, ""),
])
def test_assistant_text_extraction(raw, expected):
    assert normalize_payload(raw).assistant_text == expected


# ---------------------------------------------------------------------------
# tool_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({}, None),
    ({"tool_name": "Edit"}, "Edit"),
    ({"tool": "Write"}, "Write"),
    ({"toolName": "Bash"}, "Bash"),
    ({"tool_name": 0}, None),
    ({"tool_name": ""}, None),
])
def test_tool_name_extraction(raw, expected):
    assert normalize_payload(raw).tool_name == expected


# ---------------------------------------------------------------------------
# turn_id (host-aware)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({}, None),
    ({"prompt_id": "pid1"}, "pid1"),
    ({"promptId": "pid2"}, "pid2"),
    ({"generation_id": "gid1"}, "gid1"),
    ({"prompt_id": ""}, None),
])
def test_turn_id_claude_extraction(raw, expected):
    assert normalize_payload(raw).turn_id == expected


def test_turn_id_cursor_prefers_generation_id():
    raw = {"cursor_version": "1.0", "generation_id": "gen1", "prompt_id": "prom1"}
    assert normalize_payload(raw).turn_id == "gen1"


def test_turn_id_claude_prefers_prompt_id():
    raw = {"hook_event_name": "UserPromptSubmit", "prompt_id": "prom1", "generation_id": "gen1"}
    assert normalize_payload(raw).turn_id == "prom1"


def test_turn_id_missing_all_keys():
    assert normalize_payload({"cursor_version": "1.0"}).turn_id is None


# ---------------------------------------------------------------------------
# conversation_key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({"parent_conversation_id": "p1"}, "p1"),
    ({"conversation_id": "c1"}, "c1"),
    ({"conversationId": "c2"}, "c2"),
    ({"session_id": "s1"}, "s1"),
    ({"sessionId": "s2"}, "s2"),
])
def test_conversation_key_keys(raw, expected):
    assert normalize_payload(raw).conversation_key == expected


def test_conversation_key_env_fallback(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-conv")
    assert normalize_payload({}).conversation_key == "env-conv"


def test_conversation_key_default():
    assert normalize_payload({}).conversation_key == "unknown-session"


def test_conversation_key_parent_wins_over_session():
    raw = {"parent_conversation_id": "parent", "session_id": "sess"}
    assert normalize_payload(raw).conversation_key == "parent"


def test_conversation_key_empty_falls_through():
    raw = {"parent_conversation_id": "", "conversation_id": "c1"}
    assert normalize_payload(raw).conversation_key == "c1"
