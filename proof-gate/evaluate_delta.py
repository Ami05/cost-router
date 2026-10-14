#!/usr/bin/env python3
"""
Delta evaluator: compare two baseline JSON files and produce a diff report.
Computes per-metric deltas, applies thresholds, and generates PASS/FAIL verdicts.
"""

import json
import pathlib
import datetime
import os
import sys
import argparse
from typing import Any, Dict, List, Tuple, Optional


def load_thresholds(thresholds_path: Optional[str]) -> Dict[str, Any]:
    """Load evaluation thresholds from JSON file."""
    if thresholds_path:
        try:
            with open(thresholds_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load thresholds from {thresholds_path}: {e}", file=sys.stderr)

    # Default thresholds
    return {
        "rate_threshold": 0.02,
        "cost_threshold": 0.05,
        "count_threshold": 1,
        "metric_directions": {
            "accuracy": "higher_is_better",
            "fallback_rate": "lower_is_better",
            "deny_count": "higher_is_better",
            "nudge_count": "informational",
            "cost_estimate_usd": "lower_is_better",
            "total_estimated_tokens": "lower_is_better",
            "total_actual_tokens": "lower_is_better",
            "calibration_ratio": "closer_to_1",
            "total_sessions": "informational",
            "unique_session_ids": "informational",
            "tier_transitions": "informational",
            "policy_proposals": "informational",
            "estimated_cost_usd": "lower_is_better",
            "low_confidence_count": "lower_is_better",
            "total_turns": "informational",
            "total_signal_entries": "informational",
            "convergence_verdict": "informational"
        }
    }


def get_metric_direction(metric_name: str, directions: Dict[str, str]) -> str:
    """Get direction for a metric: higher_is_better, lower_is_better, or informational."""
    return directions.get(metric_name, "higher_is_better")


def compute_verdict(
    before_val: float,
    after_val: float,
    metric_name: str,
    directions: Dict[str, str],
    rate_threshold: float,
    cost_threshold: float,
    count_threshold: float
) -> Tuple[float, Optional[float], str]:
    """
    Compute delta, delta_pct, and verdict for a single metric.
    Returns (delta, delta_pct, verdict).
    """
    delta = after_val - before_val

    # Skip if before is 0 and it's a rate metric (can't compute percentage)
    if before_val == 0 and metric_name in ["accuracy", "fallback_rate", "calibration_ratio"]:
        delta_pct = None
    else:
        delta_pct = (delta / before_val * 100) if before_val != 0 else None

    direction = get_metric_direction(metric_name, directions)

    # Informational metrics never change verdict
    if direction == "informational":
        return delta, delta_pct, "UNCHANGED"

    # Determine threshold to use
    if metric_name in ["fallback_rate", "accuracy", "calibration_ratio"]:
        threshold = rate_threshold
    elif metric_name in ["cost_estimate_usd", "estimated_cost_usd"]:
        threshold = cost_threshold
    else:
        threshold = count_threshold

    # Compute verdict based on direction
    if direction == "higher_is_better":
        if delta > threshold:
            verdict = "IMPROVED"
        elif delta < -threshold:
            verdict = "REGRESSED"
        else:
            verdict = "UNCHANGED"
    elif direction == "lower_is_better":
        if delta < -threshold:
            verdict = "IMPROVED"
        elif delta > threshold:
            verdict = "REGRESSED"
        else:
            verdict = "UNCHANGED"
    elif direction == "closer_to_1":
        # For calibration_ratio, closer to 1 is better
        delta_from_one = abs(after_val - 1) - abs(before_val - 1)
        if delta_from_one < -threshold:
            verdict = "IMPROVED"
        elif delta_from_one > threshold:
            verdict = "REGRESSED"
        else:
            verdict = "UNCHANGED"
    else:
        verdict = "UNCHANGED"

    return delta, delta_pct, verdict


def process_section(
    section_name: str,
    before_data: Dict[str, Any],
    after_data: Dict[str, Any],
    directions: Dict[str, str],
    rate_threshold: float,
    cost_threshold: float,
    count_threshold: float
) -> Tuple[Dict[str, Any], str]:
    """
    Process a single section, computing deltas and verdicts for all numeric fields.
    Returns (section_dict, section_verdict).
    """
    metrics = []
    verdicts = []

    # Get all keys from before and after
    all_keys = set(before_data.keys()) | set(after_data.keys())

    for key in sorted(all_keys):
        before_val = before_data.get(key)
        after_val = after_data.get(key)

        # Skip non-numeric fields
        if not isinstance(before_val, (int, float)) or not isinstance(after_val, (int, float)):
            continue

        delta, delta_pct, verdict = compute_verdict(
            before_val, after_val, key, directions,
            rate_threshold, cost_threshold, count_threshold
        )

        metric_dict = {
            "name": key,
            "before": before_val,
            "after": after_val,
            "delta": round(delta, 6) if isinstance(delta, float) else delta,
        }

        if delta_pct is not None:
            metric_dict["delta_pct"] = round(delta_pct, 2)
        else:
            metric_dict["delta_pct"] = "N/A"

        metric_dict["verdict"] = verdict
        metrics.append(metric_dict)

        if verdict != "UNCHANGED":
            verdicts.append(verdict)

    # Determine section verdict
    if "REGRESSED" in verdicts:
        section_verdict = "REGRESSED"
    elif "IMPROVED" in verdicts:
        section_verdict = "IMPROVED"
    else:
        section_verdict = "UNCHANGED"

    section_dict = {
        "name": section_name,
        "metrics": metrics,
        "section_verdict": section_verdict
    }

    return section_dict, section_verdict


def evaluate_delta(before_path: str, after_path: str, thresholds_path: Optional[str] = None):
    """
    Main evaluation function: load baselines, compute deltas, generate report.
    """
    # Load thresholds
    thresholds = load_thresholds(thresholds_path)
    rate_threshold = thresholds.get("rate_threshold", 0.02)
    cost_threshold = thresholds.get("cost_threshold", 0.05)
    count_threshold = thresholds.get("count_threshold", 1)
    directions = thresholds.get("metric_directions", {})

    # Load baseline files
    try:
        with open(before_path, 'r') as f:
            before_baseline = json.load(f)
    except Exception as e:
        print(f"Error loading before baseline: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(after_path, 'r') as f:
            after_baseline = json.load(f)
    except Exception as e:
        print(f"Error loading after baseline: {e}", file=sys.stderr)
        sys.exit(1)

    # Get section names from schema (5 sections)
    section_names = [
        "session_metrics",
        "gate_metrics",
        "classifier_metrics",
        "cost_metrics",
        "routing_metrics"
    ]

    # Process each section
    sections = []
    all_verdicts = []

    for section_name in section_names:
        before_section = before_baseline.get(section_name, {})
        after_section = after_baseline.get(section_name, {})

        if not before_section and not after_section:
            print(f"Warning: section {section_name} not found in either baseline", file=sys.stderr)
            continue

        section_dict, section_verdict = process_section(
            section_name, before_section, after_section, directions,
            rate_threshold, cost_threshold, count_threshold
        )
        sections.append(section_dict)
        all_verdicts.append(section_verdict)

    # Determine overall verdict: PASS if zero REGRESSED and at least one IMPROVED
    regressed_count = sum(1 for v in all_verdicts if v == "REGRESSED")
    improved_count = sum(1 for v in all_verdicts if v == "IMPROVED")

    if regressed_count > 0:
        overall_verdict = "FAIL"
    elif improved_count > 0:
        overall_verdict = "PASS"
    else:
        overall_verdict = "PASS"  # No regressions, even if no improvements

    # Generate timestamp
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"

    # Count metric verdicts
    all_metric_verdicts = []
    for section in sections:
        for metric in section["metrics"]:
            all_metric_verdicts.append(metric["verdict"])

    summary = {
        "improved": sum(1 for v in all_metric_verdicts if v == "IMPROVED"),
        "unchanged": sum(1 for v in all_metric_verdicts if v == "UNCHANGED"),
        "regressed": sum(1 for v in all_metric_verdicts if v == "REGRESSED")
    }

    # Build JSON report
    json_report = {
        "overall_verdict": overall_verdict,
        "before_path": before_path,
        "after_path": after_path,
        "timestamp": timestamp,
        "sections": sections,
        "summary": summary
    }

    # Generate output filenames
    dt = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    json_filename = f"delta_{dt}.json"
    md_filename = f"delta_{dt}.md"

    # Ensure output directory exists
    output_dir = pathlib.Path(before_path).parent.parent / "evaluationReport"
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / json_filename
    md_path = output_dir / md_filename

    # Write JSON report
    with open(json_path, 'w') as f:
        json.dump(json_report, f, indent=2)

    # Generate markdown report
    md_lines = [
        "# Delta Evaluation Report",
        "",
        f"**Before:** {before_path}",
        f"**After:** {after_path}",
        f"**Timestamp:** {timestamp}",
        "",
        "## Metrics by Section",
        ""
    ]

    # Add table
    md_lines.append("| Section | Metric | Before | After | Delta | Delta% | Verdict |")
    md_lines.append("|---------|--------|--------|-------|-------|--------|---------|")

    for section in sections:
        section_name = section["name"]
        for metric in section["metrics"]:
            before_val = metric["before"]
            after_val = metric["after"]
            delta = metric["delta"]
            delta_pct = metric["delta_pct"]
            verdict = metric["verdict"]
            metric_name = metric["name"]

            delta_pct_str = f"{delta_pct}%" if isinstance(delta_pct, float) else delta_pct

            md_lines.append(
                f"| {section_name} | {metric_name} | {before_val} | {after_val} | {delta} | {delta_pct_str} | {verdict} |"
            )

    md_lines.extend([
        "",
        "## Summary",
        f"- **Overall Verdict:** {overall_verdict}",
        f"- **Improved:** {summary['improved']}",
        f"- **Unchanged:** {summary['unchanged']}",
        f"- **Regressed:** {summary['regressed']}",
        ""
    ])

    md_content = "\n".join(md_lines)

    # Write markdown report
    with open(md_path, 'w') as f:
        f.write(md_content)

    # Print markdown to stdout
    print(md_content)

    print(f"\nJSON report written to: {json_path}", file=sys.stderr)
    print(f"Markdown report written to: {md_path}", file=sys.stderr)

    return overall_verdict == "PASS"


def main():
    parser = argparse.ArgumentParser(
        description="Compare two baseline JSON files and generate a delta evaluation report"
    )
    parser.add_argument(
        "--before",
        required=True,
        help="Path to before baseline JSON file"
    )
    parser.add_argument(
        "--after",
        required=True,
        help="Path to after baseline JSON file"
    )
    parser.add_argument(
        "--thresholds",
        default=None,
        help="Path to evaluation_thresholds.json (optional, uses defaults if not provided)"
    )

    args = parser.parse_args()

    success = evaluate_delta(args.before, args.after, args.thresholds)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
