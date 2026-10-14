"""
Test CURSOR_EVENT_NAMES validation.

Asserts that each name in CURSOR_EVENT_NAMES is a non-empty string,
and that the set is non-empty (no unintended mutations of the constant).
"""
import pytest
from scripts.check_routing import CURSOR_EVENT_NAMES


def test_cursor_event_names_non_empty():
    """CURSOR_EVENT_NAMES must be a non-empty set."""
    assert CURSOR_EVENT_NAMES, "CURSOR_EVENT_NAMES is empty — no Cursor events will be detected"


def test_cursor_event_names_all_strings():
    """All entries in CURSOR_EVENT_NAMES must be non-empty strings."""
    for name in CURSOR_EVENT_NAMES:
        assert isinstance(name, str), f"Non-string entry in CURSOR_EVENT_NAMES: {name!r}"
        assert name, f"Empty string entry in CURSOR_EVENT_NAMES: {name!r}"


def test_cursor_event_names_no_duplicates():
    """Frozenset enforces uniqueness, but verify the intended set is intact."""
    expected = frozenset({
        "cursor_precursor_execute",
        "cursor_action",
        "cursor_write",
    })
    assert CURSOR_EVENT_NAMES == expected, (
        f"CURSOR_EVENT_NAMES diverged from canonical schema. "
        f"Expected {expected}, got {CURSOR_EVENT_NAMES}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
