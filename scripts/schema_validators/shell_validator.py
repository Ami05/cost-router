"""shell_validator — validate shell worker dispatch schemas."""
from __future__ import annotations

import re
from typing import Any

from schema_validators import ValidationResult


def validate(schema: dict[str, Any]) -> ValidationResult:
    """Validate a shell worker schema dict.

    Checks:
      - constraints field

    Returns:
        ValidationResult with ok=True when all checks pass.
    """
    from _validate_shared import check_constraints  # noqa: PLC0415

    violations = check_constraints(schema)

    # Block bare pytest runs without timeout and -k filter (S7 incident 3)
    command = schema.get("command", "")
    if "pytest" in command:
        has_timeout = "--timeout" in command
        has_k_filter = re.search(r'(?:^|\s)-k\s+\S', command)
        if not has_timeout or not has_k_filter:
            violations.append((
                "bare pytest blocked - add --timeout=30 and -k filter",
                None,
                None,
            ))

    return ValidationResult(ok=not violations, violations=violations)
