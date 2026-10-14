"""graph_update._find_project_roots fallback must never leave PLUGIN_ROOT."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import graph_update  # noqa: E402


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _setup(monkeypatch, tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(graph_update, "PLUGIN_ROOT", plugin)
    monkeypatch.setattr(graph_update, "DATA_DIR", data)
    monkeypatch.delenv("CURSOR_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return plugin, data


def test_fallback_ignores_env_and_cwd_outside_plugin_root(monkeypatch, tmp_path):
    plugin, _ = _setup(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere" / "deep" / "proj"
    outside.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(outside))
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.chdir(outside)
    roots = graph_update._find_project_roots()
    assert all(_inside(r, plugin) for r in roots)
    assert not any(_inside(r, outside) for r in roots)


def test_fallback_never_returns_filesystem_ancestors(monkeypatch, tmp_path):
    plugin, _ = _setup(monkeypatch, tmp_path)
    sub = plugin / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    roots = graph_update._find_project_roots()
    assert roots, "cwd inside PLUGIN_ROOT should still be usable"
    assert all(_inside(r, plugin) for r in roots)
    assert Path("/").resolve() not in [Path(r).resolve() for r in roots]
    assert tmp_path.resolve() not in [Path(r).resolve() for r in roots]


def test_registry_entries_outside_plugin_root_filtered(monkeypatch, tmp_path):
    plugin, data = _setup(monkeypatch, tmp_path)
    inside = plugin / "proj"
    inside.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    (data / "project_registry.json").write_text(json.dumps({
        "projects": {str(inside): {"path": str(inside)}, str(outside): {"path": str(outside)}}
    }))
    roots = graph_update._find_project_roots()
    assert [Path(r).resolve() for r in roots] == [inside.resolve()]


def test_empty_result_when_nothing_inside_plugin_root(monkeypatch, tmp_path):
    plugin, _ = _setup(monkeypatch, tmp_path)
    outside = tmp_path / "x"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert graph_update._find_project_roots() == []
