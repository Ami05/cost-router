#!/usr/bin/env python3
"""cost-router: Policy GC — detect dead/duplicate Decision Table rows and enforce size cap.

Usage:
    python3 policy_gc.py --dry-run   # Print GC report, no changes
    python3 policy_gc.py --apply     # Remove dead rows from rules/model-router.md
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_DIR, PLUGIN_ROOT, POLICY_DEAD_ROW_WINDOW, POLICY_GC_NEAR_DUPLICATE_RATIO, POLICY_MAX_TABLE_ROWS  # noqa: E402

MAX_TABLE_ROWS = POLICY_MAX_TABLE_ROWS

MDC_FILE = PLUGIN_ROOT / "rules" / "model-router.md"
SCENARIO_LIB = DATA_DIR / "scenario_library.json"


def parse_decision_table(mdc_text: str) -> list[str]:
    """Return lines containing ' -> ' and starting with '|'."""
    return [
        line
        for line in mdc_text.splitlines()
        if line.startswith("|") and " -> " in line
    ]


AUTO_TAG_PREFIX = "<!-- cr-auto:"


def filter_tagged(rows: list[str]) -> list[str]:
    """Return only tuner-written rows (carrying the cr-auto marker).

    GC must never retire or evict human-written rows.
    """
    return [r for r in rows if AUTO_TAG_PREFIX in r]


def _extract_tool_name(row: str) -> str:
    """Extract the tool name (first word before ':') from a decision table row.

    For rows like '| Write(spec_complete) | -> FW |' or '| Read:repeat | -> same |',
    returns 'Write' or 'Read' respectively.
    """
    # Strip leading pipe and whitespace to get to the content
    stripped = row.lstrip("|").lstrip()
    # First word before ':', '(', or whitespace gives the tool name
    match = re.match(r"([A-Za-z_]\w*)", stripped)
    if match:
        return match.group(1)
    # Fallback: split on any non-word delimiter
    token = re.split(r"[:\s(]", stripped)[0]
    return token.strip()


def find_dead_rows(rows: list[str], scenario_library: dict, window: int | None = None) -> list[str]:
    """Return rows whose tool name does not appear in any scenario_library key.

    A row is 'dead' when the tool name extracted from it does not match the
    prefix (tool_type) of any key in scenario_library.  Keys follow the format
    tool_type:tier:outcome.

    The *window* parameter is reserved for future narrowing (e.g. checking only
    the most recent *window* sessions); currently all library keys are checked.
    """
    if window is None:
        window = POLICY_DEAD_ROW_WINDOW

    # Build set of active tool names from scenario_library keys (tool_type:tier:outcome)
    active_tools: set[str] = set()
    for key in scenario_library:
        parts = key.split(":")
        if parts:
            active_tools.add(parts[0].lower())

    dead: list[str] = []
    for row in rows:
        tool_name = _extract_tool_name(row).lower()
        if tool_name and tool_name not in active_tools:
            dead.append(row)
    return dead


def find_near_duplicates(rows: list[str]) -> list[tuple[str, str]]:
    """Identify near-duplicate row pairs using SequenceMatcher.

    Pairs with similarity ratio > POLICY_GC_NEAR_DUPLICATE_RATIO are considered near-duplicates.
    Returns a list of (keep, retire) tuples where the shorter row is kept.
    """
    pairs: list[tuple[str, str]] = []
    seen_retire: set[str] = set()

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            if ratio > POLICY_GC_NEAR_DUPLICATE_RATIO:
                # Keep the shorter row; retire the longer one
                keep, retire = (a, b) if len(a) <= len(b) else (b, a)
                if retire not in seen_retire:
                    pairs.append((keep, retire))
                    seen_retire.add(retire)
    return pairs


def check_size_cap(rows: list[str]) -> list[str]:
    """Return tagged rows to evict when the table exceeds POLICY_MAX_TABLE_ROWS.

    *rows* is the full table (human + tuner rows) so the cap counts all of them,
    but only tuner-tagged rows are ever returned as eviction candidates
    (oldest/first first). Human rows are never evicted, so the table may stay
    over cap if there are too few tagged rows.
    """
    if len(rows) > POLICY_MAX_TABLE_ROWS:
        excess = len(rows) - POLICY_MAX_TABLE_ROWS
        return filter_tagged(rows)[:excess]
    return []


def generate_report(
    scenario_library_path: Path | None = None,
    mdc_path: Path | None = None,
) -> str:
    """Load both files, run all 3 checks, return a formatted GC report string."""
    mdc_file = mdc_path or MDC_FILE
    lib_file = scenario_library_path or SCENARIO_LIB

    # Load MDC file
    try:
        mdc_text = mdc_file.read_text(encoding="utf-8")
    except OSError:
        mdc_text = ""

    # Load scenario library
    try:
        scenario_library: dict = json.loads(lib_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        scenario_library = {}

    rows = parse_decision_table(mdc_text)
    tagged = filter_tagged(rows)
    dead_rows = find_dead_rows(tagged, scenario_library)
    near_dup_pairs = find_near_duplicates(tagged)
    cap_rows = check_size_cap(rows)

    lines: list[str] = ["## Decision Table GC report"]
    lines.append(f"Total rows: {len(rows)}")
    lines.append("")

    # Dead rows
    lines.append(f"### Dead rows ({len(dead_rows)} proposed for retirement)")
    if dead_rows:
        for row in dead_rows:
            lines.append(f"  {row}")
    else:
        lines.append("  (none)")
    lines.append("")

    # Near-duplicates
    lines.append(f"### Near-duplicate pairs ({len(near_dup_pairs)})")
    if near_dup_pairs:
        for keep, retire in near_dup_pairs:
            lines.append(f"  KEEP:   {keep}")
            lines.append(f"  RETIRE: {retire}")
    else:
        lines.append("  (none)")
    lines.append("")

    # Size cap
    if cap_rows:
        lines.append(
            f"### Size cap — OVER CAP by {len(cap_rows)} row(s) "
            f"(max {POLICY_MAX_TABLE_ROWS})"
        )
        for row in cap_rows:
            lines.append(f"  {row}")
    else:
        lines.append(f"### Size cap — OK (<= {POLICY_MAX_TABLE_ROWS} rows)")
    lines.append("")

    return "\n".join(lines)


def _apply_gc(
    scenario_library_path: Path | None = None,
    mdc_path: Path | None = None,
) -> str:
    """Remove dead, near-duplicate, and over-cap rows from model-router.md.

    Returns the GC report (computed before modifications).
    """
    mdc_file = mdc_path or MDC_FILE
    lib_file = scenario_library_path or SCENARIO_LIB

    # Generate report first (before any modifications)
    report = generate_report(lib_file, mdc_file)

    try:
        mdc_text = mdc_file.read_text(encoding="utf-8")
    except OSError:
        return report

    try:
        scenario_library: dict = json.loads(lib_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        scenario_library = {}

    rows = parse_decision_table(mdc_text)
    tagged = filter_tagged(rows)
    dead_rows = find_dead_rows(tagged, scenario_library)
    near_dup_pairs = find_near_duplicates(tagged)
    cap_rows = check_size_cap(rows)

    new_text = mdc_text

    # Remove dead rows
    for row in dead_rows:
        new_text = new_text.replace(row + "\n", "").replace(row, "")

    # Remove retired near-duplicate rows
    for _keep, retire in near_dup_pairs:
        new_text = new_text.replace(retire + "\n", "").replace(retire, "")

    # Remove oldest/excess rows from cap overflow
    for row in cap_rows:
        new_text = new_text.replace(row + "\n", "").replace(row, "")

    if new_text != mdc_text:
        # PROD-8: fails open — a read-only rules file means GC skips this
        # run rather than crashing the caller (compact_log.py's Stop-hook
        # path calls generate_report() only, but --apply is a direct CLI
        # entry point with no wrapper of its own).
        try:
            mdc_file.write_text(new_text, encoding="utf-8")
        except OSError:
            pass

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Policy GC: detect dead/duplicate Decision Table rows and enforce size cap."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print GC report without modifying any files.",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Remove dead rows from rules/model-router.md.",
    )
    args = parser.parse_args()

    try:
        if args.apply:
            report = _apply_gc()
        else:
            # Default and --dry-run both print report only
            report = generate_report()
    except Exception:  # noqa: BLE001 — CLI must not crash on an unexpected read/parse failure
        report = "[cost-router] policy_gc: unable to generate report (unexpected error)."

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
