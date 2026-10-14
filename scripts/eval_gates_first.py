#!/usr/bin/env python3
"""T022: Predictive evaluation — Gates-first vs Classifier-first routing.

Simulates both architectures on all labeled samples (exemplars + synthetic)
and produces a comparison report. EVALUATION ONLY — no production code changes.

Evaluation logic (gate simulation, metrics) lives in gate_evaluator.py.
This module owns: data loading, report rendering, and CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# -- add scripts/ to path so we can import the classifier --
SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from classify_intent import classify_intent_detailed  # noqa: E402
from gate_evaluator import compute_metrics, evaluate_gates, run_simulation  # noqa: E402


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_exemplars() -> list[dict]:
    path = ROOT_DIR / "config" / "intent_exemplars.json"
    with open(path) as f:
        data = json.load(f)
    return [
        {"text": e["text"], "expected_intent": e["intent_class"], "source": "exemplar"}
        for e in data["exemplars"]
    ]


def load_synthetic() -> list[dict]:
    path = ROOT_DIR / "tests" / "data" / "production_synthetic.json"
    if not path.exists():
        print(f"Warning: synthetic data file not found at {path}")
        print("  Continuing with empty synthetic dataset.")
        return []
    with open(path) as f:
        data = json.load(f)
    return [
        {"text": s["text"], "expected_intent": s["expected_intent"], "source": "synthetic"}
        for s in data["samples"]
    ]


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_report(metrics: dict, results: list[dict]) -> str:
    m = metrics
    lines = []
    a = lines.append

    a("# Gates-First vs Classifier-First: Predictive Evaluation Report")
    a("")
    a("## Executive Summary")
    a("")

    # Verdict
    gate_cov = m["coverage"]
    gate_acc = m["gate_accuracy"]
    unc_proj = m["uncertainty_rate_projected"]
    fcr = m["false_confidence_rate"]

    criteria_met = sum([
        gate_cov >= 40,
        gate_acc >= 85,
        gate_acc >= 85,
        unc_proj < 25,
        fcr < 5,
    ])

    if criteria_met == 4:
        verdict = "IMPLEMENT"
        verdict_text = "All four success criteria met. Gates-first architecture is recommended."
    elif criteria_met >= 3:
        verdict = "IMPLEMENT WITH MODIFICATIONS"
        verdict_text = f"{criteria_met}/4 success criteria met. Gates-first is viable with adjustments."
    else:
        verdict = "DO NOT IMPLEMENT"
        verdict_text = f"Only {criteria_met}/4 success criteria met. Gates-first is not justified."

    a(f"- **Verdict: {verdict}** — {verdict_text}")
    a(f"- Gate coverage: {gate_cov:.1f}% ({m['gate_caught_count']}/{m['total_samples']} prompts caught by gates)")
    a(f"- Gate accuracy: {gate_acc:.1f}% on caught prompts (false confidence rate: {fcr}%)")
    a(f"- Uncertainty reduction: {m['uncertainty_rate_current']:.1f}% → {unc_proj:.1f}%")
    a(f"- Projected cost savings: {m['cost_savings_pct']}% per 1000 prompts")
    a("")

    # Success criteria checklist
    a("### Success Criteria")
    a("")
    a(f"| Criterion | Threshold | Actual | Pass |")
    a(f"|---|---|---|---|")
    a(f"| Gate coverage | >= 40% | {gate_cov:.1f}% | {'YES' if gate_cov >= 40 else 'NO'} |")
    a(f"| Gate accuracy | >= 85% | {gate_acc:.1f}% | {'YES' if gate_acc >= 85 else 'NO'} |")
    a(f"| Uncertainty reduction | < 25% | {unc_proj:.1f}% | {'YES' if unc_proj < 25 else 'NO'} |")
    a(f"| False confidence rate | < 5% | {fcr}% | {'YES' if fcr < 5 else 'NO'} |")
    a("")

    # Architecture comparison
    a("## Architecture Comparison")
    a("")
    a("| Metric | Classifier-First | Gates-First | Delta |")
    a("|---|---|---|---|")
    a(f"| Accuracy (overall) | {m['classifier_accuracy_overall']:.1f}% | {gate_acc:.1f}% (gate) + {m['classifier_accuracy_residual']:.1f}% (residual) | — |")
    a(f"| Uncertainty rate | {m['uncertainty_rate_current']:.1f}% | {unc_proj:.1f}% | {m['uncertainty_rate_current'] - unc_proj:+.1f}pp |")
    a(f"| Cost / 1000 prompts | ${m['cost_classifier_first']:.2f} | ${m['cost_gates_first']:.2f} | -${m['cost_classifier_first'] - m['cost_gates_first']:.2f} |")
    a("")

    # Model distribution
    a("### Model Distribution")
    a("")
    a("| Model | Classifier-First | Gates-First |")
    a("|---|---|---|")
    for model in ["haiku", "sonnet", "opus"]:
        c1 = m["model_dist_classifier"].get(model, 0)
        c2 = m["model_dist_gates"].get(model, 0)
        a(f"| {model.capitalize()} | {c1} ({c1/m['total_samples']*100:.1f}%) | {c2} ({c2/m['total_samples']*100:.1f}%) |")
    a("")

    # Gate coverage analysis
    a("## Gate Coverage Analysis")
    a("")
    a(f"Total gate-caught: {m['gate_caught_count']}/{m['total_samples']} ({gate_cov:.1f}%)")
    a(f"Falls through to classifier: {m['gate_missed_count']}/{m['total_samples']} ({100 - gate_cov:.1f}%)")
    a("")
    a("| Gate | Count | % of Total |")
    a("|---|---|---|")
    for gate, count in sorted(m["per_gate_counts"].items(), key=lambda x: -x[1]):
        a(f"| {gate} | {count} | {count/m['total_samples']*100:.1f}% |")
    a("")

    # Per-class accuracy
    a("## Per-Class Accuracy Comparison")
    a("")
    a("| Intent Class | N | Classifier Acc | Gate Coverage | Gate Acc |")
    a("|---|---|---|---|---|")
    for cls, info in sorted(m["per_class"].items()):
        a(f"| {cls} | {info['total']} | {info['classifier_accuracy']:.1f}% | {info['gate_coverage']:.1f}% ({info['gate_caught']}) | {info['gate_accuracy']:.1f}% |")
    a("")

    # Classifier relief
    a("## Classifier Relief (Residual Performance)")
    a("")
    a(f"When gates handle easy cases, the classifier only sees {m['gate_missed_count']} prompts.")
    a(f"Classifier accuracy on residual: **{m['classifier_accuracy_residual']:.1f}%** (vs {m['classifier_accuracy_overall']:.1f}% overall)")
    a(f"Classifier accuracy on gate-caught subset: {m['classifier_accuracy_on_caught']:.1f}% (gates replace these)")
    a("")

    # Risk analysis
    a("## Risk Analysis")
    a("")
    a(f"False confidence rate: **{fcr}%** ({len(m['gate_errors'])} errors out of {m['gate_caught_count']} gate-caught)")
    a("")

    if m["severe_errors"]:
        a(f"### Severe Errors (wrong tier, 2+ levels off): {len(m['severe_errors'])}")
        a("")
        for r in m["severe_errors"]:
            a(f"- **\"{r['text'][:80]}\"** — gate={r['gate_name']} routed {r['gate_model']}, expected {r['expected_intent']}")
        a("")

    if m["mild_errors"]:
        a(f"### Mild Errors (same tier or 1 level off): {len(m['mild_errors'])}")
        a("")
        for r in m["mild_errors"]:
            a(f"- \"{r['text'][:80]}\" — gate={r['gate_name']} → {r['gate_intent']}, expected {r['expected_intent']}")
        a("")

    if not m["gate_errors"]:
        a("No gate errors detected.")
        a("")

    # Recommendations
    a("## Recommendations")
    a("")
    if verdict == "IMPLEMENT":
        a("1. **Proceed with gates-first implementation.** All criteria met.")
        a("2. Monitor false-confidence rate in production shadow mode before full rollout.")
        a("3. Add gate-miss logging to catch regressions.")
    elif verdict == "IMPLEMENT WITH MODIFICATIONS":
        a("1. **Conditionally proceed** — address failing criteria before rollout:")
        if gate_cov < 40:
            a(f"   - Gate coverage ({gate_cov:.1f}%) below 40% — add more gate rules or broaden existing ones.")
        if gate_acc < 85:
            a(f"   - Gate accuracy ({gate_acc:.1f}%) below 85% — tighten gate rules to reduce false positives.")
        if unc_proj >= 25:
            a(f"   - Projected uncertainty ({unc_proj:.1f}%) still >= 25% — gates don't relieve classifier enough.")
        if fcr >= 5:
            a(f"   - False confidence ({fcr}%) >= 5% — gates are too aggressive on ambiguous prompts.")
        a("2. Run shadow mode evaluation with production traffic before committing.")
    else:
        a("1. **Do not implement gates-first** at this time.")
        a("2. Focus on improving the classifier's keyword heuristics instead.")
        a("3. Revisit when more gate signals can be identified from production traffic.")
    a("")

    a("---")
    a(f"*Generated by eval_gates_first.py on {m['total_samples']} samples*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate gates-first vs classifier-first routing architectures."
    )
    parser.add_argument(
        "--exemplars-only",
        action="store_true",
        help="Run evaluation on exemplars only; skip loading synthetic data.",
    )
    args = parser.parse_args()

    print("Loading datasets...")
    exemplars = load_exemplars()
    if args.exemplars_only:
        print("  (--exemplars-only flag set; skipping synthetic data)")
        synthetic = []
    else:
        synthetic = load_synthetic()
    all_samples = exemplars + synthetic
    print(f"  Exemplars: {len(exemplars)}, Synthetic: {len(synthetic)}, Total: {len(all_samples)}")

    print("Running simulation...")
    results = run_simulation(all_samples, classify_intent_detailed)

    print("Computing metrics...")
    metrics = compute_metrics(results)

    print("Generating report...")
    report = generate_report(metrics, results)

    # Write report
    report_dir = ROOT_DIR / "tests" / "eval_results"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "gates_first_eval_report.md"
    report_path.write_text(report + "\n")
    print(f"Report written to {report_path}")

    # Re-print summary cleanly
    print("=" * 70)
    print("EXECUTIVE SUMMARY")
    print("=" * 70)
    m = metrics
    gate_cov = m["coverage"]
    gate_acc = m["gate_accuracy"]
    unc_proj = m["uncertainty_rate_projected"]
    fcr = m["false_confidence_rate"]

    criteria_met = sum([gate_cov >= 40, gate_acc >= 85, unc_proj < 25, fcr < 5])
    if criteria_met == 4:
        verdict = "IMPLEMENT"
    elif criteria_met >= 3:
        verdict = "IMPLEMENT WITH MODIFICATIONS"
    else:
        verdict = "DO NOT IMPLEMENT"

    print(f"  Verdict:              {verdict} ({criteria_met}/4 criteria met)")
    print(f"  Gate coverage:        {gate_cov:.1f}% ({m['gate_caught_count']}/{m['total_samples']})")
    print(f"  Gate accuracy:        {gate_acc:.1f}%")
    print(f"  Uncertainty:          {m['uncertainty_rate_current']:.1f}% → {unc_proj:.1f}%")
    print(f"  False confidence:     {fcr}%")
    print(f"  Cost savings:         {m['cost_savings_pct']}%")
    print(f"  Severe misroutes:     {len(m['severe_errors'])}")
    print("=" * 70)


if __name__ == "__main__":
    main()
