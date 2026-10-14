"""E2E tests for intent classification and semantic scoring.

Merged from:
  - tests/test_semantic_scorer.py        (SEM-102, SEM-103)
  - tests/test_classify_intent_semantic.py (SEM-201)
  - tests/test_role_semantic_alignment.py  (SEM-202)
  - tests/test_semantic_thresholds.py     (SEM-103 threshold / fallback)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from classify_intent import classify_intent, classify_intent_detailed
from semantic_scorer import (
    SemanticScorer,
    _log_low_confidence,
    _tokenize,
    check_role_semantic_alignment,
    classify_semantic,
    resolve_with_fallback,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

EXEMPLARS = Path(__file__).resolve().parent.parent.parent / "config" / "intent_exemplars.json"


# ---------------------------------------------------------------------------
# --- merged from test_semantic_scorer.py ---
# ---------------------------------------------------------------------------

# Fixtures

@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure each test gets a fresh SemanticScorer singleton."""
    SemanticScorer.reset_instance()
    yield
    SemanticScorer.reset_instance()


@pytest.fixture
def exemplar_path() -> Path:
    return EXEMPLARS


@pytest.fixture(scope="session")
def scorer() -> SemanticScorer:
    return SemanticScorer(EXEMPLARS)


@pytest.fixture(scope="module")
def module_scorer():
    return SemanticScorer(EXEMPLARS)


def test_fo_with_dispatch_text_aligned(module_scorer):
    result = module_scorer.score_role_alignment("FO", "Dispatching a subagent to handle the refactor")
    assert result["aligned"] is True
    assert result["confidence"] > 0


def test_direct_with_inline_text_aligned(module_scorer):
    result = module_scorer.score_role_alignment("DIRECT", "I will fix the bug in the function")
    assert result["aligned"] is True


def test_result_shape(module_scorer):
    result = module_scorer.score_role_alignment("FO", "some text")
    assert "aligned" in result
    assert "confidence" in result
    assert "reason" in result
    assert isinstance(result["aligned"], bool)
    assert isinstance(result["confidence"], float)
    assert isinstance(result["reason"], str)


# ---------------------------------------------------------------------------
# --- merged from test_semantic_thresholds.py ---
# ---------------------------------------------------------------------------

@pytest.fixture
def thresholds_cfg():
    return {
        "default_threshold": 0.6,
        "per_class": {
            "agentic_action": 0.55,
            "qa_architecture": 0.6,
            "qa_with_reads": 0.6,
            "qa_simple": 0.5,
            "content_generation": 0.6,
        },
    }


def test_above_threshold_no_fallback(scorer, tmp_path):
    log = tmp_path / "low.jsonl"
    cfg = {
        "default_threshold": 0.0,
        "per_class": {},
        "fallback_map": {},
    }
    cls, score, was_fallback = scorer.resolve_with_fallback(
        "Fix the bug in the routing gate", cfg, log
    )
    assert isinstance(cls, str)
    assert score > 0
    assert was_fallback is False
    assert not log.exists()


def test_below_threshold_triggers_fallback_scorer(scorer, tmp_path):
    log = tmp_path / "low.jsonl"
    cfg = {
        "default_threshold": 1.0,
        "per_class": {},
        "fallback_map": {
            "agentic_action": "qa_architecture",
            "qa_architecture": "qa_with_reads",
            "qa_with_reads": "qa_simple",
            "content_generation": "qa_simple",
            "inline_narration": "qa_architecture",
        },
    }
    cls, score, was_fallback = scorer.resolve_with_fallback(
        "Fix the bug in the routing gate", cfg, log
    )
    assert was_fallback is True
    assert cls == "qa_architecture"


def test_semantic_scorer_latency():
    """Semantic scorer should classify in < 200ms per sample (no model loading)."""
    from semantic_scorer import SemanticScorer
    scorer = SemanticScorer()

    # Warm up
    scorer.score_intent("test warmup query")

    samples = [
        "Show me the database schema",
        "Refactor this function to use async/await",
        "Run the test suite and report failures",
        "What does the authenticate middleware do?",
        "Create a new REST endpoint for user profiles",
    ]

    start = time.perf_counter()
    for sample in samples:
        scorer.score_intent(sample)
    elapsed = time.perf_counter() - start

    avg_ms = (elapsed / len(samples)) * 1000
    assert avg_ms < 200, f"Avg latency {avg_ms:.1f}ms exceeds 200ms budget"


def test_classify_semantic():
    """classify_semantic returns a 3-tuple (str, float, bool)."""
    result = classify_semantic("Fix the bug in auth module")
    assert isinstance(result, tuple)
    assert len(result) == 3
    assert isinstance(result[0], str)
    assert isinstance(result[1], float)
    assert isinstance(result[2], bool)
