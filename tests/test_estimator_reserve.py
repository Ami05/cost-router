"""Tests for DGC-202: safety reserve in dispatch context estimation.

Tests that:
- total_tokens EXCLUDES safety_reserve in warn mode (reported but non-blocking)
- total_tokens INCLUDES safety_reserve in enforce mode (counted in budget check)
- safety_reserve is 0 when bands.mode is off and outputs match prior behavior
- resume dispatches skip the reserve
- safety_reserve key is present in estimate dict used by --json output
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

# Add scripts directory to path for imports
scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from context_estimator import estimate_dispatch_context


class TestSafetyReserveWarnMode:
    """Test safety reserve behavior when bands.mode is 'warn' (default).

    In warn mode, safety_reserve is reported but excluded from total_tokens (non-blocking).
    """

    def test_safety_reserve_reported_but_excluded_in_warn_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When bands.mode is warn, safety_reserve defaults to 10000 but is excluded from total_tokens."""
        monkeypatch.setenv("COST_ROUTER_HOME", str(Path(__file__).resolve().parent / "fixtures"))

        with patch("context_estimator.config_get") as mock_config_get, \
             patch("context_estimator.config_int") as mock_config_int, \
             patch("context_estimator.corrected_estimate_tokens", return_value=100), \
             patch("context_estimator.model_context_window", return_value=200000), \
             patch("context_estimator.dispatch_budget_pct", return_value=0.5), \
             patch("context_estimator.infra_tokens", return_value=500), \
             patch("context_estimator.est_cold_write_cost_usd", return_value=0.01), \
             patch("context_estimator._project_root", return_value=Path(".")), \
             patch("context_estimator.classify_prompt_composition", return_value={
                 "invariant_tokens": 0, "incremental_tokens": 0,
                 "task_spec_tokens": 100, "invariant_ratio": 0.0
             }):

            mock_config_get.side_effect = lambda key, default: "warn" if key == "dispatch_context.bands.mode" else default
            mock_config_int.return_value = 10000

            result = estimate_dispatch_context("test prompt", model="haiku", resume=False)

            assert "safety_reserve" in result, "safety_reserve key must be present in output"
            assert result["safety_reserve"] == 10000, f"Expected safety_reserve=10000, got {result['safety_reserve']}"
            assert result["total_tokens"] == 500 + 100 + 0, \
                f"total_tokens should EXCLUDE safety_reserve in warn mode: infra + prompt + files (no reserve)"

    def test_safety_reserve_custom_config_value_warn_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When bands.mode is warn, safety_reserve respects config value but is excluded from total_tokens."""
        monkeypatch.setenv("COST_ROUTER_HOME", str(Path(__file__).resolve().parent / "fixtures"))

        with patch("context_estimator.config_get") as mock_config_get, \
             patch("context_estimator.config_int") as mock_config_int, \
             patch("context_estimator.corrected_estimate_tokens", return_value=100), \
             patch("context_estimator.model_context_window", return_value=200000), \
             patch("context_estimator.dispatch_budget_pct", return_value=0.5), \
             patch("context_estimator.infra_tokens", return_value=500), \
             patch("context_estimator.est_cold_write_cost_usd", return_value=0.01), \
             patch("context_estimator._project_root", return_value=Path(".")), \
             patch("context_estimator.classify_prompt_composition", return_value={
                 "invariant_tokens": 0, "incremental_tokens": 0,
                 "task_spec_tokens": 100, "invariant_ratio": 0.0
             }):

            mock_config_get.side_effect = lambda key, default: "warn" if key == "dispatch_context.bands.mode" else default
            mock_config_int.return_value = 15000

            result = estimate_dispatch_context("test prompt", model="haiku", resume=False)

            assert result["safety_reserve"] == 15000, f"Expected safety_reserve=15000, got {result['safety_reserve']}"
            assert result["total_tokens"] == 500 + 100 + 0, \
                f"total_tokens should EXCLUDE custom safety_reserve in warn mode"


class TestSafetyReserveEnforceMode:
    """Test safety reserve inclusion when bands.mode is 'enforce'."""

    def test_safety_reserve_included_with_enforce_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When bands.mode is enforce, safety_reserve defaults to 10000."""
        monkeypatch.setenv("COST_ROUTER_HOME", str(Path(__file__).resolve().parent / "fixtures"))

        with patch("context_estimator.config_get") as mock_config_get, \
             patch("context_estimator.config_int") as mock_config_int, \
             patch("context_estimator.corrected_estimate_tokens", return_value=100), \
             patch("context_estimator.model_context_window", return_value=200000), \
             patch("context_estimator.dispatch_budget_pct", return_value=0.5), \
             patch("context_estimator.infra_tokens", return_value=500), \
             patch("context_estimator.est_cold_write_cost_usd", return_value=0.01), \
             patch("context_estimator._project_root", return_value=Path(".")), \
             patch("context_estimator.classify_prompt_composition", return_value={
                 "invariant_tokens": 0, "incremental_tokens": 0,
                 "task_spec_tokens": 100, "invariant_ratio": 0.0
             }):

            mock_config_get.side_effect = lambda key, default: "enforce" if key == "dispatch_context.bands.mode" else default
            mock_config_int.return_value = 10000

            result = estimate_dispatch_context("test prompt", model="haiku", resume=False)

            assert result["safety_reserve"] == 10000
            assert result["total_tokens"] == 500 + 100 + 0 + 10000


