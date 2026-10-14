"""Held-out evaluation set + overlap helpers (T2).

Held-out = tests/data/real_user_corpus.json + deduplicated
tests/data/production_synthetic.json. Exemplars (config/intent_exemplars.json) are the
training set for the semantic scorer and must never be used to measure accuracy.
Pure test-support code: no classifier imports, no API key.
"""
from __future__ import annotations

import difflib
import functools
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
EXEMPLAR_PATH = ROOT / "config" / "intent_exemplars.json"
SCENARIOS_PATH = ROOT / "tests" / "intent_eval_scenarios.json"
CORPUS_PATH = ROOT / "tests" / "data" / "real_user_corpus.json"
SYNTHETIC_PATH = ROOT / "tests" / "data" / "production_synthetic.json"
NEAR_DUP_RATIO = 0.9

INTENTS = (
    "agentic_action", "analytical_qa", "content_generation", "dispatch_justification",
    "inline_narration", "qa_architecture", "qa_simple", "qa_with_reads",
)


_DIFFICULTY_MAP = {
    "canonical": "canonical", "clean": "canonical",
    "informal_typos": "paraphrase", "mid_task_shorthand": "paraphrase",
    "implicit_context": "paraphrase", "noisy": "paraphrase",
    "boundary": "adversarial", "adversarial": "adversarial", "ambiguous": "adversarial",
}


def tier(raw: str) -> str:
    """Map corpus/synthetic difficulty labels onto canonical|paraphrase|adversarial."""
    return _DIFFICULTY_MAP.get(raw, "paraphrase")


def norm(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


def near_dup(a: str, b: str) -> bool:
    """a, b already normalized."""
    if a == b:
        return True
    sm = difflib.SequenceMatcher(None, a, b)
    # cheap upper bounds first; ratio() is only computed when they cannot rule the pair out
    return (sm.real_quick_ratio() >= NEAR_DUP_RATIO and sm.quick_ratio() >= NEAR_DUP_RATIO
            and sm.ratio() >= NEAR_DUP_RATIO)


def load_exemplars() -> list[dict]:
    return json.loads(EXEMPLAR_PATH.read_text())["exemplars"]


def load_scenarios() -> list[dict]:
    return json.loads(SCENARIOS_PATH.read_text())["scenarios"]


def load_corpus() -> list[dict]:
    """Corpus rows as {text, intent_class, source, ...}."""
    rows = json.loads(CORPUS_PATH.read_text())["prompts"]
    return [
        {"text": r["prompt"], "intent_class": r["intent"], "source": "real_user_corpus",
         "difficulty": tier(r.get("type", "unknown")), "raw_difficulty": r.get("type", "unknown"), "known_gap": r.get("known_gap", False)}
        for r in rows
    ]


def load_synthetic_deduped(blocked_norm: list[str]) -> list[dict]:
    """Synthetic samples minus anything equal/near-dup of blocked texts or earlier samples."""
    samples = json.loads(SYNTHETIC_PATH.read_text())["samples"]
    kept, kept_norm = [], []
    for s in samples:
        n = norm(s["text"])
        if any(near_dup(n, b) for b in blocked_norm) or any(near_dup(n, k) for k in kept_norm):
            continue
        kept.append({"text": s["text"], "intent_class": s["expected_intent"],
                     "source": "production_synthetic",
                     "difficulty": tier(s.get("difficulty", "unknown")), "raw_difficulty": s.get("difficulty", "unknown"), "known_gap": False})
        kept_norm.append(n)
    return kept


def load_heldout() -> list[dict]:
    """Corpus + deduped synthetic; synthetic is deduped against exemplars, scenarios, corpus."""
    corpus = load_corpus()
    blocked = [norm(e["text"]) for e in load_exemplars()]
    blocked += [norm(s["prompt"]) for s in load_scenarios()]
    blocked += [norm(c["text"]) for c in corpus]
    return corpus + load_synthetic_deduped(blocked)


def find_overlaps(a: list[tuple[str, str]], b: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Pairs (name_a, name_b) where a normalized text equals/near-dups one in b."""
    na = [(n, norm(t)) for n, t in a]
    nb = [(n, norm(t)) for n, t in b]
    return [(x, y) for x, tx in na for y, ty in nb if near_dup(tx, ty)]


@functools.lru_cache(maxsize=1)
def overlap_report() -> dict[str, list]:
    """All directions: heldout<->exemplars, scenarios<->exemplars, scenarios<->corpus/heldout."""
    ex = [(f"exemplar[{i}]", e["text"]) for i, e in enumerate(load_exemplars())]
    sc = [(f"scenario {s['id']}", s["prompt"]) for s in load_scenarios()]
    ho = [(f"heldout[{i}]", h["text"]) for i, h in enumerate(load_heldout())]
    return {
        "heldout_vs_exemplars": find_overlaps(ho, ex),
        "exemplars_vs_heldout": find_overlaps(ex, ho),
        "scenarios_vs_exemplars": find_overlaps(sc, ex),
        "exemplars_vs_scenarios": find_overlaps(ex, sc),
        "scenarios_vs_heldout": find_overlaps(sc, ho),
        "heldout_vs_scenarios": find_overlaps(ho, sc),
    }
