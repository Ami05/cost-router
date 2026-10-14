"""Index read-modify-write must be race-safe and corruption must rebuild, not wipe."""
import json
import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import graph_store  # noqa: E402

PROCS = 4
PER_PROC = 10


def _worker(graphs_dir, wid):
    graphs = Path(graphs_dir)
    graph_store.GRAPHS_DIR = graphs
    graph_store.INDEX_PATH = graphs / "index.json"
    for i in range(PER_PROC):
        key = f"w{wid}_{i}"
        graph = {"last_read_at": "2026-01-01T00:00:00Z", "language": "python"}
        graph_store._update_index(key, graph, Path(f"/src/{key}.py"))


def _patch(monkeypatch, graphs):
    graphs.mkdir(exist_ok=True)
    monkeypatch.setattr(graph_store, "GRAPHS_DIR", graphs)
    monkeypatch.setattr(graph_store, "INDEX_PATH", graphs / "index.json")


def _write_graph(graphs, name):
    (graphs / f"{name}.json").write_text(json.dumps(
        {"last_read_at": "2026-01-01T00:00:00Z", "language": "python"}))


def test_concurrent_update_index_loses_nothing(monkeypatch, tmp_path):
    graphs = tmp_path / "cache"
    _patch(monkeypatch, graphs)
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_worker, args=(str(graphs), w)) for w in range(PROCS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    index = json.loads((graphs / "index.json").read_text())
    expected = {f"w{w}_{i}" for w in range(PROCS) for i in range(PER_PROC)}
    assert expected <= set(index)


def test_corrupt_index_is_rebuilt_not_wiped(monkeypatch, tmp_path):
    graphs = tmp_path / "cache"
    _patch(monkeypatch, graphs)
    for name in ("aaa", "bbb", "ccc"):
        _write_graph(graphs, name)
    (graphs / "index.json").write_text("{this is not json at all")
    graph = {"last_read_at": "2026-02-02T00:00:00Z", "language": "python"}
    graph_store._update_index("ddd", graph, Path("/src/d.py"))
    index = json.loads((graphs / "index.json").read_text())
    assert {"aaa", "bbb", "ccc", "ddd"} <= set(index)
    assert index["ddd"]["path"] == "/src/d.py"


def test_load_index_rebuilds_from_graph_files(monkeypatch, tmp_path):
    graphs = tmp_path / "cache"
    _patch(monkeypatch, graphs)
    _write_graph(graphs, "aaa")
    (graphs / "index.json").write_text("garbage-not-json")
    index = graph_store._load_index()
    assert "aaa" in index
    assert index["aaa"]["graph_file"] == "aaa.json"


def test_missing_index_loads_empty(monkeypatch, tmp_path):
    graphs = tmp_path / "cache"
    _patch(monkeypatch, graphs)
    assert graph_store._load_index() == {}


def test_rebuild_index_skips_non_graph_and_unreadable(monkeypatch, tmp_path):
    graphs = tmp_path / "cache"
    _patch(monkeypatch, graphs)
    _write_graph(graphs, "good")
    (graphs / "bad.json").write_text("not json")
    (graphs / "stale_refresh.json").write_text(json.dumps({"last_read_at": "x"}))
    (graphs / "index.json").write_text("{}")
    index = graph_store.rebuild_index()
    assert set(index) == {"good"}


def test_update_index_roundtrip_fields(monkeypatch, tmp_path):
    graphs = tmp_path / "cache"
    _patch(monkeypatch, graphs)
    graph = {"last_read_at": "2026-03-03T00:00:00Z", "language": "go"}
    graph_store._update_index("h1", graph, Path("/src/x.go"))
    entry = json.loads((graphs / "index.json").read_text())["h1"]
    assert entry["graph_file"] == "h1.json"
    assert entry["language"] == "go"
    assert entry["path"] == "/src/x.go"
