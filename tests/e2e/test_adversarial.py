"""E2E tests: adversarial robustness (malformed input, unicode, concurrency).

Exercises real code paths with edge-case inputs to confirm fail-open behaviour.
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _common  # noqa: E402
import _events  # noqa: E402
import _config  # noqa: E402
import _fileio  # noqa: E402
from _common import append_event, read_events  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # USAGE_LOG is a backwards-compat alias for routing_events.jsonl after the
    # SRP split. Keep patching it so tests that reference _common.USAGE_LOG
    # directly still write to the temp dir.
    usage_log = data_dir / "routing_events.jsonl"
    # _fileio/_events/_config hold their own import-time bindings; patch all of
    # them or read_events/append_event hit the shared tests/data log.
    for mod in (_common, _config, _fileio, _events):
        monkeypatch.setattr(mod, "DATA_DIR", data_dir, raising=False)
        monkeypatch.setattr(mod, "USAGE_LOG", usage_log, raising=False)
        monkeypatch.setattr(mod, "ROUTING_EVENTS_LOG", usage_log, raising=False)
    monkeypatch.setattr(_common, "MAX_LOG_BYTES", 5 * 1024 * 1024)
    return {"data_dir": data_dir, "usage_log": usage_log}


# 1 ──────────────────────────────────────────────────────────────────────────

def test_malformed_jsonl_lines_do_not_crash():
    """read_events with corrupt JSONL lines returns valid records and skips bad ones."""
    usage_log: Path = _common.USAGE_LOG
    usage_log.parent.mkdir(parents=True, exist_ok=True)
    usage_log.write_text(
        '{"type":"tool","session_id":"good1","ts":"2025-01-01"}\n'
        "{bad json\n"
        'null\n'
        '42\n'
        '{"type":"tool","session_id":"good2","ts":"2025-01-02"}\n',
        encoding="utf-8",
    )
    events = read_events()
    # Only the two valid dict records should be returned
    assert len(events) == 2
    assert all(isinstance(e, dict) for e in events)
    assert events[0]["session_id"] == "good1"
    assert events[1]["session_id"] == "good2"


# 2 ──────────────────────────────────────────────────────────────────────────

def test_unicode_in_routing_verdict_does_not_crash():
    """Unicode characters in routing text must not crash has_routing_verdict."""
    import check_routing as cr  # local import to avoid state pollution

    exotic_texts = [
        "Routing: DIRECT — 日本語テスト",
        "Routing: FO — 🚀 deploy agent",
        "Routing: RB — Ação de rollback",
        "Routing: \u202e\u202eDIRECT",  # RTL override characters
        "Routing: DIRECT — café résumé naïve",
    ]
    for text in exotic_texts:
        try:
            result = cr.has_routing_verdict(text)
            assert isinstance(result, bool), f"has_routing_verdict must return bool for: {text!r}"
        except Exception as exc:
            pytest.fail(f"has_routing_verdict raised {exc!r} for input: {text!r}")


# 3 ──────────────────────────────────────────────────────────────────────────

def test_concurrent_append_no_data_loss():
    """Multiple threads appending distinct events → all events persisted (no lock contention loss)."""
    n_workers = 8
    n_records_each = 5

    def _worker(worker_id: int):
        for i in range(n_records_each):
            append_event({
                "type": "tool",
                "session_id": f"concurrent-worker-{worker_id}",
                "direction": "context",
                "tool_name": "Edit",
                # _fileio TTL dedup keys on (type, session_id, reason); a unique
                # reason per record keeps them distinct (target_hash is not in it).
                "reason": f"w{worker_id}-r{i}",
                "bytes": 100,
                "est_tokens": 25,
                "target_hash": f"hash-w{worker_id}-r{i:04d}",  # unique per record
            })

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(_worker, w) for w in range(n_workers)]
        for f in futures:
            f.result()

    events = read_events()
    assert len(events) == n_workers * n_records_each, (
        f"Expected {n_workers * n_records_each} events, got {len(events)}"
    )
