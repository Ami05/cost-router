"""create_validator — validate create worker dispatch schemas."""
from __future__ import annotations

from typing import Any

from schema_validators import ValidationResult


def validate(schema: dict[str, Any]) -> ValidationResult:
    """Validate a create worker schema dict.

    Checks:
      - constraints field

    Returns:
        ValidationResult with ok=True when all checks pass.
    """
    from _validate_shared import check_constraints  # noqa: PLC0415

    violations = check_constraints(schema)
    return ValidationResult(ok=not violations, violations=violations)
