"""orchestrator_validator — validate orchestrator worker dispatch schemas.

Orchestrator constraints are behavioral rules (not schema fields), so the
constraints check is intentionally skipped here — consistent with the
existing gate logic in validate_dispatch_schema.py.

Checks:
  - `plan` field must not be present (use `step` instead)
  - `step` field must not contain more than one Phase marker
  - `step` field must not imply oversized scope (TICKET-036 / TICKET-013 fix-1):
    20+ files, 3+ distinct artifact types, or a combined-redesign signal.
    A compact `step` string can still describe unbounded execution (see
    knowledge/tickets/unboundedOrchestrator.json obs-1: 28-file, 2-task
    dispatch ran 16+ min / 188k tokens with only 2/7 sub-units completed).
    These are text-scan heuristics, not a guarantee — they exist to force a
    human to explicitly split scope before dispatch, not to size-estimate
    precisely.
  - `step` field must not imply a scattered/non-contiguous match count within
    a single host file (TICKET-041 / TICKET-013 fix-8): a mechanical
    find-replace across many scattered sites in one large file is the same
    sizing risk as the file-count signal above, just expressed as
    site/occurrence count instead of file count.
"""
from __future__ import annotations

import re
from typing import Any

from schema_validators import ValidationResult

# ── TICKET-036 sizing heuristics ────────────────────────────────────────────
# Threshold rationale (documented here because the ticket left them open):
#
# 1. File-count signal (>=20): either an explicit "<N> files/scripts/tests"
#    phrase with N >= 20, or >=20 distinct path-like/extension tokens
#    (e.g. "tests/e2e/test_x.py") enumerated in the step text.
_FILE_COUNT_PHRASE_RE = re.compile(
    r"\b(\d+)\s*(?:\+|-)?\s*(?:files?|scripts?|journeys?|tests?|modules?)\b",
    re.IGNORECASE,
)
_FILE_PATH_TOKEN_RE = re.compile(
    r"\b[\w./-]+\.(?:py|js|ts|tsx|json|md|yaml|yml|sh)\b",
    re.IGNORECASE,
)
_FILE_COUNT_THRESHOLD = 20

# 2. Artifact-type signal (>=3 distinct buckets): a redesign spanning tests,
#    scripts, configs, docs, gates, schemas, and modules in the same step is
#    exactly the "combined redesign" shape that ran away in obs-1/obs-4.
#    Buckets are intentionally coarse — precision isn't the goal, forcing a
#    manual split when scope spans many artifact kinds is.
_ARTIFACT_KEYWORD_BUCKETS: dict[str, tuple[str, ...]] = {
    "test": ("test", "tests", "journey", "journeys"),
    "script": ("script", "scripts"),
    "config": ("config", "configs", "configuration"),
    "doc": ("doc", "docs", "documentation"),
    "schema": ("schema", "schemas"),
    "gate": ("gate", "gates"),
    "module": ("module", "modules"),
}
_ARTIFACT_BUCKET_THRESHOLD = 3

# 3. Combined-redesign signal: >=2 distinct ticket IDs referenced together,
#    or >=2 occurrences of a redesign-shaped verb (redesign/rewrite/restructure/
#    "combine"+"merge" language) — either is the "two large tasks merged into
#    one step field" pattern from obs-1.
_TICKET_ID_RE = re.compile(r"\bTICKET-\d+\b", re.IGNORECASE)
_REDESIGN_VERB_RE = re.compile(
    r"\b(redesign|rewrite|restructure|reimplement)\b", re.IGNORECASE
)
_REDESIGN_VERB_THRESHOLD = 2

# 4. Scattered-site signal (TICKET-041 / TICKET-013 fix-8): a mechanical
#    find-replace across many scattered/non-contiguous locations within a
#    SINGLE host file carries the same runaway-scope risk as the file-count
#    signal above, just expressed as occurrence count instead of file count.
#    Fires on either (a) an explicit "<N> scattered/non-contiguous
#    sites/occurrences/..." phrase anywhere in the step, or (b) >=N
#    comma-separated line numbers attached to one file path (e.g.
#    "scripts/gates.py:12,45,89,140,..."), gated to a SINGLE distinct file so
#    a genuine multi-file dispatch is left to the file-count signal instead.
#    Threshold is lower than the 20-file bar: scatter within one file is
#    inherently denser (every site needs independent verification) than the
#    same count spread across files, so 15 is used deliberately, not a typo.
_SCATTERED_COUNT_PHRASE_RE = re.compile(
    r"\b(\d+)\s*(?:\+|-)?\s*(?:scattered|non-contiguous|noncontiguous)\s*"
    r"(?:sites?|call\s*sites?|occurrences?|locations?|places?|references?|matches?)\b",
    re.IGNORECASE,
)
_LINE_LIST_RE = re.compile(
    r"\b([\w./-]+\.(?:py|js|ts|tsx|json|md|yaml|yml|sh))\s*:\s*"
    r"(\d+(?:\s*,\s*\d+){2,})\b",
)
_SCATTERED_SITE_THRESHOLD = 15


