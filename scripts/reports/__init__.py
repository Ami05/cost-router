"""reports — pluggable report generators for cost-router.

Protocol
--------
Each report module must expose:

    generate(report, since: str, until: str) -> Path

where ``report`` is the WindowReport object returned by
``impact_calculator.compute_window`` and ``since``/``until`` are ISO date
strings.  The function writes its artefact to disk and returns the output
Path.

Registry
--------
REPORT_REGISTRY maps CLI flag names ("card", "digest", "dispatch") to the
corresponding module.  ``generate_reports.py`` looks up the requested
report type here and delegates entirely — no report logic lives in the CLI.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ReportGenerator(Protocol):
    """Structural protocol satisfied by every report module."""

    def generate(self, report: object, since: str, until: str) -> Path:
        """Generate the report, write it to disk, and return its Path."""
        ...


# Registry populated below after class definition.
REPORT_REGISTRY: dict[str, object] = {}

# Import siblings via relative imports (works whether the package is used as
# `scripts.reports` or loaded via sys.path with `import reports`).
from reports import routing_report, cost_report, dispatch_report  # noqa: E402

REPORT_REGISTRY["card"] = routing_report
REPORT_REGISTRY["digest"] = cost_report
REPORT_REGISTRY["dispatch"] = dispatch_report

__all__ = ["ReportGenerator", "REPORT_REGISTRY"]
