"""graph_gc must record each run in graph_gc_stats.json (no COST_ROUTER_DEBUG needed)."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import graph_gc  # noqa: E402
import graph_store  # noqa: E402

DAY = 86400


def _setup(monkeypatch, tmp_path):
    graphs = tmp_path / "cache"
    graphs.mkdir()
    for mod in (graph_gc, graph_store):
        monkeypatch.setattr(mod, "GRAPHS_DIR", graphs)
        monkeypatch.setattr(mod, "INDEX_PATH", graphs / "index.json")
    stats = tmp_path / "graph_gc_stats.json"
    monkeypatch.setattr(graph_gc, "STATS_PATH", stats)
    monkeypatch.setattr(graph_gc, "read_stdin_json", lambda: {})
    monkeypatch.setenv("COST_ROUTER_GRAPH_TTL_DAYS", "7")
    monkeypatch.delenv("COST_ROUTER_DEBUG", raising=False)
    return graphs, stats


def _old_orphan(graphs, name):
    p = graphs / f"{name}.json"
    p.write_text(json.dumps({"last_read_at": "2026-01-01T00:00:00Z", "language": "python"}))
    old = time.time() - 30 * DAY
    os.utime(p, (old, old))
    return p


def test_stats_written_without_debug_env(monkeypatch, tmp_path, capsys):
    graphs, stats = _setup(monkeypatch, tmp_path)
    for n in ("aaaa", "bbbb", "cccc"):
        _old_orphan(graphs, n)
    keep = graphs / "keep.json"
    keep.write_text(json.dumps({"last_read_at": "2026-01-01T00:00:00Z", "language": "python"}))

    assert graph_gc.main() == 0

    data = json.loads(stats.read_text())
    assert data["deleted"] == 3
    assert data["files_remaining"] == 1
    assert data["runs"] == 1
    assert data["total_deleted"] == 3
    assert data["bytes_remaining"] > 0
    assert data["last_run_at"]


def test_stats_accumulate_across_runs(monkeypatch, tmp_path, capsys):
    graphs, stats = _setup(monkeypatch, tmp_path)
    _old_orphan(graphs, "aaaa")
    graph_gc.main()
    _old_orphan(graphs, "bbbb")
    _old_orphan(graphs, "cccc")
    graph_gc.main()

    data = json.loads(stats.read_text())
    assert data["runs"] == 2
    assert data["deleted"] == 2
    assert data["total_deleted"] == 3
    assert data["files_remaining"] == 0


def test_gc_failure_does_not_raise(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(graph_gc, "_run_gc", boom)
    assert graph_gc.main() == 0
