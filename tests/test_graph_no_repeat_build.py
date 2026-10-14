"""Unchanged files build once; edits rebuild once; cache faults fail open."""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import build_file_graph  # noqa: E402
import file_graph_refresh  # noqa: E402
import graph_store  # noqa: E402
from graph_builder import _build_graph as real_build  # noqa: E402


class _Env:
    def __init__(self, monkeypatch, tmp_path, caps=None):
        self.graphs = tmp_path / "cache"
        self.builds = 0
        for mod in (graph_store, build_file_graph, file_graph_refresh):
            monkeypatch.setattr(mod, "GRAPHS_DIR", self.graphs)
            monkeypatch.setattr(mod, "INDEX_PATH", self.graphs / "index.json")
        if caps is not None:
            monkeypatch.setattr(graph_store, "_graph_caps", lambda: caps)
            monkeypatch.setattr(build_file_graph, "_graph_caps", lambda: caps, raising=False)

        def counting(path, path_hash):
            self.builds += 1
            return real_build(path, path_hash)

        monkeypatch.setattr(build_file_graph, "_build_graph", counting)

    def read(self, path):
        return build_file_graph.event_read({"tool_input": {"file_path": str(path)}}, True)

    def graph_files(self):
        return [p for p in self.graphs.glob("*.json") if p.name != "index.json"]


def test_unchanged_file_builds_once(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    src = tmp_path / "a.py"
    src.write_text("def a():\n    pass\n")
    for _ in range(4):
        env.read(src)
    assert env.builds == 1


def test_touch_without_content_change_does_not_rebuild(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    src = tmp_path / "a.py"
    src.write_text("def a():\n    pass\n")
    env.read(src)
    future = time.time() + 3600
    os.utime(src, (future, future))
    env.read(src)
    assert env.builds == 1


def test_edit_triggers_exactly_one_rebuild(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    src = tmp_path / "a.py"
    src.write_text("def a():\n    pass\n")
    env.read(src)
    src.write_text("def a():\n    pass\n\ndef b():\n    pass\n")
    env.read(src)
    env.read(src)
    assert env.builds == 2


def test_corrupt_graph_json_fails_open_and_recovers(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    src = tmp_path / "a.py"
    src.write_text("def a():\n    pass\n")
    env.read(src)
    (graph_file,) = env.graph_files()
    graph_file.write_text("{corrupt")
    result = env.read(src)
    assert isinstance(result, dict)
    assert json.loads(graph_file.read_text())  # rewritten as valid JSON
    assert env.builds == 2


def test_unparsable_source_fails_open(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    src = tmp_path / "broken.py"
    src.write_text("def (:\n  class ][ \x00\n")
    result = env.read(src)
    assert isinstance(result, dict)


def test_binary_source_fails_open(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    src = tmp_path / "blob.py"
    src.write_bytes(bytes(range(256)) * 4)
    assert isinstance(env.read(src), dict)


def test_cache_full_fails_open_without_writing(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path, caps=(1, 10**9))
    env.graphs.mkdir()
    (env.graphs / "existing.json").write_text(json.dumps(
        {"last_read_at": "2026-01-01T00:00:00Z", "language": "python"}))
    src = tmp_path / "new.py"
    src.write_text("def n():\n    pass\n")
    result = env.read(src)
    assert isinstance(result, dict)
    assert [p.name for p in env.graph_files()] == ["existing.json"]


def test_cache_is_full_helper(tmp_path):
    graphs = tmp_path / "g"
    graphs.mkdir()
    (graphs / "index.json").write_text("{}")
    (graphs / "a.json").write_text("{}")
    assert graph_store.cache_is_full(graphs, 1) is True
    assert graph_store.cache_is_full(graphs, 2) is False


def test_touch_due_throttle():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fresh = {"last_read_at": (now - timedelta(seconds=1)).isoformat()}
    old = {"last_read_at": (now - timedelta(days=1)).isoformat()}
    with patch.object(graph_store, "config_int", return_value=300):
        assert graph_store.touch_due(fresh, now) is False
        assert graph_store.touch_due(old, now) is True
        assert graph_store.touch_due({}, now) is True


def test_is_stale_false_for_touched_identical_content(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    src = tmp_path / "a.py"
    src.write_text("def a():\n    pass\n")
    env.read(src)
    h = graph_store.resolved_hash(src)
    future = time.time() + 3600
    os.utime(src, (future, future))
    assert file_graph_refresh._is_stale(src, h) is False


def test_is_stale_true_for_changed_content(monkeypatch, tmp_path):
    env = _Env(monkeypatch, tmp_path)
    src = tmp_path / "a.py"
    src.write_text("def a():\n    pass\n")
    env.read(src)
    h = graph_store.resolved_hash(src)
    src.write_text("def changed():\n    return 1\n")
    future = time.time() + 3600
    os.utime(src, (future, future))
    assert file_graph_refresh._is_stale(src, h) is True


def test_is_stale_true_when_graph_missing(monkeypatch, tmp_path):
    _Env(monkeypatch, tmp_path)
    src = tmp_path / "a.py"
    src.write_text("x = 1\n")
    assert file_graph_refresh._is_stale(src, "nohash") is True
