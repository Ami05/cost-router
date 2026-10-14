#!/usr/bin/env python3
"""
Change Gate: Enforcement layer for baseline quality gates.

Runs baseline collection and delta evaluation to determine if changes
are safe to merge. Blocks on regressions unless --force flag is used.

Usage:
    python change_gate.py [--force] [--dry-run]
"""

import json
import pathlib
import datetime
import os
import sys
import subprocess
import argparse
from typing import Optional, Dict, Any


def read_reference_baseline() -> str:
    """
    Read last_good_baseline.txt to get the reference baseline path.

    Returns:
        str: Path to the reference baseline file

    Exits with code 1 if file is missing or path is invalid.
    """
    script_dir = pathlib.Path(__file__).parent
    baseline_ptr_file = script_dir / "last_good_baseline.txt"

    if not baseline_ptr_file.exists():
        print("ERROR: last_good_baseline.txt not found", file=sys.stderr)
        sys.exit(1)

    try:
        baseline_path = baseline_ptr_file.read_text().strip()
    except Exception as e:
        print(f"ERROR: Could not read last_good_baseline.txt: {e}", file=sys.stderr)
        sys.exit(1)

    # Validate the path exists
    baseline_full_path = script_dir.parent / baseline_path if not pathlib.Path(baseline_path).is_absolute() else pathlib.Path(baseline_path)
    if not baseline_full_path.exists():
        print(f"ERROR: Reference baseline path invalid: {baseline_path}", file=sys.stderr)
        sys.exit(1)

    return str(baseline_full_path)


