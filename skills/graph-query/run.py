#!/usr/bin/env python3
"""Graph query skill: look up symbol or file in the indexed file graph.

Index structure (index.json):
  {path_hash: {graph_file, language, last_read_at}, ...}

Per-file graph structure (data/file-graphs/{path_hash}.json):
  {path_hash, language, generated_at, last_read_at, imports,
   symbols: [{name, qualname, line_start, line_end, kind, ...}]}

Usage:
  python3 skills/graph-query/run.py --symbol NAME
  python3 skills/graph-query/run.py --file PATH
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add scripts to path to import _common
SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from _common import DATA_DIR, hash_target, load_json_state, save_json_state


GRAPHS_DIR = DATA_DIR / "file-graphs"
INDEX_PATH = GRAPHS_DIR / "index.json"


def _ensure_graph_exists() -> None:
    """Check that the file graph exists; raise error if missing."""
    if not INDEX_PATH.exists():
        print(
            "Graph not built yet — trigger a file Read in Claude Code to build it, then retry.",
            file=sys.stderr,
        )
        sys.exit(1)


def _query_symbol(symbol: str) -> dict | None:
    """Look up a symbol by name or qualname across all indexed graph files.

    Returns {file, path_hash, line_start, line_end, name, qualname, kind} or None.
    """
    _ensure_graph_exists()

    if not INDEX_PATH.exists():
        print(f"Error: graph index not found at {INDEX_PATH}", file=sys.stderr)
        return None

    index = load_json_state(INDEX_PATH)
    if not index:
        print(f"Error: graph index is empty", file=sys.stderr)
        return None

    # Iterate over all path_hash -> {graph_file, language, last_read_at}
    for path_hash, entry in index.items():
        if not isinstance(entry, dict):
            continue

        graph_file_name = entry.get("graph_file")
        if not graph_file_name:
            continue

        graph_file_path = GRAPHS_DIR / graph_file_name

        try:
            graph = load_json_state(graph_file_path)
        except (OSError, json.JSONDecodeError):
            continue

        symbols = graph.get("symbols", [])
        if not isinstance(symbols, list):
            continue

        # Search by name or qualname
        for sym in symbols:
            if isinstance(sym, dict) and (
                sym.get("name") == symbol or sym.get("qualname") == symbol
            ):
                return {
                    "file": graph_file_path.name,  # Return graph filename or path_hash
                    "path_hash": path_hash,
                    "line_start": sym.get("line_start"),
                    "line_end": sym.get("line_end"),
                    "name": sym.get("name"),
                    "qualname": sym.get("qualname"),
                    "kind": sym.get("kind"),
                }

    return None


def _query_file(file_path: str) -> list[dict] | None:
    """List all symbols in a file by resolving path to hash.

    Returns [{name, qualname, kind, line_start, line_end}, ...] or None.
    """
    _ensure_graph_exists()

    if not INDEX_PATH.exists():
        print(f"Error: graph index not found at {INDEX_PATH}", file=sys.stderr)
        return None

    index = load_json_state(INDEX_PATH)
    if not index:
        print(f"Error: graph index is empty", file=sys.stderr)
        return None

    # Resolve the target path to absolute and hash it
    try:
        target_path = Path(file_path).resolve()
        path_hash = hash_target(str(target_path))
    except (OSError, ValueError):
        print(f"Error: cannot resolve path {file_path}", file=sys.stderr)
        return None

    if not path_hash:
        print(f"Error: cannot hash path {file_path}", file=sys.stderr)
        return None

    # Look up path_hash in index
    entry = index.get(path_hash)
    if not entry or not isinstance(entry, dict):
        print(f"Warning: file {file_path} not found in graph index", file=sys.stderr)
        return None

    graph_file_name = entry.get("graph_file")
    if not graph_file_name:
        print(f"Error: no graph file for {file_path}", file=sys.stderr)
        return None

    graph_file_path = GRAPHS_DIR / graph_file_name

    try:
        graph = load_json_state(graph_file_path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading graph file {graph_file_path}: {e}", file=sys.stderr)
        return None

    symbols = graph.get("symbols", [])
    if not isinstance(symbols, list):
        return []

    result = []
    for sym in symbols:
        if isinstance(sym, dict):
            result.append(
                {
                    "name": sym.get("name"),
                    "qualname": sym.get("qualname"),
                    "kind": sym.get("kind"),
                    "line_start": sym.get("line_start"),
                    "line_end": sym.get("line_end"),
                }
            )

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Query the file graph for symbol or file information"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--symbol", help="Look up a specific symbol")
    group.add_argument("--file", help="List all symbols in a file")

    args = parser.parse_args()

    if args.symbol:
        result = _query_symbol(args.symbol)
        if result is None:
            print(f"Symbol not found: {args.symbol}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0

    if args.file:
        result = _query_file(args.file)
        if result is None:
            return 1
        print(json.dumps(result, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
