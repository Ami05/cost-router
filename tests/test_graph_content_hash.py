"""Symbol graph must rebuild when file bytes change outside the Edit/Write hooks."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import build_file_graph  # noqa: E402
from graph_builder import _build_graph, file_content_sha256  # noqa: E402

DATA = Path(__file__).parent / "data"


def _payload(path):
    return {"tool_name": "Read", "tool_input": {"file_path": str(path)}}


def _symbols(result):
    return json.dumps(result)


def test_graph_stores_content_sha256(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("def foo():\n    pass\n")
    g = _build_graph(f, "h")
    assert g["content_sha256"] == file_content_sha256(f)


def test_external_edit_triggers_rebuild(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("def foo():\n    pass\n")
    with patch.object(build_file_graph, "GRAPHS_DIR", tmp_path / "graphs"), \
         patch.object(build_file_graph, "_update_index"):
        first = _symbols(build_file_graph.event_read(_payload(f), False))
        assert "foo" in first
        f.write_text("def bar():\n    pass\n")  # external edit, no stale hook
        second = _symbols(build_file_graph.event_read(_payload(f), False))
    assert "bar" in second and "foo" not in second


def test_legacy_graph_without_hash_is_stale(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    assert not build_file_graph._hash_matches({"symbols": []}, f)
    g = _build_graph(f, "h")
    assert build_file_graph._hash_matches(g, f)
