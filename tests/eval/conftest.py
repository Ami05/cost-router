"""Unified conftest for tests/eval/ — merges classifierEval and evalFramework fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

pytestmark = pytest.mark.eval


# ---------------------------------------------------------------------------
# Classifier eval fixtures (from classifierEval/conftest.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def exemplars():
    with open(PROJECT_ROOT / "config" / "intent_exemplars.json") as f:
        return json.load(f)["exemplars"]


@pytest.fixture(scope="session")
def synthetic_samples():
    path = PROJECT_ROOT / "tests" / "data" / "production_synthetic.json"
    if not path.exists():
        pytest.skip("production_synthetic.json not available (gitignored)")
    with open(path) as f:
        return json.load(f)["samples"]


@pytest.fixture(scope="session")
def real_user_corpus():
    """Load the pre-generated real-user eval corpus. No API key required."""
    path = PROJECT_ROOT / "tests" / "data" / "real_user_corpus.json"
    with open(path) as f:
        data = json.load(f)
    return data["prompts"]


def pytest_generate_tests(metafunc):
    """Parametrize test_real_user_corpus_classification with corpus entries."""
    if "corpus_entry" in metafunc.fixturenames:
        corpus_path = PROJECT_ROOT / "tests" / "data" / "real_user_corpus.json"
        with open(corpus_path) as f:
            data = json.load(f)
        entries = data["prompts"]
        ids = [
            f"{e['intent']}__{e['type']}__{i}"
            for i, e in enumerate(entries)
        ]
        metafunc.parametrize("corpus_entry", entries, ids=ids)


@pytest.fixture(scope="session")
def all_intent_classes():
    return (
        "qa_simple",
        "qa_with_reads",
        "qa_architecture",
        "agentic_action",
        "content_generation",
        "dispatch_justification",
        "inline_narration",
    )


@pytest.fixture(scope="session")
def scorer():
    from semantic_scorer import SemanticScorer

    return SemanticScorer()


# ---------------------------------------------------------------------------
# EvalFramework fixtures (from evalFramework/conftest.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def eval_results():
    """Run full eval suite once per test session."""
    from .runner import run_eval

    return run_eval()


@pytest.fixture(scope="session")
def eval_summary(eval_results):
    from .scorer import strategy_summary

    return strategy_summary(eval_results)
