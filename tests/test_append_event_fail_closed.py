"""Integration test: append_event fail-closed behavior when routing_events.jsonl is unreachable.

TICKET-078: Fail-closed validation for append_event
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _fileio import append_event  # noqa: E402


def test_append_event_fail_closed_on_readonly_path():
    """When append_event cannot write to routing_events.jsonl, it should not raise an exception (fail-closed)."""
    test_event = {
        "event_type": "routing_declared",
        "session_id": "test-session-123",
        "model_key": "haiku",
        "timestamp": "2026-09-21T00:00:00Z",
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        # Block DATA_DIR creation by occupying its path with a file instead of a dir,
        # so append_event's DATA_DIR.mkdir() raises OSError (FileExistsError).
        blocked_data_dir = Path(tmpdir) / "blocked_dir"
        blocked_data_dir.write_text("not a directory")

        # Patch the DATA_DIR path (the actual constant append_event writes under)
        with patch("_fileio.DATA_DIR", blocked_data_dir):
            # This should NOT raise an exception, even though the directory doesn't exist
            try:
                result = append_event(test_event)
                # If no exception, fail-closed behavior is working
                assert result is None or isinstance(result, dict)
            except (FileNotFoundError, OSError, PermissionError) as e:
                raise AssertionError(
                    f"append_event raised {type(e).__name__} instead of failing closed: {e}"
                )


def test_append_event_idempotent_on_write_failure():
    """append_event should handle write failures gracefully and allow future calls to proceed."""
    test_event_1 = {
        "event_type": "routing_declared",
        "session_id": "test-session-456",
        "model_key": "sonnet",
        "timestamp": "2026-09-21T00:00:00Z",
    }

    test_event_2 = {
        "event_type": "extraction_fallback",
        "session_id": "test-session-456",
        "timestamp": "2026-09-21T00:00:01Z",
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        blocked_data_dir = Path(tmpdir) / "blocked_dir"
        blocked_data_dir.write_text("not a directory")

        with patch("_fileio.DATA_DIR", blocked_data_dir):
            # First call — write attempt fails gracefully
            try:
                append_event(test_event_1)
            except Exception as e:
                raise AssertionError(
                    f"First append_event raised exception: {type(e).__name__}: {e}"
                )

            # Second call — should also not raise, idempotent fail-closed
            try:
                append_event(test_event_2)
            except Exception as e:
                raise AssertionError(
                    f"Second append_event raised exception: {type(e).__name__}: {e}"
                )


if __name__ == "__main__":
    test_append_event_fail_closed_on_readonly_path()
    print("PASS: test_append_event_fail_closed_on_readonly_path")
    test_append_event_idempotent_on_write_failure()
    print("PASS: test_append_event_idempotent_on_write_failure")
