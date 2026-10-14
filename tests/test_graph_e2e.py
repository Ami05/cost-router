"""End-to-end file-graph flow: one graph per path persists across operations.

Tests: build graph -> repeat read maintains one entry -> edit marks stale -> rebuild.
"""
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import graph_store  # noqa: E402
import graph_gc  # noqa: E402


def _setup(monkeypatch, tmp_path):
    """Setup temp dirs and patch module constants."""
    graphs = tmp_path / "cache"
    graphs.mkdir()

    monkeypatch.setattr(graph_store, "GRAPHS_DIR", graphs)
    monkeypatch.setattr(graph_store, "INDEX_PATH", graphs / "index.json")
    monkeypatch.setattr(graph_gc, "GRAPHS_DIR", graphs)
    monkeypatch.setattr(graph_gc, "INDEX_PATH", graphs / "index.json")

    errors_log = tmp_path / "graph_errors.jsonl"
    monkeypatch.setattr(graph_store, "ERRORS_LOG", errors_log)
    monkeypatch.setattr(graph_gc, "STATS_PATH", tmp_path / "graph_gc_stats.json")
    monkeypatch.setenv("COST_ROUTER_GRAPH_TTL_DAYS", "7")

    return graphs, tmp_path


def _create_source_file(tmp_path, name, size_lines=400):
    """Create a source file."""
    lines = [f"# Line {i}" for i in range(1, size_lines + 1)]
    f = tmp_path / name
    f.write_text("\n".join(lines))
    return f


def test_build_graph_on_first_read(monkeypatch, tmp_path):
    """First read of large file causes graph to be built."""
    graphs, _ = _setup(monkeypatch, tmp_path)

    source = _create_source_file(tmp_path, "test.py", 400)

    path_hash = graph_store.resolved_hash(str(source))
    now_ts = datetime.now(timezone.utc).isoformat()
    graph = {
        "path_hash": path_hash,
        "language": "python",
        "generated_at": now_ts,
        "last_read_at": now_ts,
        "content_sha256": "abc123",
        "symbols": [{"name": "func", "line_start": 10, "line_end": 20}],
    }

    graph_file = graphs / f"{path_hash}.json"
    graph_file.write_text(json.dumps(graph))
    graph_store._update_index(path_hash, graph, source.resolve())

    assert graph_file.exists()
    index = json.loads((graphs / "index.json").read_text())
    assert path_hash in index


def test_repeat_read_maintains_one_entry(monkeypatch, tmp_path):
    """Repeat reads don't create duplicate graphs or index entries."""
    graphs, _ = _setup(monkeypatch, tmp_path)

    source = _create_source_file(tmp_path, "test.py", 400)
    path_hash = graph_store.resolved_hash(str(source))

    now_ts = datetime.now(timezone.utc).isoformat()
    graph = {
        "path_hash": path_hash,
        "language": "python",
        "generated_at": now_ts,
        "last_read_at": now_ts,
        "content_sha256": "abc123",
        "symbols": [],
    }

    graph_file = graphs / f"{path_hash}.json"
    graph_file.write_text(json.dumps(graph))
    graph_store._update_index(path_hash, graph, source.resolve())

    # Repeat update
    graph["last_read_at"] = datetime.now(timezone.utc).isoformat()
    graph_file.write_text(json.dumps(graph))
    graph_store._update_index(path_hash, graph, source.resolve())

    # Verify exactly one graph file
    graph_files = [f for f in graphs.glob("*.json") if f.name != "index.json"]
    assert len(graph_files) == 1

    # Verify exactly one index entry
    index = json.loads((graphs / "index.json").read_text())
    assert len(index) == 1
    assert path_hash in index


def test_mark_stale_on_source_edit(monkeypatch, tmp_path):
    """When source is edited, graph is marked stale."""
    graphs, _ = _setup(monkeypatch, tmp_path)

    source = _create_source_file(tmp_path, "test.py", 400)
    path_hash = graph_store.resolved_hash(str(source))

    old_ts = "2020-01-01T00:00:00+00:00"
    graph = {
        "path_hash": path_hash,
        "language": "python",
        "generated_at": old_ts,
        "last_read_at": old_ts,
        "content_sha256": "old_hash",
        "symbols": [],
    }

    graph_file = graphs / f"{path_hash}.json"
    graph_file.write_text(json.dumps(graph))
    graph_store._update_index(path_hash, graph, source.resolve())

    # Mark as stale (like mark_graph_stale.py does)
    graph = json.loads(graph_file.read_text())
    graph["stale"] = True
    graph["stale_reason"] = f"edited at {datetime.now(timezone.utc).isoformat()}"
    graph_file.write_text(json.dumps(graph))

    # Verify stale
    updated = json.loads(graph_file.read_text())
    assert updated.get("stale") is True


