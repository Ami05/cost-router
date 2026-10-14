"""Session-wide isolation via COST_ROUTER_DATA_DIR for subprocess hook tests.

Uses tests/data/ (not plugin data/) so generated artifacts stay inspectable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TEST_DATA_DIR = Path(__file__).resolve().parent / "data"
# When running under pytest-xdist, give each worker its own data dir to avoid race conditions
worker_id = os.environ.get("PYTEST_XDIST_WORKER")
if worker_id:
    TEST_DATA_DIR = TEST_DATA_DIR / worker_id
TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("COST_ROUTER_DATA_DIR", str(TEST_DATA_DIR))


@pytest.fixture(autouse=True, scope="session")
def _redirect_test_data_dir():
    yield


@pytest.fixture(autouse=True, scope="session")
def _clean_stale_test_state():
    """Remove leftover routing state/usage files from previous crashed test runs.

    Individual test classes patch ROUTING_STATE_FILE to a test-specific path
    inside tests/ and restore it in tearDown — but if a prior run crashed before
    tearDown, the file lingers and poisons the next full-suite run.
    """
    tests_dir = Path(__file__).resolve().parent
    for scan_dir in (tests_dir, tests_dir / "data"):
        for pattern in (".routing_state_*.json", ".usage_*.jsonl", ".rstate_sg_*.json", ".rstate_rt_*.json", ".counters_rt_*.json", ".subagent_rt_*.json"):
            for stale in scan_dir.glob(pattern):
                try:
                    stale.unlink()
                except OSError:
                    pass
    yield


_RESET_JSON_FILES = {
    "subagent_state.json": "{}",
    "routing_state.json": "{}",
    "gate_counters.json": "{}",
}
_DELETE_FILES = (
    "agent_call_stats.json",
    "dispatch_registry.json",
)
_RESET_JSONL_FILES = (
    "routing_events.jsonl",
    "semantic_role_alignment.jsonl",
    "semantic_low_confidence.jsonl",
    "extraction_errors.jsonl",
)


@pytest.fixture(autouse=True)
def _reset_shared_gate_state():
    """Reset accumulated gate state files before each test.

    Prevents subagent inflight counters, routing event logs, and shell gate
    counters from bleeding across test runs.
    """
    data_dir = Path(os.environ.get("COST_ROUTER_DATA_DIR", str(TEST_DATA_DIR)))
    for fname, initial in _RESET_JSON_FILES.items():
        (data_dir / fname).write_text(initial)
    for fname in _RESET_JSONL_FILES:
        fpath = data_dir / fname
        if fpath.exists():
            fpath.write_text("")
    # Accumulating stores (subagent cost stats, dispatch registry) must not carry
    # cost/worker state between tests or runs: delete so each consumer starts empty.
    for fname in _DELETE_FILES:
        try:
            (data_dir / fname).unlink()
        except OSError:
            pass
    yield


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Restore modules deleted by pb11 tests (del sys.modules[mod] pattern).

    test_pb11_integration.py and test_pb11_signals.py delete check_routing,
    _common, etc. from sys.modules then reimport them. This creates a new
    module object, causing patch("check_routing.X") to patch the wrong object
    (the new one in sys.modules) while cr in test_routing_gate.py still holds
    the old object. Gate patches never take effect on the old module.
    """
    mods = [
        "_common", "check_routing", "file_hash_guard",
        "session_summary", "estimate_dispatch_context", "classify_intent",
    ]
    saved = {k: sys.modules.get(k) for k in mods}
    yield
    for k, v in saved.items():
        if v is not None:
            sys.modules[k] = v
        elif k in sys.modules:
            del sys.modules[k]
