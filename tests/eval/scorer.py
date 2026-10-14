"""Scoring module for dispatch strategy evaluation.

Computes per-scenario and aggregate scores across dimensions:
  - correctness: diff vs golden output (0-100)
  - cost_efficiency: min_cost / this_cost (0-100)
  - reliability: 1 / attempts (0-100)
  - latency_score: min_latency / this_latency (0-100)

Composite = weighted sum of dimensions.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any


DEFAULT_WEIGHTS = {
    "correctness": 0.50,
    "cost_efficiency": 0.25,
    "reliability": 0.25,
}


@dataclass
class ScenarioResult:
    """Raw result from running one scenario with one strategy."""
    scenario_id: str
    strategy: str
    output_files: dict[str, str]
    cost_usd: float
    latency_s: float
    attempts: int
    silent_wrong_target: bool = False
    error: str | None = None


@dataclass
class ScoredResult:
    """Scored result after comparing against golden output."""
    scenario_id: str
    strategy: str
    correctness: float
    cost_usd: float
    cost_efficiency: float
    latency_s: float
    latency_score: float
    reliability: float
    silent_wrong_target: bool
    attempts: int = 1
    composite: float = 0.0
    error: str | None = None


def score_correctness(
    produced: dict[str, str],
    golden: dict[str, str],
) -> float:
    if not golden:
        return 0.0
    total_ratio = 0.0
    for filename, expected_content in golden.items():
        produced_content = produced.get(filename, "")
        ratio = difflib.SequenceMatcher(
            None,
            expected_content.splitlines(keepends=True),
            produced_content.splitlines(keepends=True),
        ).ratio()
        total_ratio += ratio
    return round((total_ratio / len(golden)) * 100, 2)


def score_reliability(attempts: int) -> float:
    if attempts <= 0:
        return 0.0
    return round((1.0 / attempts) * 100, 2)


def score_scenario(
    result: ScenarioResult,
    golden: dict[str, str],
) -> ScoredResult:
    correctness = score_correctness(result.output_files, golden)
    if result.silent_wrong_target:
        correctness = max(0, correctness - 50)
    return ScoredResult(
        scenario_id=result.scenario_id,
        strategy=result.strategy,
        correctness=correctness,
        cost_usd=result.cost_usd,
        cost_efficiency=0.0,
        latency_s=result.latency_s,
        latency_score=0.0,
        reliability=score_reliability(result.attempts),
        silent_wrong_target=result.silent_wrong_target,
        attempts=result.attempts,
        error=result.error,
    )


def aggregate_scores(
    scored: list[ScoredResult],
    weights: dict[str, float] | None = None,
) -> list[ScoredResult]:
    w = weights or DEFAULT_WEIGHTS
    by_scenario: dict[str, list[ScoredResult]] = {}
    for s in scored:
        by_scenario.setdefault(s.scenario_id, []).append(s)
    for scenario_id, results in by_scenario.items():
        costs = [r.cost_usd for r in results if r.cost_usd > 0]
        latencies = [r.latency_s for r in results if r.latency_s > 0]
        min_cost = min(costs) if costs else 1.0
        min_latency = min(latencies) if latencies else 1.0
        for r in results:
            r.cost_efficiency = (
                round((min_cost / r.cost_usd) * 100, 2) if r.cost_usd > 0 else 0.0
            )
            r.latency_score = (
                round((min_latency / r.latency_s) * 100, 2) if r.latency_s > 0 else 0.0
            )
            r.composite = round(
                r.correctness * w.get("correctness", 0.5)
                + r.cost_efficiency * w.get("cost_efficiency", 0.25)
                + r.reliability * w.get("reliability", 0.25),
                2,
            )
    return scored


def strategy_summary(scored: list[ScoredResult]) -> dict[str, dict[str, float]]:
    by_strategy: dict[str, list[ScoredResult]] = {}
    for s in scored:
        by_strategy.setdefault(s.strategy, []).append(s)
    summary: dict[str, dict[str, float]] = {}
    for strategy, results in by_strategy.items():
        n = len(results)
        summary[strategy] = {
            "avg_correctness": round(sum(r.correctness for r in results) / n, 2),
            "avg_cost_usd": round(sum(r.cost_usd for r in results) / n, 4),
            "avg_cost_efficiency": round(sum(r.cost_efficiency for r in results) / n, 2),
            "avg_latency_s": round(sum(r.latency_s for r in results) / n, 2),
            "avg_reliability": round(sum(r.reliability for r in results) / n, 2),
            "avg_composite": round(sum(r.composite for r in results) / n, 2),
            "silent_wrong_target_count": sum(1 for r in results if r.silent_wrong_target),
            "scenario_count": n,
        }
    return summary
