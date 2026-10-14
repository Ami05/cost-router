"""Consolidated classifier eval tests.

Groups:
  - Baseline accuracy (4 tests)
  - Cross-class confusion (4 tests)
  - Difficulty robustness (3 tests)
  - Incremental lift (4 tests)
  - Threshold sensitivity (2 tests)
"""

from collections import defaultdict

import pytest


# ---------------------------------------------------------------------------
# Baseline accuracy
# ---------------------------------------------------------------------------

def test_keyword_baseline_accuracy(exemplars):
    """Keyword classifier should achieve >= 30% overall accuracy on exemplars."""
    from classify_intent import classify_intent

    correct = 0
    for ex in exemplars:
        predicted = classify_intent(ex["text"])
        if predicted == ex["intent_class"]:
            correct += 1

    accuracy = correct / len(exemplars)
    assert accuracy >= 0.30, (
        f"Keyword baseline accuracy {accuracy:.2%} is below 30% threshold"
    )


def test_semantic_baseline_accuracy(exemplars):
    """Semantic classifier should achieve >= 88% overall accuracy on exemplars."""
    from classify_intent import classify_intent_detailed

    correct = 0
    for ex in exemplars:
        result = classify_intent_detailed(ex["text"])
        if result["intent"] == ex["intent_class"]:
            correct += 1

    accuracy = correct / len(exemplars)
    assert accuracy >= 0.88, (
        f"Semantic baseline accuracy {accuracy:.2%} is below 88% threshold"
    )


def test_no_class_at_zero_percent(exemplars, all_intent_classes):
    """Every class with exemplars should have > 0% accuracy via semantic classifier."""
    from classify_intent import classify_intent_detailed

    class_correct = defaultdict(int)
    class_total = defaultdict(int)

    for ex in exemplars:
        cls = ex["intent_class"]
        class_total[cls] += 1
        result = classify_intent_detailed(ex["text"])
        if result["intent"] == cls:
            class_correct[cls] += 1

    zero_classes = []
    for cls in all_intent_classes:
        if cls not in class_total:
            # No exemplars for this class — skip (content_generation expected)
            continue
        acc = class_correct[cls] / class_total[cls]
        if acc == 0.0:
            zero_classes.append(cls)

    assert not zero_classes, (
        f"Classes with 0% accuracy: {zero_classes}"
    )


def test_production_synthetic_overall(synthetic_samples):
    """Semantic classifier should achieve >= 69% on synthetic samples."""
    from classify_intent import classify_intent_detailed

    correct = 0
    for sample in synthetic_samples:
        result = classify_intent_detailed(sample["text"])
        if result["intent"] == sample["expected_intent"]:
            correct += 1

    accuracy = correct / len(synthetic_samples)
    assert accuracy >= 0.69, (
        f"Synthetic accuracy {accuracy:.2%} is below 69% threshold"
    )


# ---------------------------------------------------------------------------
# Per-category / per-confidence-band accuracy floors (TEST-002)
#
# Aggregate-only accuracy checks (above) can mask a single category
# regressing while the overall number stays flat. These floors are derived
# from the actual per-category / per-band accuracy on config/intent_exemplars.json
# as of 2026-09-27, minus a 2 percentage-point regression allowance:
#
#   agentic_action          92.86% -> floor 0.90
#   content_generation      83.33% -> floor 0.81
#   dispatch_justification  93.75% -> floor 0.91
#   inline_narration        81.25% -> floor 0.79
#   qa_architecture        100.00% -> floor 0.98
#   qa_simple               93.75% -> floor 0.91
#   qa_with_reads           89.47% -> floor 0.87
#
#   confidence band high (>=0.8)    89.71% -> floor 0.87
#   confidence band medium (0.5-0.8) 96.67% -> floor 0.94
#   confidence band low (<0.5)       90.91% -> floor 0.88
# ---------------------------------------------------------------------------

_PER_CATEGORY_ACCURACY_FLOOR = {
    "agentic_action": 0.90,
    "content_generation": 0.81,
    "dispatch_justification": 0.91,
    "inline_narration": 0.79,
    "qa_architecture": 0.98,
    "qa_simple": 0.91,
    "qa_with_reads": 0.87,
}

_PER_CONFIDENCE_BAND_ACCURACY_FLOOR = {
    "high": 0.87,
    "medium": 0.94,
    "low": 0.88,
}


