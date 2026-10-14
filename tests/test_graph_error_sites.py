"""Each fail-open site in graph_store/graph_gc records its error AND keeps its fallback value."""
import fcntl
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import graph_gc  # noqa: E402
import graph_store  # noqa: E402


def _setup(monkeypatch, tmp_path):
    graphs = tmp_path / "cache"
    graphs.mkdir()
    errors_log = tmp_path / "graph_errors.jsonl"
    monkeypatch.setattr(graph_store, "GRAPHS_DIR", graphs)
    monkeypatch.setattr(graph_store, "INDEX_PATH", graphs / "index.json")
    monkeypatch.setattr(graph_store, "ERRORS_LOG", errors_log)
    monkeypatch.setattr(graph_gc, "GRAPHS_DIR", graphs)
    return graphs, errors_log


def _components(errors_log):
    return [json.loads(line)["component"] for line in errors_log.read_text().splitlines()]


def _boom_os(*args, **kwargs):
    raise OSError("injected")


def test_resolved_hash_records_and_falls_back(monkeypatch, tmp_path):
    _, errors_log = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "resolve", _boom_os)

    result = graph_store.resolved_hash("/some/where/file.py")

    assert result  # fallback hash of the unresolved path, not None
    assert "graph_store.resolved_hash.resolve" in _components(errors_log)


def test_update_index_records_and_does_not_raise(monkeypatch, tmp_path):
    _, errors_log = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(graph_store, "save_json_state", _boom_os)
    graph = {"last_read_at": "2026-01-01T00:00:00+00:00", "language": "python"}

    graph_store._update_index("abc", graph, tmp_path / "f.py")

    assert "graph_store._update_index" in _components(errors_log)


def test_rebuild_index_records_and_returns_empty(monkeypatch, tmp_path):
    _, errors_log = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(os, "scandir", _boom_os)

    assert graph_store.rebuild_index() == {}
    assert "graph_store.rebuild_index" in _components(errors_log)


def test_index_lock_timeout_is_recorded(monkeypatch, tmp_path):
    """Lock contention past the wait window proceeds unserialized (fail-open) but must be visible."""
    graphs, errors_log = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(graph_store, "_LOCK_WAIT_S", 0.05)
    lock_path = graphs / "index.lock"
    holder = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with graph_store._index_lock():
            pass
    finally:
        os.close(holder)

    assert any(c.startswith("graph_store._index_lock") for c in _components(errors_log))


def test_gc_main_records_failure_and_stays_fail_open(monkeypatch, tmp_path, capsys):
    _, errors_log = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(graph_gc, "read_stdin_json", lambda: {})

    def boom():
        raise RuntimeError("gc exploded")

    monkeypatch.setattr(graph_gc, "_run_gc", boom)

    assert graph_gc.main() == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert "graph_gc.main" in _components(errors_log)


def test_write_stats_counts_unparseable_error_lines(monkeypatch, tmp_path):
    _, errors_log = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(graph_gc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(graph_gc, "STATS_PATH", tmp_path / "graph_gc_stats.json")
    errors_log.write_text('{"component":"a"}\nnot json\n[1]\n')

    graph_gc._write_stats(0)

    counts = json.loads((tmp_path / "graph_gc_stats.json").read_text())["errors_by_component"]
    assert counts == {"a": 1, "_unparseable_error_log_line": 2}


def test_write_stats_reports_unreadable_error_log(monkeypatch, tmp_path):
    _, errors_log = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(graph_gc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(graph_gc, "STATS_PATH", tmp_path / "graph_gc_stats.json")
    errors_log.write_text('{"component":"a"}\n')
    real_open = Path.open

    def flaky(self, *args, **kwargs):
        if self == errors_log and args[:1] == ("r",):
            raise OSError("unreadable")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky)

    graph_gc._write_stats(0)

    counts = json.loads((tmp_path / "graph_gc_stats.json").read_text())["errors_by_component"]
    assert counts["_errors_log_unreadable"] == 1


def test_gc_unlink_records_and_returns_false(monkeypatch, tmp_path):
    _, errors_log = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "unlink", _boom_os)

    assert graph_gc._unlink("deadbeef") is False
    assert "graph_gc._unlink" in _components(errors_log)
