"""graph_update._write_stats: includes errors_by_component counts in output.

Tests that graph_update's stats file tracks errors by component.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import graph_store  # noqa: E402
import graph_update  # noqa: E402


def _setup(monkeypatch, tmp_path):
    """Setup temp dirs and patch DATA_DIR."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setattr(graph_store, "ERRORS_LOG", data_dir / "graph_errors.jsonl")
    monkeypatch.setattr(graph_update, "DATA_DIR", data_dir)

    return data_dir


def test_graph_update_write_stats_includes_errors_by_component(monkeypatch, tmp_path):
    """graph_update._write_stats includes errors_by_component in output."""
    data_dir = _setup(monkeypatch, tmp_path)

    # Create some errors in the log
    for i in range(3):
        graph_store.record_graph_error("component_a", ValueError("error"))
    for i in range(2):
        graph_store.record_graph_error("component_b", RuntimeError("error"))

    # Call _write_stats
    session_id = "test_session"
    stats_list = [
        {"project": "proj1", "files_changed": 5, "errors": 0},
        {"project": "proj2", "files_changed": 3, "errors": 0},
    ]

    graph_update._write_stats(session_id, stats_list)

    # Verify stats file has errors_by_component
    stats_file = data_dir / "graph_update_stats.json"
    assert stats_file.exists()

    stats = json.loads(stats_file.read_text())
    assert "sessions" in stats
    assert session_id in stats["sessions"]

    session_stat = stats["sessions"][session_id]
    assert "errors_by_component" in session_stat

    if session_stat["errors_by_component"]:
        assert session_stat["errors_by_component"]["component_a"] == 3
        assert session_stat["errors_by_component"]["component_b"] == 2


def test_graph_update_write_stats_handles_no_errors(monkeypatch, tmp_path):
    """graph_update._write_stats handles absence of errors log gracefully."""
    data_dir = _setup(monkeypatch, tmp_path)

    session_id = "clean_session"
    stats_list = []

    graph_update._write_stats(session_id, stats_list)

    stats_file = data_dir / "graph_update_stats.json"
    stats = json.loads(stats_file.read_text())

    session_stat = stats["sessions"][session_id]
    assert session_stat["errors_by_component"] is None
