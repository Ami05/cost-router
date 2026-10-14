"""TEST-003: CI staleness check for the classify_intent.py golden fixture.

tests/intent_eval_scenarios.json is a hand-labeled golden set for
scripts/classify_intent.py. If classify_intent.py changes (new intent class,
keyword, or scoring logic) without the golden set being reviewed/regenerated,
eval results silently drift out of sync with the code under test.

This test compares a stored content hash (`_source_hash`) embedded in the
golden fixture against the live sha256 of scripts/classify_intent.py.
Content hashing is used instead of mtime comparison because mtimes are not a
reliable signal here (e.g. dev -> production resync or a checkout can touch
a file's mtime without changing its content, per CLAUDE.md's dev/prod split).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CLASSIFIER_PATH = PROJECT_ROOT / "scripts" / "classify_intent.py"
GOLDEN_PATH = PROJECT_ROOT / "tests" / "intent_eval_scenarios.json"


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_golden_fixture_hash_matches_classifier_source():
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    stored_hash = golden.get("_source_hash")

    assert stored_hash, (
        f"{GOLDEN_PATH} is missing a '_source_hash' field. "
        "Add the current sha256 of scripts/classify_intent.py to "
        "'_source_hash' after reviewing/regenerating the golden scenarios."
    )

    current_hash = _sha256_of(CLASSIFIER_PATH)

    assert stored_hash == current_hash, (
        "tests/intent_eval_scenarios.json is stale relative to "
        "scripts/classify_intent.py (sha256 mismatch: "
        f"golden fixture recorded {stored_hash}, live file hashes to "
        f"{current_hash}). classify_intent.py changed since this golden set "
        "was last reviewed. Review tests/intent_eval_scenarios.json for "
        "needed additions/updates, then update its '_source_hash' field to "
        f"{current_hash!r} (e.g. via: python3 -c \"import hashlib; "
        "print(hashlib.sha256(open('scripts/classify_intent.py','rb')"
        ".read()).hexdigest())\")."
    )


# ---------------------------------------------------------------------------
# Eval artifact freshness (exemplars, corpus, confusable pairs, baselines)
# ---------------------------------------------------------------------------

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report as _report  # noqa: E402


def test_artifact_manifest_hashes_are_current():
    stale = _report.stale_manifest_entries()
    assert not stale, (
        f"eval artifacts changed since tests/eval_results/artifact_hashes.json was recorded: {stale}. "
        f"Review, then regenerate: {_report.REGENERATE_COMMANDS}"
    )


def test_option_d_baseline_recorded_against_current_sources():
    stale = _report.stale_baseline_files(_report.load_baseline())
    assert not stale, (
        f"option_d_eval.json was recorded against different source files: {stale}. "
        f"Regenerate: {_report.REGENERATE_COMMANDS}"
    )


def test_semantic_vs_keyword_baseline_recorded_against_current_sources():
    path = PROJECT_ROOT / "tests" / "eval_results" / "semantic_vs_keyword.json"
    recorded = json.loads(path.read_text())["heldout_comparison"]["source_hashes"]
    current = {k: v for k, v in _report.current_source_hashes().items() if k in recorded}
    assert recorded == current, (
        "semantic_vs_keyword.json held-out section is stale. Regenerate: "
        f"{_report.REGENERATE_COMMANDS}"
    )
