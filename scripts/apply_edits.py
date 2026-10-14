#!/usr/bin/env python3
"""Deterministic edit applicator — zero LLM cost for mechanical file mutations.

Takes a JSON edit spec (from FO-generated plan) and applies edits mechanically.
Supports: create, str_replace, line_replace.

Usage:
    python apply_edits.py --spec edits.json [--dry-run] [--strict]
    echo '{"edits": [...]}' | python apply_edits.py --stdin [--dry-run]

Exit codes:
    0 — all edits applied successfully
    1 — one or more edits failed (details in JSON output)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def apply_edit(
    file_path: Path,
    edit: dict[str, Any],
    dry_run: bool = False,
    strict: bool = True,
) -> dict[str, Any]:
    """Apply a single edit and return a result dict."""
    edit_type = edit["edit_type"]
    result: dict[str, Any] = {
        "file_path": str(file_path),
        "edit_type": edit_type,
        "status": "ok",
    }

    try:
        if edit_type == "create":
            if dry_run:
                result["status"] = "dry_run"
                return result
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(edit["new_content"])

        elif edit_type == "str_replace":
            if not file_path.exists():
                if strict:
                    raise FileNotFoundError(f"file not found: {file_path}")
                result["status"] = "skipped"
                result["reason"] = "file not found"
                return result

            content = file_path.read_text()
            old = edit["old_content"]
            new = edit["new_content"]

            if old not in content:
                if strict:
                    raise ValueError(
                        f"old_content not found in {file_path}: {old[:80]}..."
                    )
                result["status"] = "skipped"
                result["reason"] = "old_content not found"
                return result

            count = content.count(old)
            if count > 1 and strict:
                raise ValueError(
                    f"old_content matched {count} times in {file_path} — "
                    f"ambiguous; use line_replace or provide more context"
                )

            if dry_run:
                result["status"] = "dry_run"
                result["matches"] = count
                return result

            # When strict=False, replace all occurrences; when strict=True, replace only first
            num_replacements = count if not strict else 1
            content = content.replace(old, new, num_replacements)
            file_path.write_text(content)

        elif edit_type == "line_replace":
            if not file_path.exists():
                if strict:
                    raise FileNotFoundError(f"file not found: {file_path}")
                result["status"] = "skipped"
                result["reason"] = "file not found"
                return result

            lines = file_path.read_text().splitlines(keepends=True)
            start = edit.get("line_start", 1) - 1
            end = edit.get("line_end", len(lines))
            new_lines = edit["new_content"].splitlines(keepends=True)

            if dry_run:
                result["status"] = "dry_run"
                result["replacing_lines"] = f"{start+1}-{end}"
                return result

            lines[start:end] = new_lines
            file_path.write_text("".join(lines))

        else:
            raise ValueError(f"unknown edit_type: {edit_type}")

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


def apply_spec(
    spec: dict[str, Any] | list[dict[str, Any]],
    base_dir: Path | None = None,
    dry_run: bool = False,
    strict: bool = True,
) -> list[dict[str, Any]]:
    """Apply all edits in a spec and return results."""
    if isinstance(spec, list):
        edits = spec
    else:
        edits = spec.get("edits", [])
    results = []

    for edit in edits:
        raw_path = edit["file_path"]
        if base_dir:
            file_path = base_dir / raw_path
        else:
            file_path = Path(raw_path)

        r = apply_edit(file_path, edit, dry_run=dry_run, strict=strict)
        results.append(r)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply JSON edit specs to files")
    parser.add_argument("--spec", type=str, help="Path to JSON edit spec file")
    parser.add_argument("--stdin", action="store_true", help="Read spec from stdin")
    parser.add_argument("--base-dir", type=str, help="Base directory for relative paths")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--strict", action="store_true", default=True,
                        help="Fail on missing files/content (default)")
    parser.add_argument("--no-strict", action="store_false", dest="strict",
                        help="Skip edits that can't be applied")
    args = parser.parse_args()

    if args.stdin:
        spec = json.load(sys.stdin)
    elif args.spec:
        with open(args.spec) as f:
            spec = json.load(f)
    else:
        parser.error("provide --spec or --stdin")
        return 1

    base_dir = Path(args.base_dir) if args.base_dir else None
    results = apply_spec(spec, base_dir=base_dir, dry_run=args.dry_run, strict=args.strict)

    json.dump({"results": results}, sys.stdout, indent=2)
    print()

    errors = [r for r in results if r["status"] == "error"]
    if errors:
        print(f"\n{len(errors)} edit(s) failed:", file=sys.stderr)
        for e in errors:
            print(f"  {e['file_path']}: {e.get('error', 'unknown')}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