def _confidence_band(confidence):
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def test_per_category_accuracy_floor(exemplars):
    """No intent category should regress more than 2pp from its baseline accuracy."""
    from classify_intent import classify_intent_detailed

    class_total = defaultdict(int)
    class_correct = defaultdict(int)

    for ex in exemplars:
        cls = ex["intent_class"]
        class_total[cls] += 1
        result = classify_intent_detailed(ex["text"])
        if result["intent"] == cls:
            class_correct[cls] += 1

    failures = []
    for cls, floor in _PER_CATEGORY_ACCURACY_FLOOR.items():
        total = class_total.get(cls, 0)
        if total == 0:
            continue
        acc = class_correct[cls] / total
        if acc < floor:
            failures.append((cls, f"{acc:.2%} < floor {floor:.0%}"))

    assert not failures, (
        f"Categories below their 2pp-regression accuracy floor: {failures}"
    )


def test_per_confidence_band_accuracy_floor(exemplars):
    """No confidence band (high/medium/low) should regress more than 2pp from baseline."""
    from classify_intent import classify_intent_detailed

    band_total = defaultdict(int)
    band_correct = defaultdict(int)

    for ex in exemplars:
        result = classify_intent_detailed(ex["text"])
        band = _confidence_band(result.get("confidence", 0))
        band_total[band] += 1
        if result["intent"] == ex["intent_class"]:
            band_correct[band] += 1

    failures = []
    for band, floor in _PER_CONFIDENCE_BAND_ACCURACY_FLOOR.items():
        total = band_total.get(band, 0)
        if total == 0:
            continue
        acc = band_correct[band] / total
        if acc < floor:
            failures.append((band, f"{acc:.2%} < floor {floor:.0%}"))

    assert not failures, (
        f"Confidence bands below their 2pp-regression accuracy floor: {failures}"
    )


# ---------------------------------------------------------------------------
# Cross-class confusion
# ---------------------------------------------------------------------------

def _build_confusion_matrix(exemplars, all_intent_classes):
    """Build a confusion matrix: actual -> predicted -> count."""
    from classify_intent import classify_intent_detailed

    matrix = {cls: defaultdict(int) for cls in all_intent_classes}
    for ex in exemplars:
        actual = ex["intent_class"]
        predicted = classify_intent_detailed(ex["text"])["intent"]
        if actual in matrix:
            matrix[actual][predicted] += 1
    return matrix


def test_confusion_matrix_shape(exemplars, all_intent_classes):
    """Confusion matrix should have all 7 classes as keys."""
    matrix = _build_confusion_matrix(exemplars, all_intent_classes)
    assert set(matrix.keys()) == set(all_intent_classes), (
        f"Missing classes in confusion matrix: "
        f"{set(all_intent_classes) - set(matrix.keys())}"
    )


def test_qa_simple_leakage(exemplars):
    """No class should have > 30% of its exemplars misclassified as qa_simple."""
    from classify_intent import classify_intent_detailed

    class_total = defaultdict(int)
    class_leak_to_qa_simple = defaultdict(int)

    for ex in exemplars:
        actual = ex["intent_class"]
        if actual == "qa_simple":
            continue  # Skip qa_simple itself
        class_total[actual] += 1
        predicted = classify_intent_detailed(ex["text"])["intent"]
        if predicted == "qa_simple":
            class_leak_to_qa_simple[actual] += 1

    leaky_classes = []
    for cls, total in class_total.items():
        leak_rate = class_leak_to_qa_simple[cls] / total
        if leak_rate > 0.30:
            leaky_classes.append((cls, f"{leak_rate:.0%}"))

    assert not leaky_classes, (
        f"Classes with > 30% leakage to qa_simple: {leaky_classes}"
    )


