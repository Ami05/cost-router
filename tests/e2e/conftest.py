"""Shared e2e test fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Mark all e2e tests
pytestmark = pytest.mark.e2e


@pytest.fixture
def mock_payload():
    """Create a mock hook payload."""
    def _make(tool="Edit", routing_role="DIRECT", session_id="test-session", **extras):
        payload = {
            "tool": tool,
            "routing_role": routing_role,
            "session_id": session_id,
        }
        payload.update(extras)
        return payload
    return _make


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Redirect DATA_DIR to a temp directory.

    _common re-exports DATA_DIR/USAGE_LOG/ROUTING_EVENTS_LOG from _config, but
    the actual read/write code paths (_fileio.append_event, _events) hold their
    own module-level bindings captured at import time via `from _config import
    ...`. Patching only _common's copy is a no-op for those modules, so the
    real production paths get written to/read from instead of tmp_path. Patch
    every module that holds its own binding of these constants.
    """
    import _common
    import _config
    import _fileio
    import _events

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    routing_log = data_dir / "routing_events.jsonl"

    for mod in (_common, _config, _fileio, _events):
        monkeypatch.setattr(mod, "DATA_DIR", data_dir, raising=False)
        monkeypatch.setattr(mod, "USAGE_LOG", routing_log, raising=False)
        monkeypatch.setattr(mod, "ROUTING_EVENTS_LOG", routing_log, raising=False)

    return data_dir