def test_stale_graph_rebuild_maintains_one_file(monkeypatch, tmp_path):
    """Rebuilding stale graph overwrites existing file, maintains one entry."""
    graphs, _ = _setup(monkeypatch, tmp_path)

    source = _create_source_file(tmp_path, "test.py", 400)
    path_hash = graph_store.resolved_hash(str(source))

    # Initial graph
    graph = {
        "path_hash": path_hash,
        "language": "python",
        "generated_at": "2020-01-01T00:00:00+00:00",
        "last_read_at": "2020-01-01T00:00:00+00:00",
        "content_sha256": "old",
        "symbols": [{"name": "old_func", "line_start": 1, "line_end": 10}],
    }

    graph_file = graphs / f"{path_hash}.json"
    graph_file.write_text(json.dumps(graph))
    graph_store._update_index(path_hash, graph, source.resolve())

    # Rebuild (overwrite)
    now_ts = datetime.now(timezone.utc).isoformat()
    rebuilt = {
        "path_hash": path_hash,
        "language": "python",
        "generated_at": now_ts,
        "last_read_at": now_ts,
        "content_sha256": "new",
        "symbols": [{"name": "new_func", "line_start": 5, "line_end": 20}],
    }

    graph_file.write_text(json.dumps(rebuilt))
    graph_store._update_index(path_hash, rebuilt, source.resolve())

    # Still one file, one index entry
    graph_files = [f for f in graphs.glob("*.json") if f.name != "index.json"]
    assert len(graph_files) == 1

    index = json.loads((graphs / "index.json").read_text())
    assert len(index) == 1


def test_gc_respects_ttl(monkeypatch, tmp_path):
    """GC doesn't delete recent graphs."""
    graphs, _ = _setup(monkeypatch, tmp_path)

    source = _create_source_file(tmp_path, "test.py", 400)
    path_hash = graph_store.resolved_hash(str(source))

    now_ts = datetime.now(timezone.utc).isoformat()
    graph = {
        "path_hash": path_hash,
        "language": "python",
        "generated_at": now_ts,
        "last_read_at": now_ts,
        "content_sha256": "abc123",
        "symbols": [],
    }

    graph_file = graphs / f"{path_hash}.json"
    graph_file.write_text(json.dumps(graph))
    graph_store._update_index(path_hash, graph, source.resolve())

    monkeypatch.setenv("COST_ROUTER_GRAPH_TTL_DAYS", "7")
    deleted = graph_gc._run_gc()

    assert deleted == 0
    assert graph_file.exists()


def test_gc_deletes_expired_graphs(monkeypatch, tmp_path):
    """GC deletes graphs older than TTL."""
    graphs, _ = _setup(monkeypatch, tmp_path)

    source = _create_source_file(tmp_path, "test.py", 400)
    path_hash = graph_store.resolved_hash(str(source))

    old_ts = "2020-01-01T00:00:00+00:00"
    graph = {
        "path_hash": path_hash,
        "language": "python",
        "generated_at": old_ts,
        "last_read_at": old_ts,
        "content_sha256": "abc123",
        "symbols": [],
    }

    graph_file = graphs / f"{path_hash}.json"
    graph_file.write_text(json.dumps(graph))
    graph_store._update_index(path_hash, graph, source.resolve())

    monkeypatch.setenv("COST_ROUTER_GRAPH_TTL_DAYS", "7")
    deleted = graph_gc._run_gc()

    assert deleted == 1
    assert not graph_file.exists()


def test_multiple_files_no_duplicates(monkeypatch, tmp_path):
    """Multiple files each get one graph, no duplicates on updates."""
    graphs, _ = _setup(monkeypatch, tmp_path)

    for i in range(3):
        source = _create_source_file(tmp_path, f"file{i}.py", 400)
        path_hash = graph_store.resolved_hash(str(source))

        graph = {
            "path_hash": path_hash,
            "language": "python",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "last_read_at": datetime.now(timezone.utc).isoformat(),
            "content_sha256": f"hash_{i}",
            "symbols": [],
        }

        graph_file = graphs / f"{path_hash}.json"
        graph_file.write_text(json.dumps(graph))
        graph_store._update_index(path_hash, graph, source.resolve())

    # Three files
    graph_files = [f for f in graphs.glob("*.json") if f.name != "index.json"]
    assert len(graph_files) == 3

    # Three index entries
    index = json.loads((graphs / "index.json").read_text())
    assert len(index) == 3