def test_dispatch_narration_separation(exemplars):
    """dispatch_justification and inline_narration cross-confusion should be < 20%."""
    from classify_intent import classify_intent_detailed

    dispatch_total = 0
    dispatch_as_narration = 0
    narration_total = 0
    narration_as_dispatch = 0

    for ex in exemplars:
        actual = ex["intent_class"]
        predicted = classify_intent_detailed(ex["text"])["intent"]

        if actual == "dispatch_justification":
            dispatch_total += 1
            if predicted == "inline_narration":
                dispatch_as_narration += 1
        elif actual == "inline_narration":
            narration_total += 1
            if predicted == "dispatch_justification":
                narration_as_dispatch += 1

    if dispatch_total == 0 or narration_total == 0:
        pytest.fail("Need both dispatch and narration exemplars")

    d_to_n_rate = dispatch_as_narration / dispatch_total
    n_to_d_rate = narration_as_dispatch / narration_total

    assert d_to_n_rate < 0.20, (
        f"dispatch -> narration confusion {d_to_n_rate:.0%} >= 20%"
    )
    assert n_to_d_rate < 0.20, (
        f"narration -> dispatch confusion {n_to_d_rate:.0%} >= 20%"
    )


def test_per_class_f1(exemplars, all_intent_classes):
    """All classes with exemplars should have F1 >= 0.4."""
    from classify_intent import classify_intent_detailed

    tp = defaultdict(int)
    fp = defaultdict(int)
    fn = defaultdict(int)
    classes_with_exemplars = set()

    for ex in exemplars:
        actual = ex["intent_class"]
        classes_with_exemplars.add(actual)
        predicted = classify_intent_detailed(ex["text"])["intent"]

        if predicted == actual:
            tp[actual] += 1
        else:
            fn[actual] += 1
            fp[predicted] += 1

    low_f1_classes = []
    for cls in all_intent_classes:
        if cls not in classes_with_exemplars:
            continue
        precision = tp[cls] / (tp[cls] + fp[cls]) if (tp[cls] + fp[cls]) > 0 else 0
        recall = tp[cls] / (tp[cls] + fn[cls]) if (tp[cls] + fn[cls]) > 0 else 0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0
        if f1 < 0.4:
            low_f1_classes.append((cls, f"{f1:.3f}"))

    assert not low_f1_classes, (
        f"Classes with F1 < 0.4: {low_f1_classes}"
    )


# ---------------------------------------------------------------------------
# Difficulty robustness
# ---------------------------------------------------------------------------

def _accuracy_by_difficulty(exemplars, difficulty):
    """Compute per-class accuracy for a specific difficulty level."""
    from classify_intent import classify_intent_detailed

    class_correct = defaultdict(int)
    class_total = defaultdict(int)

    for ex in exemplars:
        if ex.get("difficulty") != difficulty:
            continue
        cls = ex["intent_class"]
        class_total[cls] += 1
        result = classify_intent_detailed(ex["text"])
        if result["intent"] == cls:
            class_correct[cls] += 1

    return {
        cls: (class_correct[cls] / class_total[cls] if class_total[cls] > 0 else None)
        for cls in class_total
    }


def test_canonical_vs_paraphrase_gap(exemplars):
    """Semantic accuracy gap between canonical and paraphrase < 30pp per class."""
    canonical = _accuracy_by_difficulty(exemplars, "canonical")
    paraphrase = _accuracy_by_difficulty(exemplars, "paraphrase")

    big_gaps = []
    for cls in canonical:
        if cls not in paraphrase:
            continue
        if canonical[cls] is None or paraphrase[cls] is None:
            continue
        gap = canonical[cls] - paraphrase[cls]
        if gap > 0.30:
            big_gaps.append((cls, f"canonical={canonical[cls]:.0%}, "
                            f"paraphrase={paraphrase[cls]:.0%}, gap={gap:.0%}"))

    assert not big_gaps, (
        f"Classes with > 30pp canonical-paraphrase gap: {big_gaps}"
    )


def test_canonical_vs_adversarial_gap(exemplars):
    """Semantic accuracy gap between canonical and adversarial < 40pp per class."""
    canonical = _accuracy_by_difficulty(exemplars, "canonical")
    adversarial = _accuracy_by_difficulty(exemplars, "adversarial")

    big_gaps = []
    for cls in canonical:
        if cls not in adversarial:
            continue
        if canonical[cls] is None or adversarial[cls] is None:
            continue
        gap = canonical[cls] - adversarial[cls]
        if gap > 0.40:
            big_gaps.append((cls, f"canonical={canonical[cls]:.0%}, "
                            f"adversarial={adversarial[cls]:.0%}, gap={gap:.0%}"))

    assert not big_gaps, (
        f"Classes with > 40pp canonical-adversarial gap: {big_gaps}"
    )


