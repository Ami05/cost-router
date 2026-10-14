"""file_graph_gate: deny full Read of large file with graph; allow offset/limit and small files.

Tests gate_fn directly with correct emit_gate_result output format.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import graph_store  # noqa: E402
import file_graph_gate  # noqa: E402


def _setup(monkeypatch, tmp_path):
    """Setup temp dirs and patch module constants."""
    graphs = tmp_path / "cache"
    graphs.mkdir()

    monkeypatch.setattr(graph_store, "GRAPHS_DIR", graphs)
    monkeypatch.setattr(graph_store, "INDEX_PATH", graphs / "index.json")
    monkeypatch.setattr(file_graph_gate, "GRAPHS_DIR", graphs)

    errors_log = tmp_path / "graph_errors.jsonl"
    monkeypatch.setattr(graph_store, "ERRORS_LOG", errors_log)

    return graphs, tmp_path


def _create_temp_file(tmp_path, name, num_lines):
    """Create a temp file with num_lines lines."""
    f = tmp_path / name
    f.write_text("\n".join([f"line {i}" for i in range(num_lines)]))
    return f


def _write_graph_json(graphs_dir, path_hash, symbols=None):
    """Write a graph JSON file for the given hash."""
    if symbols is None:
        symbols = [
            {"name": "func_a", "line_start": 10, "line_end": 20},
            {"name": "func_b", "line_start": 30, "line_end": 50},
        ]

    graph = {
        "path_hash": path_hash,
        "language": "python",
        "generated_at": "2026-01-01T00:00:00Z",
        "last_read_at": "2026-01-01T00:00:00Z",
        "symbols": symbols,
    }
    (graphs_dir / f"{path_hash}.json").write_text(json.dumps(graph))


def test_gate_allows_subagent_context(monkeypatch, tmp_path):
    """Gate allows reads from subagent context."""
    graphs, _ = _setup(monkeypatch, tmp_path)
    large_file = _create_temp_file(tmp_path, "large.py", 500)

    path_hash = graph_store.resolved_hash(str(large_file))
    _write_graph_json(graphs, path_hash)

    payload = {
        "tool": "read",
        "tool_input": {"file_path": str(large_file)},
        "subagent_type": "Explore",
    }

    result = file_graph_gate.gate_fn(payload)

    # Non-Claude format: check permission key
    assert result.get("permission") == "allow"


def test_gate_allows_no_file_path(monkeypatch, tmp_path):
    """Gate allows when no file path can be extracted."""
    graphs, _ = _setup(monkeypatch, tmp_path)

    payload = {
        "tool": "read",
        "tool_input": {},
    }

    result = file_graph_gate.gate_fn(payload)

    assert result.get("permission") == "allow"


def test_gate_allows_image_files(monkeypatch, tmp_path):
    """Gate allows reads of image/PDF files."""
    graphs, _ = _setup(monkeypatch, tmp_path)

    for ext in [".png", ".jpg", ".pdf", ".svg"]:
        payload = {
            "tool": "read",
            "tool_input": {"file_path": f"/some/file{ext}"},
        }

        result = file_graph_gate.gate_fn(payload)
        assert result.get("permission") == "allow"


def test_gate_allows_partial_read(monkeypatch, tmp_path):
    """Gate allows offset/limit reads even of large files with graphs."""
    graphs, _ = _setup(monkeypatch, tmp_path)
    large_file = _create_temp_file(tmp_path, "large.py", 500)

    path_hash = graph_store.resolved_hash(str(large_file))
    _write_graph_json(graphs, path_hash)

    payload = {
        "tool": "read",
        "tool_input": {
            "file_path": str(large_file),
            "offset": 100,
            "limit": 50,
        },
    }

    result = file_graph_gate.gate_fn(payload)

    assert result.get("permission") == "allow"


def test_gate_allows_small_file(monkeypatch, tmp_path):
    """Gate allows full reads of files <= FILE_GRAPH_LINE_THRESHOLD lines."""
    graphs, _ = _setup(monkeypatch, tmp_path)
    small_file = _create_temp_file(tmp_path, "small.py", 100)

    payload = {
        "tool": "read",
        "tool_input": {"file_path": str(small_file)},
    }

    result = file_graph_gate.gate_fn(payload)

    assert result.get("permission") == "allow"


def test_gate_allows_no_node_map(monkeypatch, tmp_path):
    """Gate allows full reads of large files without node maps."""
    graphs, _ = _setup(monkeypatch, tmp_path)
    large_file = _create_temp_file(tmp_path, "large.py", 500)

    payload = {
        "tool": "read",
        "tool_input": {"file_path": str(large_file)},
    }

    result = file_graph_gate.gate_fn(payload)

    assert result.get("permission") == "allow"


def test_gate_denies_full_read_large_file_with_graph(monkeypatch, tmp_path):
    """Gate denies full reads of large files with node maps."""
    graphs, _ = _setup(monkeypatch, tmp_path)
    large_file = _create_temp_file(tmp_path, "large.py", 500)

    path_hash = graph_store.resolved_hash(str(large_file))
    _write_graph_json(graphs, path_hash)

    payload = {
        "tool": "read",
        "tool_input": {"file_path": str(large_file)},
    }

    result = file_graph_gate.gate_fn(payload)

    assert result.get("permission") == "deny"
    assert "agent_message" in result
    assert "file-graph-gate" in result["agent_message"]


def test_gate_allows_file_in_graphs_dir(monkeypatch, tmp_path):
    """Gate allows reads of files inside GRAPHS_DIR itself."""
    graphs, _ = _setup(monkeypatch, tmp_path)

    file_in_graphs = graphs / "some_file.py"
    file_in_graphs.write_text("x" * 5000)

    payload = {
        "tool": "read",
        "tool_input": {"file_path": str(file_in_graphs)},
    }

    result = file_graph_gate.gate_fn(payload)

    assert result.get("permission") == "allow"


def test_gate_fail_open_on_small_file_stat_error(monkeypatch, tmp_path):
    """Gate fails open on _is_small_file error, records it, allows."""
    import builtins

    graphs, _ = _setup(monkeypatch, tmp_path)
    errors_log = tmp_path / "graph_errors.jsonl"
    large_file = _create_temp_file(tmp_path, "large.py", 500)

    path_hash = graph_store.resolved_hash(str(large_file))
    _write_graph_json(graphs, path_hash)

    real_open = builtins.open
    target = str(large_file)

    def flaky_open(file, *args, **kwargs):
        if str(file) == target:
            raise OSError("open failed")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)

    payload = {
        "tool": "read",
        "tool_input": {"file_path": str(large_file)},
    }

    result = file_graph_gate.gate_fn(payload)

    assert result.get("permission") == "allow"

    entries = errors_log.read_text().strip().split("\n")
    err = json.loads(entries[0])
    assert err["component"] == "file_graph_gate._is_small_file"


def test_gate_fail_open_on_graph_load_error(monkeypatch, tmp_path):
    """Gate fails open on _load_graph error, records it, allows."""
    graphs, _ = _setup(monkeypatch, tmp_path)
    errors_log = tmp_path / "graph_errors.jsonl"
    large_file = _create_temp_file(tmp_path, "large.py", 500)

    path_hash = graph_store.resolved_hash(str(large_file))
    _write_graph_json(graphs, path_hash)

    def boom(*args, **kwargs):
        raise ValueError("load failed")

    monkeypatch.setattr(file_graph_gate, "load_json_state", boom)

    payload = {
        "tool": "read",
        "tool_input": {"file_path": str(large_file)},
    }

    result = file_graph_gate.gate_fn(payload)

    assert result.get("permission") == "allow"

    entries = errors_log.read_text().strip().split("\n")
    err = json.loads(entries[0])
    assert err["component"] == "file_graph_gate._load_graph"


def test_gate_allows_with_only_limit(monkeypatch, tmp_path):
    """Gate allows when limit is present (partial read)."""
    graphs, _ = _setup(monkeypatch, tmp_path)
    large_file = _create_temp_file(tmp_path, "large.py", 500)

    path_hash = graph_store.resolved_hash(str(large_file))
    _write_graph_json(graphs, path_hash)

    payload = {
        "tool": "read",
        "tool_input": {
            "file_path": str(large_file),
            "limit": 50,
        },
    }

    result = file_graph_gate.gate_fn(payload)

    assert result.get("permission") == "allow"


def test_gate_allows_with_only_offset(monkeypatch, tmp_path):
    """Gate allows when offset is present (partial read)."""
    graphs, _ = _setup(monkeypatch, tmp_path)
    large_file = _create_temp_file(tmp_path, "large.py", 500)

    path_hash = graph_store.resolved_hash(str(large_file))
    _write_graph_json(graphs, path_hash)

    payload = {
        "tool": "read",
        "tool_input": {
            "file_path": str(large_file),
            "offset": 100,
        },
    }

    result = file_graph_gate.gate_fn(payload)

    assert result.get("permission") == "allow"
