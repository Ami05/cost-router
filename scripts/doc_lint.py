#!/usr/bin/env python3
"""doc-lint: assert SKILL.md contains required concrete model mappings.

Prevents the COST-2 regression where model IDs silently disappear from the
skill, making it impossible for agents to actually reach the fast tier.

Exit 0 on pass, 1 on failure. Intended for CI (scripts/check_routing test suite
or a standalone job).
"""

from __future__ import annotations

import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
_dev_layout = repo_root / ".claude" / "rules" / "model-router.md"
_plugin_layout = repo_root / "rules" / "model-router.md"
SKILL_PATH = next(
    (p for p in [_dev_layout, _plugin_layout] if p.is_file()),
    _dev_layout,  # default to dev layout
)

REQUIRED_STRINGS = [
    'model: "haiku"',
    'model: "composer-2.5-fast"',
    "claude-haiku-4-5-20251001",
    "composer-2.5-fast",
    "DIRECT does NOT switch models",
]


def main() -> int:
    if not SKILL_PATH.is_file():
        print(f"doc-lint: rule not found at {SKILL_PATH}", file=sys.stderr)
        return 1
    content = SKILL_PATH.read_text(encoding="utf-8")
    failures: list[str] = []
    for required in REQUIRED_STRINGS:
        if required not in content:
            failures.append(f"  missing: {required!r}")
    if failures:
        print("doc-lint FAILED — rule is missing required model-ID strings:")
        for line in failures:
            print(line)
        return 1
    print("doc-lint OK — rule contains all required model-ID strings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