def test_production_synthetic_by_perturbation(synthetic_samples):
    """No perturbation type should have accuracy < 30% overall."""
    from classify_intent import classify_intent_detailed

    perturb_correct = defaultdict(int)
    perturb_total = defaultdict(int)

    for sample in synthetic_samples:
        ptype = sample.get("perturbation_type", "clean")
        perturb_total[ptype] += 1
        result = classify_intent_detailed(sample["text"])
        if result["intent"] == sample["expected_intent"]:
            perturb_correct[ptype] += 1

    low_perturbations = []
    for ptype, total in perturb_total.items():
        acc = perturb_correct[ptype] / total
        if acc < 0.30:
            low_perturbations.append((ptype, f"{acc:.0%}"))

    assert not low_perturbations, (
        f"Perturbation types with < 30% accuracy: {low_perturbations}"
    )


# ---------------------------------------------------------------------------
# Incremental lift
# ---------------------------------------------------------------------------

def test_config_fix_lifts_accuracy(exemplars):
    """classify_intent_detailed should outperform classify_intent on dispatch/narration."""
    from classify_intent import classify_intent, classify_intent_detailed

    target_classes = {"dispatch_justification", "inline_narration"}
    target_exemplars = [e for e in exemplars if e["intent_class"] in target_classes]

    if not target_exemplars:
        pytest.fail("No dispatch/narration exemplars found")

    kw_correct = 0
    sem_correct = 0
    for ex in target_exemplars:
        kw_pred = classify_intent(ex["text"])
        sem_result = classify_intent_detailed(ex["text"])
        if kw_pred == ex["intent_class"]:
            kw_correct += 1
        if sem_result["intent"] == ex["intent_class"]:
            sem_correct += 1

    kw_acc = kw_correct / len(target_exemplars)
    sem_acc = sem_correct / len(target_exemplars)
    assert sem_acc >= kw_acc, (
        f"Semantic ({sem_acc:.2%}) should beat keyword ({kw_acc:.2%}) "
        f"on dispatch/narration exemplars"
    )


def test_decision_gate_1(synthetic_samples):
    """Synthetic accuracy should reach >= 55% (Gate 1)."""
    from classify_intent import classify_intent_detailed

    correct = sum(
        1
        for s in synthetic_samples
        if classify_intent_detailed(s["text"])["intent"] == s["expected_intent"]
    )
    accuracy = correct / len(synthetic_samples)
    assert accuracy >= 0.55, f"Gate 1: synthetic accuracy {accuracy:.2%} < 55%"


def test_decision_gate_2(synthetic_samples):
    """Synthetic accuracy should reach >= 70% (Gate 2)."""
    from classify_intent import classify_intent_detailed

    correct = sum(
        1
        for s in synthetic_samples
        if classify_intent_detailed(s["text"])["intent"] == s["expected_intent"]
    )
    accuracy = correct / len(synthetic_samples)
    assert accuracy >= 0.70, f"Gate 2: synthetic accuracy {accuracy:.2%} < 70%"


def test_convergence_ceiling(exemplars):
    """Exemplar accuracy should exceed 0.88 (achieved 93.6% minus 5% margin)."""
    from classify_intent import classify_intent_detailed

    correct = sum(
        1
        for e in exemplars
        if classify_intent_detailed(e["text"])["intent"] == e["intent_class"]
    )
    acc = correct / len(exemplars)
    assert acc >= 0.88, f"Exemplar accuracy {acc:.4f} < 0.88"


# ---------------------------------------------------------------------------
# Threshold sensitivity
# ---------------------------------------------------------------------------

