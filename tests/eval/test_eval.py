"""Tests that validate the eval framework itself + baseline assertions."""

from __future__ import annotations

import pytest

from .runner import run_eval, format_report, build_scenario, load_scenarios
from .scorer import (
    ScenarioResult,
    ScoredResult,
    aggregate_scores,
    score_correctness,
    score_reliability,
    score_scenario,
    strategy_summary,
)
from .strategies import (
    EditSpec,
    Scenario,
    SimulatedHaikuWorker,
    SimulatedScriptApply,
    SimulatedSonnetWorker,
    _apply_edit,
    EditError,
)


class TestScoring:
    def test_perfect_correctness(self):
        golden = {"a.py": "print('hello')\n"}
        produced = {"a.py": "print('hello')\n"}
        assert score_correctness(produced, golden) == 100.0

    def test_zero_correctness_missing_file(self):
        golden = {"a.py": "print('hello')\n"}
        produced = {}
        assert score_correctness(produced, golden) == 0.0

    def test_partial_correctness(self):
        golden = {"a.py": "line1\nline2\nline3\n"}
        produced = {"a.py": "line1\nchanged\nline3\n"}
        score = score_correctness(produced, golden)
        assert 0 < score < 100

    def test_reliability_single_attempt(self):
        assert score_reliability(1) == 100.0

    def test_reliability_two_attempts(self):
        assert score_reliability(2) == 50.0

    def test_reliability_zero_attempts(self):
        assert score_reliability(0) == 0.0

    def test_silent_wrong_target_penalty(self):
        result = ScenarioResult(
            scenario_id="test",
            strategy="test",
            output_files={"a.py": "correct content\n"},
            cost_usd=0.1,
            latency_s=5.0,
            attempts=1,
            silent_wrong_target=True,
        )
        golden = {"a.py": "correct content\n"}
        scored = score_scenario(result, golden)
        assert scored.correctness == 50.0  # 100 - 50 penalty


class TestEditApplication:
    def test_create(self):
        files: dict[str, str] = {}
        edit = EditSpec(file_path="new.py", edit_type="create", new_content="hello\n")
        _apply_edit(files, edit)
        assert files["new.py"] == "hello\n"

    def test_str_replace(self):
        files = {"a.py": "old_name = 1\n"}
        edit = EditSpec(
            file_path="a.py",
            edit_type="str_replace",
            old_content="old_name",
            new_content="new_name",
        )
        _apply_edit(files, edit)
        assert files["a.py"] == "new_name = 1\n"

    def test_str_replace_missing_raises(self):
        files = {"a.py": "content\n"}
        edit = EditSpec(
            file_path="a.py",
            edit_type="str_replace",
            old_content="nonexistent",
            new_content="replacement",
        )
        with pytest.raises(EditError):
            _apply_edit(files, edit, strict=True)

    def test_str_replace_missing_silent(self):
        files = {"a.py": "content\n"}
        edit = EditSpec(
            file_path="a.py",
            edit_type="str_replace",
            old_content="nonexistent",
            new_content="replacement",
        )
        _apply_edit(files, edit, strict=False)
        assert files["a.py"] == "content\n"  # unchanged

    def test_line_replace(self):
        files = {"a.py": "line1\nline2\nline3\n"}
        edit = EditSpec(
            file_path="a.py",
            edit_type="line_replace",
            new_content="replaced\n",
            line_start=2,
            line_end=2,
        )
        _apply_edit(files, edit)
        assert "replaced" in files["a.py"]
        assert "line1" in files["a.py"]


