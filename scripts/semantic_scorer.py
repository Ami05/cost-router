#!/usr/bin/env python3
"""Semantic intent scorer using TF-IDF cosine similarity.

Computes similarity between an input prompt and a labeled exemplar set.
Returns confidence scores per intent class, not binary classification.

Backends:
  1. TF-IDF cosine similarity — zero external dependencies (stdlib only)
  2. Optional embedding model — activatable via config flag (future)
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from scoring_algorithms import (
    TfIdfEngine as _TfIdfEngine,
    apply_threshold_fallback as _apply_threshold_fallback,
    cosine_similarity as _cosine_similarity_fn,
    tokenize as _tokenize,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_EXEMPLAR_PATH = _CONFIG_DIR / "intent_exemplars.json"
_THRESHOLD_PATH = _CONFIG_DIR / "semantic_thresholds.json"
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_LOW_CONFIDENCE_LOG = _DATA_DIR / "semantic_low_confidence.jsonl"
_CLAUDE_CLASSIFIER_LOG = _DATA_DIR / "claude_classifier.jsonl"
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# _tokenize, _TfIdfEngine imported from scoring_algorithms above.


# ---------------------------------------------------------------------------
# SemanticScorer Helper Functions
# ---------------------------------------------------------------------------

def _claude_classifier_enabled() -> bool:
    """Check if Claude classifier backend is enabled in config.

    Reads _CONFIG_PATH json and returns cfg.get('claude_classifier', False).
    Returns False on any Exception.
    """
    try:
        with open(_CONFIG_PATH, encoding='utf-8') as f:
            config = json.load(f)
        return config.get('claude_classifier', False)
    except Exception:
        return False


def _log_claude_backend(text_hash: str, tfidf_fallback_class: str,
                       claude_class: str, changed: bool,
                       status: str, latency_ms: float) -> None:
    """Log Claude classifier execution metrics to _CLAUDE_CLASSIFIER_LOG.

    Appends a JSON entry with fields: text_hash, tfidf_fallback_class,
    claude_class, changed, status, latency_ms, and timestamp.
    Silently ignores OSError (file not writable, directory missing, etc.).
    """
    try:
        entry = {
            'text_hash': text_hash,
            'tfidf_fallback_class': tfidf_fallback_class,
            'claude_class': claude_class,
            'changed': changed,
            'status': status,
            'latency_ms': latency_ms,
            'timestamp': time.time(),
        }
        with open(_CLAUDE_CLASSIFIER_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
    except OSError:
        pass


# ---------------------------------------------------------------------------
# ClaudeBackend Classifier
# ---------------------------------------------------------------------------

class ClaudeBackend:
    """Claude LLM-based intent classifier with caching and error handling.

    Classifies text into one of the valid_classes by calling the Claude CLI.
    Caches results by text SHA256 hash to avoid redundant API calls.
    Logs execution metrics (latency, status, parse errors) for telemetry.
    """

    _cache: dict[str, str | None] = {}

    @classmethod
    def classify(
        cls,
        text: str,
        valid_classes: list[str],
        tfidf_fallback_class: str,
    ) -> str | None:
        """Classify text into one of valid_classes using Claude, with fallback.

        Args:
            text: Input text to classify.
            valid_classes: List of acceptable intent classes.
            tfidf_fallback_class: Fallback class from TF-IDF if Claude fails or times out.

        Returns:
            Classified intent class (string) or None on all failure modes.
            On any error (timeout, parse, subprocess, validation), logs the failure
            and returns None (caller should use tfidf_fallback_class).
        """
        text_hash = hashlib.sha256(text.encode()).hexdigest()

        # Check cache first
        if text_hash in cls._cache:
            cached = cls._cache[text_hash]
            if cached is None:
                return None
            return cached if cached in valid_classes else None

        # Build Claude prompt
        classes_str = ", ".join(f'"{c}"' for c in valid_classes)
        prompt = (
            f"Classify the following text into exactly one of these classes: {classes_str}.\n\n"
            f"Text: {text}\n\n"
            f"Return ONLY valid JSON with a single field 'intent_class' set to one of the valid classes above. "
            f"Example: {{\"intent_class\": \"qa_simple\"}}"
        )

        start_time = time.time()
        status = "ok"
        result_class: str | None = None

        try:
            # Call Claude CLI with subprocess
            proc = subprocess.run(
                [
                    "claude",
                    "-p", prompt,
                    "--model", "claude-haiku-4-5-20251001",
                    "--output-format", "json",
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )

            if proc.returncode != 0:
                status = "subprocess_error"
                latency_ms = (time.time() - start_time) * 1000
                _log_claude_backend(
                    text_hash, tfidf_fallback_class, "", False, status, latency_ms
                )
                cls._cache[text_hash] = None
                return None

            # Parse stdout as outer JSON
            try:
                outer = json.loads(proc.stdout)
                # Extract the 'result' field which contains the inner JSON
                result_str = outer.get("result", "")
                inner = json.loads(result_str) if isinstance(result_str, str) else result_str
                result_class = inner.get("intent_class")
            except (json.JSONDecodeError, AttributeError, TypeError):
                status = "parse_error"
                latency_ms = (time.time() - start_time) * 1000
                _log_claude_backend(
                    text_hash, tfidf_fallback_class, "", False, status, latency_ms
                )
                cls._cache[text_hash] = None
                return None

            # Validate result class
            if result_class not in valid_classes:
                status = "validation_error"
                latency_ms = (time.time() - start_time) * 1000
                _log_claude_backend(
                    text_hash, tfidf_fallback_class, result_class or "", False, status, latency_ms
                )
                cls._cache[text_hash] = None
                return None

            # Success
            changed = result_class != tfidf_fallback_class
            latency_ms = (time.time() - start_time) * 1000
            _log_claude_backend(
                text_hash, tfidf_fallback_class, result_class, changed, status, latency_ms
            )
            cls._cache[text_hash] = result_class
            return result_class

        except subprocess.TimeoutExpired:
            status = "timeout"
            latency_ms = (time.time() - start_time) * 1000
            _log_claude_backend(
                text_hash, tfidf_fallback_class, "", False, status, latency_ms
            )
            cls._cache[text_hash] = None
            return None
        except Exception as e:
            status = f"error_{type(e).__name__}"
            latency_ms = (time.time() - start_time) * 1000
            _log_claude_backend(
                text_hash, tfidf_fallback_class, "", False, status, latency_ms
            )
            cls._cache[text_hash] = None
            return None


# ---------------------------------------------------------------------------
# SemanticScorer
# ---------------------------------------------------------------------------

class SemanticScorer:
    """Score prompts against labeled exemplars using TF-IDF similarity.

    Lazy-loads exemplars on first use and caches for process lifetime.
    """

    _instance: SemanticScorer | None = None

    def __init__(self, exemplar_path: str | Path | None = None) -> None:
        self._exemplar_path = Path(exemplar_path) if exemplar_path else _EXEMPLAR_PATH
        self._engine: _TfIdfEngine | None = None
        self._exemplar_classes: list[str] = []
        self._class_set: set[str] = set()
        self._class_centroids: dict[str, dict[str, float]] = {}

    @classmethod
    def get_instance(cls, exemplar_path: str | Path | None = None) -> SemanticScorer:
        if cls._instance is None:
            cls._instance = cls(exemplar_path)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def _ensure_loaded(self) -> None:
        if self._engine is not None:
            return
        data = json.loads(self._exemplar_path.read_text())
        exemplars = data["exemplars"]

        docs: list[list[str]] = []
        classes: list[str] = []
        class_set: set[str] = set()

        for ex in exemplars:
            tokens = _tokenize(ex["text"])
            docs.append(tokens)
            classes.append(ex["intent_class"])
            class_set.add(ex["intent_class"])

        self._engine = _TfIdfEngine(docs)
        self._exemplar_classes = classes
        self._class_set = class_set

        # Build class centroids: concatenate all exemplar tokens per class
        # into one pseudo-document, then compute a TF-IDF vector for each.
        class_tokens: dict[str, list[str]] = defaultdict(list)
        for ex in exemplars:
            class_tokens[ex["intent_class"]].extend(_tokenize(ex["text"]))
        self._class_centroids = {
            cls: self._engine.query_vector(tokens)
            for cls, tokens in class_tokens.items()
        }

    @staticmethod
    def _cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
        """Cosine similarity between two sparse TF-IDF vectors (dicts)."""
        return _cosine_similarity_fn(vec_a, vec_b)

    def score_intent(self, text: str) -> dict[str, float]:
        """Score input text against all exemplar classes.

        Returns dict mapping intent_class -> confidence score (0.0-1.0).
        Scores are normalized so they sum to 1.0.

        Uses c-TF-IDF centroid blending: final score is a weighted blend of
        centroid cosine similarity (0.5) and top-K exemplar average (0.5).
        Weights tuned via T018 grid search (was 0.6/0.4).
        """
        self._ensure_loaded()
        assert self._engine is not None

        query_tokens = _tokenize(text)
        if not query_tokens:
            # No meaningful tokens — uniform distribution
            n = len(self._class_set)
            return {cls: 1.0 / n for cls in sorted(self._class_set)}

        similarities = self._engine.similarities(query_tokens)
        query_vec = self._engine.query_vector(query_tokens)

        # Aggregate per-class: average top-K similarities (K=3) to reduce
        # noise from outlier exemplars while staying robust.
        class_sims: dict[str, list[float]] = defaultdict(list)
        for sim, cls in zip(similarities, self._exemplar_classes):
            class_sims[cls].append(sim)

        top_k = 3
        class_scores: dict[str, float] = {}
        for cls in sorted(self._class_set):
            sims = sorted(class_sims.get(cls, [0.0]), reverse=True)[:top_k]
            topk_avg = sum(sims) / len(sims) if sims else 0.0

            # Centroid similarity: cosine between input vector and class centroid
            centroid = self._class_centroids.get(cls, {})
            centroid_sim = self._cosine_similarity(query_vec, centroid)

            # Blend: 0.5 centroid + 0.5 top-K average (T018 auto-tuned)
            class_scores[cls] = 0.5 * centroid_sim + 0.5 * topk_avg

        # Normalize to sum=1.0
        total = sum(class_scores.values())
        if total > 0:
            class_scores = {cls: s / total for cls, s in class_scores.items()}
        else:
            n = len(self._class_set)
            class_scores = {cls: 1.0 / n for cls in sorted(self._class_set)}

        return class_scores

    def top_class(self, text: str) -> tuple[str, float]:
        """Return (top_class, confidence) for the given text."""
        scores = self.score_intent(text)
        best = max(scores, key=scores.get)  # type: ignore[arg-type]
        return best, scores[best]

    def resolve_with_fallback(
        self,
        text: str,
        thresholds: dict | None = None,
        log_path: Path | None = None,
    ) -> tuple[str, float, bool]:
        """Score text and apply confidence threshold with fallback.

        Returns (intent_class, confidence, was_fallback).
        """
        scores = self.score_intent(text)
        final_class, confidence, was_fallback = _resolve_with_fallback(scores, thresholds)
        if was_fallback:
            _log_low_confidence(text, scores, final_class, was_fallback, log_path)

        # If fallback triggered, try Claude backend for refinement
        if was_fallback and _claude_classifier_enabled():
            claude_class = ClaudeBackend.classify(text, list(self._class_set), final_class)
            if claude_class is not None:
                return claude_class, 0.9, False

        return final_class, confidence, was_fallback

    def score_role_alignment(self, role: str, text: str) -> dict:
        """Check whether text aligns with the expected behaviour for role.

        FO and RB roles should produce dispatch_justification text.
        DIRECT and FW roles should produce inline_narration text.

        Returns:
            {
                "aligned": bool,
                "confidence": float,   # score of the expected class
                "reason": str,         # human-readable explanation
            }
        """
        self._ensure_loaded()

        # Only meaningful if both alignment classes are present in the exemplar set.
        has_dispatch = "dispatch_justification" in self._class_set
        has_narration = "inline_narration" in self._class_set

        if not has_dispatch or not has_narration:
            # Exemplar set doesn't cover alignment classes — skip advisory.
            return {
                "aligned": True,
                "confidence": 1.0,
                "reason": "alignment exemplars not loaded",
            }

        scores = self.score_intent(text)
        dispatch_score = scores.get("dispatch_justification", 0.0)
        narration_score = scores.get("inline_narration", 0.0)

        role_upper = role.upper()

        # FO/RB → should dispatch
        if role_upper in ("FO", "RB"):
            expected_class = "dispatch_justification"
            expected_score = dispatch_score
            opposing_score = narration_score
            aligned = dispatch_score >= narration_score
            if aligned:
                reason = f"{role_upper} role: text looks like dispatch justification (score={dispatch_score:.3f})"
            else:
                reason = (
                    f"{role_upper} role expects dispatch justification but text looks like "
                    f"inline narration (dispatch={dispatch_score:.3f}, narration={narration_score:.3f})"
                )
        # DIRECT/FW → should narrate inline
        elif role_upper in ("DIRECT", "FW"):
            expected_class = "inline_narration"
            expected_score = narration_score
            opposing_score = dispatch_score
            aligned = narration_score >= dispatch_score
            if aligned:
                reason = f"{role_upper} role: text looks like inline narration (score={narration_score:.3f})"
            else:
                reason = (
                    f"{role_upper} role expects inline narration but text looks like "
                    f"dispatch justification (dispatch={dispatch_score:.3f}, narration={narration_score:.3f})"
                )
        else:
            # Unknown role — treat as aligned
            return {
                "aligned": True,
                "confidence": 1.0,
                "reason": f"unknown role '{role}' — no alignment policy",
            }

        return {
            "aligned": aligned,
            "confidence": round(expected_score, 4),
            "reason": reason,
        }


# ---------------------------------------------------------------------------
# Confidence thresholds & fallback (SEM-103)
# ---------------------------------------------------------------------------

def _load_thresholds(path: Path | None = None) -> dict[str, Any]:
    p = path or _THRESHOLD_PATH
    if not p.exists():
        return {"default_threshold": 0.6, "per_class": {}, "fallback_map": {}}
    return json.loads(p.read_text())


def _resolve_with_fallback(
    scores: dict[str, float],
    thresholds: dict[str, Any] | None = None,
) -> tuple[str, float, bool]:
    """Apply confidence thresholds and return (final_class, confidence, was_fallback).

    Loads thresholds from disk if not provided, then delegates to the
    pure apply_threshold_fallback() from scoring_algorithms.
    """
    if thresholds is None:
        thresholds = _load_thresholds()
    return _apply_threshold_fallback(scores, thresholds)


def _log_low_confidence(
    input_text: str,
    scores: dict[str, float],
    final_class: str,
    was_fallback: bool,
    log_path: Path | None = None,
) -> None:
    """Write a structured JSON-lines entry for low-confidence/fallback events."""
    path = log_path or _LOW_CONFIDENCE_LOG
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_class, top_score = ranked[0]
    second_class, second_score = ranked[1] if len(ranked) > 1 else ("", 0.0)

    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "input_text_hash": hashlib.sha256(input_text.encode()).hexdigest()[:16],
        "top_class": top_class,
        "top_score": round(top_score, 4),
        "second_class": second_class,
        "second_score": round(second_score, 4),
        "fallback_class": final_class,
        "was_fallback": was_fallback,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # Data dir unwritable — continue without logging


def classify_semantic(
    text: str,
    scorer: SemanticScorer | None = None,
    thresholds: dict[str, Any] | None = None,
    log_path: Path | None = None,
) -> tuple[str, float, bool]:
    """Full semantic classification with threshold fallback and logging.

    Returns (final_class, confidence, was_fallback).
    Logs low-confidence events to JSONL for tune-routing consumption.
    """
    if scorer is None:
        scorer = SemanticScorer.get_instance()

    scores = scorer.score_intent(text)
    final_class, confidence, was_fallback = _resolve_with_fallback(scores, thresholds)

    if was_fallback:
        _log_low_confidence(text, scores, final_class, was_fallback, log_path)

    return final_class, confidence, was_fallback


resolve_with_fallback = _resolve_with_fallback  # public alias for external imports


def check_role_semantic_alignment(role: str, text: str) -> dict:
    """Module-level convenience for role alignment check."""
    scorer = SemanticScorer.get_instance()
    return scorer.score_role_alignment(role, text)