def test_threshold_sweep(exemplars, all_intent_classes, scorer):
    """Configured threshold should be within 0.15 of optimal per class."""
    import json
    from pathlib import Path

    thresholds_path = PROJECT_ROOT / "config" / "semantic_thresholds.json"
    configured = json.loads(thresholds_path.read_text())
    per_class_thresholds = configured.get("per_class", {})
    default_threshold = configured.get("default_threshold", 0.6)

    class_exemplars = defaultdict(list)
    for ex in exemplars:
        class_exemplars[ex["intent_class"]].append(ex)

    sweep_values = [round(0.3 + i * 0.05, 2) for i in range(9)]  # 0.30 to 0.70

    scored = []
    for ex in exemplars:
        scores = scorer.score_intent(ex["text"])
        top_class = max(scores, key=scores.get)
        top_score = scores[top_class]
        scored.append((ex["intent_class"], top_class, top_score))

    bad_classes = []
    for cls in all_intent_classes:
        if cls not in class_exemplars:
            continue

        best_threshold = None
        best_f1 = -1.0

        for t in sweep_values:
            tp = fp = fn = 0
            for actual, predicted, score in scored:
                pred_cls = predicted if (predicted == cls and score >= t) else None
                if actual == cls and pred_cls == cls:
                    tp += 1
                elif actual != cls and pred_cls == cls:
                    fp += 1
                elif actual == cls and pred_cls != cls:
                    fn += 1

            precision = tp / (tp + fp) if (tp + fp) > 0 else 0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0
            f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0

            if f1 > best_f1 or (f1 == best_f1 and best_threshold is None):
                best_f1 = f1
                best_threshold = t

        configured_t = per_class_thresholds.get(cls, default_threshold)
        if best_threshold is not None and abs(configured_t - best_threshold) > 0.15:
            bad_classes.append(
                (cls, f"configured={configured_t}, optimal={best_threshold}, "
                 f"gap={abs(configured_t - best_threshold):.2f}")
            )

    assert not bad_classes, (
        f"Classes with threshold > 0.15 from optimal: {bad_classes}"
    )


def test_fallback_path_coverage(exemplars):
    """classify_intent_detailed should use fallback for at least 1 input."""
    from classify_intent import classify_intent_detailed

    fallback_seen = False
    for ex in exemplars:
        result = classify_intent_detailed(ex["text"])
        if result["source"] == "fallback":
            fallback_seen = True
            break

    if not fallback_seen:
        result = classify_intent_detailed("hmm ok sure thing")
        if result["source"] == "fallback":
            fallback_seen = True

    assert fallback_seen, (
        "No input triggered the fallback path (source='fallback')"
    )


# ---------------------------------------------------------------------------
# Real-user corpus parametrized test
# ---------------------------------------------------------------------------

def test_real_user_corpus_classification(corpus_entry):
    """Each real-user corpus entry must classify to its declared intent.

    corpus_entry is injected via pytest_generate_tests in conftest.py.
    The corpus (tests/data/real_user_corpus.json) is pre-generated and committed
    — no ANTHROPIC_API_KEY required at runtime.

    Uses classify_intent_detailed (hybrid keyword + semantic) so that informal,
    shorthand, and boundary prompts get the best available classifier.
    """
    from classify_intent import classify_intent_detailed

    text = corpus_entry["prompt"]
    expected = corpus_entry["intent"]
    ptype = corpus_entry.get("type", "unknown")
    notes = corpus_entry.get("notes", "")

    result = classify_intent_detailed(text)
    predicted = result["intent"]

    if corpus_entry.get("known_gap"):
        assert predicted != expected, (
            f"known_gap entry now passes; remove known_gap flag: {text!r}"
        )
        pytest.xfail(f"{corpus_entry.get('known_gap_ticket', 'known gap')}: predicted {predicted}, expected {expected}")

    assert predicted == expected, (
        f"Misclassified [{ptype}] prompt: {text!r}\n"
        f"  Expected : {expected}\n"
        f"  Got      : {predicted} (source={result['source']}, confidence={result.get('confidence', '?')})\n"
        f"  Notes    : {notes}"
    )


def test_real_user_corpus_meta_consistent():
    """meta.total / classes / prompts_per_class must match the actual prompts."""
    import json
    from collections import Counter

    data = json.load(open(PROJECT_ROOT / "tests" / "data" / "real_user_corpus.json"))
    prompts, meta = data["prompts"], data["meta"]
    counts = Counter(p["intent"] for p in prompts)
    assert meta["total"] == len(prompts), f"meta.total {meta['total']} != {len(prompts)}"
    assert sorted(meta["classes"]) == sorted(counts), "meta.classes != intents present"
    assert meta["prompts_per_class"] == dict(counts), "meta.prompts_per_class mismatch"


def _norm_text(t):
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", t.lower())).strip()


