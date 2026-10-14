#!/usr/bin/env python3
"""cost-router: offline derived-outcome analyzer (DGC-004 S2).

Raw ``outcome`` / ``result_status`` on subagent_stop_telemetry rows are almost
always UNKNOWN because they depend on a continuation-manifest block in the
subagent's final report. This module derives a proxy outcome per dispatch from
existing routing events instead. Read-only: never writes to the events file,
stdlib only, no network, no API key.

``success_proxy`` means "completed cleanly" (a stop was seen, no stall, no
retry). It is NOT verified correctness.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DEFAULT_RETRY_WINDOW_SEC = 900
MIN_PER_TYPE = 100
MIN_TOTAL = 300
OUTCOMES = ("no_stop_event", "partial", "retried", "success_proxy")
_WANTED = frozenset({"subagent_dispatch", "subagent_stop_telemetry", "dispatch_worker_declared"})


def default_archive_paths(events_path: str | Path) -> list[Path]:
    """Sibling archive files written by log_compactor (live file is trimmed, evicted rows land here).

    log_compactor keeps up to 3 numbered generations (usage_archive.{1,2,3}.jsonl)
    on top of the current usage_archive.jsonl, so all of them must be included
    or derived outcome counts undercount after a second or third rotation.
    """
    base = Path(events_path).parent
    return [
        base / "usage_archive.1.jsonl",
        base / "usage_archive.2.jsonl",
        base / "usage_archive.3.jsonl",
        base / "usage_archive.jsonl",
    ]


def load_events(
    path: str | Path, archive_paths: Iterable[str | Path] = ()
) -> list[dict[str, Any]]:
    """Load relevant event rows from the live file plus archives.

    Skips blank and malformed lines. Archive files that do not exist are ignored; the
    live file must exist. Identical raw lines are deduplicated across all files, since
    the archive can hold repeated rows.
    """
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    sources = [Path(p) for p in archive_paths] + [Path(path)]
    for src in sources:
        if src != Path(path) and not src.exists():
            continue
        with open(src, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line in seen:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("type") in _WANTED:
                    seen.add(line)
                    events.append(row)
    return events


def _ts(row: dict[str, Any]) -> datetime:
    try:
        return datetime.fromisoformat(str(row.get("ts", "")))
    except ValueError:
        return datetime.min.replace(tzinfo=None)


def _sort_key(row: dict[str, Any]) -> tuple[str, str]:
    # ISO 8601 with a fixed offset sorts lexicographically; fall back to "".
    return (str(row.get("ts", "")), str(row.get("type", "")))


def stop_epoch(events: list[dict[str, Any]]) -> str | None:
    """ts of the earliest subagent_stop_telemetry row, or None if there are none.

    Before this point stops were not logged, so a dispatch with no stop row reflects
    missing telemetry, not a failed run.
    """
    stamps = [str(e.get("ts", "")) for e in events if e.get("type") == "subagent_stop_telemetry" and e.get("ts")]
    return min(stamps) if stamps else None


def derive(
    events: list[dict[str, Any]],
    retry_window_sec: int = DEFAULT_RETRY_WINDOW_SEC,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Return one derived row per declared subagent_dispatch event.

    ``since``: drop dispatches whose ts sorts before it (pass stop_epoch(events) to
    exclude the era before stop telemetry existed).
    """
    dispatches = sorted((e for e in events if e.get("type") == "subagent_dispatch"), key=_sort_key)
    stops = sorted((e for e in events if e.get("type") == "subagent_stop_telemetry"), key=_sort_key)

    worker_by_key: dict[tuple[str, str], str] = {}
    for e in events:
        if e.get("type") == "dispatch_worker_declared":
            w = str(e.get("worker") or "untyped")
            w = "untyped" if w == "not_json_schema" else w
            worker_by_key[(str(e.get("session_id")), str(e.get("subagent_id")))] = w

    # Only dispatches with a dispatch_worker_declared event are real Agent-tool
    # dispatches; the rest are harness-internal subagent stops with no start.
    dispatches = [d for d in dispatches if (str(d.get("session_id")), str(d.get("subagent_id"))) in worker_by_key]
    if since is not None:
        dispatches = [d for d in dispatches if str(d.get("ts", "")) >= since]

    stops_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in stops:
        stops_by_id[str(s.get("agent_id"))].append(s)

    # Chronological pairing: n-th dispatch of an id takes n-th stop of that id.
    seen: dict[str, int] = defaultdict(int)
    paired: list[dict[str, Any] | None] = []
    for d in dispatches:
        sid = str(d.get("subagent_id"))
        idx = seen[sid]
        seen[sid] += 1
        candidates = stops_by_id.get(sid, [])
        paired.append(candidates[idx] if idx < len(candidates) else None)

    rows: list[dict[str, Any]] = []
    for i, d in enumerate(dispatches):
        stop = paired[i]
        retried = _has_later_retry(dispatches, i, retry_window_sec)
        if stop is None:
            derived = "no_stop_event"
        elif int(stop.get("stall_events") or 0) > 0:
            derived = "partial"
        elif retried:
            derived = "retried"
        else:
            derived = "success_proxy"
        sid = str(d.get("subagent_id"))
        rows.append({
            "ts": d.get("ts"),
            "session_id": d.get("session_id"),
            "subagent_id": d.get("subagent_id"),
            "worker": worker_by_key.get((str(d.get("session_id")), sid), "untyped"),
            "raw_outcome": stop.get("outcome") if stop else None,
            "raw_result_status": stop.get("result_status") if stop else None,
            "derived_outcome": derived,
        })
    return rows