def run_collect_baseline() -> str:
    """
    Run collect_baseline.py to capture current state.

    Returns:
        str: Path to the newly created baseline file

    Exits with code 1 if collection fails.
    """
    script_dir = pathlib.Path(__file__).parent
    collect_script = script_dir / "collect_baseline.py"
    baseline_dir = script_dir / "baseline"

    try:
        result = subprocess.run(
            [sys.executable, str(collect_script)],
            cwd=str(script_dir.parent),
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print(f"ERROR: collect_baseline.py failed", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            sys.exit(1)

        # Parse stdout to find the baseline file path
        # Look for "Baseline written to: ..." in output
        output_lines = result.stdout.split('\n')
        baseline_path = None

        for line in output_lines:
            if "Baseline written to:" in line:
                baseline_path = line.split("Baseline written to:")[-1].strip()
                break

        if not baseline_path:
            # Fall back to finding newest file in baseline directory
            if not baseline_dir.exists():
                print("ERROR: baseline directory not created by collect_baseline.py", file=sys.stderr)
                sys.exit(1)

            baseline_files = sorted(baseline_dir.glob("baseline_*.json"))
            if not baseline_files:
                print("ERROR: no baseline files found in baseline directory", file=sys.stderr)
                sys.exit(1)

            baseline_path = str(baseline_files[-1])

        return baseline_path

    except Exception as e:
        print(f"ERROR: Failed to run collect_baseline.py: {e}", file=sys.stderr)
        sys.exit(1)


def run_evaluate_delta(reference_baseline: str, new_baseline: str) -> Dict[str, Any]:
    """
    Run evaluate_delta.py to compare baselines.

    Args:
        reference_baseline: Path to the reference baseline
        new_baseline: Path to the new baseline

    Returns:
        dict: Parsed delta report JSON

    Exits with code 1 if evaluation fails.
    """
    script_dir = pathlib.Path(__file__).parent
    evaluate_script = script_dir / "evaluate_delta.py"

    try:
        result = subprocess.run(
            [sys.executable, str(evaluate_script),
             "--before", reference_baseline,
             "--after", new_baseline],
            cwd=str(script_dir.parent),
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print(f"ERROR: evaluate_delta.py failed", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            sys.exit(1)

        return result.returncode == 0

    except Exception as e:
        print(f"ERROR: Failed to run evaluate_delta.py: {e}", file=sys.stderr)
        sys.exit(1)


def read_delta_report() -> Dict[str, Any]:
    """
    Read the most recently generated delta report from evaluationReport/.

    Returns:
        dict: Parsed delta report JSON

    Exits with code 1 if no report found.
    """
    script_dir = pathlib.Path(__file__).parent
    report_dir = script_dir / "evaluationReport"

    if not report_dir.exists():
        print("ERROR: evaluationReport directory not found", file=sys.stderr)
        sys.exit(1)

    # Find newest delta_*.json file
    json_files = sorted(report_dir.glob("delta_*.json"))
    if not json_files:
        print("ERROR: no delta report JSON files found", file=sys.stderr)
        sys.exit(1)

    newest_report = json_files[-1]

    try:
        with open(newest_report, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to read delta report: {e}", file=sys.stderr)
        sys.exit(1)


def update_reference_baseline(new_baseline: str, dry_run: bool = False) -> None:
    """
    Update last_good_baseline.txt to point to the new baseline.

    Args:
        new_baseline: Path to the new baseline file
        dry_run: If True, don't actually write the file
    """
    script_dir = pathlib.Path(__file__).parent
    baseline_ptr_file = script_dir / "last_good_baseline.txt"

    # Convert to relative path if possible (relative to script dir)
    try:
        new_baseline_path = pathlib.Path(new_baseline)
        relative_path = new_baseline_path.relative_to(script_dir.parent)
        path_to_write = str(relative_path)
    except ValueError:
        # Can't make relative, use absolute
        path_to_write = new_baseline

    if dry_run:
        print(f"[DRY RUN] Would update last_good_baseline.txt to: {path_to_write}")
    else:
        try:
            baseline_ptr_file.write_text(path_to_write + "\n")
        except Exception as e:
            print(f"ERROR: Failed to update last_good_baseline.txt: {e}", file=sys.stderr)
            sys.exit(1)


def print_regression_details(report: Dict[str, Any]) -> None:
    """
    Print regression details from the delta report.

    Args:
        report: Delta report dictionary
    """
    sections = report.get("sections", [])

    for section in sections:
        section_name = section.get("name", "unknown")
        metrics = section.get("metrics", [])

        regressed_metrics = [m for m in metrics if m.get("verdict") == "REGRESSED"]

        if regressed_metrics:
            print(f"\nRegressed metrics in {section_name}:")
            for metric in regressed_metrics:
                name = metric.get("name", "unknown")
                before = metric.get("before")
                after = metric.get("after")
                delta = metric.get("delta")
                delta_pct = metric.get("delta_pct", "N/A")

                print(f"  - {name}")
                print(f"      Before: {before}, After: {after}, Delta: {delta} ({delta_pct}%)")


def main():
    parser = argparse.ArgumentParser(
        description="Change gate: enforce baseline quality before merge"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force merge even on regression (logged but not blocked)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all checks but don't update last_good_baseline.txt"
    )

    args = parser.parse_args()

    # Step 1: Read reference baseline
    reference_baseline = read_reference_baseline()
    print(f"Reference baseline: {reference_baseline}")

    # Step 2: Collect current baseline
    print("\nCollecting current baseline...")
    new_baseline = run_collect_baseline()
    print(f"New baseline: {new_baseline}")

    # Step 3: Run delta evaluation
    print("\nEvaluating delta...")
    run_evaluate_delta(reference_baseline, new_baseline)

    # Step 4: Read delta report
    report = read_delta_report()
    overall_verdict = report.get("overall_verdict", "UNKNOWN")

    # Step 5: Decision logic
    if overall_verdict == "PASS":
        # Update reference and exit 0
        update_reference_baseline(new_baseline, dry_run=args.dry_run)
        print("\nCHANGE GATE: PASS — safe to merge")
        sys.exit(0)
    elif overall_verdict == "FAIL":
        # Print regression details
        print("\nCHANGE GATE: FAIL — regressions detected:")
        print_regression_details(report)

        if args.force:
            print("\nFORCED — override logged")
            sys.exit(0)
        else:
            sys.exit(1)
    else:
        print(f"\nCHANGE GATE: UNKNOWN verdict: {overall_verdict}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
