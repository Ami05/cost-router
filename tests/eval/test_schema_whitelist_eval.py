#!/usr/bin/env python3
"""Offline eval for dispatch schema constraint whitelist.

Loads committed schema_whitelist_scenarios.json and runs each constraint
through validate_dispatch_schema. No API key, no network — pure Python.

Scenarios are generated once via:
  python3 scripts/generate_whitelist_scenarios.py

Usage:
  python3 tests/eval/test_schema_whitelist_eval.py --run
  pytest tests/eval/test_schema_whitelist_eval.py -v

Exit codes: 0 = GO (thresholds met), 1 = HOLD
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow importing scripts/ modules
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from validate_dispatch_schema import validate_dispatch_schema, _CONSTRAINT_ALLOWLIST, _token_count  # noqa: E402

SCENARIOS_PATH = Path(__file__).parent / "schema_whitelist_scenarios.json"

# Thresholds
FPR_THRESHOLD = 0.05  # max 5% of valid constraints incorrectly blocked
FNR_THRESHOLD = 0.02  # max 2% of invalid constraints incorrectly passed

# Padding to push payload past the 200-token thin-dispatch exemption (600 was only 198 tokens)
_PADDING = "x" * 800


def _wrap_constraint(text: str) -> str:
    """Wrap a single constraint string in a minimal valid mutation JSON payload."""
    payload = {
        "worker": "mutation",
        "intent": "test constraint validation",
        "files": ["scripts/validate_dispatch_schema.py:1"],
        "pattern": _PADDING,
        "constraints": [text],
        "output": "targeted_edits",
    }
    return json.dumps(payload)


def run_eval(scenarios_path: Path = SCENARIOS_PATH) -> dict:
    """Load scenarios and evaluate each against the gate. Returns report dict."""
    if not scenarios_path.exists():
        raise FileNotFoundError(
            f"Scenarios file not found: {scenarios_path}\n"
            "Run: python3 scripts/generate_whitelist_scenarios.py"
        )

    scenarios = json.loads(scenarios_path.read_text())
    if not scenarios:
        raise ValueError("Scenarios file is empty")

    by_category: dict[str, list[dict]] = {}
    for s in scenarios:
        cat = s["category"]
        by_category.setdefault(cat, [])

        prompt = _wrap_constraint(s["text"])
        result = validate_dispatch_schema(prompt, session_id="whitelist-eval")
        actual = result.decision
        expected = s["expected"]

        by_category[cat].append({
            "text": s["text"],
            "expected": expected,
            "actual": actual,
            "correct": actual == expected,
        })

    # Aggregate scores
    category_scores: dict[str, dict] = {}
    total = total_correct = total_fp = total_fn = 0
    allow_total = deny_total = 0

    for cat, items in by_category.items():
        n = len(items)
        correct = sum(1 for i in items if i["correct"])
        fp = sum(1 for i in items if i["expected"] == "allow" and i["actual"] == "deny")
        fn = sum(1 for i in items if i["expected"] == "deny" and i["actual"] == "allow")

        category_scores[cat] = {
            "total": n,
            "correct": correct,
            "accuracy": round(correct / n, 3) if n else 0.0,
            "false_positives": fp,
            "false_negatives": fn,
            "failures": [i for i in items if not i["correct"]],
        }
        total += n
        total_correct += correct
        total_fp += fp
        total_fn += fn
        allow_total += sum(1 for i in items if i["expected"] == "allow")
        deny_total += sum(1 for i in items if i["expected"] == "deny")

    fpr = round(total_fp / allow_total, 3) if allow_total else 0.0
    fnr = round(total_fn / deny_total, 3) if deny_total else 0.0

    return {
        "total": total,
        "total_correct": total_correct,
        "overall_accuracy": round(total_correct / total, 3) if total else 0.0,
        "false_positive_rate": fpr,
        "false_negative_rate": fnr,
        "fpr_threshold": FPR_THRESHOLD,
        "fnr_threshold": FNR_THRESHOLD,
        "go": fpr <= FPR_THRESHOLD and fnr <= FNR_THRESHOLD,
        "by_category": category_scores,
    }


def print_report(report: dict) -> None:
    print("\n── Schema Whitelist Eval ──────────────────────────────────")
    print(f"  Scenarios     : {report['total']}")
    print(f"  Overall acc   : {report['overall_accuracy']:.1%}")
    print(f"  FPR (valid blocked)  : {report['false_positive_rate']:.1%}  "
          f"threshold={report['fpr_threshold']:.0%}")
    print(f"  FNR (invalid passed) : {report['false_negative_rate']:.1%}  "
          f"threshold={report['fnr_threshold']:.0%}")
    print()
    print(f"  {'Category':<35} {'Acc':>6}  {'FP':>4}  {'FN':>4}")
    print(f"  {'-'*35}  {'-'*6}  {'-'*4}  {'-'*4}")
    for cat, s in report["by_category"].items():
        print(f"  {cat:<35} {s['accuracy']:>6.1%}  {s['false_positives']:>4}  {s['false_negatives']:>4}")

    print()
    verdict = "GO ✓" if report["go"] else "HOLD ✗"
    print(f"  Verdict: {verdict}")

    if not report["go"]:
        if report["false_positive_rate"] > report["fpr_threshold"]:
            print(f"  ↳ FPR too high — allowlist is blocking valid constraints")
        if report["false_negative_rate"] > report["fnr_threshold"]:
            print(f"  ↳ FNR too high — injection strings are passing through")

        for cat, s in report["by_category"].items():
            if s["failures"]:
                print(f"\n  Failures in '{cat}':")
                for f in s["failures"][:3]:
                    print(f"    [{f['expected']}→{f['actual']}] {f['text']!r}")

    print("───────────────────────────────────────────────────────────\n")


# ---------------------------------------------------------------------------
# pytest integration — runs as a test when scenarios file exists
# ---------------------------------------------------------------------------

def test_padding_above_exemption():
    """Guard against silent exemption if padding is insufficient.

    The 200-token thin-dispatch exemption must be bypassed to test constraints.
    This assertion ensures _PADDING is large enough to trigger full validation.
    """
    payload = _wrap_constraint("x")
    tokens = _token_count(payload)
    assert tokens > 200, (
        "Padding insufficient — all payloads would be exempt from schema validation "
        f"(got {tokens} tokens, need >200)"
    )


def test_allowlist_loaded():
    """Guard against silent allowlist degradation if config file is missing."""
    assert _CONSTRAINT_ALLOWLIST is not None, (
        "constraint_allowlist.json not found or malformed — allowlist check is disabled "
        "and injection strings may pass through"
    )


def test_whitelist_eval_go():
    """Eval must return GO verdict — FPR ≤ 5%, FNR ≤ 2%."""
    if not SCENARIOS_PATH.exists():
        import pytest
        pytest.skip(f"Scenarios not seeded yet. Run: python3 scripts/generate_whitelist_scenarios.py")

    report = run_eval()
    assert report["go"], (
        f"HOLD: FPR={report['false_positive_rate']:.1%} "
        f"FNR={report['false_negative_rate']:.1%}\n"
        + json.dumps({k: v for k, v in report["by_category"].items()
                      if v["failures"]}, indent=2)
    )


def test_whitelist_eval_fpr():
    """False positive rate (valid constraints blocked) must be ≤ 5%."""
    if not SCENARIOS_PATH.exists():
        import pytest
        pytest.skip("Scenarios not seeded.")
    report = run_eval()
    assert report["false_positive_rate"] <= FPR_THRESHOLD, (
        f"FPR {report['false_positive_rate']:.1%} exceeds {FPR_THRESHOLD:.0%}"
    )


def test_whitelist_eval_fnr():
    """False negative rate (invalid constraints passed) must be ≤ 2%."""
    if not SCENARIOS_PATH.exists():
        import pytest
        pytest.skip("Scenarios not seeded.")
    report = run_eval()
    assert report["false_negative_rate"] <= FNR_THRESHOLD, (
        f"FNR {report['false_negative_rate']:.1%} exceeds {FNR_THRESHOLD:.0%}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Schema whitelist offline eval")
    parser.add_argument("--run", action="store_true", help="Run eval on committed scenarios")
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS_PATH)
    args = parser.parse_args()

    if not args.run:
        parser.print_help()
        return 1

    try:
        report = run_eval(args.scenarios)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return 1

    print_report(report)
    return 0 if report["go"] else 1


if __name__ == "__main__":
    sys.exit(main())
