#!/usr/bin/env python3
"""Lint: flag import statements inside try/except Exception blocks.

Imports hidden inside broad except handlers silently swallow ImportError,
causing functions to fail-open without any signal. This lint ensures every
import either lives at module scope or is inside a try/except with a narrow
handler (e.g. except ImportError).

Usage:
    python3 scripts/lint_lazy_imports.py [directory ...]
    # defaults to scripts/

Exit code 0 = clean, 1 = violations found.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

BROAD_EXCEPTIONS = {"Exception", "BaseException"}


def scan_file(path: Path) -> list[dict]:
    """Return list of violations in a single file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue

        broad_catch = False
        for handler in node.handlers:
            if handler.type is None:  # bare except
                broad_catch = True
            elif isinstance(handler.type, ast.Name) and handler.type.id in BROAD_EXCEPTIONS:
                broad_catch = True

        if not broad_catch:
            continue

        for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                module = getattr(child, "module", None) or ", ".join(
                    a.name for a in child.names
                )
                names = [a.name for a in child.names] if isinstance(child, ast.ImportFrom) else []
                violations.append({
                    "file": str(path),
                    "line": child.lineno,
                    "module": module,
                    "names": names,
                })

    return violations


def main() -> int:
    dirs = [Path(d) for d in sys.argv[1:]] if len(sys.argv) > 1 else [Path("scripts")]

    all_violations = []
    for d in dirs:
        for f in sorted(d.glob("*.py")):
            all_violations.extend(scan_file(f))

    if not all_violations:
        print("lint_lazy_imports: clean — 0 violations")
        return 0

    print(f"lint_lazy_imports: {len(all_violations)} violation(s)\n")
    for v in all_violations:
        imported = ", ".join(v["names"]) if v["names"] else v["module"]
        print(f"  {v['file']}:{v['line']}  from {v['module']} import {imported}")

    print(
        "\nFix: move the import outside the try block, or narrow the except "
        "clause to specific exceptions (e.g. KeyError, FileNotFoundError)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
