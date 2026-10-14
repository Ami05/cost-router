#!/usr/bin/env python3
"""Lint: enforce Layer 1 / Layer 2 boundary — no direct extraction calls in Layer 2 files.

Architecture contract:
  Layer 1 (extraction): _model_utils.py, _session.py — owns raw payload parsing.
  Layer 2 (routing):    check_routing.py, routing_resolver.py, session_hook.py
                        — must access all payload fields via normalize_payload().

Direct calls to extraction functions from Layer 2 files bypass the normalization
contract and silently re-introduce the anti-pattern described in TICKET-034.
This lint detects those calls at AST level so CI blocks the PR before merge.

Usage:
    python3 scripts/lint_layer_boundary.py [directory ...]
    # defaults to scripts/

Exit code 0 = clean, 1 = violations found.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# Layer 2 files — must not call extraction functions directly.
LAYER2_FILES = {
    "check_routing.py",
    "routing_resolver.py",
    "session_hook.py",
}

# Extraction functions that belong exclusively to Layer 1.
# All access from Layer 2 must go through normalize_payload().
EXTRACTION_FUNCTIONS = {
    "model_of",
    "tier_of_model",
    "session_id_of",
    "subagent_id_of",
    "_tool_name",
    "_turn_id",
    "_text_for_prompt",
}


def scan_file(path: Path) -> list[dict]:
    """Return list of boundary violations in a single Layer 2 file."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations = []
    for node in ast.walk(tree):
        # Direct call: model_of(...)  or  _tool_name(...)
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in EXTRACTION_FUNCTIONS:
                violations.append({
                    "file": str(path),
                    "line": node.lineno,
                    "call": name,
                })

    return violations


def main() -> int:
    dirs = [Path(d) for d in sys.argv[1:]] if len(sys.argv) > 1 else [Path("scripts")]

    all_violations: list[dict] = []
    for d in dirs:
        for f in sorted(d.glob("*.py")):
            if f.name in LAYER2_FILES:
                all_violations.extend(scan_file(f))

    if not all_violations:
        print("lint_layer_boundary: clean — 0 violations")
        return 0

    print(f"lint_layer_boundary: {len(all_violations)} violation(s)\n")
    for v in all_violations:
        print(f"  {v['file']}:{v['line']}  direct call to '{v['call']}()'")

    print(
        "\nFix: route all payload field access through normalize_payload() in Layer 1."
        "\nLayer 2 files (check_routing.py, routing_resolver.py, session_hook.py)"
        " must not call extraction functions directly (TICKET-034)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
