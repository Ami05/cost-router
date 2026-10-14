#!/usr/bin/env python3
"""Hint-adoption report from graph_read_events.jsonl (one line per Read hook).

The hint rides on the Read it is attached to, so it can only change *later* reads. Per session, the
first large-file read is the control; later large-file reads are post-exposure. Savings are attributed
only to the adoption lift (later targeted rate minus first targeted rate); the upper bound assumes every
targeted large read would otherwise have been a full read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR  # noqa: E402

LARGE_FILE_LINES = 300  # mirrors file_graph_gate.line_threshold
TOKENS_PER_LINE = 10  # rough constant
HINT_OVERHEAD_TOKENS = 120  # ~470 chars measured in prod


def _lines_requested(event: dict[str, Any]) -> int:
    file_lines = event.get("file_lines") or 0
    limit, offset = event.get("limit"), event.get("offset") or 0
    return limit if limit else max(file_lines - offset, 0)


def _rate(n: int, d: int) -> float:
    return round(n / d, 3) if d else 0.0


def compute(events: list[dict[str, Any]]) -> dict[str, Any]:
    large_by_session: dict[str, list[dict[str, Any]]] = {}
    hinted = 0
    missing_session = 0
    for e in events:
        if e.get("hint_shown"):
            hinted += 1
        sid = e.get("session_id")
        if not sid:
            missing_session += 1
            sid = "unknown"
        if isinstance(e.get("file_lines"), int) and e["file_lines"] > LARGE_FILE_LINES:
            large_by_session.setdefault(sid, []).append(e)

    def targeted(e: dict[str, Any]) -> bool:
        return e.get("offset") is not None or e.get("limit") is not None

    first = [s[0] for s in large_by_session.values()]
    later = [e for s in large_by_session.values() for e in s[1:]]
    first_t = sum(targeted(e) for e in first)
    later_t = sum(targeted(e) for e in later)
    rate_first, rate_later = _rate(first_t, len(first)), _rate(later_t, len(later))

    avoided_upper = sum(
        e["file_lines"] - _lines_requested(e) for e in first + later if targeted(e) and _lines_requested(e) < e["file_lines"]
    )
    avoided_later = sum(
        e["file_lines"] - _lines_requested(e) for e in later if targeted(e) and _lines_requested(e) < e["file_lines"]
    )
    lift = max(rate_later - rate_first, 0.0)
    attributable = round(avoided_later * (lift / rate_later), 1) if rate_later else 0.0
    overhead = hinted * HINT_OVERHEAD_TOKENS
    return {
        "total_reads": len(events),
        "hinted_reads": hinted,
        "large_sessions": len(large_by_session),
        "targeted_rate_first_large_read": rate_first,
        "targeted_rate_later_large_reads": rate_later,
        "adoption_lift": round(lift, 3),
        "est_lines_avoided_upper": avoided_upper,
        "est_lines_avoided_attributable": attributable,
        "net_tokens_upper": avoided_upper * TOKENS_PER_LINE - overhead,
        "net_tokens_attributable": round(attributable * TOKENS_PER_LINE - overhead, 1),
        "missing_session_id": missing_session,
    }


def load_events(path: Path) -> tuple[list[dict[str, Any]], int, str | None]:
    """(events, parse_errors, io_error). Bad lines are counted, never skipped silently."""
    events: list[dict[str, Any]] = []
    parse_errors = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    parse_errors += 1
                    continue
                if isinstance(event, dict):
                    events.append(event)
                else:
                    parse_errors += 1
    except FileNotFoundError:
        return [], 0, "events file not found"
    except OSError as exc:
        return events, parse_errors, f"{type(exc).__name__}: {exc}"
    return events, parse_errors, None


def main(events_path: str | None = None) -> int:
    path = Path(events_path) if events_path else DATA_DIR / "graph_read_events.jsonl"
    events, parse_errors, io_error = load_events(path)
    result = compute(events)
    result["parse_errors"] = parse_errors
    result["io_error"] = io_error
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    raise SystemExit(main(argv[1] if len(argv) > 1 and argv[0] == "--path" else None))
