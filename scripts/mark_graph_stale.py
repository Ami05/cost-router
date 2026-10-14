#!/usr/bin/env python3
"""cost-router hook: mark a file's local symbol graph as stale after edits.

PostToolUse (matcher: Write|Edit|MultiEdit) — sets stale:true on the cached
graph so the next PreToolUse:Read triggers a forced re-parse instead of
serving outdated symbols.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    _warn,
    hash_target,
    load_json_state,
    now_iso,
    read_stdin_json,
    save_json_state,
)

GRAPHS_DIR = DATA_DIR / "file-graphs"


def _run(payload: dict) -> None:
    ti = payload.get("tool_input") or {}
    file_path = ti.get("file_path") or ti.get("path") or payload.get("file_path")
    if not file_path:
        return

    try:
        resolved = Path(file_path).resolve()
    except (OSError, ValueError) as exc:
        from graph_store import record_graph_error
        record_graph_error("mark_graph_stale._run.resolve", exc, str(file_path))
        return

    path_hash = hash_target(str(resolved))
    if not path_hash:
        return

    graph_file = GRAPHS_DIR / f"{path_hash}.json"
    if not graph_file.is_file():
        return  # file was never read this session — no-op

    graph = load_json_state(graph_file)
    if not graph:
        return

    graph["stale"] = True
    graph["stale_reason"] = f"file edited at {now_iso()}"
    save_json_state(graph_file, graph)


def main() -> int:
    try:
        payload = read_stdin_json()
        _run(payload)
    except Exception as e:  # noqa: BLE001
        _warn("mark_graph_stale_failed", {"file": "mark_graph_stale.py", "error": str(e)})
    print(json.dumps({}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
