"""T2/T3: held-out split integrity and per-class floor verdict.

No API key. Exemplars train the semantic scorer; accuracy is measured only on
held-out data (real_user_corpus + deduped production_synthetic).
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import eval_option_d  # noqa: E402
import heldout  # noqa: E402


def test_heldout_set_not_empty_and_covers_all_8_intents():
    ho = heldout.load_heldout()
    assert ho, "held-out set is empty"
    present = {h["intent_class"] for h in ho}
    assert present == set(heldout.INTENTS)


def test_empty_heldout_fails_not_skips(monkeypatch):
    monkeypatch.setattr(heldout, "load_heldout", lambda: [])
    with pytest.raises(RuntimeError, match="held-out set is empty"):
        eval_option_d.load_heldout_items()


@pytest.mark.parametrize("direction", [
    "heldout_vs_exemplars", "exemplars_vs_heldout",
    "scenarios_vs_exemplars", "exemplars_vs_scenarios",
    "scenarios_vs_heldout", "heldout_vs_scenarios",
])
def test_zero_overlap_in_every_direction(direction):
    """Normalized (case/punctuation/whitespace) equality or difflib >= 0.9 counts as overlap."""
    overlaps = heldout.overlap_report()[direction]
    assert not overlaps, f"{direction}: {len(overlaps)} overlaps, e.g. {overlaps[:3]}"


def test_overlap_detector_catches_known_duplicate():
    """Guard the guard: a trivially reformatted copy must be detected."""
    a = [("a", "Is the fail-open behavior intentional?")]
    b = [("b", "is  the fail open behavior   intentional")]
    assert heldout.find_overlaps(a, b)


def test_heldout_synthetic_deduped_against_exemplars():
    ex = [heldout.norm(e["text"]) for e in heldout.load_exemplars()]
    for h in heldout.load_heldout():
        n = heldout.norm(h["text"])
        assert not any(heldout.near_dup(n, e) for e in ex), h["text"]


def test_baseline_has_per_class_table_for_all_8_intents():
    base = json.loads(eval_option_d.BASELINE_PATH.read_text())
    per_class = base["heldout"]["score"]["per_class"]
    assert set(per_class) == set(heldout.INTENTS)
    assert set(base["floors"]) == set(heldout.INTENTS)
    for c in heldout.INTENTS:
        assert per_class[c]["support"] >= base["floors"][c]["min_support"], c


def test_committed_baseline_is_not_stale():
    """Re-scoring the held-out set must reproduce the committed per-class table."""
    base = json.loads(eval_option_d.BASELINE_PATH.read_text())
    items = eval_option_d.load_heldout_items()
    live = eval_option_d.score_population(eval_option_d.run_population(items, "text", "label"))
    assert live["per_class"] == base["heldout"]["score"]["per_class"], (
        "option_d_eval.json is stale; rerun: python3 tests/eval/eval_option_d.py --write-baseline"
    )


def _held_score():
    base = json.loads(eval_option_d.BASELINE_PATH.read_text())
    return base["heldout"]["score"], base["floors"]


def test_no_class_below_its_floor_at_baseline():
    score, floors = _held_score()
    assert eval_option_d.class_floor_failures(score["per_class"], floors) == []


@pytest.mark.parametrize("cls", heldout.INTENTS)
def test_verdict_fails_when_single_class_drops(cls):
    """Aggregate stays perfect; one class under its floor must still flip the verdict."""
    score, floors = _held_score()
    per_class = copy.deepcopy(score["per_class"])
    per_class[cls]["recall"] = max(0.0, floors[cls]["recall"] - 0.05)
    held = {"accuracy": 1.0, "per_class": per_class}
    ok = {"accuracy": 1.0}
    verdict = eval_option_d.evaluate_option_d_warranted(ok, held, ok, ok, floors)
    assert verdict["gaps"]["class_below_floor"] is True
    assert verdict["verdict"].startswith("GO")
    assert [f["class"] for f in verdict["class_failures"]] == [cls]


def test_verdict_fails_on_insufficient_support():
    score, floors = _held_score()
    per_class = copy.deepcopy(score["per_class"])
    per_class["analytical_qa"]["support"] = floors["analytical_qa"]["min_support"] - 1
    failures = eval_option_d.class_floor_failures(per_class, floors)
    assert any("support" in f["reason"] for f in failures)


def test_verdict_holds_when_all_classes_pass():
    score, floors = _held_score()
    ok = {"accuracy": 1.0}
    held = {"accuracy": 1.0, "per_class": score["per_class"]}
    verdict = eval_option_d.evaluate_option_d_warranted(ok, held, ok, ok, floors)
    assert verdict["gaps"]["class_below_floor"] is False
    assert verdict["verdict"].startswith("HOLD")


# ---------------------------------------------------------------------------
# T5: class balance and per intent x difficulty-tier support
# ---------------------------------------------------------------------------

MIN_HELDOUT_SUPPORT = 12
MIN_QA_SIMPLE_SCENARIOS = 6


def test_min_heldout_support_per_intent():
    from collections import Counter

    counts = Counter(h["intent_class"] for h in heldout.load_heldout())
    thin = {i: counts[i] for i in heldout.INTENTS if counts[i] < MIN_HELDOUT_SUPPORT}
    assert not thin, f"intents under {MIN_HELDOUT_SUPPORT} held-out support: {thin}"


def test_qa_simple_scenarios_at_least_6():
    n = sum(1 for s in heldout.load_scenarios() if s["expected_intent"] == "qa_simple")
    assert n >= MIN_QA_SIMPLE_SCENARIOS, n


def test_every_intent_has_scenarios():
    present = {s["expected_intent"] for s in heldout.load_scenarios()}
    assert present == set(heldout.INTENTS), set(heldout.INTENTS) - present


def test_no_empty_intent_by_heldout_tier_cell():
    from collections import Counter

    cells = Counter((h["intent_class"], h["difficulty"]) for h in heldout.load_heldout())
    empty = [(i, t) for i in heldout.INTENTS for t in ("canonical", "paraphrase", "adversarial")
             if cells[(i, t)] == 0]
    assert not empty, f"empty intent x tier cells: {empty}"
