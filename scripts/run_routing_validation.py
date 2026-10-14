#!/usr/bin/env python3
"""Run planted-failure routing validation scenarios.

Usage: python3 run_routing_validation.py [--scenarios <path>] [--runs N] [--output <path>]

HG-301: Each scenario runs N times (default 3). All N must pass for the
scenario to be considered valid. Results written to data/routing_validation.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from enforce_explicit_model import check_explicit_model  # noqa: E402
from enforce_role_gates import check_role_gate  # noqa: E402


def _route_scenario(scenario: dict) -> dict:
    """Route a scenario to the correct gate and return the result."""
    inp = scenario["input"]
    tool_name = inp.get("tool_name", "")

    if tool_name in ("Task", "Agent"):
        result = check_explicit_model(inp)
    else:
        result = check_role_gate(inp)

    return {"decision": result.decision, "message": result.message or ""}


def run_scenario(scenario: dict, n: int) -> dict:
    """Run a single scenario N times and collect results."""
    expected = scenario["expected"]
    results = []
    all_pass = True

    for i in range(n):
        result = _route_scenario(scenario)
        passed = result["decision"] == expected["decision"]
        if passed and "message_contains" in expected:
            passed = expected["message_contains"] in result["message"]

        results.append({"run": i + 1, "result": result, "passed": passed})
        if not passed:
            all_pass = False

    return {
        "id": scenario["id"],
        "description": scenario["description"],
        "runs": results,
        "all_passed": all_pass,
        "pass_rate": f"{sum(1 for r in results if r['passed'])}/{n}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run routing validation scenarios")
    parser.add_argument(
        "--scenarios",
        default=str(SCRIPTS.parent / "tests" / "routing_scenarios.json"),
        help="Path to scenarios JSON",
    )
    parser.add_argument("--runs", type=int, default=3, help="Number of runs per scenario")
    parser.add_argument(
        "--output",
        default=str(SCRIPTS.parent / "data" / "routing_validation.json"),
        help="Output JSON path",
    )
    args = parser.parse_args()

    scenarios_data = json.loads(Path(args.scenarios).read_text(encoding="utf-8"))
    scenarios = [s for s in scenarios_data.get("scenarios", []) if "expected" in s]

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runs_per_scenario": args.runs,
        "scenarios": [],
        "overall_pass": True,
    }

    for scenario in scenarios:
        result = run_scenario(scenario, args.runs)
        report["scenarios"].append(result)
        if not result["all_passed"]:
            report["overall_pass"] = False

    passed = sum(1 for s in report["scenarios"] if s["all_passed"])
    total = len(report["scenarios"])
    report["summary"] = f"{passed}/{total} scenarios passed"

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Validation: {report['summary']}")
    if not report["overall_pass"]:
        print("FAIL: Not all scenarios passed. See output for details.")
        return 1

    print(f"Results written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