class TestSafetyReserveOffMode:
    """Test safety reserve behavior when bands.mode is 'off'."""

    def test_safety_reserve_excluded_with_off_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When bands.mode is off, safety_reserve is 0 and outputs unchanged."""
        monkeypatch.setenv("COST_ROUTER_HOME", str(Path(__file__).resolve().parent / "fixtures"))

        with patch("context_estimator.config_get") as mock_config_get, \
             patch("context_estimator.config_int") as mock_config_int, \
             patch("context_estimator.corrected_estimate_tokens", return_value=100), \
             patch("context_estimator.model_context_window", return_value=200000), \
             patch("context_estimator.dispatch_budget_pct", return_value=0.5), \
             patch("context_estimator.infra_tokens", return_value=500), \
             patch("context_estimator.est_cold_write_cost_usd", return_value=0.01), \
             patch("context_estimator._project_root", return_value=Path(".")), \
             patch("context_estimator.classify_prompt_composition", return_value={
                 "invariant_tokens": 0, "incremental_tokens": 0,
                 "task_spec_tokens": 100, "invariant_ratio": 0.0
             }):

            mock_config_get.side_effect = lambda key, default: "off" if key == "dispatch_context.bands.mode" else default
            mock_config_int.return_value = 10000

            result = estimate_dispatch_context("test prompt", model="haiku", resume=False)

            assert result["safety_reserve"] == 0, "safety_reserve must be 0 when bands.mode is off"
            assert result["total_tokens"] == 500 + 100 + 0, \
                "total_tokens should NOT include safety_reserve when bands.mode is off"


class TestSafetyReserveResumeDispatch:
    """Test that resume dispatches skip safety reserve."""

    def test_resume_skips_safety_reserve_with_warn_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resume dispatches skip safety_reserve even when bands.mode is warn."""
        monkeypatch.setenv("COST_ROUTER_HOME", str(Path(__file__).resolve().parent / "fixtures"))

        with patch("context_estimator.config_get") as mock_config_get, \
             patch("context_estimator.config_int") as mock_config_int, \
             patch("context_estimator.corrected_estimate_tokens", return_value=100), \
             patch("context_estimator.model_context_window", return_value=200000), \
             patch("context_estimator.dispatch_budget_pct", return_value=0.5), \
             patch("context_estimator.infra_tokens", return_value=500), \
             patch("context_estimator.est_cold_write_cost_usd", return_value=0.01), \
             patch("context_estimator._project_root", return_value=Path(".")), \
             patch("context_estimator.classify_prompt_composition", return_value={
                 "invariant_tokens": 0, "incremental_tokens": 0,
                 "task_spec_tokens": 100, "invariant_ratio": 0.0
             }):

            mock_config_get.side_effect = lambda key, default: "warn" if key == "dispatch_context.bands.mode" else default
            mock_config_int.return_value = 10000

            result = estimate_dispatch_context("test prompt", model="haiku", resume=True)

            assert result["resume"] is True
            assert result["safety_reserve"] == 0, "resume=True should skip safety_reserve"
            assert result["total_tokens"] == 0 + 100 + 0, \
                "resume=True should skip both infra and safety_reserve"

    def test_resume_skips_safety_reserve_with_enforce_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resume dispatches skip safety_reserve even when bands.mode is enforce."""
        monkeypatch.setenv("COST_ROUTER_HOME", str(Path(__file__).resolve().parent / "fixtures"))

        with patch("context_estimator.config_get") as mock_config_get, \
             patch("context_estimator.config_int") as mock_config_int, \
             patch("context_estimator.corrected_estimate_tokens", return_value=100), \
             patch("context_estimator.model_context_window", return_value=200000), \
             patch("context_estimator.dispatch_budget_pct", return_value=0.5), \
             patch("context_estimator.infra_tokens", return_value=500), \
             patch("context_estimator.est_cold_write_cost_usd", return_value=0.01), \
             patch("context_estimator._project_root", return_value=Path(".")), \
             patch("context_estimator.classify_prompt_composition", return_value={
                 "invariant_tokens": 0, "incremental_tokens": 0,
                 "task_spec_tokens": 100, "invariant_ratio": 0.0
             }):

            mock_config_get.side_effect = lambda key, default: "enforce" if key == "dispatch_context.bands.mode" else default
            mock_config_int.return_value = 10000

            result = estimate_dispatch_context("test prompt", model="haiku", resume=True)

            assert result["safety_reserve"] == 0
            assert result["total_tokens"] == 0 + 100 + 0


class TestSafetyReserveJsonOutput:
    """Test that safety_reserve appears in JSON-serializable output."""

    def test_safety_reserve_in_json_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """safety_reserve key is present in estimate dict for JSON serialization."""
        monkeypatch.setenv("COST_ROUTER_HOME", str(Path(__file__).resolve().parent / "fixtures"))

        with patch("context_estimator.config_get") as mock_config_get, \
             patch("context_estimator.config_int") as mock_config_int, \
             patch("context_estimator.corrected_estimate_tokens", return_value=100), \
             patch("context_estimator.model_context_window", return_value=200000), \
             patch("context_estimator.dispatch_budget_pct", return_value=0.5), \
             patch("context_estimator.infra_tokens", return_value=500), \
             patch("context_estimator.est_cold_write_cost_usd", return_value=0.01), \
             patch("context_estimator._project_root", return_value=Path(".")), \
             patch("context_estimator.classify_prompt_composition", return_value={
                 "invariant_tokens": 0, "incremental_tokens": 0,
                 "task_spec_tokens": 100, "invariant_ratio": 0.0
             }):

            mock_config_get.side_effect = lambda key, default: "warn" if key == "dispatch_context.bands.mode" else default
            mock_config_int.return_value = 10000

            result = estimate_dispatch_context("test prompt", model="haiku", resume=False)

            # Verify key is present and is JSON-serializable (int type)
            assert "safety_reserve" in result
            assert isinstance(result["safety_reserve"], int)
            assert result["safety_reserve"] == 10000

            # Verify the full dict is JSON-serializable
            import json
            serialized = json.dumps(result)
            assert "safety_reserve" in serialized
            deserialized = json.loads(serialized)
            assert deserialized["safety_reserve"] == 10000

    def test_safety_reserve_off_in_json_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """safety_reserve key is present and 0 when bands.mode is off."""
        monkeypatch.setenv("COST_ROUTER_HOME", str(Path(__file__).resolve().parent / "fixtures"))

        with patch("context_estimator.config_get") as mock_config_get, \
             patch("context_estimator.config_int") as mock_config_int, \
             patch("context_estimator.corrected_estimate_tokens", return_value=100), \
             patch("context_estimator.model_context_window", return_value=200000), \
             patch("context_estimator.dispatch_budget_pct", return_value=0.5), \
             patch("context_estimator.infra_tokens", return_value=500), \
             patch("context_estimator.est_cold_write_cost_usd", return_value=0.01), \
             patch("context_estimator._project_root", return_value=Path(".")), \
             patch("context_estimator.classify_prompt_composition", return_value={
                 "invariant_tokens": 0, "incremental_tokens": 0,
                 "task_spec_tokens": 100, "invariant_ratio": 0.0
             }):

            mock_config_get.side_effect = lambda key, default: "off" if key == "dispatch_context.bands.mode" else default
            mock_config_int.return_value = 10000

            result = estimate_dispatch_context("test prompt", model="haiku", resume=False)

            import json
            serialized = json.dumps(result)
            deserialized = json.loads(serialized)
            assert deserialized["safety_reserve"] == 0


class TestSafetyReserveEdgeCases:
    """Test edge cases and interactions with other components."""

    def test_safety_reserve_zero_config_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config can override safety_reserve to 0 even in warn mode."""
        monkeypatch.setenv("COST_ROUTER_HOME", str(Path(__file__).resolve().parent / "fixtures"))

        with patch("context_estimator.config_get") as mock_config_get, \
             patch("context_estimator.config_int") as mock_config_int, \
             patch("context_estimator.corrected_estimate_tokens", return_value=100), \
             patch("context_estimator.model_context_window", return_value=200000), \
             patch("context_estimator.dispatch_budget_pct", return_value=0.5), \
             patch("context_estimator.infra_tokens", return_value=500), \
             patch("context_estimator.est_cold_write_cost_usd", return_value=0.01), \
             patch("context_estimator._project_root", return_value=Path(".")), \
             patch("context_estimator.classify_prompt_composition", return_value={
                 "invariant_tokens": 0, "incremental_tokens": 0,
                 "task_spec_tokens": 100, "invariant_ratio": 0.0
             }):

            mock_config_get.side_effect = lambda key, default: "warn" if key == "dispatch_context.bands.mode" else default
            mock_config_int.return_value = 0

            result = estimate_dispatch_context("test prompt", model="haiku", resume=False)

            assert result["safety_reserve"] == 0
            assert result["total_tokens"] == 500 + 100 + 0

    def test_safety_reserve_large_value_warn_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Large safety_reserve values in warn mode are reported but excluded from total_tokens."""
        monkeypatch.setenv("COST_ROUTER_HOME", str(Path(__file__).resolve().parent / "fixtures"))

        with patch("context_estimator.config_get") as mock_config_get, \
             patch("context_estimator.config_int") as mock_config_int, \
             patch("context_estimator.corrected_estimate_tokens", return_value=100), \
             patch("context_estimator.model_context_window", return_value=200000), \
             patch("context_estimator.dispatch_budget_pct", return_value=0.5), \
             patch("context_estimator.infra_tokens", return_value=500), \
             patch("context_estimator.est_cold_write_cost_usd", return_value=0.01), \
             patch("context_estimator._project_root", return_value=Path(".")), \
             patch("context_estimator.classify_prompt_composition", return_value={
                 "invariant_tokens": 0, "incremental_tokens": 0,
                 "task_spec_tokens": 100, "invariant_ratio": 0.0
             }):

            mock_config_get.side_effect = lambda key, default: "warn" if key == "dispatch_context.bands.mode" else default
            mock_config_int.return_value = 50000

            result = estimate_dispatch_context("test prompt", model="haiku", resume=False)

            assert result["safety_reserve"] == 50000, "Large reserve value should be reported"
            assert result["total_tokens"] == 500 + 100 + 0, \
                "total_tokens should EXCLUDE large safety_reserve in warn mode"
