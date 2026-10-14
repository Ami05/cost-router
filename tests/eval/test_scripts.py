"""Consolidated eval script tests.

Groups:
  - TestEvalGatesFirstBasics / TestGracefulMissingData / TestOutputStructure /
    TestReportGeneration / TestSimulationStructure / TestCliFlags  (from test_eval_gates_first.py)
  - TestEvalIntentClassifier  (from test_eval_intent_classifier.py)
  - TestPerturbationBenchmark  (from test_perturbation_benchmark.py)
  - TestRunRoutingValidation  (from test_run_routing_validation.py)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Resolve script paths relative to project root
_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# eval_gates_first.py tests (from test_eval_gates_first.py)
# ---------------------------------------------------------------------------

_GATES_SCRIPT = str(SCRIPTS_DIR / "eval_gates_first.py")


class TestEvalGatesFirstBasics:
    """Basic functionality tests."""

    def test_runs_without_error(self):
        """Test that the script runs without error via subprocess."""
        result = subprocess.run(
            ["python3", _GATES_SCRIPT],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f"Script failed with stderr: {result.stderr}"

    def test_exemplars_only_flag(self):
        """Test that --exemplars-only flag runs without error."""
        result = subprocess.run(
            ["python3", _GATES_SCRIPT, "--exemplars-only"],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f"Script failed with stderr: {result.stderr}"
        assert "exemplars-only" in result.stdout or "--exemplars-only" in result.stdout


class TestGracefulMissingData:
    """Test handling of missing synthetic data."""

    def test_missing_synthetic_graceful(self):
        """Test that missing synthetic data does not crash the script."""
        from eval_gates_first import load_synthetic

        with mock.patch("eval_gates_first.ROOT_DIR", PROJECT_ROOT):
            result = load_synthetic()
            assert isinstance(result, list)
            assert result is not None


class TestOutputStructure:
    """Test that output metrics have expected structure."""

    def test_output_keys(self):
        """Test that evaluation output contains key metric fields."""
        from classify_intent import classify_intent_detailed
        from eval_gates_first import compute_metrics, load_exemplars, run_simulation

        samples = load_exemplars()
        assert len(samples) > 0, "No exemplars loaded"

        results = run_simulation(samples, classify_intent_detailed)
        assert len(results) > 0, "No results from simulation"

        metrics = compute_metrics(results)

        required_keys = [
            "total_samples",
            "coverage",
            "gate_accuracy",
            "classifier_accuracy_overall",
            "uncertainty_rate_current",
            "uncertainty_rate_projected",
            "cost_classifier_first",
            "cost_gates_first",
            "cost_savings_pct",
            "false_confidence_rate",
            "per_class",
            "gate_errors",
            "model_dist_classifier",
            "model_dist_gates",
        ]

        for key in required_keys:
            assert key in metrics, f"Missing metric key: {key}"

        assert isinstance(metrics["total_samples"], int)
        assert isinstance(metrics["coverage"], float)
        assert isinstance(metrics["gate_accuracy"], float)
        assert isinstance(metrics["classifier_accuracy_overall"], float)
        assert isinstance(metrics["cost_savings_pct"], (int, float))
        assert isinstance(metrics["per_class"], dict)


class TestReportGeneration:
    """Test report generation."""

    def test_report_generation(self):
        """Test that report is generated successfully."""
        from classify_intent import classify_intent_detailed
        from eval_gates_first import compute_metrics, generate_report, load_exemplars, run_simulation

        samples = load_exemplars()
        results = run_simulation(samples, classify_intent_detailed)
        metrics = compute_metrics(results)
        report = generate_report(metrics, results)

        assert isinstance(report, str)
        assert len(report) > 0
        assert "Gates-First vs Classifier-First" in report
        assert "Executive Summary" in report
        assert "Verdict" in report


class TestSimulationStructure:
    """Test simulation result structure."""

    def test_simulation_result_keys(self):
        """Test that each simulation result has required keys."""
        from classify_intent import classify_intent_detailed
        from eval_gates_first import load_exemplars, run_simulation

        samples = load_exemplars()
        results = run_simulation(samples, classify_intent_detailed)

        required_keys = [
            "text",
            "expected_intent",
            "data_source",
            "gate_name",
            "gate_intent",
            "gate_model",
            "gate_correct",
            "classifier_intent",
            "classifier_confidence",
            "classifier_source",
            "classifier_correct",
        ]

        for result in results:
            for key in required_keys:
                assert key in result, f"Missing key in result: {key}"


class TestCliFlags:
    """Test CLI flag handling."""

    def test_exemplars_only_affects_sample_count(self):
        """Test that --exemplars-only reduces total samples."""
        result_exemplars_only = subprocess.run(
            ["python3", _GATES_SCRIPT, "--exemplars-only"],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert result_exemplars_only.returncode == 0

        exemplars_only_count = None
        for line in result_exemplars_only.stdout.split("\n"):
            if "Total:" in line:
                parts = line.split()
                try:
                    exemplars_only_count = int(parts[-1])
                    break
                except (ValueError, IndexError):
                    pass

        result_all = subprocess.run(
            ["python3", _GATES_SCRIPT],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert result_all.returncode == 0

        all_count = None
        for line in result_all.stdout.split("\n"):
            if "Total:" in line:
                parts = line.split()
                try:
                    all_count = int(parts[-1])
                    break
                except (ValueError, IndexError):
                    pass

        if exemplars_only_count is not None and all_count is not None:
            assert all_count >= exemplars_only_count


# ---------------------------------------------------------------------------
# eval_intent_classifier.py tests (from test_eval_intent_classifier.py)
# ---------------------------------------------------------------------------

_CLASSIFIER_SCRIPT = str(SCRIPTS_DIR / "eval_intent_classifier.py")


class TestEvalIntentClassifier:
    """Tests for eval_intent_classifier.py."""

    def test_runs_without_error(self):
        """Test that the script runs without error with default arguments."""
        result = subprocess.run(
            ["python3", _CLASSIFIER_SCRIPT],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Script failed with stderr: {result.stderr}"

    def test_json_output_written(self, tmp_path):
        """Test that the script writes a valid JSON output file."""
        output_path = tmp_path / "intent_eval.json"

        result = subprocess.run(
            ["python3", _CLASSIFIER_SCRIPT, "--output", str(output_path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        assert output_path.exists(), f"Output file not created at {output_path}"

        with open(output_path) as f:
            data = json.load(f)
        assert isinstance(data, dict), "Output is not a JSON object"

    def test_json_output_has_accuracy(self, tmp_path):
        """Test that output JSON contains 'accuracy' key with valid float value."""
        output_path = tmp_path / "intent_eval.json"
        result = subprocess.run(
            ["python3", _CLASSIFIER_SCRIPT, "--output", str(output_path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        with open(output_path) as f:
            data = json.load(f)

        assert "accuracy" in data, "Output JSON missing 'accuracy' key"
        accuracy = data["accuracy"]
        assert isinstance(accuracy, (int, float)), f"accuracy is not a number: {type(accuracy)}"
        assert 0 <= accuracy <= 1, f"accuracy {accuracy} is not between 0 and 1"

    def test_json_output_has_per_class(self, tmp_path):
        """Test that output JSON contains 'per_class_metrics' key with valid dict."""
        output_path = tmp_path / "intent_eval.json"
        result = subprocess.run(
            ["python3", _CLASSIFIER_SCRIPT, "--output", str(output_path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        with open(output_path) as f:
            data = json.load(f)

        assert "per_class_metrics" in data, "Output JSON missing 'per_class_metrics' key"
        per_class = data["per_class_metrics"]
        assert isinstance(per_class, dict), f"per_class_metrics is not a dict: {type(per_class)}"
        assert len(per_class) > 0, "per_class_metrics is empty"

    def test_min_accuracy_flag_pass(self):
        """Test that --min-accuracy 0.0 passes (threshold is always met)."""
        result = subprocess.run(
            ["python3", _CLASSIFIER_SCRIPT, "--min-accuracy", "0.0"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Script failed with min-accuracy 0.0: {result.stderr}"

    def test_min_accuracy_flag_fail(self):
        """Test that --min-accuracy 1.01 fails (impossible threshold)."""
        result = subprocess.run(
            ["python3", _CLASSIFIER_SCRIPT, "--min-accuracy", "1.01"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, (
            f"Script should have failed with min-accuracy 1.01, "
            f"but got exit code {result.returncode}"
        )

    def test_custom_output_path(self):
        """Test that --output flag writes to a custom path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_output = os.path.join(tmpdir, "test_intent_eval.json")

            result = subprocess.run(
                ["python3", _CLASSIFIER_SCRIPT, "--output", custom_output],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"Script failed with custom output: {result.stderr}"

            assert os.path.exists(custom_output), f"Custom output file not created at {custom_output}"

            with open(custom_output) as f:
                data = json.load(f)
            assert "accuracy" in data, "Custom output file missing 'accuracy' key"
            accuracy = data["accuracy"]
            assert 0 <= accuracy <= 1, f"accuracy {accuracy} is not between 0 and 1"


# ---------------------------------------------------------------------------
# perturbation_benchmark.py tests (from test_perturbation_benchmark.py)
# ---------------------------------------------------------------------------

_PERTURBATION_SCRIPT = str(SCRIPTS_DIR / "perturbation_benchmark.py")


class TestPerturbationBenchmark:
    """Tests for perturbation_benchmark.py script."""

    def test_runs_without_error(self):
        """Test that the script runs without error."""
        result = subprocess.run(
            [sys.executable, _PERTURBATION_SCRIPT],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Script failed with stderr: {result.stderr}"

    def test_json_flag_output(self):
        """Test that --json flag produces valid JSON output."""
        result = subprocess.run(
            [sys.executable, _PERTURBATION_SCRIPT, "--json"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Script failed with stderr: {result.stderr}"
        output = json.loads(result.stdout)
        assert isinstance(output, dict), "JSON output should be a dictionary"

    def test_json_has_perturbation_types(self):
        """Test that JSON output contains results for expected perturbation types."""
        result = subprocess.run(
            [sys.executable, _PERTURBATION_SCRIPT, "--json"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        output = json.loads(result.stdout)
        assert "per_type" in output, "Output should contain 'per_type' key"
        per_type = output["per_type"]

        expected_types = ["clean", "typo", "truncated", "verbose"]
        for ptype in expected_types:
            assert ptype in per_type, f"Expected perturbation type '{ptype}' not found in results"

    def test_json_output_file_written(self, tmp_path):
        """Test that output JSON file is written to specified path."""
        output_file = tmp_path / "perturbation_benchmark.json"

        result = subprocess.run(
            [sys.executable, _PERTURBATION_SCRIPT, "--output", str(output_file)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        assert output_file.exists(), f"Output file not found at {output_file}"

        with open(output_file) as f:
            data = json.load(f)
        assert isinstance(data, dict), "Output file should contain valid JSON dict"

    def test_all_types_have_accuracy(self):
        """Test that all perturbation types have accuracy key and value."""
        result = subprocess.run(
            [sys.executable, _PERTURBATION_SCRIPT, "--json"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        output = json.loads(result.stdout)
        per_type = output.get("per_type", {})

        for ptype, info in per_type.items():
            assert "accuracy" in info, f"Perturbation type '{ptype}' missing 'accuracy' key"
            assert isinstance(info["accuracy"], (int, float)), (
                f"Accuracy for '{ptype}' should be a number, got {type(info['accuracy'])}"
            )

    def test_custom_exemplars_path(self):
        """Test that --exemplars-path argument works with custom path."""
        exemplars_path = PROJECT_ROOT / "config" / "intent_exemplars.json"

        result = subprocess.run(
            [sys.executable, _PERTURBATION_SCRIPT, "--exemplars-path", str(exemplars_path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode in [0, 1], f"Unexpected exit code: {result.returncode}"

    def test_accuracy_gap_present(self):
        """Test that JSON output contains accuracy gap information."""
        result = subprocess.run(
            [sys.executable, _PERTURBATION_SCRIPT, "--json"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        output = json.loads(result.stdout)
        assert "max_accuracy_gap" in output, "Output should contain 'max_accuracy_gap' key"
        assert "clean_accuracy" in output, "Output should contain 'clean_accuracy' key"
        assert "worst_type" in output, "Output should contain 'worst_type' key"
        assert "worst_accuracy" in output, "Output should contain 'worst_accuracy' key"


# ---------------------------------------------------------------------------
# run_routing_validation.py tests (from test_run_routing_validation.py)
# ---------------------------------------------------------------------------

_ROUTING_SCRIPT = SCRIPTS_DIR / "run_routing_validation.py"


class TestRunRoutingValidation:
    """Test suite for run_routing_validation.py script."""

    def test_runs_without_error(self):
        """Verify the script runs with default args and exits cleanly."""
        result = subprocess.run(
            [sys.executable, str(_ROUTING_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"

    def test_json_output_written(self):
        """Verify the script writes valid JSON output to data/routing_validation.json."""
        output_file = PROJECT_ROOT / "data" / "routing_validation.json"

        subprocess.run(
            [sys.executable, str(_ROUTING_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )

        assert output_file.exists(), f"Output file not created at {output_file}"

        try:
            with open(output_file) as f:
                json.load(f)
        except json.JSONDecodeError as e:
            pytest.fail(f"Output file is not valid JSON: {e}")

    def test_output_has_overall_pass(self):
        """Verify output JSON contains 'overall_pass' boolean key."""
        output_file = PROJECT_ROOT / "data" / "routing_validation.json"

        subprocess.run(
            [sys.executable, str(_ROUTING_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )

        with open(output_file) as f:
            data = json.load(f)

        assert "overall_pass" in data, "Missing 'overall_pass' key in output"
        assert isinstance(data["overall_pass"], bool), "'overall_pass' is not a boolean"

    def test_output_has_scenarios(self):
        """Verify output JSON contains 'scenarios' list."""
        output_file = PROJECT_ROOT / "data" / "routing_validation.json"

        subprocess.run(
            [sys.executable, str(_ROUTING_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )

        with open(output_file) as f:
            data = json.load(f)

        assert "scenarios" in data, "Missing 'scenarios' key in output"
        assert isinstance(data["scenarios"], list), "'scenarios' is not a list"

    def test_each_scenario_has_pass_rate(self):
        """Verify each scenario in output has 'pass_rate' key."""
        output_file = PROJECT_ROOT / "data" / "routing_validation.json"

        subprocess.run(
            [sys.executable, str(_ROUTING_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )

        with open(output_file) as f:
            data = json.load(f)

        scenarios = data.get("scenarios", [])
        assert len(scenarios) > 0, "No scenarios found in output"

        for scenario in scenarios:
            assert "pass_rate" in scenario, (
                f"Scenario {scenario.get('id')} missing 'pass_rate' key"
            )

    def test_custom_output_path(self):
        """Verify script respects --output flag with custom path."""
        output_file = Path("/tmp/test_routing_val.json")

        if output_file.exists():
            output_file.unlink()

        result = subprocess.run(
            [sys.executable, str(_ROUTING_SCRIPT), "--output", str(output_file)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert output_file.exists(), f"Output file not created at {output_file}"

        try:
            with open(output_file) as f:
                json.load(f)
        except json.JSONDecodeError as e:
            pytest.fail(f"Output file is not valid JSON: {e}")

        output_file.unlink()
