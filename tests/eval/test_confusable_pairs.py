"""T4: confusable-pair cases (tests/data/eval_confusable_pairs.json).

Known classifier misses are flagged known_gap with a ticket (T18) and asserted
strictly: they must still fail, so a fix forces the flag to be removed.
No API key; classifier code is not modified.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import heldout  # noqa: E402
from classify_intent import classify_intent_detailed  # noqa: E402

PAIRS_PATH = heldout.ROOT / "tests" / "data" / "eval_confusable_pairs.json"
_DOC = json.loads(PAIRS_PATH.read_text())
CASES = _DOC["cases"]
PAIRS = _DOC["pairs"]
REQUIRED_KINDS = {"standard", "reverse", "one_word", "very_long", "multi_intent"}
# Pair-level accuracy floors = measured accuracy (known gaps count as misses). Exact on purpose:
# any regression drops below; a T18 fix raises accuracy and should raise the floor.
PAIR_FLOORS = {
    "dispatch_inline_agentic": 15 / 15,
    "qa_simple_qa_with_reads": 10 / 10,
    "qa_architecture_analytical_qa": 10 / 10,
    "agentic_content_generation": 10 / 10,
}

_PRED: dict[str, str] = {}


def _predict(text: str) -> str:
    if text not in _PRED:
        _PRED[text] = classify_intent_detailed(text)["intent"]
    return _PRED[text]


def test_file_has_four_pairs_with_at_least_8_cases_each():
    assert set(PAIRS) == set(PAIR_FLOORS)
    counts = Counter(c["pair"] for c in CASES)
    for pair in PAIRS:
        assert counts[pair] >= 8, f"{pair} has only {counts[pair]} cases"


@pytest.mark.parametrize("pair", sorted(PAIRS))
def test_pair_covers_every_class_both_directions_and_all_kinds(pair):
    cases = [c for c in CASES if c["pair"] == pair]
    for cls in PAIRS[pair]:
        assert any(c["expected_intent"] == cls for c in cases), f"{pair}: no case expects {cls}"
        kinds = {c["kind"] for c in cases if c["expected_intent"] == cls}
        # 'reverse' = the opposite-direction confusion case; per class every other kind is required
        assert (REQUIRED_KINDS - {"reverse"}) <= kinds, f"{pair}/{cls} missing kinds"
    assert REQUIRED_KINDS <= {c["kind"] for c in cases}, f"{pair} missing a case kind"
    assert {c["expected_intent"] for c in cases} <= set(PAIRS[pair])


def test_very_long_cases_are_actually_long_and_one_word_is_one_word():
    for c in CASES:
        if c["kind"] == "very_long":
            assert len(c["prompt"]) >= 200, c["id"]
        if c["kind"] == "one_word":
            assert len(c["prompt"].split()) == 1, c["id"]


def test_no_overlap_with_exemplars_scenarios_corpus_or_each_other():
    cases = [(c["id"], c["prompt"]) for c in CASES]
    others = [(f"exemplar[{i}]", e["text"]) for i, e in enumerate(heldout.load_exemplars())]
    others += [(f"scenario {s['id']}", s["prompt"]) for s in heldout.load_scenarios()]
    others += [(f"corpus[{i}]", h["text"]) for i, h in enumerate(heldout.load_corpus())]
    assert heldout.find_overlaps(cases, others) == []
    assert heldout.find_overlaps(others, cases) == []
    norms = [heldout.norm(p) for _, p in cases]
    for i, n in enumerate(norms):
        for m in norms[:i]:
            assert not heldout.near_dup(n, m), cases[i]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_confusable_case(case):
    predicted = _predict(case["prompt"])
    if case.get("known_gap"):
        assert predicted != case["expected_intent"], (
            f"{case['id']} now passes; remove known_gap flag"
        )
        pytest.xfail(f"{case.get('known_gap_ticket', 'known gap')}: predicted {predicted}, "
                     f"expected {case['expected_intent']}")
    assert predicted == case["expected_intent"], f"{case['id']}: got {predicted}"


@pytest.mark.parametrize("pair", sorted(PAIRS))
def test_pair_level_accuracy_floor(pair):
    cases = [c for c in CASES if c["pair"] == pair]
    assert len(cases) >= 8
    acc = sum(_predict(c["prompt"]) == c["expected_intent"] for c in cases) / len(cases)
    assert acc >= PAIR_FLOORS[pair] - 1e-9, f"{pair} accuracy {acc:.2f} below floor {PAIR_FLOORS[pair]:.2f}"
