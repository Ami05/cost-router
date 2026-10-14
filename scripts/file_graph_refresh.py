#!/usr/bin/env python3
"""cost-router hook: refresh stale file-graph entries on session stop."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    PLUGIN_ROOT,
    hash_target,
    load_json_state,
    now_iso,
    read_stdin_json,
    save_json_state,
)

GRAPHS_DIR = DATA_DIR / "file-graphs"
INDEX_PATH = GRAPHS_DIR / "index.json"


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError as exc:
        from graph_store import record_graph_error
        record_graph_error("file_graph_refresh._parse_ts", exc)
        return None


def _is_stale(path: Path, path_hash: str) -> bool:
    """Return True if the cached graph for *path* is missing or older than the file."""
    graph_file = GRAPHS_DIR / f"{path_hash}.json"
    if not graph_file.exists():
        return True
    try:
        data = json.loads(graph_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        from graph_store import record_graph_error
        record_graph_error("file_graph_refresh._is_stale.read", exc, str(graph_file))
        return True
    if data.get("stale"):
        return True
    generated_at = _parse_ts(data.get("generated_at"))
    if generated_at is None:
        return True
    try:
        file_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError as exc:
        from graph_store import record_graph_error
        record_graph_error("file_graph_refresh._is_stale.stat", exc, str(path))
        return True
    if generated_at >= file_mtime:
        return False
    from graph_builder import file_content_sha256

    current = file_content_sha256(path)
    return not (current is not None and data.get("content_sha256") == current)


def _upsert_project_registry() -> None:
    """Update project_registry.json with a last_seen_at entry for the current project."""
    registry_path = DATA_DIR / "project_registry.json"
    registry = load_json_state(registry_path)
    if not isinstance(registry.get("projects"), dict):
        registry["projects"] = {}
    project_root = str(Path.cwd().resolve())
    if not project_root.startswith(str(PLUGIN_ROOT)):
        return
    registry["projects"][project_root] = {
        "last_seen_at": now_iso(),
        "path": project_root,
    }
    save_json_state(registry_path, registry)


def main() -> int:
    _payload = read_stdin_json()
    _upsert_project_registry()
    print(json.dumps({}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