def _scan_oversized_scope(step_val: str) -> list[str]:
    """Return a list of human-readable reasons the step implies oversized
    scope, or an empty list if none of the three TICKET-036 signals fire.
    """
    reasons: list[str] = []

    phrase_hits = [int(n) for n in _FILE_COUNT_PHRASE_RE.findall(step_val)]
    path_hits = len(set(_FILE_PATH_TOKEN_RE.findall(step_val)))
    max_file_signal = max(phrase_hits, default=0)
    if max_file_signal >= _FILE_COUNT_THRESHOLD or path_hits >= _FILE_COUNT_THRESHOLD:
        signal = max(max_file_signal, path_hits)
        reasons.append(
            f"implies {signal}+ files (>= {_FILE_COUNT_THRESHOLD} threshold)"
        )

    lowered = step_val.lower()
    matched_buckets = [
        bucket
        for bucket, keywords in _ARTIFACT_KEYWORD_BUCKETS.items()
        if any(re.search(rf"\b{kw}\b", lowered) for kw in keywords)
    ]
    if len(matched_buckets) >= _ARTIFACT_BUCKET_THRESHOLD:
        reasons.append(
            f"spans {len(matched_buckets)} distinct artifact types "
            f"({', '.join(sorted(matched_buckets))}) "
            f"(>= {_ARTIFACT_BUCKET_THRESHOLD} threshold)"
        )

    distinct_tickets = {t.upper() for t in _TICKET_ID_RE.findall(step_val)}
    redesign_verb_count = len(_REDESIGN_VERB_RE.findall(step_val))
    if len(distinct_tickets) >= 2 or redesign_verb_count >= _REDESIGN_VERB_THRESHOLD:
        reasons.append(
            "combined redesign scope "
            f"({len(distinct_tickets)} distinct ticket IDs, "
            f"{redesign_verb_count} redesign-verb mentions)"
        )

    scattered_phrase_hits = [
        int(n) for n in _SCATTERED_COUNT_PHRASE_RE.findall(step_val)
    ]
    max_scattered_phrase = max(scattered_phrase_hits, default=0)

    line_list_matches = _LINE_LIST_RE.findall(step_val)
    distinct_files_with_lines = {f for f, _ in line_list_matches}
    max_line_list_count = 0
    if len(distinct_files_with_lines) == 1:
        for _file, nums in line_list_matches:
            count = len([n for n in nums.split(",") if n.strip()])
            max_line_list_count = max(max_line_list_count, count)

    scattered_signal = max(max_scattered_phrase, max_line_list_count)
    if scattered_signal >= _SCATTERED_SITE_THRESHOLD:
        reasons.append(
            f"implies {scattered_signal}+ scattered/non-contiguous sites within "
            f"a single host file (>= {_SCATTERED_SITE_THRESHOLD} threshold)"
        )

    return reasons


def validate(schema: dict[str, Any]) -> ValidationResult:
    """Validate an orchestrator worker schema dict.

    Returns:
        ValidationResult with ok=True when all checks pass.
    """
    violations: list[tuple[str, str | None, str | None]] = []

    if "plan" in schema:
        violations.append((
            "orchestrator schema uses `step` (single phase), not `plan` — max_phases_per_dispatch: 1",
            "orchestrator_plan_field",
            None,
        ))

    step_val = schema.get("step", "")
    if isinstance(step_val, str):
        phase_count = len(re.findall(r"(?:^|\n)\s*Phase\s+\d+\s*:", step_val, re.IGNORECASE))
        if phase_count > 1:
            violations.append((
                f"orchestrator `step` must describe exactly one phase (found {phase_count} Phase markers) — max_phases_per_dispatch: 1",
                "orchestrator_multi_phase",
                None,
            ))

        oversized_reasons = _scan_oversized_scope(step_val)
        if oversized_reasons:
            violations.append((
                "orchestrator `step` implies oversized scope: "
                + "; ".join(oversized_reasons)
                + ". Split into separate per-artifact dispatches (one dispatch "
                "per file group / artifact type / sub-ticket) instead of one "
                "combined orchestrator step — see TICKET-036.",
                "orchestrator_oversized_scope",
                None,
            ))

    return ValidationResult(ok=not violations, violations=violations)
