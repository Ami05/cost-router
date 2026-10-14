"""Tests for FV (FastValidator) read-only role gate enforcement."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from enforce_role_gates import extract_role_from_context, check_role_gate


def test_extract_role_from_context_fv():
    """extract_role_from_context should return FV from 'Routing: FV - check' text."""
    payload = {
        "text": "Here is my analysis. Routing: FV - check",
        "tool_name": "Read",
    }
    role = extract_role_from_context(payload)
    assert role == "FV", f"Expected 'FV', got {role!r}"


def test_check_role_gate_fv_denies_edit_write():
    """check_role_gate should deny Edit and Write for FV role."""
    # Test Edit denial
    payload_edit = {
        "text": "Routing: FV",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/some/file.py", "old_string": "x", "new_string": "y"},
    }
    result = check_role_gate(payload_edit)
    assert result.decision == "deny", f"Expected deny for Edit, got {result.decision}"
    assert "read-only role" in result.message.lower(), f"Expected 'read-only role' in message: {result.message}"

    # Test Write denial
    payload_write = {
        "text": "Routing: FV",
        "tool_name": "Write",
        "tool_input": {"path": "/some/file.py", "content": "test"},
    }
    result = check_role_gate(payload_write)
    assert result.decision == "deny", f"Expected deny for Write, got {result.decision}"
    assert "read-only role" in result.message.lower(), f"Expected 'read-only role' in message: {result.message}"


def test_check_role_gate_fv_allows_read_bash():
    """check_role_gate should allow Read and Bash for FV role."""
    # Test Read allow
    payload_read = {
        "text": "Routing: FV",
        "tool_name": "Read",
        "tool_input": {"file_path": "/some/file.py"},
    }
    result = check_role_gate(payload_read)
    assert result.decision == "allow", f"Expected allow for Read, got {result.decision}"

    # Test Bash allow
    payload_bash = {
        "text": "Routing: FV",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
    }
    result = check_role_gate(payload_bash)
    assert result.decision == "allow", f"Expected allow for Bash, got {result.decision}"
