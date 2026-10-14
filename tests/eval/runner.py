"""Eval runner — orchestrates scenarios against strategies and produces scored reports."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from .scorer import (
    ScenarioResult,
    ScoredResult,
    aggregate_scores,
    score_scenario,
    strategy_summary,
)
from .strategies import (
    STRATEGY_REGISTRY,
    DispatchStrategy,
    EditSpec,
    Scenario,
)

SCENARIOS_FILE = Path(__file__).parent / "scenarios.json"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_scenarios(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or SCENARIOS_FILE
    with open(p) as f:
        data = json.load(f)
    return data["scenarios"]


def load_fixture(fixture_dir: str) -> tuple[dict[str, str], list[EditSpec], dict[str, str]]:
    """Load source files, edits, and golden output from a fixture directory.

    Expected fixture layout:
      fixtures/<name>/source/       — initial file state
      fixtures/<name>/golden/       — expected output state
      fixtures/<name>/edits.json    — edit operations
    """
    base = FIXTURES_DIR / fixture_dir
    source_dir = base / "source"
    golden_dir = base / "golden"
    edits_file = base / "edits.json"

    source_files: dict[str, str] = {}
    if source_dir.exists():
        for f in source_dir.iterdir():
            if f.is_file():
                source_files[f.name] = f.read_text()

    golden_files: dict[str, str] = {}
    if golden_dir.exists():
        for f in golden_dir.iterdir():
            if f.is_file():
                golden_files[f.name] = f.read_text()

    edits: list[EditSpec] = []
    if edits_file.exists():
        with open(edits_file) as f:
            raw = json.load(f)
        for e in raw:
            edits.append(EditSpec(
                file_path=e["file_path"],
                edit_type=e["edit_type"],
                old_content=e.get("old_content"),
                new_content=e.get("new_content", ""),
                line_start=e.get("line_start"),
                line_end=e.get("line_end"),
            ))

    return source_files, edits, golden_files


def build_scenario(raw: dict[str, Any]) -> Scenario:
    source_files, edits, golden_output = load_fixture(raw["fixture_dir"])
    return Scenario(
        id=raw["id"],
        name=raw["name"],
        complexity=raw["complexity"],
        description=raw["description"],
        source_files=source_files,
        edits=edits,
        golden_output=golden_output,
        tags=raw.get("tags", []),
    )


def run_eval(
    strategy_names: list[str] | None = None,
    scenario_ids: list[str] | None = None,
    scenarios_path: Path | None = None,
) -> list[ScoredResult]:
    """Run evaluation: all strategies x all scenarios (or filtered subsets)."""
    raw_scenarios = load_scenarios(scenarios_path)

    if scenario_ids:
        raw_scenarios = [s for s in raw_scenarios if s["id"] in scenario_ids]

    strategies: list[DispatchStrategy] = []
    names = strategy_names or list(STRATEGY_REGISTRY.keys())
    for name in names:
        cls = STRATEGY_REGISTRY.get(name)
        if cls is None:
            print(f"WARNING: unknown strategy '{name}', skipping", file=sys.stderr)
            continue
        strategies.append(cls())

    all_scored: list[ScoredResult] = []

    for raw in raw_scenarios:
        scenario = build_scenario(raw)

        for strategy in strategies:
            result = strategy.execute(scenario)
            scored = score_scenario(result, scenario.golden_output)
            all_scored.append(scored)

    all_scored = aggregate_scores(all_scored)
    return all_scored


def format_report(scored: list[ScoredResult]) -> str:
    """Format scored results as a readable table."""
    summary = strategy_summary(scored)

    lines = []
    lines.append("")
    lines.append("=" * 90)
    lines.append("DISPATCH STRATEGY EVAL REPORT")
    lines.append("=" * 90)

    header = (
        f"{'Strategy':<20} {'Correct':>8} {'Cost':>8} {'CostEff':>8} "
        f"{'Latency':>8} {'Reliab':>8} {'Composite':>10} {'WrongTgt':>9}"
    )
    lines.append("")
    lines.append(header)
    lines.append("-" * 90)

    for name, metrics in summary.items():
        lines.append(
            f"{name:<20} {metrics['avg_correctness']:>7.1f}% "
            f"${metrics['avg_cost_usd']:>7.4f} {metrics['avg_cost_efficiency']:>7.1f}% "
            f"{metrics['avg_latency_s']:>7.1f}s {metrics['avg_reliability']:>7.1f}% "
            f"{metrics['avg_composite']:>9.1f} {metrics['silent_wrong_target_count']:>9d}"
        )

    lines.append("")
    lines.append("-" * 90)

    lines.append("")
    lines.append("PER-SCENARIO DETAIL")
    lines.append("-" * 90)
    detail_header = (
        f"{'ID':<8} {'Strategy':<20} {'Correct':>8} {'Cost':>8} "
        f"{'Attempts':>8} {'Composite':>10}"
    )
    lines.append(detail_header)
    lines.append("-" * 90)

    for s in sorted(scored, key=lambda x: (x.scenario_id, x.strategy)):
        lines.append(
            f"{s.scenario_id:<8} {s.strategy:<20} {s.correctness:>7.1f}% "
            f"${s.cost_usd:>7.4f} {s.attempts:>8d} {s.composite:>9.1f}"
        )

    lines.append("")
    lines.append("=" * 90)
    return "\n".join(lines)


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Run dispatch strategy eval")
    parser.add_argument("--strategies", nargs="+", help="Strategy names to eval")
    parser.add_argument("--scenarios", nargs="+", help="Scenario IDs to eval")
    parser.add_argument("--json-output", type=str, help="Write JSON results to file")
    args = parser.parse_args()

    scored = run_eval(
        strategy_names=args.strategies,
        scenario_ids=args.scenarios,
    )

    print(format_report(scored))

    if args.json_output:
        import dataclasses
        out = [dataclasses.asdict(s) for s in scored]
        with open(args.json_output, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nJSON results written to {args.json_output}")


if __name__ == "__main__":
    main()
