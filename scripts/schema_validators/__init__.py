"""schema_validators — per-worker dispatch schema validators.

Each module exposes a ``validate(schema: dict) -> ValidationResult`` function.
``ValidationResult`` is a dataclass with ``ok: bool`` and
``violations: list[tuple[str, str | None, str | None]]`` where each tuple is
``(reason, sub_reason, rejected_item)``.

SCHEMA_VALIDATORS maps worker name -> validator module.
``validate_dispatch_schema.py`` delegates all content checks here.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    """Result returned by every per-worker validator."""

    ok: bool
    violations: list[tuple[str, str | None, str | None]] = field(default_factory=list)


from schema_validators import (  # noqa: E402
    mutation_validator,
    read_validator,
    create_validator,
    shell_validator,
    orchestrator_validator,
)

SCHEMA_VALIDATORS: dict[str, object] = {
    "mutation": mutation_validator,
    "read": read_validator,
    "create": create_validator,
    "shell": shell_validator,
    "orchestrator": orchestrator_validator,
}

__all__ = ["ValidationResult", "SCHEMA_VALIDATORS"]