def count_undeclared(events: list[dict[str, Any]]) -> int:
    """Count subagent_dispatch events that derive() excludes (no dispatch_worker_declared)."""
    declared = {
        (str(e.get("session_id")), str(e.get("subagent_id")))
        for e in events if e.get("type") == "dispatch_worker_declared"
    }
    return sum(
        1 for e in events
        if e.get("type") == "subagent_dispatch"
        and (str(e.get("session_id")), str(e.get("subagent_id"))) not in declared
    )


def _has_later_retry(dispatches: list[dict[str, Any]], i: int, window_sec: int) -> bool:
    d = dispatches[i]
    start = _ts(d)
    for later in dispatches[i + 1:]:
        if later.get("session_id") != d.get("session_id") or later.get("target_hash") != d.get("target_hash"):
            continue
        if later.get("subagent_id") == d.get("subagent_id"):
            continue
        delta = (_ts(later) - start).total_seconds()
        if 0 <= delta <= window_sec:
            return True
    return False


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion k/n; (0, 1) when n == 0."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_worker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_worker[r["worker"]].append(r)

    per_type: dict[str, Any] = {}
    for worker, items in sorted(by_worker.items()):
        n = len(items)
        counts = {o: sum(1 for r in items if r["derived_outcome"] == o) for o in OUTCOMES}
        lo, hi = wilson_interval(counts["no_stop_event"], n)
        per_type[worker] = {
            "n": n,
            "counts": counts,
            "no_stop_rate_wilson95": [round(lo, 4), round(hi, 4)],
            "meets_min_per_type": n >= MIN_PER_TYPE,
        }

    stamps = sorted(str(r["ts"]) for r in rows if r.get("ts"))
    total = len(rows)
    typed = [w for w in per_type if w != "untyped"]
    sufficient = total >= MIN_TOTAL and bool(typed) and all(per_type[w]["meets_min_per_type"] for w in typed)
    return {
        "date_range": [stamps[0], stamps[-1]] if stamps else None,
        "total_n": total,
        "per_worker_type": per_type,
        "derived_coverage": 1.0 if total else 0.0,
        "status": "ok" if sufficient else "insufficient_data",
        "thresholds": {"min_per_type": MIN_PER_TYPE, "min_total": MIN_TOTAL},
        "note": "success_proxy = completed cleanly (stop seen, no stall, no retry); not verified correct.",
        "retry_detection": "target_hash is a 12-hex hash of the first 500 chars of the prompt, so a retry with an edited prompt is not detected; retried may undercount.",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", required=True, help="path to routing_events.jsonl (read-only)")
    ap.add_argument(
        "--archive",
        action="append",
        help="archive jsonl to merge (repeatable); default: usage_archive[.1].jsonl next to --events",
    )
    ap.add_argument("--no-archive", action="store_true", help="read only the live events file")
    ap.add_argument("--include-pre-stop-logging", action="store_true", help="keep dispatches older than the first stop row")
    ap.add_argument("--out", help="optional path to write derived rows as jsonl")
    ap.add_argument("--retry-window-sec", type=int, default=DEFAULT_RETRY_WINDOW_SEC)
    args = ap.parse_args(argv)

    if args.no_archive:
        archives: list[str | Path] = []
    else:
        archives = list(args.archive) if args.archive else default_archive_paths(args.events)
    events = load_events(args.events, archives)
    since = None if args.include_pre_stop_logging else stop_epoch(events)
    all_rows = derive(events, args.retry_window_sec)
    rows = derive(events, args.retry_window_sec, since)
    if args.out:
        if Path(args.out).resolve() == Path(args.events).resolve():
            print("refusing to overwrite the events file", file=sys.stderr)
            return 2
        with open(args.out, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    summary = summarize(rows)
    summary["excluded_undeclared"] = count_undeclared(events)
    summary["stop_epoch"] = since
    summary["excluded_pre_stop_logging"] = len(all_rows) - len(rows)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
