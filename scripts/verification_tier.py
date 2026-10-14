#!/usr/bin/env python3
"""TICKET-040 (fix-7): Tiered verification policy.

Classifies a completed mechanical-fix dispatch as LIGHT or FULL tier so
that single-file, no-design-ambiguity fixes don't always pay for a full
independent re-verification run. unboundedOrchestrator.json obs-3 showed a
narrowly-scoped, single-file fix nearly matching the cost of a 28-file
redesign once mandatory full-suite re-verification was added on top — the
mandatory full re-run, not the fix itself, was the dominant cost.

Classification rule (LIGHT requires ALL of):
  - exactly one file touched by the dispatch (`files` list length == 1)
  - no schema/API-surface-change signal in the dispatch's own
    pattern/why/constraints text (see _FULL_TIER_KEYWORDS)
  - the transformation was already fully decided before dispatch, i.e. this
    is a `mutation`-schema dispatch (has a non-empty `pattern`), never an
    `orchestrator`/design dispatch, which by definition carries ambiguity.

Anything that doesn't clear all three -> FULL tier (fail closed: ambiguous
or under-specified input gets the expensive, safe tier).

What each tier actually checks:
  - LIGHT: a single targeted test run scoped to the touched file (or its
    direct test-module counterpart) is sufficient. This is deliberately the
    same scope pytest/etc. commands already treat as "not bulk" (a single
    .py file target) — LIGHT tier only changes the *guidance* given when the
    orchestrator instead reaches for a broad/full-suite command, redirecting
    it to the cheap scoped command rather than defaulting to full re-run.
  - FULL: mandatory full independent re-run (existing bulk-verify P0 policy,
    unchanged) — a subagent with Bash access runs the full/bulk verification
    command and reports back; the orchestrator never runs it inline.
"""

from __future__ import annotations

import json
import re

LIGHT = "light"
FULL = "full"

# TICKET-040: presence of any of these words in pattern/why/constraints signals
# design ambiguity or a schema/API-surface change -> forces FULL tier even for
# an otherwise single-file dispatch.
_FULL_TIER_KEYWORDS = (
    "schema",
    "api",
    "interface",
    "contract",
    "protocol",
    "migration",
    "breaking",
    "signature change",
    "public api",
)
_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _FULL_TIER_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def classify_fix_tier(
    files: list | tuple | None,
    pattern: str | None = None,
    why: str | None = None,
    constraints: list | tuple | None = None,
    worker: str | None = None,
) -> str:
    """Return LIGHT or FULL for a decided mechanical dispatch.

    Fail-closed: any missing/ambiguous input (no files, more than one file,
    no decided pattern, a non-`mutation` worker, or a design/schema keyword
    anywhere in pattern/why/constraints) returns FULL.
    """
    if worker is not None and worker != "mutation":
        return FULL
    if not isinstance(files, (list, tuple)) or len(files) != 1:
        return FULL
    if not isinstance(pattern, str) or not pattern.strip():
        # No fully-decided transformation -> ambiguity was left to the worker.
        return FULL

    text_bits = [pattern]
    if isinstance(why, str):
        text_bits.append(why)
    if isinstance(constraints, (list, tuple)):
        text_bits.extend(c for c in constraints if isinstance(c, str))
    combined = " ".join(text_bits)
    if _KEYWORD_RE.search(combined):
        return FULL
    return LIGHT


def classify_dispatch_json(task_text: str | None) -> tuple[str, list[str]]:
    """Parse a dispatch's own JSON prompt and classify its verification tier.

    Never raises. Returns (tier, files) — files is the raw `files` list from
    the dispatch JSON (possibly empty) so callers can scope a targeted
    verification command against it. Unparseable/non-dict/non-mutation input
    always classifies FULL with an empty files list.
    """
    if not task_text:
        return FULL, []
    try:
        data = json.loads(task_text)
    except (ValueError, TypeError):
        return FULL, []
    if not isinstance(data, dict):
        return FULL, []
    files = data.get("files")
    files = list(files) if isinstance(files, (list, tuple)) else []
    tier = classify_fix_tier(
        files,
        pattern=data.get("pattern"),
        why=data.get("why"),
        constraints=data.get("constraints"),
        worker=data.get("worker"),
    )
    return tier, files


def requires_full_rerun(tier: str) -> bool:
    """FULL tier mandates the existing bulk-verify full independent re-run.
    LIGHT tier does not — a targeted single-file test run is sufficient."""
    return tier != LIGHT


def touched_file_from_files(files: list[str]) -> str | None:
    """Extract the bare file path from a single `path:line`/`path:start,end`
    dispatch files[] entry. Returns None for anything but exactly one entry."""
    if len(files) != 1:
        return None
    return files[0].split(":", 1)[0].strip() or None


def light_tier_command_ok(command: str | None, touched_file: str | None) -> bool:
    """True if a verification `command` is already scoped to `touched_file`
    (i.e. mentions its filename stem) rather than being a broad/full-suite
    run. Fail-closed to False on any missing input — treat as needing
    redirect to a scoped command rather than assuming it's already scoped."""
    if not command or not touched_file:
        return False
    stem = touched_file.rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    if not stem:
        return False
    return stem.lower() in command.lower()