def test_no_overlap_between_exemplars_scenarios_corpus():
    """Normalized, no equal/near-duplicate (ratio >= 0.9) across exemplars, scenarios, corpus."""
    import difflib
    import json

    root = PROJECT_ROOT
    ex = json.load(open(root / "config" / "intent_exemplars.json"))["exemplars"]
    sc = json.load(open(root / "tests" / "intent_eval_scenarios.json"))["scenarios"]
    co = json.load(open(root / "tests" / "data" / "real_user_corpus.json"))["prompts"]
    exemplar_texts = [_norm_text(e["text"]) for e in ex]
    # Scope: entries added by T1 (notes start with "T1"); older scenarios/corpus
    # entries pre-date this check and overlap exemplars (tracked for T2).
    held = [("scenario " + s["id"], _norm_text(s["prompt"])) for s in sc
            if s.get("notes", "").startswith("T1")]
    held += [(f"corpus[{i}]", _norm_text(c["prompt"])) for i, c in enumerate(co)
             if c.get("notes", "").startswith("T1")]

    # Pre-T1 near-duplicates inside the exemplar set itself are tracked by T2; only
    # held-out vs exemplar and held-out vs held-out are checked here.
    offenders = []
    for i, (name, t) in enumerate(held):
        for e in exemplar_texts:
            if difflib.SequenceMatcher(None, t, e).ratio() >= 0.9:
                offenders.append((name, "exemplar", t))
                break
        for name2, t2 in held[:i]:
            if difflib.SequenceMatcher(None, t, t2).ratio() >= 0.9:
                offenders.append((name, name2, t))
                break
    assert not offenders, f"overlap: {offenders[:5]}"


@pytest.mark.parametrize("prompt", [
    "",
    "   \n\t  ",
    "x" * 2000,
    "word " * 400,
    "このテストを修正してください",
    "¿Cómo funciona el sistema de enrutamiento?",
    "请评估这个方案的成本影响",
])
def test_edge_prompts_return_valid_intent(prompt):
    from classify_intent import classify_intent_detailed

    valid = {
        "agentic_action", "qa_architecture", "analytical_qa", "qa_with_reads",
        "qa_simple", "content_generation", "dispatch_justification", "inline_narration",
    }
    result = classify_intent_detailed(prompt)
    assert result["intent"] in valid


# ---------------------------------------------------------------------------
# T13: semantic-upgrade threshold boundary + keyword-corroboration branch
# (classify_intent_detailed, mocked semantic scorer; behavior asserted as implemented)
# ---------------------------------------------------------------------------

class _StubScorer:
    """Returns a fixed (intent, confidence, was_fallback) from resolve_with_fallback."""

    def __init__(self, intent, confidence, was_fallback=False):
        self._r = (intent, confidence, was_fallback)

    def resolve_with_fallback(self, _text):
        return self._r


def _detailed(monkeypatch, text, intent, confidence, was_fallback=False):
    import classify_intent as ci

    monkeypatch.setattr(ci, "_get_semantic_scorer",
                        lambda: _StubScorer(intent, confidence, was_fallback))
    return ci.classify_intent_detailed(text)


def test_upgrade_threshold_constant_is_040():
    import classify_intent as ci

    assert ci._SEMANTIC_UPGRADE_THRESHOLD == 0.40


@pytest.mark.parametrize("confidence, was_fallback, expected_intent, expected_source", [
    (0.39, False, "qa_simple", "semantic"),         # below: blocked upgrade, source keeps semantic
    (0.39, True, "qa_simple", "fallback"),          # below + scorer fallback flag -> fallback
    (0.40, False, "agentic_action", "semantic"),    # exactly 0.40 is inclusive: upgrade allowed
    (0.40, True, "agentic_action", "fallback"),
    (0.41, False, "agentic_action", "semantic"),
    (0.41, True, "agentic_action", "fallback"),
])
def test_confidence_boundary_039_040_041(monkeypatch, confidence, was_fallback,
                                         expected_intent, expected_source):
    # "hello there": keyword -> qa_simple, no keyword hits at all, no '?': no corroboration override
    r = _detailed(monkeypatch, "hello there", "agentic_action", confidence, was_fallback)
    assert r["intent"] == expected_intent
    assert r["source"] == expected_source
    assert r["confidence"] == confidence


def test_confidence_exactly_040_is_not_downgraded_but_0399_is(monkeypatch):
    up = _detailed(monkeypatch, "hello there", "qa_architecture", 0.40)
    down = _detailed(monkeypatch, "hello there", "qa_architecture", 0.3999999)
    assert up["intent"] == "qa_architecture"
    assert down["intent"] == "qa_simple"


