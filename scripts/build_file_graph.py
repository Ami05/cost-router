#!/usr/bin/env python3
"""cost-router hook: build a lightweight local symbol graph before file reads."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    emit_gate_result,
    gate_allow,
    hash_target,
    load_json_state,
    now_iso,
    read_stdin_json,
    save_json_state,
    session_id_of,
)
from _model_utils import extract_file_path, is_claude_payload  # noqa: E402
from graph_builder import _build_graph, file_content_sha256  # noqa: E402
from graph_store import (  # noqa: E402
    GRAPHS_DIR,
    INDEX_PATH,
    STALE_REFRESH_FILE,
    _format_output,
    _graph_caps,
    _update_index,
    cache_is_full,
    record_graph_read,
    touch_due,
)
from parsers import EXT_PARSERS, _language_for  # noqa: E402


def _hash_matches(graph: dict[str, Any], resolved: Path) -> bool:
    """True only if the cached content hash equals the file's current sha256.

    Missing/unreadable hashes (legacy graphs) count as stale to force a rebuild.
    """
    cached = graph.get("content_sha256")
    return bool(cached) and cached == file_content_sha256(resolved)


def _noop(is_claude: bool = False) -> None:
    print(json.dumps(emit_gate_result(gate_allow(), is_claude)))
    sys.stdout.flush()


def _run() -> None:
    payload = read_stdin_json()
    is_claude = is_claude_payload(payload)
    raw_path = extract_file_path(payload)
    if not raw_path:
        _noop(is_claude)
        return

    file_path = Path(raw_path)
    try:
        resolved = file_path.resolve()
    except OSError as exc:
        from graph_store import record_graph_error
        record_graph_error("build_file_graph._run.resolve", exc, str(file_path))
        _noop(is_claude)
        return

    if not resolved.is_file():
        _noop(is_claude)
        return

    path_hash = hash_target(str(resolved))
    if not path_hash:
        _noop(is_claude)
        return

    GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
    graph_file = GRAPHS_DIR / f"{path_hash}.json"
    ts = now_iso()

    if graph_file.is_file():
        graph = load_json_state(graph_file)
        if graph and not graph.get("stale") and _hash_matches(graph, resolved):
            if touch_due(graph):
                graph["last_read_at"] = ts
                save_json_state(graph_file, graph)
                _update_index(path_hash, graph, resolved)
            record_graph_read(session_id_of(payload), path_hash, resolved, payload.get("tool_input") or {}, graph, "hit", True)
            print(_format_output(resolved, graph, is_claude))
            return
        # Graph is absent or stale — fall through to force a full rebuild.

    if not graph_file.exists() and cache_is_full(GRAPHS_DIR, _graph_caps()[0]):
        record_graph_read(session_id_of(payload), path_hash, resolved, payload.get("tool_input") or {}, None, "cache_full", False)
        _noop(is_claude)
        return

    graph = _build_graph(resolved, path_hash)
    if graph is None:
        record_graph_read(session_id_of(payload), path_hash, resolved, payload.get("tool_input") or {}, None, "build_failed", False)
        _noop(is_claude)
        return

    save_json_state(graph_file, graph)
    _update_index(path_hash, graph, resolved)
    record_graph_read(session_id_of(payload), path_hash, resolved, payload.get("tool_input") or {}, graph, "built", True)
    print(_format_output(resolved, graph, is_claude))


def event_read(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """PreToolUse Read — build a local symbol graph and return GateResult."""
    raw_path = extract_file_path(payload)
    if not raw_path:
        return emit_gate_result(gate_allow(), is_claude)

    file_path = Path(raw_path)
    try:
        resolved = file_path.resolve()
    except OSError as exc:
        from graph_store import record_graph_error
        record_graph_error("build_file_graph.event_read.resolve", exc, str(file_path))
        return emit_gate_result(gate_allow(), is_claude)

    if not resolved.is_file():
        return emit_gate_result(gate_allow(), is_claude)

    path_hash = hash_target(str(resolved))
    if not path_hash:
        return emit_gate_result(gate_allow(), is_claude)

    GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
    graph_file = GRAPHS_DIR / f"{path_hash}.json"
    ts = now_iso()

    if graph_file.is_file():
        graph = load_json_state(graph_file)
        if graph and not graph.get("stale") and _hash_matches(graph, resolved):
            if touch_due(graph):
                graph["last_read_at"] = ts
                save_json_state(graph_file, graph)
                _update_index(path_hash, graph, resolved)
            record_graph_read(session_id_of(payload), path_hash, resolved, payload.get("tool_input") or {}, graph, "hit", True)
            output_text = _format_output(resolved, graph, is_claude)
            output_dict = json.loads(output_text)
            return output_dict
        # Graph is absent or stale — fall through to force a full rebuild.

    if not graph_file.exists() and cache_is_full(GRAPHS_DIR, _graph_caps()[0]):
        record_graph_read(session_id_of(payload), path_hash, resolved, payload.get("tool_input") or {}, None, "cache_full", False)
        return emit_gate_result(gate_allow(), is_claude)

    graph = _build_graph(resolved, path_hash)
    if graph is None:
        record_graph_read(session_id_of(payload), path_hash, resolved, payload.get("tool_input") or {}, None, "build_failed", False)
        return emit_gate_result(gate_allow(), is_claude)

    save_json_state(graph_file, graph)
    _update_index(path_hash, graph, resolved)
    record_graph_read(session_id_of(payload), path_hash, resolved, payload.get("tool_input") or {}, graph, "built", True)
    output_text = _format_output(resolved, graph, is_claude)
    output_dict = json.loads(output_text)
    return output_dict


def main() -> int:
    try:
        _run()
    except Exception as exc:  # noqa: BLE001
        from graph_store import record_graph_error
        record_graph_error("build_file_graph.main", exc)
        payload = read_stdin_json()
        is_claude = is_claude_payload(payload)
        _noop(is_claude)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