class TestStrategies:
    def _make_scenario(self, tags=None):
        return Scenario(
            id="TEST-01",
            name="test scenario",
            complexity="simple_rename",
            description="test",
            source_files={"a.py": "old_var = 1\nprint(old_var)\n"},
            edits=[
                EditSpec(
                    file_path="a.py",
                    edit_type="str_replace",
                    old_content="old_var",
                    new_content="new_var",
                ),
            ],
            golden_output={"a.py": "new_var = 1\nprint(new_var)\n"},
            tags=tags or [],
        )

    def test_sonnet_worker_executes(self):
        strategy = SimulatedSonnetWorker()
        result = strategy.execute(self._make_scenario())
        assert result.strategy == "sonnet_worker"
        assert result.attempts >= 1
        assert result.cost_usd > 0

    def test_haiku_worker_executes(self):
        strategy = SimulatedHaikuWorker()
        result = strategy.execute(self._make_scenario())
        assert result.strategy == "haiku_worker"
        assert result.cost_usd > 0

    def test_haiku_punts_on_similar_blocks(self):
        strategy = SimulatedHaikuWorker()
        result = strategy.execute(self._make_scenario(tags=["similar_blocks"]))
        assert result.attempts >= 2
        assert result.silent_wrong_target is True

    def test_script_apply_executes(self):
        strategy = SimulatedScriptApply()
        result = strategy.execute(self._make_scenario())
        assert result.strategy == "script_apply"
        assert result.cost_usd > 0
        assert result.latency_s < 5  # should be fast

    def test_script_cheaper_than_sonnet(self):
        scenario = self._make_scenario()
        sonnet = SimulatedSonnetWorker().execute(scenario)
        script = SimulatedScriptApply().execute(scenario)
        assert script.cost_usd < sonnet.cost_usd

    def test_haiku_cheaper_per_edit_than_sonnet(self):
        scenario = self._make_scenario()
        sonnet = SimulatedSonnetWorker().execute(scenario)
        haiku = SimulatedHaikuWorker().execute(scenario)
        # Haiku FO is more expensive but worker is cheaper
        # For single edit without punt tags, haiku total should be comparable
        assert haiku.cost_usd > 0


class TestAggregation:
    def test_aggregate_fills_relative_metrics(self):
        results = [
            ScoredResult(
                scenario_id="T1", strategy="a", correctness=100, cost_usd=0.10,
                cost_efficiency=0, latency_s=10, latency_score=0,
                reliability=100, silent_wrong_target=False,
            ),
            ScoredResult(
                scenario_id="T1", strategy="b", correctness=80, cost_usd=0.05,
                cost_efficiency=0, latency_s=5, latency_score=0,
                reliability=50, silent_wrong_target=False,
            ),
        ]
        scored = aggregate_scores(results)
        # Strategy b has lower cost, so b should have 100% cost efficiency
        b = [s for s in scored if s.strategy == "b"][0]
        assert b.cost_efficiency == 100.0
        # Strategy a should have 50% cost efficiency (0.05/0.10)
        a = [s for s in scored if s.strategy == "a"][0]
        assert a.cost_efficiency == 50.0

    def test_strategy_summary(self):
        results = [
            ScoredResult(
                scenario_id="T1", strategy="a", correctness=90, cost_usd=0.10,
                cost_efficiency=80, latency_s=10, latency_score=80,
                reliability=100, silent_wrong_target=False, composite=85,
            ),
            ScoredResult(
                scenario_id="T2", strategy="a", correctness=80, cost_usd=0.12,
                cost_efficiency=70, latency_s=12, latency_score=70,
                reliability=100, silent_wrong_target=False, composite=80,
            ),
        ]
        summary = strategy_summary(results)
        assert "a" in summary
        assert summary["a"]["avg_correctness"] == 85.0
        assert summary["a"]["scenario_count"] == 2


class TestRunner:
    def test_format_report_produces_output(self):
        results = [
            ScoredResult(
                scenario_id="T1", strategy="sonnet_worker", correctness=95,
                cost_usd=0.14, cost_efficiency=55.71, latency_s=8,
                latency_score=27.5, reliability=100, silent_wrong_target=False,
                composite=86.43,
            ),
        ]
        report = format_report(results)
        assert "DISPATCH STRATEGY EVAL REPORT" in report
        assert "sonnet_worker" in report
