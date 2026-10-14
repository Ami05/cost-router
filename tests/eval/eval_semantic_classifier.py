#!/usr/bin/env python3
"""SEM-301: Evaluation harness — keyword-only vs semantic-enhanced intent classifier.

Run with:
    python tests/eval_semantic_classifier.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Allow sibling imports from scripts/ and tests/eval/ (this file lives in tests/eval/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import heldout  # noqa: E402
from classify_intent import classify_intent, classify_intent_detailed  # noqa: E402
from semantic_scorer import SemanticScorer, classify_semantic  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent.parent
_SCENARIOS_PATH = _ROOT / "tests" / "intent_eval_scenarios.json"
_THRESHOLDS_PATH = _ROOT / "config" / "semantic_thresholds.json"
_RESULTS_DIR = _ROOT / "tests" / "eval_results"
_RESULTS_PATH = _RESULTS_DIR / "semantic_vs_keyword.json"
_LOW_CONF_LOG = _ROOT / "data" / "semantic_low_confidence.jsonl"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_scenarios() -> list[dict]:
    with open(_SCENARIOS_PATH) as f:
        data = json.load(f)
    # Support both top-level list and {"scenarios": [...]} shapes
    if isinstance(data, list):
        return data
    return data.get("scenarios", [])


def _load_thresholds() -> dict:
    with open(_THRESHOLDS_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------

def _accuracy(correct: int, total: int) -> float:
    return round(correct / total, 4) if total > 0 else 0.0


def _build_confusion_matrix(
    results: list[dict],
    pred_key: str,
    classes: list[str],
) -> dict[str, dict[str, int]]:
    """Return matrix[expected][predicted] = count."""
    matrix: dict[str, dict[str, int]] = {c: {c2: 0 for c2 in classes} for c in classes}
    for r in results:
        expected = r["expected"]
        predicted = r[pred_key]
        if expected not in matrix:
            matrix[expected] = {c: 0 for c in classes}
        if predicted not in matrix[expected]:
            matrix[expected][predicted] = 0
        matrix[expected][predicted] += 1
    return matrix


def _per_category_accuracy(results: list[dict], pred_key: str) -> dict[str, float]:
    cat_correct: dict[str, int] = defaultdict(int)
    cat_total: dict[str, int] = defaultdict(int)
    for r in results:
        cat = r["category"]
        cat_total[cat] += 1
        if r["expected"] == r[pred_key]:
            cat_correct[cat] += 1
    return {
        cat: _accuracy(cat_correct[cat], cat_total[cat])
        for cat in cat_total
    }


def _mean_confidence_per_class(results: list[dict]) -> dict[str, float]:
    """Mean semantic confidence grouped by expected (ground-truth) class."""
    class_conf: dict[str, list[float]] = defaultdict(list)
    for r in results:
        class_conf[r["expected"]].append(r["semantic_confidence"])
    return {
        cls: round(sum(vals) / len(vals), 4)
        for cls, vals in sorted(class_conf.items())
    }


# ---------------------------------------------------------------------------
# Core evaluation loop
# ---------------------------------------------------------------------------

def run_evaluation() -> tuple[dict, list[dict]]:
    scenarios = _load_scenarios()
    thresholds = _load_thresholds()

    # Instantiate SemanticScorer once — it loads exemplars on first call.
    scorer = SemanticScorer()

    all_classes = sorted({s["expected_intent"] for s in scenarios})

    results: list[dict] = []
    for scenario in scenarios:
        prompt: str = scenario["prompt"]
        expected: str = scenario["expected_intent"]

        # --- Keyword-only ---
        keyword_pred = classify_intent(prompt)

        # --- Semantic-enhanced ---
        # classify_semantic returns (final_class, confidence, was_fallback)
        sem_class, sem_conf, _was_fallback = classify_semantic(
            prompt,
            scorer,
            thresholds,
            log_path=_LOW_CONF_LOG,
        )

        results.append({
            "id": scenario["id"],
            "prompt": prompt,
            "expected": expected,
            "category": scenario["category"],
            "known_gap": scenario.get("known_gap", False),
            "keyword_pred": keyword_pred,
            "semantic_pred": sem_class,
            "semantic_confidence": sem_conf,
            "keyword_correct": keyword_pred == expected,
            "semantic_correct": sem_class == expected,
        })

    # ---- Keyword metrics ----
    kw_correct = sum(1 for r in results if r["keyword_correct"])
    total = len(results)
    kw_overall = _accuracy(kw_correct, total)
    kw_by_category = _per_category_accuracy(results, "keyword_pred")
    kw_confusion = _build_confusion_matrix(results, "keyword_pred", all_classes)

    # ---- Semantic metrics ----
    sem_correct = sum(1 for r in results if r["semantic_correct"])
    sem_overall = _accuracy(sem_correct, total)
    sem_by_category = _per_category_accuracy(results, "semantic_pred")
    sem_confusion = _build_confusion_matrix(results, "semantic_pred", all_classes)
    mean_conf = _mean_confidence_per_class(results)

    # ---- Known-gap results ----
    known_gap_results = [
        {
            "id": r["id"],
            "prompt": r["prompt"],
            "expected": r["expected"],
            "keyword_pred": r["keyword_pred"],
            "semantic_pred": r["semantic_pred"],
            "improved": (not r["keyword_correct"]) and r["semantic_correct"],
        }
        for r in results
        if r["known_gap"]
    ]

    # ---- Improvement summary ----
    kw_para = kw_by_category.get("paraphrase", 0.0)
    sem_para = sem_by_category.get("paraphrase", 0.0)
    paraphrase_recall_delta = round(sem_para - kw_para, 4)

    kw_canon = kw_by_category.get("canonical", 0.0)
    sem_canon = sem_by_category.get("canonical", 0.0)
    canonical_accuracy_maintained = sem_canon >= kw_canon

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "keyword_baseline": {
            "overall_accuracy": kw_overall,
            "by_category": kw_by_category,
            "confusion_matrix": kw_confusion,
        },
        "semantic_enhanced": {
            "overall_accuracy": sem_overall,
            "by_category": sem_by_category,
            "confusion_matrix": sem_confusion,
            "mean_confidence_per_class": mean_conf,
        },
        "known_gap_results": known_gap_results,
        "improvement_summary": {
            "paraphrase_recall_delta": paraphrase_recall_delta,
            "canonical_accuracy_maintained": canonical_accuracy_maintained,
        },
    }

    return report, results


# ---------------------------------------------------------------------------
# T10: held-out per-class keyword-vs-semantic comparison (the gate)
# ---------------------------------------------------------------------------

# Two "semantic" candidates are compared against keyword-only classify_intent():
#   hybrid        = classify_intent_detailed (the shipped pipeline: keyword, then semantic upgrade)
#   semantic_only = classify_semantic (TF-IDF scorer + threshold fallback, no keyword layer)
MODES = ("hybrid", "semantic_only")


def _prf(preds: list[str], gold: list[str], cls: str) -> dict:
    tp = sum(1 for p, g in zip(preds, gold) if p == cls == g)
    fp = sum(1 for p, g in zip(preds, gold) if p == cls != g)
    fn = sum(1 for p, g in zip(preds, gold) if g == cls != p)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3), "support": tp + fn}


def run_heldout_comparison(log_path: Path | None = None) -> dict:
    """Per-class P/R/F1 of keyword vs each semantic mode on the held-out set.

    Fully offline: no network, no API key. classify_semantic logs low-confidence events,
    so callers must pass a scratch `log_path` (never the repo data/ log) when testing.
    """
    items = heldout.load_heldout()
    if not items:
        raise RuntimeError("held-out set is empty")
    gold = [i["intent_class"] for i in items]
    kw = [classify_intent(i["text"]) for i in items]
    thresholds = _load_thresholds()
    scorer = SemanticScorer()
    preds = {
        "hybrid": [classify_intent_detailed(i["text"])["intent"] for i in items],
        "semantic_only": [
            classify_semantic(i["text"], scorer, thresholds, log_path=log_path)[0] for i in items
        ],
    }
    out: dict = {"n": len(items), "modes": {}}
    for mode, pred in preds.items():
        out["modes"][mode] = {
            c: {"keyword": _prf(kw, gold, c), "semantic": _prf(pred, gold, c)}
            for c in heldout.INTENTS
        }
    return out


def find_regressions(comparison: dict) -> list[tuple[str, str, str]]:
    """(mode, class, metric) where semantic is worse than keyword on recall or F1."""
    bad = []
    for mode, per_class in comparison["modes"].items():
        for cls, cell in per_class.items():
            for metric in ("recall", "f1"):
                if cell["semantic"][metric] < cell["keyword"][metric]:
                    bad.append((mode, cls, metric))
    return bad


def update_baseline(log_path: Path) -> Path:
    """Overwrite the held-out section of semantic_vs_keyword.json (explicit --update-baseline only)."""
    import hashlib

    report = json.loads(_RESULTS_PATH.read_text()) if _RESULTS_PATH.exists() else {}
    comparison = run_heldout_comparison(log_path)
    comparison["source_hashes"] = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in {
            "classify_intent.py": _ROOT / "scripts" / "classify_intent.py",
            "semantic_scorer.py": _ROOT / "scripts" / "semantic_scorer.py",
            "intent_exemplars.json": _ROOT / "config" / "intent_exemplars.json",
            "real_user_corpus.json": _ROOT / "tests" / "data" / "real_user_corpus.json",
            "production_synthetic.json": _ROOT / "tests" / "data" / "production_synthetic.json",
        }.items()
    }
    report["heldout_comparison"] = comparison
    _RESULTS_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return _RESULTS_PATH


# ---------------------------------------------------------------------------
# Human-readable summary
# ---------------------------------------------------------------------------

def print_summary(report: dict, results: list[dict]) -> None:  # noqa: ARG001
    kw = report["keyword_baseline"]
    sem = report["semantic_enhanced"]
    imp = report["improvement_summary"]
    known = report["known_gap_results"]

    print("=" * 62)
    print("  Semantic vs Keyword Classifier -- Evaluation Report")
    print(f"  Run at: {report['timestamp']}")
    print("=" * 62)

    print("\nOverall Accuracy")
    print(f"  Keyword baseline : {kw['overall_accuracy']:.1%}")
    print(f"  Semantic enhanced: {sem['overall_accuracy']:.1%}")

    print("\nPer-Category Accuracy")
    all_cats = sorted(set(list(kw["by_category"]) + list(sem["by_category"])))
    for cat in all_cats:
        kw_acc = kw["by_category"].get(cat, 0.0)
        sem_acc = sem["by_category"].get(cat, 0.0)
        delta = sem_acc - kw_acc
        sign = "+" if delta >= 0 else ""
        print(
            f"  {cat:<14}  keyword={kw_acc:.1%}  semantic={sem_acc:.1%}"
            f"  delta={sign}{delta:.1%}"
        )

    print("\nMean Confidence per Class (semantic, by ground-truth class)")
    for cls, conf in sem["mean_confidence_per_class"].items():
        print(f"  {cls:<22}  {conf:.4f}")

    print("\nImprovement Summary")
    print(f"  Paraphrase recall delta       : {imp['paraphrase_recall_delta']:+.1%}")
    print(f"  Canonical accuracy maintained : {imp['canonical_accuracy_maintained']}")

    improved_count = sum(1 for k in known if k["improved"])
    print(f"\nKnown-Gap Scenarios  ({len(known)} total,  {improved_count} improved by semantic)")
    for k in known:
        status = "IMPROVED " if k["improved"] else "no change"
        print(
            f"  [{status}] {k['id']:<8}  expected={k['expected']:<18}"
            f"  kw={k['keyword_pred']:<18}  sem={k['semantic_pred']}"
        )

    print("\n" + "=" * 62)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    import argparse
    import tempfile

    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baseline", action="store_true",
                    help="explicitly rewrite the held-out section of tests/eval_results/semantic_vs_keyword.json")
    args = ap.parse_args(argv)
    if args.update_baseline:
        scratch = Path(tempfile.mkdtemp()) / "semantic_low_confidence.jsonl"
        print(f"Baseline updated: {update_baseline(scratch)}")
        return

    _LOW_CONF_LOG.parent.mkdir(parents=True, exist_ok=True)
    print("Running evaluation -- loading scenarios and scoring ...")
    report, results = run_evaluation()
    print("(results file NOT modified; pass --update-baseline to refresh the held-out baseline)\n")
    print_summary(report, results)


if __name__ == "__main__":
    main()
