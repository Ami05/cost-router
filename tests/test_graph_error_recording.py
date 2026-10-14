"""Graph error recording: record_graph_error appends JSON lines, never raises, truncates ~1000.

Tests error recording in graph_errors.jsonl and fail-open behavior.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import graph_store  # noqa: E402
import graph_gc  # noqa: E402


def _setup(monkeypatch, tmp_path):
    """Setup temp dirs and patch module constants."""
    graphs = tmp_path / "cache"
    graphs.mkdir()
    errors_log = tmp_path / "graph_errors.jsonl"
    data_dir = tmp_path

    monkeypatch.setattr(graph_store, "GRAPHS_DIR", graphs)
    monkeypatch.setattr(graph_store, "INDEX_PATH", graphs / "index.json")
    monkeypatch.setattr(graph_store, "ERRORS_LOG", errors_log)
    monkeypatch.setattr(graph_gc, "GRAPHS_DIR", graphs)
    monkeypatch.setattr(graph_gc, "INDEX_PATH", graphs / "index.json")
    monkeypatch.setattr(graph_gc, "DATA_DIR", data_dir)
    monkeypatch.setattr(graph_gc, "DEBUG_LOG", data_dir / "debug.log")
    monkeypatch.setattr(graph_gc, "STATS_PATH", data_dir / "graph_gc_stats.json")
    monkeypatch.setenv("COST_ROUTER_GRAPH_TTL_DAYS", "7")

    return graphs, errors_log, data_dir


def test_record_graph_error_appends_json_line(monkeypatch, tmp_path):
    """record_graph_error appends {ts, component, exc_type, msg, path}."""
    graphs, errors_log, _ = _setup(monkeypatch, tmp_path)

    exc = ValueError("test error message")
    graph_store.record_graph_error("test_component", exc, "/some/file.py")

    assert errors_log.exists()
    lines = errors_log.read_text().strip().split("\n")
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["component"] == "test_component"
    assert entry["exc_type"] == "ValueError"
    assert entry["msg"] == "test error message"
    assert entry["path"] == "/some/file.py"
    assert "ts" in entry


def test_record_graph_error_without_path(monkeypatch, tmp_path):
    """record_graph_error works without optional path field."""
    graphs, errors_log, _ = _setup(monkeypatch, tmp_path)

    exc = RuntimeError("boom")
    graph_store.record_graph_error("some_func", exc)

    lines = errors_log.read_text().strip().split("\n")
    entry = json.loads(lines[0])
    assert "path" not in entry
    assert entry["component"] == "some_func"


def test_record_graph_error_truncates_long_messages(monkeypatch, tmp_path):
    """record_graph_error truncates exception messages to 200 chars."""
    graphs, errors_log, _ = _setup(monkeypatch, tmp_path)

    long_msg = "x" * 300
    exc = ValueError(long_msg)
    graph_store.record_graph_error("truncate_test", exc)

    entry = json.loads(errors_log.read_text().strip().split("\n")[0])
    assert len(entry["msg"]) == 200


def test_record_graph_error_never_raises_on_write_fail(monkeypatch, tmp_path):
    """record_graph_error never raises even if dir unwritable."""
    graphs, errors_log, _ = _setup(monkeypatch, tmp_path)

    read_only = tmp_path / "readonly"
    read_only.mkdir()
    monkeypatch.setattr(graph_store, "ERRORS_LOG", read_only / "errors.jsonl")
    read_only.chmod(0o555)

    try:
        exc = ValueError("test")
        # Should not raise even though we can't write
        graph_store.record_graph_error("test", exc)
    finally:
        read_only.chmod(0o755)


def test_record_graph_error_truncates_to_1000_lines(monkeypatch, tmp_path):
    """record_graph_error keeps only last ~1000 lines."""
    graphs, errors_log, _ = _setup(monkeypatch, tmp_path)

    for i in range(1100):
        exc = ValueError(f"error {i}")
        graph_store.record_graph_error(f"comp_{i % 10}", exc)

    lines = errors_log.read_text().strip().split("\n")
    assert len(lines) <= 1000

    last_entry = json.loads(lines[-1])
    assert "1099" in last_entry["msg"]


def test_cache_usage_records_error_on_scandir_fail(monkeypatch, tmp_path):
    """_cache_usage catches OSError, records it, returns (0, 0)."""
    graphs, errors_log, _ = _setup(monkeypatch, tmp_path)

    def boom(*args):
        raise OSError("scandir failed")

    monkeypatch.setattr(os, "scandir", boom)

    count, size = graph_store._cache_usage(graphs)

    assert count == 0
    assert size == 0

    entries = errors_log.read_text().strip().split("\n")
    err = json.loads(entries[0])
    assert err["component"] == "graph_store._cache_usage"


def test_cache_is_full_records_error_on_scandir_fail(monkeypatch, tmp_path):
    """cache_is_full catches OSError, records it, returns False (fail-open)."""
    graphs, errors_log, _ = _setup(monkeypatch, tmp_path)

    def boom(*args):
        raise OSError("scandir failed")

    monkeypatch.setattr(os, "scandir", boom)

    is_full = graph_store.cache_is_full(graphs, 100)

    assert is_full is False

    entries = errors_log.read_text().strip().split("\n")
    err = json.loads(entries[0])
    assert err["component"] == "graph_store.cache_is_full"


def test_graph_gc_write_stats_includes_errors_by_component(monkeypatch, tmp_path):
    """graph_gc._write_stats collects errors_by_component from graph_errors.jsonl."""
    graphs, errors_log, data_dir = _setup(monkeypatch, tmp_path)

    for i in range(3):
        graph_store.record_graph_error("component_a", ValueError("error"))
    for i in range(2):
        graph_store.record_graph_error("component_b", RuntimeError("error"))

    graph_gc._write_stats(0)

    stats_path = data_dir / "graph_gc_stats.json"
    stats = json.loads(stats_path.read_text())

    assert "errors_by_component" in stats
    assert stats["errors_by_component"]["component_a"] == 3
    assert stats["errors_by_component"]["component_b"] == 2


def test_graph_gc_write_stats_handles_missing_errors_log(monkeypatch, tmp_path):
    """graph_gc._write_stats handles missing errors log gracefully."""
    graphs, errors_log, data_dir = _setup(monkeypatch, tmp_path)

    graph_gc._write_stats(0)

    stats_path = data_dir / "graph_gc_stats.json"
    stats = json.loads(stats_path.read_text())

    assert stats.get("errors_by_component") is None
