#!/usr/bin/env python3
"""cost-router hook: incremental file-graph update on session stop.

Walks all project roots from project_registry.json, checks each source file
for staleness, and rebuilds its graph entry if needed. Uses our own
build_file_graph._build_graph and file_graph_refresh._is_stale — no external
graphify library required.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    DATA_DIR,
    PLUGIN_ROOT,
    hash_target,
    load_json_state,
    now_iso,
    read_stdin_json,
    save_json_state,
    session_id_of,
    _warn,
)
from _config import config_int, config_get
from graph_store import _graph_caps, _cache_usage

SESSION_LIMIT_GRAPH_UPDATE = 100

# Source extensions to walk
_SOURCE_EXTS = {
    ".py", ".ts", ".js", ".java", ".md",
    ".yaml", ".yml", ".json", ".sh",
}

# Directories to skip during walk — union with config graph.extra_skip_dirs
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "dist", "build",
    # Never index our own graph cache: the registry root is the plugin dir,
    # so walking data/file-graphs/*.json would create graphs of graphs forever.
    "file-graphs",
    ".tox", ".nox", "venv", "env", "site-packages", "target", "vendor",
    ".next", ".cache", ".idea", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".gradle", "coverage", "out",
}
_SKIP_DIRS.update(config_get("graph.extra_skip_dirs", []))


# ---------------------------------------------------------------------------
# Debug helper
# ---------------------------------------------------------------------------

def _debug_log(msg: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "debug.log", "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {msg}\n")
    except Exception as exc:
        from graph_store import record_graph_error
        record_graph_error("graph_update._debug_log", exc)


# ---------------------------------------------------------------------------
# Step 1: Find project roots
# ---------------------------------------------------------------------------

def _find_project_roots() -> list[Path]:
    """Read project roots from registry. Fall back to env var + cwd walk if registry empty."""
    registry_path = DATA_DIR / "project_registry.json"
    registry = load_json_state(registry_path)
    projects = registry.get("projects", {})

    if projects:
        roots = []
        for abs_str in projects.keys():
            root = Path(abs_str)
            if root.is_dir() and str(root).startswith(str(PLUGIN_ROOT)):
                roots.append(root)
        if roots:
            return roots

    # Fallback: cwd + env vars (for first session before registry exists)
    candidates = set()
    candidates.add(Path.cwd().resolve())
    for env_key in ("CURSOR_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        val = os.environ.get(env_key)
        if val:
            candidates.add(Path(val).resolve())

    roots = []
    for c in candidates:
        if c.is_dir() and str(c).startswith(str(PLUGIN_ROOT)):
            roots.append(c)
    return roots


# ---------------------------------------------------------------------------
# Step 2: Incremental update for one project root
# ---------------------------------------------------------------------------

def _update_project(root: Path, session_id: str) -> dict:
    """Walk source files under root and rebuild stale graph entries."""
    # Lazy imports to avoid circular-import issues at module load time
    from file_graph_refresh import _is_stale, GRAPHS_DIR  # noqa: E402
    from build_file_graph import _build_graph  # noqa: E402
    from graph_store import _update_index  # noqa: E402

    try:
        capped = False
        max_files, max_bytes = _graph_caps()
        max_depth = config_int("graph.max_walk_depth", 12)
        files_changed = 0
        files_skipped = 0
        errors = 0

        GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
        cache_files, cache_bytes = _cache_usage(GRAPHS_DIR)
        graphs_resolved = GRAPHS_DIR.resolve()

        for dirpath, dirnames, filenames in os.walk(root):
            if capped:
                break
            # Prune by depth: count path separators relative to root
            rel_depth = len(Path(dirpath).relative_to(root).parts)
            if rel_depth >= max_depth:
                dirnames[:] = []
                continue
            # Prune skip dirs in-place so os.walk won't descend into them
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS
                and (Path(dirpath) / d).resolve() != graphs_resolved
            ]

            for filename in filenames:
                file_path = Path(dirpath) / filename
                if file_path.suffix.lower() not in _SOURCE_EXTS:
                    continue

                try:
                    path_hash = hash_target(str(file_path.resolve()))
                    if not path_hash:
                        files_skipped += 1
                        continue

                    if not _is_stale(file_path, path_hash):
                        files_skipped += 1
                        continue

                    graph = _build_graph(file_path.resolve(), path_hash)
                    if graph is None:
                        errors += 1
                        continue

                    graph_file = GRAPHS_DIR / f"{path_hash}.json"
                    existed = graph_file.exists()
                    if not existed and (
                        cache_files >= max_files or cache_bytes >= max_bytes
                    ):
                        capped = True
                        _debug_log(
                            f"[graph-update] cache cap reached "
                            f"({cache_files} files, {cache_bytes} bytes)"
                        )
                        break
                    save_json_state(graph_file, graph)
                    _update_index(path_hash, graph, file_path.resolve())
                    if not existed:
                        cache_files += 1
                        try:
                            cache_bytes += graph_file.stat().st_size
                        except OSError as exc:
                            from graph_store import record_graph_error
                            record_graph_error("graph_update._update_project.stat", exc, str(graph_file))
                    files_changed += 1

                except Exception as e:
                    from graph_store import record_graph_error
                    record_graph_error("graph_update._update_project.file_process", e, str(file_path))
                    errors += 1
                    _debug_log(f"[graph-update] error on {file_path}: {e}")

        return {
            "files_changed": files_changed,
            "files_skipped": files_skipped,
            "errors": errors,
            "capped": capped,
            "root": str(root),
        }

    except Exception as e:
        from graph_store import record_graph_error
        record_graph_error("graph_update._update_project", e, str(root))
        return {
            "files_changed": 0,
            "files_skipped": 0,
            "errors": 1,
            "root": str(root),
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Step 3: Write per-session stats
# ---------------------------------------------------------------------------

def _write_stats(session_id: str, stats_list: list[dict]) -> None:
    """Write per-session stats to DATA_DIR/graph_update_stats.json."""
    stats_file = DATA_DIR / "graph_update_stats.json"
    state = load_json_state(stats_file)
    sessions = state.get("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}

    total_files = sum(s.get("files_changed", 0) for s in stats_list)
    total_errors = sum(s.get("errors", 0) for s in stats_list)

    # Count errors by component
    errors_by_component: dict[str, int] = {}
    try:
        errors_log = DATA_DIR / "graph_errors.jsonl"
        if errors_log.is_file():
            try:
                with errors_log.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            component = entry.get("component", "unknown")
                            errors_by_component[component] = errors_by_component.get(component, 0) + 1
                        except (json.JSONDecodeError, ValueError):
                            pass
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass

    sessions[session_id] = {
        "projects_scanned": len(stats_list),
        "files_changed": total_files,
        "errors": total_errors,
        "ts": now_iso(),
        "errors_by_component": errors_by_component if errors_by_component else None,
    }

    # Cap tracked sessions
    if len(sessions) > SESSION_LIMIT_GRAPH_UPDATE:
        keys = list(sessions.keys())
        for k in keys[:-SESSION_LIMIT_GRAPH_UPDATE]:
            del sessions[k]

    save_json_state(stats_file, {"sessions": sessions})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        payload = read_stdin_json()
        session_id = session_id_of(payload) if payload else "unknown"

        roots = _find_project_roots()
        stats_list: list[dict] = []

        for root in roots:
            stats = _update_project(root, session_id)
            stats_list.append(stats)
            if os.environ.get("COST_ROUTER_DEBUG"):
                _debug_log(f"[graph-update] {root.name}: {stats}")

        if stats_list:
            _write_stats(session_id, stats_list)

        if os.environ.get("COST_ROUTER_DEBUG"):
            _debug_log(
                f"[graph-update] done. {len(roots)} project(s) scanned."
            )

    except Exception as e:
        _warn("graph_update_failed", {"file": "graph_update.py", "error": str(e)})

    print(json.dumps({}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
