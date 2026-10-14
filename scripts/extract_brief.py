#!/usr/bin/env python3
"""Extract a single task from a tickets JSON file into a scoped brief.

Usage: python3 extract_brief.py --tickets <path> --task-id <id> [--output <path>]
   or: python3 extract_brief.py --stdin [--output <path>]

HG-104: Produces a brief containing only the task description, acceptance
criteria, file paths, and constraints — not the full plan or other tasks.

When using --stdin, expects JSON payload with keys: tickets_path (str) and task_id (str).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import read_stdin_json


def extract_brief(tickets_path: str, task_id: str) -> str:
    """Extract a brief for a single task from a tickets JSON file."""
    path = Path(tickets_path)
    if not path.exists():
        print(f"Error: tickets file not found: {tickets_path}", file=sys.stderr)
        sys.exit(1)

    tickets = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(tickets, dict) and "tickets" in tickets:
        tickets = tickets["tickets"]

    ticket = None
    for t in tickets:
        if t.get("id") == task_id:
            ticket = t
            break

    if ticket is None:
        print(f"Error: task {task_id} not found in {tickets_path}", file=sys.stderr)
        sys.exit(1)

    # Build brief
    lines = []
    lines.append("## Task")
    lines.append(ticket.get("title", task_id))
    desc = ticket.get("description", "")
    if desc:
        lines.append(desc)
    lines.append("")

    # Acceptance criteria
    criteria = ticket.get("acceptance_criteria", [])
    if criteria:
        lines.append("## Acceptance Criteria")
        for c in criteria:
            lines.append(f"- {c}")
        lines.append("")

    # Files from steps
    steps = ticket.get("steps", [])
    files = []
    for step in steps:
        f = step.get("file", "")
        action = step.get("action", "")
        if f:
            files.append(f"- {f} — {action}")
    if files:
        lines.append("## Files")
        lines.extend(files)
        lines.append("")

    # Constraints
    lines.append("## Constraints")
    lines.append("- Follow existing code patterns and conventions")
    lines.append("- Stdlib-only; no external dependencies")
    lines.append("- Fail-open on errors (gate pattern)")
    lines.append("")

    lines.append("## Report Contract")
    lines.append("Return STATUS: DONE|DONE_WITH_CONCERNS|BLOCKED|NEEDS_CONTEXT as first line.")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract task brief from tickets JSON")
    parser.add_argument("--tickets", help="Path to tickets JSON file")
    parser.add_argument("--task-id", help="Task ID to extract")
    parser.add_argument("--stdin", action="store_true", help="Read tickets_path and task_id from stdin JSON")
    parser.add_argument("--output", help="Output file path (default: stdout)")
    args = parser.parse_args()

    # Handle stdin mode
    if args.stdin:
        payload = read_stdin_json()
        tickets_path = payload.get("tickets_path")
        task_id = payload.get("task_id")
        if not tickets_path or not task_id:
            print("Error: stdin mode requires JSON with 'tickets_path' and 'task_id' keys", file=sys.stderr)
            return 1
    else:
        if not args.tickets or not args.task_id:
            print("Error: either --stdin or both --tickets and --task-id are required", file=sys.stderr)
            return 1
        tickets_path = args.tickets
        task_id = args.task_id

    brief = extract_brief(tickets_path, task_id)

    if args.output:
        Path(args.output).write_text(brief, encoding="utf-8")
        print(f"Brief written to {args.output}")
    else:
        print(brief)

    return 0


if __name__ == "__main__":
    sys.exit(main())
