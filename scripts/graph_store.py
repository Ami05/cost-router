#!/usr/bin/env python3
"""Graph serialization and index management."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from _config import DATA_DIR, config_int
from _gate import emit_gate_result, gate_allow
from _fileio import load_json_state, save_json_state

GRAPHS_DIR = DATA_DIR / "file-graphs"
INDEX_PATH = GRAPHS_DIR / "index.json"
STALE_REFRESH_FILE = DATA_DIR / "stale_refresh.json"
ERRORS_LOG = DATA_DIR / "graph_errors.jsonl"
READ_EVENTS_LOG = DATA_DIR / "graph_read_events.jsonl"

_NON_GRAPH_FILES = {"index.json", "stale_refresh.json"}
_DEFAULT_MAX_FILES = 5000
_DEFAULT_MAX_TOTAL_MB = 200
_DEFAULT_TOUCH_INTERVAL_S = 300
_LOCK_WAIT_S = 2.0


def record_graph_error(component: str, exc: Exception, path: str | Path | None = None) -> None:
    """Record an exception from a file-graph operation to graph_errors.jsonl.

    Appends one JSON line with {ts, component, exc_type, msg, path} and keeps
    only the last ~1000 lines by truncating. Never raises.

    Args:
        component: Function or operation name (e.g. 'graph_store._cache_usage')
        exc: The exception that occurred
        path: Optional file path related to the error
    """
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "component": component,
            "exc_type": type(exc).__name__,
            "msg": str(exc)[:200],  # truncate long messages
        }
        if path is not None:
            entry["path"] = str(path)

        ERRORS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERRORS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

        # Truncate to keep only ~1000 lines
        try:
            with ERRORS_LOG.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > 1000:
                with ERRORS_LOG.open("w", encoding="utf-8") as f:
                    f.writelines(lines[-1000:])
        except OSError:
            pass  # Don't fail if truncation fails
    except Exception:
        pass  # Never raise from error recording


def record_graph_read(
    session_id: str | None,
    path_hash: str,
    resolved: Path,
    tool_input: dict | None,
    graph: dict | None,
    state: str,
    hint_shown: bool,
) -> None:
    """Record a file-graph read event to graph_read_events.jsonl for hint adoption measurement.

    Appends one JSON line with {ts, session_id, path_hash, path, file_lines, symbol_count,
    offset, limit, state, hint_shown} and keeps only the last ~5000 lines by truncating.
    Never raises.

    Args:
        session_id: Session ID from payload, or None
        path_hash: Hash of the resolved path
        resolved: Resolved Path object
        tool_input: Tool input dict from payload (e.g. containing offset/limit)
        graph: The loaded/built graph dict, or None
        state: One of: hit, built, stale_rebuilt, cache_full, build_failed
        hint_shown: True if hint was displayed (hit/built/stale_rebuilt), False otherwise
    """
    try:
        # Determine file_lines: try to get from resolved path
        try:
            with resolved.open("r", encoding="utf-8") as f:
                file_lines = sum(1 for _ in f)
        except OSError as exc:
            record_graph_error("graph_store.record_graph_read.file_lines", exc, resolved)
            file_lines = 0

        # Determine symbol_count from graph
        symbol_count = len(graph.get("symbols", [])) if graph else 0

        # Extract offset/limit from tool_input
        tool_input = tool_input or {}
        offset = tool_input.get("offset")
        limit = tool_input.get("limit")

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "path_hash": path_hash,
            "path": str(resolved),
            "file_lines": file_lines,
            "symbol_count": symbol_count,
            "offset": offset,
            "limit": limit,
            "state": state,
            "hint_shown": hint_shown,
        }

        READ_EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with READ_EVENTS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

        # Truncate to keep only ~5000 lines
        try:
            with READ_EVENTS_LOG.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > 5000:
                with READ_EVENTS_LOG.open("w", encoding="utf-8") as f:
                    f.writelines(lines[-5000:])
        except OSError as exc:
            record_graph_error("graph_store.record_graph_read.truncate", exc, READ_EVENTS_LOG)
    except Exception as exc:
        record_graph_error("graph_store.record_graph_read", exc, resolved)


def resolved_hash(file_path: str | Path) -> str | None:
    """Hash of the resolved path — the one key every graph writer and reader must share."""
    from _common import hash_target

    try:
        return hash_target(str(Path(file_path).resolve()))
    except (OSError, RuntimeError, ValueError) as exc:
        record_graph_error("graph_store.resolved_hash.resolve", exc, str(file_path))
        return hash_target(str(file_path))


def _graph_caps() -> tuple[int, int]:
    """(max_files, max_total_bytes) from config graph.*; <=0 falls back to defaults."""
    max_files = config_int("graph.max_files", _DEFAULT_MAX_FILES)
    max_mb = config_int("graph.max_total_mb", _DEFAULT_MAX_TOTAL_MB)
    return (
        max_files if max_files > 0 else _DEFAULT_MAX_FILES,
        (max_mb if max_mb > 0 else _DEFAULT_MAX_TOTAL_MB) * 1024 * 1024,
    )


def _cache_usage(graphs_dir: Path) -> tuple[int, int]:
    """(file_count, total_bytes) of graph cache, via scandir (no recursion)."""
    count = size = 0
    try:
        with os.scandir(graphs_dir) as it:
            for entry in it:
                if entry.name.endswith(".json") and entry.name not in _NON_GRAPH_FILES:
                    count += 1
                    size += entry.stat().st_size
    except OSError as exc:
        record_graph_error("graph_store._cache_usage", exc, str(graphs_dir))
    return count, size


def cache_is_full(graphs_dir: Path, max_files: int) -> bool:
    """Cheap count-only cap check (no stat, early exit) for the per-Read hot path."""
    count = 0
    try:
        with os.scandir(graphs_dir) as it:
            for entry in it:
                if entry.name.endswith(".json") and entry.name not in _NON_GRAPH_FILES:
                    count += 1
                    if count >= max_files:
                        return True
    except OSError as exc:
        record_graph_error("graph_store.cache_is_full", exc, str(graphs_dir))
    return False


def touch_due(graph: dict, now: datetime | None = None) -> bool:
    """True when last_read_at is older than graph.touch_interval_s (throttles cache-hit rewrites)."""
    interval = config_int("graph.touch_interval_s", _DEFAULT_TOUCH_INTERVAL_S)
    if interval <= 0:
        return True
    raw = graph.get("last_read_at")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - last).total_seconds() >= interval


@contextlib.contextmanager
def _index_lock() -> Iterator[None]:
    """Advisory flock around index read-modify-write. Fail-open after a short wait."""
    fd = None
    try:
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(INDEX_PATH.with_name("index.lock")), os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    record_graph_error("graph_store._index_lock.timeout", TimeoutError(f"flock not acquired in {_LOCK_WAIT_S}s"), INDEX_PATH)
                    break
                time.sleep(0.01)
    except OSError as exc:
        record_graph_error("graph_store._index_lock", exc)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)  # closing releases the flock
            except OSError as exc:
                record_graph_error("graph_store._index_lock.close", exc)


def rebuild_index() -> dict:
    """Reconstruct index entries from graph files. Source path is unknown, so left empty."""
    index: dict[str, dict] = {}
    max_files, _ = _graph_caps()
    try:
        with os.scandir(INDEX_PATH.parent) as it:
            for entry in it:
                if not entry.name.endswith(".json") or entry.name in _NON_GRAPH_FILES:
                    continue
                if len(index) >= max_files:
                    break
                graph = load_json_state(Path(entry.path))
                if not isinstance(graph, dict) or not graph.get("last_read_at"):
                    continue
                key = entry.name[: -len(".json")]
                index[key] = {
                    "last_read_at": graph["last_read_at"],
                    "graph_file": entry.name,
                    "language": graph.get("language", ""),
                    "path": "",
                }
    except OSError as exc:
        record_graph_error("graph_store.rebuild_index", exc, str(INDEX_PATH.parent))
    return index


def _load_index() -> dict:
    """Load index; a present-but-unparseable index is rebuilt rather than treated as empty."""
    index = load_json_state(INDEX_PATH)
    if isinstance(index, dict) and index:
        return index
    try:
        corrupt = INDEX_PATH.stat().st_size > 2
    except OSError:
        corrupt = False
    return rebuild_index() if corrupt else {}


def _update_index(path_hash: str, graph: dict, resolved: Path) -> None:
    try:
        with _index_lock():
            index = _load_index()
            index[path_hash] = {
                "last_read_at": graph["last_read_at"],
                "graph_file": f"{path_hash}.json",
                "language": graph["language"],
                "path": str(resolved),
            }
            save_json_state(INDEX_PATH, index)
    except Exception as exc:  # noqa: BLE001 — hooks stay fail-open
        record_graph_error("graph_store._update_index", exc, str(resolved))


def _format_output(path: Path, graph: dict, is_claude: bool) -> str:
    symbols = graph.get("symbols") or []
    if not symbols:
        return json.dumps(emit_gate_result(gate_allow(), is_claude))

    basename = path.name
    parts: list[str] = []
    for symbol in symbols[:8]:
        line_start = symbol.get("line_start", "?")
        line_end = symbol.get("line_end", line_start)
        signature = symbol.get("signature")
        if signature:
            part = f"{signature} [L{line_start}-{line_end}"
            calls_out = symbol.get("calls_out")
            if calls_out and symbol.get("kind") in ("function", "method"):
                part += f", calls: {', '.join(calls_out)}"
            part += "]"
        else:
            name = symbol.get("name", "?")
            part = f"{name} (L{line_start}-{line_end})"
        parts.append(part)

    summary = (
        f"[cost-router] local graph for {basename}: {len(symbols)} symbols — "
        f"{', '.join(parts)}. Use targeted section read (offset/limit) instead of full file."
    )
    if is_claude:
        return json.dumps(emit_gate_result(gate_allow(summary), is_claude), separators=(",", ":"))
    return json.dumps({"additional_context": summary}, separators=(",", ":"))
