#!/usr/bin/env python3
"""Doc-lint: assert every file in commands/, skills/, scripts/ is mentioned in README.md.

Exit 0 if all files are referenced. Exit 1 with a clear report if any are missing.
Run as: python scripts/readme_coverage_lint.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def _check_mentioned(readme: str, names: list[str]) -> list[str]:
    return [name for name in names if name not in readme]


def main() -> int:
    readme = _readme_text()
    missing: list[str] = []

    # commands/*.md
    for f in sorted((ROOT / "commands").glob("*.md")):
        missing.extend(_check_mentioned(readme, [f.name]))

    # skills/*/SKILL.md — check the skill directory name
    for d in sorted((ROOT / "skills").iterdir()):
        if d.is_dir():
            missing.extend(_check_mentioned(readme, [d.name]))

    # scripts/*.py and *.sh
    for f in sorted((ROOT / "scripts").glob("*.py")) + sorted((ROOT / "scripts").glob("*.sh")):
        missing.extend(_check_mentioned(readme, [f.name]))

    if missing:
        print("doc-lint FAILED — these files/dirs are not mentioned in README.md:")
        for name in missing:
            print(f"  - {name}")
        return 1

    print(f"doc-lint OK — all checked names appear in README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
