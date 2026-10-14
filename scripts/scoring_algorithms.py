#!/usr/bin/env python3
"""Pure-computation scoring algorithms extracted from semantic_scorer.py.

All functions and classes here are stateless, deterministic, and I/O-free.
No file reads, no subprocess calls, no imports from _common or I/O modules.

Consumers: semantic_scorer.py imports from this module.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

# ---------------------------------------------------------------------------
# Stop words & tokenizer
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "it", "to", "of", "in", "for", "on", "and",
    "or", "be", "do", "does", "did", "was", "were", "are", "been", "being",
    "have", "has", "had", "i", "me", "my", "we", "our", "you", "your",
    "this", "that", "these", "those", "with", "at", "by", "from", "as",
    "into", "through", "not", "no", "but", "if", "so", "can", "will",
    "just", "about", "up", "out", "its", "also", "than", "then",
})


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-word chars, drop stop words and single chars."""
    tokens = re.split(r"\W+", text.lower())
    return [t for t in tokens if t and len(t) > 1 and t not in _STOP_WORDS]


# ---------------------------------------------------------------------------
# TF-IDF Engine
# ---------------------------------------------------------------------------

class TfIdfEngine:
    """Minimal TF-IDF engine operating on pre-tokenized documents."""

    def __init__(self, docs: list[list[str]]) -> None:
        self._n_docs = len(docs)
        # Document frequency: how many docs contain each term
        df: dict[str, int] = defaultdict(int)
        for doc in docs:
            for term in set(doc):
                df[term] += 1
        self._idf: dict[str, float] = {
            term: math.log((self._n_docs + 1) / (freq + 1)) + 1.0
            for term, freq in df.items()
        }
        # Pre-compute TF-IDF vectors for all docs
        self._vectors = [self._tfidf_vector(doc) for doc in docs]

    def _tfidf_vector(self, tokens: list[str]) -> dict[str, float]:
        tf = Counter(tokens)
        total = len(tokens) if tokens else 1
        return {
            term: (count / total) * self._idf.get(term, 1.0)
            for term, count in tf.items()
        }

    def query_vector(self, tokens: list[str]) -> dict[str, float]:
        return self._tfidf_vector(tokens)

    def cosine_similarity(self, vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
        common = set(vec_a) & set(vec_b)
        if not common:
            return 0.0
        dot = sum(vec_a[k] * vec_b[k] for k in common)
        mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
        mag_b = math.sqrt(sum(v * v for v in vec_b.values()))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    def similarities(self, query_tokens: list[str]) -> list[float]:
        qvec = self.query_vector(query_tokens)
        return [self.cosine_similarity(qvec, dvec) for dvec in self._vectors]


# ---------------------------------------------------------------------------
# Cosine similarity (standalone, mirrors SemanticScorer._cosine_similarity)
# ---------------------------------------------------------------------------

def cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Cosine similarity between two sparse TF-IDF vectors (dicts)."""
    shared = set(vec_a) & set(vec_b)
    if not shared:
        return 0.0
    dot = sum(vec_a[k] * vec_b[k] for k in shared)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Threshold fallback (pure logic — no file I/O)
# ---------------------------------------------------------------------------

def apply_threshold_fallback(
    scores: dict[str, float],
    thresholds: dict,
) -> tuple[str, float, bool]:
    """Apply confidence thresholds and return (final_class, confidence, was_fallback).

    If the top-scoring class is below its threshold, fall back to the
    conservative alternative per fallback_map.

    Args:
        scores: Mapping of intent_class -> confidence score.
        thresholds: Already-loaded threshold config dict with keys:
            default_threshold, per_class, fallback_map.

    Returns:
        (final_class, confidence, was_fallback)
    """
    default_t = thresholds.get("default_threshold", 0.6)
    per_class = thresholds.get("per_class", {})
    fallback_map = thresholds.get("fallback_map", {})

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_class, top_score = ranked[0]
    threshold = per_class.get(top_class, default_t)

    if top_score >= threshold:
        return top_class, top_score, False

    # Below threshold — apply fallback
    fallback_class = fallback_map.get(top_class, "qa_simple")
    fallback_score = scores.get(fallback_class, 0.0)
    return fallback_class, fallback_score, True
