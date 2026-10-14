#!/usr/bin/env python3
"""PreToolUse:Read gate — denies full reads of large files when a node map exists.

When build_file_graph.py has already built a per-file symbol graph for a large
file, a full reload wastes tokens that a targeted offset/limit read could avoid.
This gate denies the full read and tells the caller which symbols exist so they
can use a targeted section read instead.

Fail-open: any error (missing file, bad JSON, unreadable path) allows the read.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    config_int,
    emit_gate_result,
    gate_allow,
    gate_deny,
    load_json_state,
)
from _model_utils import is_claude_payload, tool_input_of  # noqa: E402
from graph_store import resolved_hash  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FILE_GRAPH_LINE_THRESHOLD: int = config_int(
    "file_graph_gate.line_threshold", default=300
)

GRAPHS_DIR = DATA_DIR / "file-graphs"

IMAGE_PDF_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf",
    ".svg", ".bmp", ".ico", ".tiff",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_subagent_context(payload: dict[str, Any]) -> bool:
    """True when the read originates from a dispatched Task worker."""
    for key in (
        "agent_id", "agent_type", "subagent_id",
        "subagent_type", "parent_conversation_id",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return True
    return payload.get("is_parallel_worker") is True


def _file_path_of(payload: dict[str, Any]) -> str | None:
    """Extract the file path being read from the hook payload."""
    tool_input = tool_input_of(payload)
    for key in ("path", "file_path", "target_file"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_image_or_pdf(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in IMAGE_PDF_EXTS


def _is_small_file(file_path: str) -> bool:
    """True if the file has <= FILE_GRAPH_LINE_THRESHOLD lines. Fail-open."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for i, _ in enumerate(f, 1):
                if i > FILE_GRAPH_LINE_THRESHOLD:
                    return False
        return True
    except OSError as exc:
        from graph_store import record_graph_error
        record_graph_error("file_graph_gate._is_small_file", exc, file_path)
        return True  # fail-open: can't read -> don't block


def _is_full_read(tool_input: dict[str, Any]) -> bool:
    """True if neither 'offset' nor 'limit' is present and non-None."""
    offset = tool_input.get("offset")
    limit = tool_input.get("limit")
    return offset is None and limit is None


def _has_node_map(file_path: str) -> bool:
    """True if DATA_DIR/file-graphs/<hash>.json exists for this path."""
    try:
        target_hash = resolved_hash(file_path)
        if not target_hash:
            return False
        graph_file = GRAPHS_DIR / f"{target_hash}.json"
        return graph_file.is_file()
    except Exception as exc:  # noqa: BLE001
        from graph_store import record_graph_error
        record_graph_error("file_graph_gate._has_node_map", exc, file_path)
        return False  # fail-open


def _top_symbols(graph_json: dict[str, Any], n: int = 5) -> list[str]:
    """Extract top N symbol names from the graph JSON."""
    try:
        symbols = graph_json.get("symbols", [])
        if not isinstance(symbols, list):
            return []
        names: list[str] = []
        for sym in symbols:
            if isinstance(sym, dict):
                name = sym.get("name")
                if isinstance(name, str) and name:
                    names.append(name)
        return names[:n]
    except Exception as exc:  # noqa: BLE001
        from graph_store import record_graph_error
        record_graph_error("file_graph_gate._top_symbols", exc)
        return []


def _symbol_line_ranges(graph_json: dict[str, Any], n: int = 5) -> list[str]:
    """Extract top N symbols with their line ranges for targeted reads."""
    try:
        symbols = graph_json.get("symbols", [])
        if not isinstance(symbols, list):
            return []
        entries: list[str] = []
        for sym in symbols:
            if not isinstance(sym, dict):
                continue
            name = sym.get("name")
            start = sym.get("line_start")
            end = sym.get("line_end")
            if isinstance(name, str) and name and isinstance(start, int) and isinstance(end, int):
                span = end - start + 1
                entries.append(f"{name} (offset={start}, limit={span})")
        return entries[:n]
    except Exception as exc:  # noqa: BLE001
        from graph_store import record_graph_error
        record_graph_error("file_graph_gate._symbol_line_ranges", exc)
        return []


def _load_graph(file_path: str) -> dict[str, Any] | None:
    """Load the file-graph JSON for the given file path. Returns None on error."""
    try:
        target_hash = resolved_hash(file_path)
        if not target_hash:
            return None
        graph_file = GRAPHS_DIR / f"{target_hash}.json"
        if not graph_file.is_file():
            return None
        return load_json_state(graph_file)
    except Exception as exc:  # noqa: BLE001
        from graph_store import record_graph_error
        record_graph_error("file_graph_gate._load_graph", exc, file_path)
        return None


# ---------------------------------------------------------------------------
# Gate entry point
# ---------------------------------------------------------------------------


def gate_fn(payload: dict[str, Any]) -> dict[str, Any]:
    """Gate function for GATE_REGISTRY pre_read chain.

    Returns the JSON dict to emit (allow or deny).
    """
    is_claude = is_claude_payload(payload)

    # 1. Subagent context -> always allow
    if _is_subagent_context(payload):
        return emit_gate_result(gate_allow(), is_claude)

    # 2. No file path -> allow
    file_path = _file_path_of(payload)
    if not file_path:
        return emit_gate_result(gate_allow(), is_claude)

    # 3. Image/PDF -> allow
    if _is_image_or_pdf(file_path):
        return emit_gate_result(gate_allow(), is_claude)

    # 4. Partial read (offset or limit present) -> allow
    tool_input = tool_input_of(payload)
    if not _is_full_read(tool_input):
        return emit_gate_result(gate_allow(), is_claude)

    # 5. File <= FILE_GRAPH_LINE_THRESHOLD lines -> allow
    if _is_small_file(file_path):
        return emit_gate_result(gate_allow(), is_claude)

    # 6. File is inside GRAPHS_DIR itself -> allow (prevent circular dependency)
    try:
        Path(file_path).resolve().relative_to(GRAPHS_DIR.resolve())
        return emit_gate_result(gate_allow(), is_claude)
    except ValueError:
        pass
    except Exception as exc:  # noqa: BLE001
        from graph_store import record_graph_error
        record_graph_error("file_graph_gate.gate_fn.relative_to", exc, file_path)
        pass  # fail-open

    # 7. No node map -> allow (fail-open)
    if not _has_node_map(file_path):
        return emit_gate_result(gate_allow(), is_claude)

    # 7. Full read of large file WITH node map -> deny
    graph = _load_graph(file_path)
    if graph is None:
        # Can't load graph details — fail-open
        return emit_gate_result(gate_allow(), is_claude)

    all_symbols = _top_symbols(graph, n=len(graph.get("symbols", [])) or 9999)
    n_total = len(all_symbols)
    ranges = _symbol_line_ranges(graph, n=5)

    short_path = Path(file_path).name

    if ranges:
        symbols_str = "; ".join(ranges)
    else:
        symbols_str = ", ".join(_top_symbols(graph, n=5)) if all_symbols else "(none)"

    msg = (
        f"[file-graph-gate] {short_path} has a node map ({n_total} symbols). "
        f"Use targeted read: {symbols_str}"
    )
    return emit_gate_result(gate_deny(msg), is_claude)
