#!/usr/bin/env python3
"""FL-eval: offline golden-set evaluator for classify_intent.py.

Runs every labeled scenario in tests/intent_eval_scenarios.json through
classify_intent(), builds a confusion matrix, and reports per-class
precision/recall/F1 plus overall accuracy. Mirrors the shape of
run_routing_validation.py so it slots into the same CI/report pattern.

Usage: python3 eval_intent_classifier.py [--scenarios <path>] [--output <path>] [--min-accuracy 0.7]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from classify_intent import classify_intent  # noqa: E402

_INTENTS = ("qa_simple", "qa_with_reads", "qa_architecture", "agentic_action", "content_generation", "dispatch_justification", "inline_narration")


def run_eval(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """Run classify_intent() over all scenarios, return confusion matrix + metrics."""
    confusion: dict[str, dict[str, int]] = {
        expected: {predicted: 0 for predicted in _INTENTS} for expected in _INTENTS
    }
    per_scenario: list[dict[str, Any]] = []
    known_gap_failures = 0
    unexpected_failures = 0

    for scenario in scenarios:
        prompt = scenario["prompt"]
        expected = scenario["expected_intent"]
        predicted = classify_intent(prompt)
        passed = predicted == expected

        if expected in confusion and predicted in confusion[expected]:
            confusion[expected][predicted] += 1

        if not passed:
            if scenario.get("known_gap"):
                known_gap_failures += 1
            else:
                unexpected_failures += 1

        per_scenario.append({
            "id": scenario.get("id", ""),
            "category": scenario.get("category", ""),
            "expected": expected,
            "predicted": predicted,
            "passed": passed,
            "known_gap": bool(scenario.get("known_gap", False)),
        })

    metrics = _compute_metrics(confusion)
    total = len(scenarios)
    correct = sum(1 for s in per_scenario if s["passed"])

    return {
        "confusion_matrix": confusion,
        "per_class_metrics": metrics,
        "per_scenario": per_scenario,
        "total_scenarios": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "known_gap_failures": known_gap_failures,
        "unexpected_failures": unexpected_failures,
        "known_gap_total": sum(1 for s in per_scenario if s["known_gap"]),
        "known_gap_correct": sum(1 for s in per_scenario if s["passed"] and s["known_gap"]),
        "known_gap_accuracy": round(
            sum(1 for s in per_scenario if s["passed"] and s["known_gap"])
            / max(sum(1 for s in per_scenario if s["known_gap"]), 1),
            4,
        ),
    }


def _compute_metrics(confusion: dict[str, dict[str, int]]) -> dict[str, dict[str, float]]:
    """Compute precision/recall/F1 per intent class from the confusion matrix."""
    metrics: dict[str, dict[str, float]] = {}
    for cls in _INTENTS:
        tp = confusion.get(cls, {}).get(cls, 0)
        fn = sum(v for k, v in confusion.get(cls, {}).items() if k != cls)
        fp = sum(confusion.get(other, {}).get(cls, 0) for other in _INTENTS if other != cls)

        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

        metrics[cls] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": tp + fn,
        }
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate classify_intent.py against a golden set")
    parser.add_argument(
        "--scenarios",
        default=str(SCRIPTS.parent / "tests" / "intent_eval_scenarios.json"),
    )
    parser.add_argument(
        "--output",
        default=str(SCRIPTS.parent / "tests" / "data" / "intent_eval.json"),
    )
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=0.7,
        help="Fail (exit 1) if overall accuracy on non-known-gap scenarios drops below this.",
    )
    args = parser.parse_args()

    data = json.loads(Path(args.scenarios).read_text(encoding="utf-8"))
    scenarios = data.get("scenarios", [])

    report = run_eval(scenarios)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Accuracy: {report['accuracy']:.2%} ({report['correct']}/{report['total_scenarios']})")
    print(f"Known-gap failures (expected today): {report['known_gap_failures']}")
    print(f"Unexpected failures (regressions): {report['unexpected_failures']}")
    print(f"Known-gap accuracy (upgrade target): {report.get('known_gap_accuracy', 0):.2%} ({report.get('known_gap_correct', 0)}/{report.get('known_gap_total', 0)})")
    for cls, m in report["per_class_metrics"].items():
        print(f"  {cls:16s} precision={m['precision']:.2f} recall={m['recall']:.2f} f1={m['f1']:.2f} (n={m['support']})")
    print(f"Results written to {args.output}")

    non_gap_total = report["total_scenarios"] - sum(
        1 for s in report["per_scenario"] if s["known_gap"]
    )
    non_gap_correct = sum(
        1 for s in report["per_scenario"] if s["passed"] and not s["known_gap"]
    )
    non_gap_accuracy = (non_gap_correct / non_gap_total) if non_gap_total else 1.0

    if non_gap_accuracy < args.min_accuracy:
        print(f"FAIL: non-known-gap accuracy {non_gap_accuracy:.2%} below threshold {args.min_accuracy:.2%}")
        return 1

    if report["unexpected_failures"] > 0:
        print("WARN: unexpected failures present but above min-accuracy threshold.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
