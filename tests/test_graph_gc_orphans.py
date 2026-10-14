"""graph_gc must sweep unindexed/orphaned graphs, honor caps and the per-run delete budget."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import _config  # noqa: E402
import graph_gc  # noqa: E402
import graph_store  # noqa: E402

DAY = 86400


def _setup(monkeypatch, tmp_path, ttl_days=7, caps=None, max_deletes=None):
    graphs = tmp_path / "cache"
    graphs.mkdir()
    index = graphs / "index.json"
    for mod in (graph_gc, graph_store):
        monkeypatch.setattr(mod, "GRAPHS_DIR", graphs)
        monkeypatch.setattr(mod, "INDEX_PATH", index)
    monkeypatch.setenv("COST_ROUTER_GRAPH_TTL_DAYS", str(ttl_days))
    if caps is not None:
        monkeypatch.setattr(graph_store, "_graph_caps", lambda: caps)
        monkeypatch.setattr(graph_gc, "_graph_caps", lambda: caps, raising=False)
    if max_deletes is not None:
        real = _config.config_int

        def fake(path, default=0, flat_key=None):
            if path == "graph.gc_max_deletes_per_run":
                return max_deletes
            return real(path, default, flat_key)

        monkeypatch.setattr(_config, "config_int", fake)
        monkeypatch.setattr(graph_gc, "config_int", fake, raising=False)
        monkeypatch.setattr(graph_store, "config_int", fake, raising=False)
    return graphs, index


def _graph(graphs, name, age_days=0):
    p = graphs / f"{name}.json"
    p.write_text(json.dumps({"last_read_at": "2026-01-01T00:00:00Z", "language": "python"}))
    t = time.time() - age_days * DAY
    os.utime(p, (t, t))
    return p


def _iso(age_days):
    t = time.time() - age_days * DAY
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _entry(name, src, age_days=0):
    return {"last_read_at": _iso(age_days), "graph_file": f"{name}.json", "language": "python", "path": str(src)}


def _write_index(index_path, entries):
    index_path.write_text(json.dumps(entries))


def test_old_unindexed_graph_is_swept_recent_kept(monkeypatch, tmp_path):
    graphs, index = _setup(monkeypatch, tmp_path)
    src = tmp_path / "a.py"
    src.write_text("x = 1\n")
    kept = _graph(graphs, "kept")
    _write_index(index, {"kept": _entry("kept", src)})
    old_orphan = _graph(graphs, "orphan_old", age_days=60)
    new_orphan = _graph(graphs, "orphan_new", age_days=0)
    graph_gc._run_gc()
    assert not old_orphan.exists()
    assert new_orphan.exists()
    assert kept.exists()


def test_non_graph_files_are_never_swept(monkeypatch, tmp_path):
    graphs, index = _setup(monkeypatch, tmp_path)
    src = tmp_path / "a.py"
    src.write_text("x = 1\n")
    _graph(graphs, "kept")
    _write_index(index, {"kept": _entry("kept", src)})
    stale = graphs / "stale_refresh.json"
    stale.write_text("{}")
    t = time.time() - 90 * DAY
    os.utime(stale, (t, t))
    os.utime(index, (t, t))
    graph_gc._run_gc()
    assert index.exists()
    assert stale.exists()


def test_index_entry_with_missing_source_is_dropped(monkeypatch, tmp_path):
    graphs, index = _setup(monkeypatch, tmp_path)
    live_src = tmp_path / "live.py"
    live_src.write_text("x = 1\n")
    _graph(graphs, "live")
    _graph(graphs, "gone")
    _write_index(index, {
        "live": _entry("live", live_src),
        "gone": _entry("gone", tmp_path / "deleted_source.py"),
    })
    graph_gc._run_gc()
    remaining = json.loads(index.read_text())
    assert "live" in remaining
    assert "gone" not in remaining
    assert not (graphs / "gone.json").exists()
    assert (graphs / "live.json").exists()


def test_expired_indexed_entry_removed(monkeypatch, tmp_path):
    graphs, index = _setup(monkeypatch, tmp_path, ttl_days=7)
    src = tmp_path / "a.py"
    src.write_text("x = 1\n")
    _graph(graphs, "old")
    _write_index(index, {"old": _entry("old", src, age_days=30)})
    graph_gc._run_gc()
    assert not (graphs / "old.json").exists()
    assert "old" not in json.loads(index.read_text())


def test_file_count_cap_enforced(monkeypatch, tmp_path):
    graphs, index = _setup(monkeypatch, tmp_path, caps=(2, 10**9))
    entries = {}
    for i in range(5):
        src = tmp_path / f"s{i}.py"
        src.write_text("x = 1\n")
        _graph(graphs, f"g{i}")
        entries[f"g{i}"] = _entry(f"g{i}", src, age_days=i)
    _write_index(index, entries)
    graph_gc._run_gc()
    left = [p for p in graphs.glob("*.json") if p.name not in ("index.json", "stale_refresh.json")]
    assert len(left) <= 2


def test_total_bytes_cap_enforced(monkeypatch, tmp_path):
    graphs, index = _setup(monkeypatch, tmp_path, caps=(10**6, 300))
    entries = {}
    for i in range(5):
        src = tmp_path / f"s{i}.py"
        src.write_text("x = 1\n")
        p = _graph(graphs, f"g{i}")
        p.write_text(json.dumps({"last_read_at": "2026-01-01T00:00:00Z", "pad": "x" * 200}))
        entries[f"g{i}"] = _entry(f"g{i}", src, age_days=i)
    _write_index(index, entries)
    graph_gc._run_gc()
    total = sum(p.stat().st_size for p in graphs.glob("*.json") if p.name != "index.json")
    assert total <= 300


def test_max_deletes_per_run_budget(monkeypatch, tmp_path):
    graphs, index = _setup(monkeypatch, tmp_path, max_deletes=3)
    for i in range(10):
        _graph(graphs, f"orphan{i}", age_days=60)
    src = tmp_path / "a.py"
    src.write_text("x = 1\n")
    _graph(graphs, "kept")
    _write_index(index, {"kept": _entry("kept", src)})
    graph_gc._run_gc()
    left = [p for p in graphs.glob("*.json") if p.name.startswith("orphan")]
    assert len(left) >= 7
    assert len(left) < 10


def test_gc_without_index_is_safe(monkeypatch, tmp_path):
    graphs, index = _setup(monkeypatch, tmp_path)
    assert not index.exists()
    graph_gc._run_gc()  # must not raise
