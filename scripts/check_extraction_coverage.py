#!/usr/bin/env python3
"""CI gate: NormalizedPayload field count must not exceed fixture payload count.

Exits 1 if scripts/_payload.py defines more fields in NormalizedPayload
(excluding 'raw') than there are .json files in tests/fixtures/payloads/.
Exits 0 when coverage is sufficient or _payload.py does not yet exist.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PAYLOAD_MODULE = Path(__file__).resolve().parent / "_payload.py"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "payloads"
EXCLUDED_FIELDS = {"raw"}


def count_normalized_payload_fields(path: Path) -> int | None:
    """Return field count for NormalizedPayload dataclass, excluding EXCLUDED_FIELDS.

    Returns None if the class is not found in the module.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "NormalizedPayload":
            continue
        fields: list[str] = []
        for item in node.body:
            # dataclass fields are annotated assignments at class body level
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                name = item.target.id
                if name not in EXCLUDED_FIELDS:
                    fields.append(name)
        return len(fields)

    return None


def count_fixture_files(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return len(list(directory.glob("*.json")))


def main() -> int:
    # If _payload.py doesn't exist yet, the gate is a no-op (pass).
    if not PAYLOAD_MODULE.exists():
        print(
            f"check_extraction_coverage: SKIP — {PAYLOAD_MODULE.name} not found; "
            "gate is a no-op until the file is created."
        )
        return 0

    field_count = count_normalized_payload_fields(PAYLOAD_MODULE)
    if field_count is None:
        print(
            "check_extraction_coverage: SKIP — NormalizedPayload class not found "
            f"in {PAYLOAD_MODULE.name}; gate is a no-op until the class is defined."
        )
        return 0

    fixture_count = count_fixture_files(FIXTURE_DIR)

    if field_count > fixture_count:
        print(
            f"check_extraction_coverage: FAIL — "
            f"NormalizedPayload has {field_count} field(s) (excluding 'raw') "
            f"but only {fixture_count} fixture(s) exist in "
            f"tests/fixtures/payloads/. "
            f"Add {field_count - fixture_count} more fixture file(s) to match coverage."
        )
        return 1

    print(
        f"check_extraction_coverage: PASS — "
        f"{fixture_count} fixture(s) >= {field_count} NormalizedPayload field(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
