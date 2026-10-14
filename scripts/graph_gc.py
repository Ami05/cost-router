#!/usr/bin/env python3
"""cost-router hook: TTL cleanup for local file-graph cache on session stop."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    GRAPH_TTL_DAYS,
    load_json_state,
    now_iso,
    read_stdin_json,
    save_json_state,
)
from _config import config_int  # noqa: E402

GRAPHS_DIR = DATA_DIR / "file-graphs"
INDEX_PATH = GRAPHS_DIR / "index.json"
DEBUG_LOG = DATA_DIR / "debug.log"


def _ttl_days() -> int:
    raw = os.environ.get("COST_ROUTER_GRAPH_TTL_DAYS")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError as exc:
            from graph_store import record_graph_error
            record_graph_error("graph_gc._ttl_days", exc)
    return GRAPH_TTL_DAYS



def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _debug_log(message: str) -> None:
    if not os.environ.get("COST_ROUTER_DEBUG"):
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with DEBUG_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


_NON_GRAPH_FILES = {"index.json", "stale_refresh.json"}
_DEFAULT_MAX_DELETES = 5000


def _unlink(path_hash: str) -> bool:
    try:
        (GRAPHS_DIR / f"{path_hash}.json").unlink(missing_ok=True)
        return True
    except OSError as exc:
        from graph_store import record_graph_error  # noqa: E402
        record_graph_error("graph_gc._unlink", exc, f"{path_hash}.json")
        return False


def _run_gc() -> int:
    """Delete expired/orphaned graphs. Bounded per run so a huge backlog drains over many runs."""
    import graph_store  # noqa: E402 — lazy: keeps module import cheap

    # Honor patched module attrs (tests) so graph_store and this module agree on the location.
    graph_store.GRAPHS_DIR, graph_store.INDEX_PATH = GRAPHS_DIR, INDEX_PATH

    max_deletes = config_int("graph.gc_max_deletes_per_run", _DEFAULT_MAX_DELETES)
    if max_deletes <= 0:
        max_deletes = _DEFAULT_MAX_DELETES
    max_files, max_bytes = graph_store._graph_caps()
    cutoff = datetime.now(timezone.utc) - timedelta(days=_ttl_days())
    deleted = 0

    with graph_store._index_lock():
        index = graph_store._load_index()
        changed = False

        # 1) Index pass: expired, malformed, or source file gone.
        for path_hash, entry in list(index.items()):
            if deleted >= max_deletes:
                break
            drop = not isinstance(entry, dict)
            if not drop:
                last_read = _parse_ts(entry.get("last_read_at"))
                src = entry.get("path")
                drop = (
                    last_read is None
                    or last_read < cutoff
                    or (bool(src) and not Path(src).exists())
                )
            if drop:
                _unlink(path_hash)
                index.pop(path_hash, None)
                changed = True
                deleted += 1

        # 2) Directory sweep: graph files with no index entry (orphans).
        orphans: list[tuple[float, str, int]] = []
        total_files = total_bytes = 0
        try:
            with os.scandir(GRAPHS_DIR) as it:
                for de in it:
                    if not de.name.endswith(".json") or de.name in _NON_GRAPH_FILES:
                        continue
                    try:
                        st = de.stat()
                    except OSError as exc:
                        from graph_store import record_graph_error  # noqa: E402
                        record_graph_error("graph_gc._run_gc.stat", exc, de.name)
                        continue
                    stem = de.name[: -len(".json")]
                    total_files += 1
                    total_bytes += st.st_size
                    if stem not in index:
                        orphans.append((st.st_mtime, stem, st.st_size))
        except OSError as exc:
            from graph_store import record_graph_error  # noqa: E402
            record_graph_error("graph_gc._run_gc.scandir", exc, str(GRAPHS_DIR))

        orphans.sort()  # oldest first
        cutoff_ts = cutoff.timestamp()
        over_cap = total_files > max_files or total_bytes > max_bytes
        for mtime, stem, size in orphans:
            if deleted >= max_deletes:
                break
            if mtime < cutoff_ts or over_cap:
                if _unlink(stem):
                    deleted += 1
                    total_files -= 1
                    total_bytes -= size
                    over_cap = total_files > max_files or total_bytes > max_bytes

        # 3) Still over caps: evict oldest indexed entries.
        if total_files > max_files or total_bytes > max_bytes:
            by_age = sorted(
                index.items(),
                key=lambda kv: _parse_ts(kv[1].get("last_read_at")) or cutoff,
            )
            for path_hash, _entry in by_age:
                if deleted >= max_deletes or (
                    total_files <= max_files and total_bytes <= max_bytes
                ):
                    break
                size = 0
                try:
                    size = (GRAPHS_DIR / f"{path_hash}.json").stat().st_size
                except OSError as exc:
                    from graph_store import record_graph_error
                    record_graph_error("graph_gc._run_gc.evict_stat", exc, f"{path_hash}.json")
                _unlink(path_hash)
                index.pop(path_hash, None)
                changed = True
                deleted += 1
                total_files -= 1
                total_bytes -= size

        if changed:
            save_json_state(INDEX_PATH, index)
    return deleted


STATS_PATH = DATA_DIR / "graph_gc_stats.json"


def _write_stats(deleted: int) -> None:
    """Record the last GC run so it is verifiable without COST_ROUTER_DEBUG."""
    import graph_store  # noqa: E402

    files, size = graph_store._cache_usage(GRAPHS_DIR)
    prev = load_json_state(STATS_PATH)
    runs = prev.get("runs", 0) if isinstance(prev, dict) else 0
    total = prev.get("total_deleted", 0) if isinstance(prev, dict) else 0

    # Count errors by component
    errors_by_component: dict[str, int] = {}
    errors_log = DATA_DIR / "graph_errors.jsonl"
    try:
        if errors_log.is_file():
            with errors_log.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        component = entry.get("component", "unknown")
                    except (ValueError, AttributeError):
                        component = "_unparseable_error_log_line"
                    errors_by_component[component] = errors_by_component.get(component, 0) + 1
    except OSError as exc:
        graph_store.record_graph_error("graph_gc._write_stats.read_errors_log", exc, errors_log)
        errors_by_component["_errors_log_unreadable"] = 1

    save_json_state(STATS_PATH, {
        "last_run_at": now_iso(),
        "deleted": deleted,
        "files_remaining": files,
        "bytes_remaining": size,
        "runs": runs + 1,
        "total_deleted": total + deleted,
        "errors_by_component": errors_by_component if errors_by_component else None,
    })


def main() -> int:
    try:
        read_stdin_json()
        deleted = _run_gc()
        _write_stats(deleted)
        _debug_log(f"graph_gc: deleted {deleted} expired graph(s)")
    except Exception as exc:  # noqa: BLE001 — Stop hook stays fail-open, but the failure is recorded
        from graph_store import record_graph_error  # noqa: E402
        record_graph_error("graph_gc.main", exc)

    print(json.dumps({}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
