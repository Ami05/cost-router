"""Shared eval contract — standardized result shape for all eval tests."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class EvalResult:
    """Standard result from an eval test."""
    metric_name: str          # e.g. "exemplar_accuracy", "synthetic_accuracy"
    value: float              # the measured value (0.0–1.0 for accuracy)
    threshold: float          # minimum acceptable value
    passed: bool              # value >= threshold
    category: str = ""        # e.g. "classifier", "routing", "gates"
    details: dict = field(default_factory=dict)  # extra context

    @classmethod
    def accuracy(cls, name: str, correct: int, total: int, threshold: float, category: str = "classifier") -> "EvalResult":
        value = correct / total if total > 0 else 0.0
        return cls(metric_name=name, value=value, threshold=threshold, passed=value >= threshold, category=category, details={"correct": correct, "total": total})
