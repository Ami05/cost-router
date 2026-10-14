"""Every graph reader/writer must key graphs by the resolved-path hash."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import build_file_graph  # noqa: E402
import graph_store  # noqa: E402
from _common import hash_target  # noqa: E402


def test_symlink_and_real_path_same_hash(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("def f():\n    pass\n")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    assert graph_store.resolved_hash(link) == graph_store.resolved_hash(real)
    assert graph_store.resolved_hash(link) == hash_target(str(real.resolve()))


def test_relative_path_same_hash(tmp_path, monkeypatch):
    real = tmp_path / "real.py"
    real.write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    assert graph_store.resolved_hash("real.py") == graph_store.resolved_hash(real)
    assert graph_store.resolved_hash("./sub/../real.py") == graph_store.resolved_hash(real)


def test_str_and_path_inputs_agree(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("x = 1\n")
    assert graph_store.resolved_hash(str(real)) == graph_store.resolved_hash(real)


def test_nonexistent_path_does_not_raise(tmp_path):
    assert graph_store.resolved_hash(tmp_path / "nope.py")


def test_read_via_symlink_and_real_share_one_graph(tmp_path, monkeypatch):
    graphs = tmp_path / "cache"
    for mod in (graph_store, build_file_graph):
        monkeypatch.setattr(mod, "GRAPHS_DIR", graphs)
        monkeypatch.setattr(mod, "INDEX_PATH", graphs / "index.json")
    real = tmp_path / "real.py"
    real.write_text("def f():\n    pass\n")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    build_file_graph.event_read({"tool_input": {"file_path": str(link)}}, True)
    build_file_graph.event_read({"tool_input": {"file_path": str(real)}}, True)
    written = [p for p in graphs.glob("*.json") if p.name != "index.json"]
    assert len(written) == 1
    assert written[0].stem == graph_store.resolved_hash(real)
