"""mark_graph_stale hook via subprocess: sets stale=true in graph JSON.

Tests the real mark_graph_stale.py hook script via subprocess stdin/stdout.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import graph_store  # noqa: E402


def _setup_isolated(tmp_path):
    """Setup isolated DATA_DIR for subprocess hooks."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    graphs_dir = data_dir / "file-graphs"
    graphs_dir.mkdir()
    return data_dir, graphs_dir


def _run_hook(hook_script, payload, data_dir):
    """Run hook script via subprocess with stdin JSON, return stdout dict."""
    script_path = Path(__file__).parent.parent / "scripts" / hook_script
    env = os.environ.copy()
    env["COST_ROUTER_DATA_DIR"] = str(data_dir)

    result = subprocess.run(
        [sys.executable, str(script_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Hook failed with code {result.returncode}: {result.stderr}")

    return json.loads(result.stdout) if result.stdout.strip() else {}


def test_mark_graph_stale_sets_stale_true_via_subprocess(tmp_path):
    """mark_graph_stale.py hook sets stale=true in graph JSON."""
    data_dir, graphs_dir = _setup_isolated(tmp_path)

    source_file = tmp_path / "source.py"
    source_file.write_text("x" * 1000)

    # Create graph
    path_hash = graph_store.resolved_hash(str(source_file))
    graph = {
        "path_hash": path_hash,
        "language": "python",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "last_read_at": datetime.now(timezone.utc).isoformat(),
        "symbols": [],
    }

    graph_file = graphs_dir / f"{path_hash}.json"
    graph_file.write_text(json.dumps(graph))

    # Call hook via subprocess
    payload = {
        "tool": "write",
        "tool_input": {"file_path": str(source_file)},
    }

    result = _run_hook("mark_graph_stale.py", payload, data_dir)

    # Verify graph now has stale=true and stale_reason
    updated = json.loads(graph_file.read_text())
    assert updated.get("stale") is True
    assert "stale_reason" in updated


def test_mark_graph_stale_handles_missing_graph(tmp_path):
    """mark_graph_stale hook gracefully handles file with no graph."""
    data_dir, graphs_dir = _setup_isolated(tmp_path)

    source_file = tmp_path / "nosuch.py"
    source_file.write_text("content")

    payload = {
        "tool": "write",
        "tool_input": {"file_path": str(source_file)},
    }

    result = _run_hook("mark_graph_stale.py", payload, data_dir)

    # Should return empty JSON without error (no-op)
    assert result == {}


def test_mark_graph_stale_preserves_graph_fields(tmp_path):
    """mark_graph_stale preserves symbols, language, content_sha256."""
    data_dir, graphs_dir = _setup_isolated(tmp_path)

    source_file = tmp_path / "test.py"
    source_file.write_text("def foo(): pass")

    path_hash = graph_store.resolved_hash(str(source_file))
    original_symbols = [{"name": "foo", "line_start": 1, "line_end": 1}]
    graph = {
        "path_hash": path_hash,
        "language": "python",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "last_read_at": "2026-01-01T00:00:00+00:00",
        "symbols": original_symbols,
        "content_sha256": "abc123",
    }

    graph_file = graphs_dir / f"{path_hash}.json"
    graph_file.write_text(json.dumps(graph))

    payload = {
        "tool": "write",
        "tool_input": {"file_path": str(source_file)},
    }

    result = _run_hook("mark_graph_stale.py", payload, data_dir)

    updated = json.loads(graph_file.read_text())
    assert updated["language"] == "python"
    assert updated["symbols"] == original_symbols
    assert updated["content_sha256"] == "abc123"
    assert updated.get("stale") is True
