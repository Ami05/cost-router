"""graph_update must index new graphs, skip its own cache dir, and honor caps."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import file_graph_refresh  # noqa: E402
import graph_store  # noqa: E402
import graph_update  # noqa: E402
from _common import hash_target  # noqa: E402


def _run(root, graphs, caps=None):
    ctx = [
        patch.object(file_graph_refresh, "GRAPHS_DIR", graphs),
        patch.object(graph_store, "GRAPHS_DIR", graphs),
        patch.object(graph_store, "INDEX_PATH", graphs / "index.json"),
    ]
    if caps is not None:
        ctx.append(patch.object(graph_update, "_graph_caps", return_value=caps))
    for c in ctx:
        c.start()
    try:
        return graph_update._update_project(root, "sid")
    finally:
        for c in reversed(ctx):
            c.stop()


def test_new_graphs_appear_in_index(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    files = [root / "a.py", root / "b.py"]
    files[0].write_text("def a():\n    pass\n")
    files[1].write_text("def b():\n    pass\n")
    graphs = tmp_path / "cache"
    stats = _run(root, graphs)
    assert stats["files_changed"] == 2
    index = json.loads((graphs / "index.json").read_text())
    for f in files:
        assert hash_target(str(f.resolve())) in index


def test_file_graphs_dir_is_not_indexed(tmp_path):
    root = tmp_path / "proj"
    (root / "file-graphs").mkdir(parents=True)
    for i in range(3):
        (root / "file-graphs" / f"g{i}.json").write_text("{}")
    graphs = tmp_path / "cache"
    stats = _run(root, graphs)
    assert stats["files_changed"] == 0
    assert not list(graphs.glob("*.json"))


def test_cap_limits_new_graph_files(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    for n in ("a", "b", "c"):
        (root / f"{n}.py").write_text(f"def {n}():\n    pass\n")
    graphs = tmp_path / "cache"
    stats = _run(root, graphs, caps=(1, 10**9))
    written = [p for p in graphs.glob("*.json") if p.name != "index.json"]
    assert len(written) == 1
    assert stats["capped"] is True
