"""Eval tests for intent classifier accuracy.

Implements the validation layer from the classifier accuracy spike report:
  1. Semantic scorer accuracy — all 7 classes via classify_intent_detailed()
  2. Per-class precision/recall — dispatch_justification and inline_narration
  3. Threshold sensitivity sweep — 0.3 to 0.7
  4. Fallback path coverage — each class falls back correctly when below threshold
  5. Paraphrase robustness — per-class breakdown: canonical vs paraphrase vs adversarial
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

import eval_option_d  # noqa: E402  (floors + min-support guard live here)
import heldout  # noqa: E402
from classify_intent import classify_intent_detailed  # noqa: E402
from semantic_scorer import SemanticScorer  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent.parent
EXEMPLAR_PATH = ROOT / "config" / "intent_exemplars.json"
THRESHOLD_PATH = ROOT / "config" / "semantic_thresholds.json"

_INTENTS = (
    "qa_simple",
    "qa_with_reads",
    "qa_architecture",
    "agentic_action",
    "content_generation",
    "dispatch_justification",
    "inline_narration",
    "analytical_qa",
)

# Load once at module import — exemplars and thresholds are static config files
_ALL_EXEMPLARS: list[dict] = json.loads(EXEMPLAR_PATH.read_text())["exemplars"]
_THRESHOLDS: dict = json.loads(THRESHOLD_PATH.read_text())

# T2: ACCURACY is measured on the held-out set (real_user_corpus + deduped
# production_synthetic), never on the exemplars the semantic scorer was built from.
# _ALL_EXEMPLARS stays only for structural/config checks (counts, source, fallback).
_HELDOUT: list[dict] = heldout.load_heldout()
_FLOORS: dict = eval_option_d.load_floors()


@pytest.fixture(autouse=True, scope="module")
def _heldout_not_empty():
    assert _HELDOUT, "held-out set is empty; accuracy tests must not fall back to exemplars"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singleton():
    SemanticScorer.reset_instance()
    yield
    SemanticScorer.reset_instance()


@pytest.fixture
def scorer():
    return SemanticScorer.get_instance(EXEMPLAR_PATH)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PRED_CACHE: dict[str, str] = {}


def _predict(text: str) -> str:
    # classify_intent_detailed is deterministic; memoize so 500+ held-out texts are scored once.
    if text not in _PRED_CACHE:
        _PRED_CACHE[text] = classify_intent_detailed(text)["intent"]
    return _PRED_CACHE[text]


def _classify_all(exemplars: list[dict]) -> list[tuple[str, str]]:
    """Return list of (predicted, actual) pairs."""
    return [(_predict(e["text"]), e["intent_class"]) for e in exemplars]


def _accuracy(pairs: list[tuple[str, str]]) -> float:
    if not pairs:
        return 0.0
    return sum(1 for pred, actual in pairs if pred == actual) / len(pairs)


# ---------------------------------------------------------------------------
# 1 — Semantic scorer accuracy across all 7 classes
# ---------------------------------------------------------------------------


class TestSemanticScorerAccuracy:
    """SEM-ACC-01: classify_intent_detailed() accuracy across all 7 intent classes."""

    def test_overall_accuracy_on_heldout(self):
        acc = _accuracy(_classify_all(_HELDOUT))
        assert acc >= eval_option_d.OPTION_D_WARRANTED_BELOW["heldout_accuracy"], (
            f"Held-out overall accuracy {acc:.1%} below floor"
        )

    @pytest.mark.parametrize("intent", _INTENTS)
    def test_per_class_recall_floor_on_heldout(self, intent):
        subset = [e for e in _HELDOUT if e["intent_class"] == intent]
        floor = _FLOORS[intent]
        assert len(subset) >= floor["min_support"], (
            f"{intent} held-out support {len(subset)} < min {floor['min_support']}"
        )
        recall = _accuracy(_classify_all(subset))
        assert recall >= floor["recall"], f"{intent} held-out recall {recall:.1%} below floor {floor['recall']:.0%}"

    def test_source_field_is_valid(self):
        """classify_intent_detailed must return a known source for every exemplar."""
        valid = {"keyword", "semantic", "fallback"}
        for ex in _ALL_EXEMPLARS:
            result = classify_intent_detailed(ex["text"])
            assert result["source"] in valid, (
                f"Unknown source '{result['source']}' for: {ex['text'][:60]}"
            )

    def test_confidence_is_normalised(self):
        """Confidence scores must be in [0, 1]."""
        for ex in _ALL_EXEMPLARS[:20]:  # spot-check first 20
            result = classify_intent_detailed(ex["text"])
            assert 0.0 <= result["confidence"] <= 1.0, (
                f"Confidence {result['confidence']} out of range for: {ex['text'][:60]}"
            )


# ---------------------------------------------------------------------------
# 2 — Per-class precision/recall for dispatch_justification and inline_narration
# ---------------------------------------------------------------------------


class TestNewClassPrecisionRecall:
    """SEM-ACC-02: Precision and recall for the two new intent classes."""

    @pytest.mark.parametrize("target", ["dispatch_justification", "inline_narration"])
    def test_recall_floor(self, target):
        true_pos = [e for e in _HELDOUT if e["intent_class"] == target]
        assert len(true_pos) >= _FLOORS[target]["min_support"], f"Too few held-out samples for {target}"
        correct = sum(
            1 for e in true_pos if _predict(e["text"]) == target
        )
        recall = correct / len(true_pos)
        assert recall >= _FLOORS[target]["recall"], f"{target} recall {recall:.1%} below floor"

    @pytest.mark.parametrize("target", ["dispatch_justification", "inline_narration"])
    def test_precision_floor(self, target):
        predicted_as_target = [
            e for e in _HELDOUT
            if _predict(e["text"]) == target
        ]
        assert predicted_as_target, f"Classifier never predicts {target}"
        correct = sum(1 for e in predicted_as_target if e["intent_class"] == target)
        precision = correct / len(predicted_as_target)
        assert precision >= _FLOORS[target]["precision"], f"{target} precision {precision:.1%} below floor"

    @pytest.mark.parametrize("target", ["dispatch_justification", "inline_narration"])
    def test_exemplar_count_sufficient(self, target):
        """New classes need ≥ 15 exemplars for reliable TF-IDF scoring."""
        count = sum(1 for e in _ALL_EXEMPLARS if e["intent_class"] == target)
        assert count >= 15, f"{target} has only {count} exemplars — add more for robust scoring"


# ---------------------------------------------------------------------------
# 3 — Threshold sensitivity sweep (0.3 – 0.7)
# ---------------------------------------------------------------------------


class TestThresholdSensitivity:
    """SEM-ACC-03: Accuracy and fallback rate across threshold sweep 0.3 to 0.7."""

    _SWEEP_CACHE: dict[float, dict] = {}

    def _sweep(self, scorer, threshold: float) -> dict:
        if threshold in self._SWEEP_CACHE:
            return self._SWEEP_CACHE[threshold]
        overrides = {
            "default_threshold": threshold,
            "per_class": {intent: threshold for intent in _INTENTS},
            "fallback_map": _THRESHOLDS["fallback_map"],
        }
        correct = fallbacks = 0
        for ex in _HELDOUT:
            intent, _, was_fallback = scorer.resolve_with_fallback(
                ex["text"], thresholds=overrides
            )
            if intent == ex["intent_class"]:
                correct += 1
            if was_fallback:
                fallbacks += 1
        n = len(_HELDOUT)
        self._SWEEP_CACHE[threshold] = {"accuracy": correct / n, "fallback_rate": fallbacks / n}
        return self._SWEEP_CACHE[threshold]

    def test_lower_threshold_does_not_degrade_accuracy(self, scorer):
        low = self._sweep(scorer, 0.3)
        high = self._sweep(scorer, 0.7)
        # Allow 5% slack — lower threshold should not lose accuracy by more than that
        assert low["accuracy"] >= high["accuracy"] - 0.05, (
            f"Threshold 0.3 accuracy {low['accuracy']:.1%} "
            f"unexpectedly below threshold 0.7 accuracy {high['accuracy']:.1%}"
        )

    def test_higher_threshold_increases_fallback_rate(self, scorer):
        low = self._sweep(scorer, 0.3)
        high = self._sweep(scorer, 0.7)
        assert high["fallback_rate"] >= low["fallback_rate"], (
            "Expected threshold 0.7 to produce more fallbacks than threshold 0.3"
        )

    # Reference only (not asserted): semantic-only held-out accuracy at 0.3/0.4/0.5 measured
    # 60.7%/45.6%/29.7%. Falls as the threshold rises because low-confidence results fall
    # back to qa_simple by construction.
    SEMANTIC_ONLY_BASELINE = {0.3: 0.607, 0.4: 0.456, 0.5: 0.297}
    MIN_HELDOUT_N = 500

    def test_hybrid_accuracy_above_85_pct_at_every_threshold(self, monkeypatch):
        """T17: 85% floor at every threshold, measured on the production hybrid path.

        A semantic-only sweep is not a meaningful gate: production never serves the
        scorer alone (keyword hits short-circuit it and keyword corroboration gates
        semantic upgrades), and semantic-only accuracy decays with threshold by design
        (see SEMANTIC_ONLY_BASELINE). The same 85% floor is therefore enforced on
        classify_intent_detailed with the threshold injected into the scorer it uses.
        The held-out size is asserted so the loop cannot pass vacuously.
        """
        import classify_intent as ci

        assert len(_HELDOUT) >= self.MIN_HELDOUT_N, (
            f"held-out set too small ({len(_HELDOUT)} < {self.MIN_HELDOUT_N}); sweep would be weak"
        )
        sc = ci._get_semantic_scorer()
        orig = sc.resolve_with_fallback
        for threshold in (0.3, 0.4, 0.5, 0.6, 0.7):
            overrides = {
                "default_threshold": threshold,
                "per_class": {intent: threshold for intent in _INTENTS},
                "fallback_map": _THRESHOLDS["fallback_map"],
            }
            monkeypatch.setattr(
                sc, "resolve_with_fallback",
                lambda text, thresholds=None, _o=orig, _ov=overrides: _o(text, thresholds=_ov),
            )
            correct = sum(
                1 for ex in _HELDOUT
                if ci.classify_intent_detailed(ex["text"])["intent"] == ex["intent_class"]
            )
            acc = correct / len(_HELDOUT)
            assert acc >= 0.85, f"Hybrid accuracy {acc:.1%} at threshold {threshold} fell below 85%"


# ---------------------------------------------------------------------------
# 4 — Fallback path coverage
# ---------------------------------------------------------------------------


class TestFallbackPathCoverage:
    """SEM-ACC-04: Each class falls back correctly when confidence is below threshold."""

    def test_all_classes_have_fallback_entry(self):
        fallback_map = _THRESHOLDS.get("fallback_map", {})
        for intent in _INTENTS:
            assert intent in fallback_map, f"{intent} missing from fallback_map"

    def test_fallback_targets_are_valid_intent_classes(self):
        for cls, target in _THRESHOLDS["fallback_map"].items():
            if cls.startswith("_"):  # skip _doc and other metadata keys
                continue
            assert target in _INTENTS, (
                f"{cls} fallback target '{target}' is not a valid intent class"
            )

    def test_dispatch_justification_fallback_is_agentic_action(self):
        assert _THRESHOLDS["fallback_map"]["dispatch_justification"] == "agentic_action", (
            "dispatch_justification should fall back to agentic_action per semantic_thresholds.json"
        )

    def test_inline_narration_fallback_is_qa_simple(self):
        assert _THRESHOLDS["fallback_map"]["inline_narration"] == "qa_simple", (
            "inline_narration should fall back to qa_simple per semantic_thresholds.json"
        )

    def test_strict_threshold_triggers_fallbacks(self, scorer):
        """At threshold 0.99 most calls should fall back to their mapped class."""
        strict = {
            "default_threshold": 0.99,
            "per_class": {intent: 0.99 for intent in _INTENTS},
            "fallback_map": _THRESHOLDS["fallback_map"],
        }
        probes = [
            "What does this function do?",
            "Read the config file and explain the routing",
            "Dispatch a subagent to handle this task",
            "The agent is now writing the implementation plan",
        ]
        fallback_count = sum(
            1 for text in probes if scorer.resolve_with_fallback(text, thresholds=strict)[2]
        )
        assert fallback_count >= len(probes) // 2, (
            f"Expected ≥{len(probes) // 2} fallbacks at threshold 0.99, got {fallback_count}"
        )

    def test_fallback_never_returns_unknown_class(self, scorer):
        """resolve_with_fallback must always return a known intent class."""
        for ex in _ALL_EXEMPLARS[:30]:  # spot-check
            intent, _, _ = scorer.resolve_with_fallback(ex["text"])
            assert intent in _INTENTS, (
                f"resolve_with_fallback returned unknown class '{intent}'"
            )


# ---------------------------------------------------------------------------
# 5 — Paraphrase robustness
# ---------------------------------------------------------------------------


class TestParaphraseRobustness:
    """SEM-ACC-05: Per-class accuracy breakdown by difficulty level."""

    DIFFICULTIES = ("canonical", "paraphrase", "adversarial")  # held-out tiers via heldout.tier()

    def test_canonical_accuracy_above_70_pct(self):
        canonical = [e for e in _HELDOUT if e.get("difficulty") == "canonical"]
        if not canonical:
            pytest.fail("No canonical held-out samples")
        acc = _accuracy(_classify_all(canonical))
        assert acc >= 0.70, f"Canonical accuracy {acc:.1%} below 70%"

    def test_canonical_not_worse_than_adversarial_per_class(self):
        """Canonical accuracy should be >= adversarial accuracy (±10% slack) per class."""
        for intent in _INTENTS:
            canon = [
                e for e in _HELDOUT
                if e["intent_class"] == intent and e.get("difficulty") == "canonical"
            ]
            advers = [
                e for e in _HELDOUT
                if e["intent_class"] == intent and e.get("difficulty") == "adversarial"
            ]
            if not canon or not advers:
                continue
            canon_acc = _accuracy(_classify_all(canon))
            advers_acc = _accuracy(_classify_all(advers))
            assert canon_acc >= advers_acc - 0.10, (
                f"{intent}: canonical {canon_acc:.1%} more than 10% below "
                f"adversarial {advers_acc:.1%}"
            )

    @pytest.mark.parametrize("target", ["dispatch_justification", "inline_narration"])
    def test_new_class_canonical_above_50_pct(self, target):
        canonical = [
            e for e in _HELDOUT
            if e["intent_class"] == target and e.get("difficulty") == "canonical"
        ]
        if not canonical:
            pytest.fail(f"No canonical held-out samples for {target}")
        acc = _accuracy(_classify_all(canonical))
        assert acc >= 0.50, f"{target} canonical accuracy {acc:.1%} below 50%"

    @pytest.mark.parametrize("target", ["dispatch_justification", "inline_narration"])
    def test_new_class_has_all_difficulty_levels(self, target):
        """Both new classes must have exemplars at every difficulty level."""
        for diff in self.DIFFICULTIES:
            subset = [
                e for e in _ALL_EXEMPLARS
                if e["intent_class"] == target and e.get("difficulty") == diff
            ]
            assert len(subset) >= 1, (
                f"{target} has no '{diff}' difficulty exemplars — add at least one"
            )
