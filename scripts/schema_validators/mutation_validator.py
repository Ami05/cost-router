"""mutation_validator — validate mutation worker dispatch schemas.

Checks constraints, examples, and where/why field values.
"""
from __future__ import annotations

from typing import Any

from schema_validators import ValidationResult


def validate(schema: dict[str, Any]) -> ValidationResult:
    """Validate a mutation worker schema dict.

    Checks:
      - constraints field (delegated to _check_constraints from parent module)
      - examples field (list, max 3 items, each {before,after} <= 400 chars, no backticks)
      - where field (<= 170 chars string or None)
      - why field (<= 150 chars string or None)

    Returns:
        ValidationResult with ok=True when all checks pass.
    """
    from _validate_shared import check_constraints, check_examples, check_field_values  # noqa: PLC0415

    violations: list[tuple[str, str | None, str | None]] = []
    violations.extend(check_constraints(schema))
    violations.extend(check_examples("mutation", schema))
    violations.extend(check_field_values("mutation", schema))
    return ValidationResult(ok=not violations, violations=violations)