def test_scorer_qa_simple_passes_through_regardless_of_confidence(monkeypatch):
    low = _detailed(monkeypatch, "hello there", "qa_simple", 0.05)
    assert low == {"intent": "qa_simple", "confidence": 0.05, "source": "semantic"}
    fb = _detailed(monkeypatch, "hello there", "qa_simple", 0.05, True)
    assert fb["source"] == "fallback"


def test_keyword_hit_bypasses_semantic_scorer_entirely(monkeypatch):
    import classify_intent as ci

    def boom():
        raise AssertionError("semantic scorer must not be consulted on a keyword hit")

    monkeypatch.setattr(ci, "_get_semantic_scorer", boom)
    r = ci.classify_intent_detailed("Fix the regex in enforce_role_gates.py")
    assert r == {"intent": "agentic_action", "confidence": 1.0, "source": "keyword"}


class TestKeywordCorroborationOverride:
    """Semantic upgrades lacking keyword corroboration are blocked (classify_intent ~701-720)."""

    def test_question_mark_with_zero_keyword_hits_blocks_upgrade(self, monkeypatch):
        r = _detailed(monkeypatch, "hello there?", "qa_architecture", 0.95)
        assert r["intent"] == "qa_simple"
        assert r["source"] == "semantic"

    def test_question_mark_with_qa_simple_keywords_blocks_upgrade(self, monkeypatch):
        r = _detailed(monkeypatch, "what is this thing?", "content_generation", 0.95)
        assert r["intent"] == "qa_simple"

    def test_no_question_mark_and_zero_hits_allows_upgrade(self, monkeypatch):
        r = _detailed(monkeypatch, "hello there", "qa_architecture", 0.95)
        assert r["intent"] == "qa_architecture"

    def test_no_question_mark_with_qa_simple_hits_allows_non_agentic_upgrade(self, monkeypatch):
        # opener "tell" is not an interrogative, so the missing '?' means no question-based block
        r = _detailed(monkeypatch, "tell me what this thing is", "qa_architecture", 0.95)
        assert r["intent"] == "qa_architecture"

    @pytest.mark.parametrize("text", ["what is this thing", "is the cache enabled by default",
                                      "whats the difference between FO and RB roles"])
    def test_yes_no_and_what_is_openers_count_as_questions_without_question_mark(self, monkeypatch, text):
        r = _detailed(monkeypatch, text, "qa_architecture", 0.95)
        assert r["intent"] == "qa_simple"

    @pytest.mark.parametrize("text", ["how do all these pieces fit together", "where: tie priority"])
    def test_open_wh_openers_are_not_treated_as_simple_questions(self, monkeypatch, text):
        # how/where/why... questions are often architecture or file reads; no keyword block
        r = _detailed(monkeypatch, text, "qa_architecture", 0.95)
        assert r["intent"] == "qa_architecture"

    def test_semantic_only_analytical_qa_upgrade_requires_keyword_support(self, monkeypatch):
        # no analytical signal in the text -> semantic analytical_qa is blocked (T19)
        r = _detailed(monkeypatch, "hello there", "analytical_qa", 0.95)
        assert r["intent"] == "qa_simple"

    def test_agentic_upgrade_blocked_without_imperative_verb(self, monkeypatch):
        r = _detailed(monkeypatch, "what is this thing", "agentic_action", 0.95)
        assert r["intent"] == "qa_simple"

    def test_keyword_support_for_scorer_class_overrides_block(self, monkeypatch):
        # qa_with_reads has keyword score 1.0 here, so even a '?' ending does not downgrade it
        text = "I haven't used this project before \u2014 what does it actually do?"
        r = _detailed(monkeypatch, text, "qa_with_reads", 0.95)
        assert r["intent"] == "qa_with_reads"

    def test_keyword_support_without_question_mark(self, monkeypatch):
        r = _detailed(monkeypatch, "How many signals are there, and show me the full list.",
                      "qa_with_reads", 0.95)
        assert r["intent"] == "qa_with_reads"

    def test_override_runs_before_threshold_check(self, monkeypatch):
        # blocked by corroboration -> qa_simple; low confidence then does not matter
        r = _detailed(monkeypatch, "hello there?", "qa_architecture", 0.10, True)
        assert r["intent"] == "qa_simple"
        assert r["source"] == "fallback"


# make PROJECT_ROOT available in this module scope for test_threshold_sweep
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
