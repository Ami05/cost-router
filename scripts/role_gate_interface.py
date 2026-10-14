"""Protocol interface for role-gate transcript helpers.

Inverted dependency surface so estimate_dispatch_context.py and
check_routing.py don't import enforce_role_gates directly.

Inversion status per importer
-------------------------------
enforce_role_gates → DOES NOT IMPORT this module (it is the concrete impl).

estimate_dispatch_context.py → NEEDS INVERSION (currently done).
  Uses: subagent_transcript_path(transcript_path, agent_id) -> Path | None
  Inject a RoleGateQuery instance at the call site (_worker_frozen_in_plan_mode).

check_routing.py - INVERSION DONE (SOLID-018).
  Uses subagent_transcript_path via RoleGateQuery default at _subagent_recent_assistant_text.

gates.py → NO INVERSION NEEDED.
  Imports event_pre_write / event_pre_read — these are gate entrypoints,
  not infrastructure helpers. Direct imports of concrete gate functions are
  correct and intentional.

run_routing_validation.py → NO INVERSION NEEDED.
  Imports check_role_gate — same reasoning as gates.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


class RoleGateResult(dict):
    """Thin TypedDict-compatible wrapper; kept as dict subclass for compat."""


@runtime_checkable
class RoleGateQuery(Protocol):
    """Minimal protocol for transcript-path resolution.

    Concrete implementation: enforce_role_gates._subagent_transcript_path.
    Callers receive an instance of this via dependency injection rather than
    importing enforce_role_gates directly — this breaks the import cycle and
    keeps the estimator and routing modules decoupled from gate internals.
    """

    def subagent_transcript_path(
        self,
        transcript_path: str,
        agent_id: str,
    ) -> "Path | None":
        """Resolve the on-disk file that holds *agent_id*'s own turns.

        Returns the per-agent Path if it exists, else None.
        Callers must fall back to the main transcript_path on None.
        """
        ...


class _EnforceRoleGateAdapter:
    """Wraps enforce_role_gates._subagent_transcript_path as a RoleGateQuery.

    Import this lazily (inside a function) to avoid pulling enforce_role_gates
    into module-level scope of estimate_dispatch_context / check_routing.
    """

    def subagent_transcript_path(
        self,
        transcript_path: str,
        agent_id: str,
    ) -> "Path | None":
        import enforce_role_gates  # noqa: PLC0415

        return enforce_role_gates._subagent_transcript_path(transcript_path, agent_id)


def default_role_gate_query() -> RoleGateQuery:
    """Return the production RoleGateQuery backed by enforce_role_gates.

    Call this lazily (not at import time) so the module graph stays clean.
    Pass the result into functions that accept a RoleGateQuery parameter.
    """
    return _EnforceRoleGateAdapter()
