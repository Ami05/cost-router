#!/usr/bin/env python3
"""Test force_fixed_window override in token_estimation config.

Tests that:
1. With force_fixed_window set to a positive value, all models resolve to that window
2. With force_fixed_window = 0 or absent, models resolve to their normal windows
3. _context_pressure_check uses the forced window for ratio computation
"""

from __future__ import annotations

from typing import Any

import pytest

import sys
from pathlib import Path

# Add scripts to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _model_utils import model_context_window_resolved, model_context_window
from routing_resolver import _context_pressure_check


class TestForceFixedWindow:
    """Test force_fixed_window override behavior."""

    def test_force_fixed_window_200k_all_models(self, monkeypatch):
        """With force_fixed_window=200000, all models resolve to (200000, False)."""
        def mock_config_int(path: str, default: int = 0) -> int:
            if path == "token_estimation.force_fixed_window":
                return 200000
            # Return defaults for other paths (only used if force_fixed_window is 0)
            if path == "token_estimation.context_windows.default":
                return 200000
            return default

        monkeypatch.setattr(
            "_model_utils.config_int",
            mock_config_int,
        )

        # Test various model names
        test_models = [
            "claude-4.5-haiku-20251001",
            "claude-sonnet-5-thinking-medium",
            "claude-opus-4-20250514",
            "composer-2.5-fast",
            "unknown-model-xyz",
            None,
        ]

        for model in test_models:
            window, is_default = model_context_window_resolved(model)
            assert window == 200000, f"Model {model} should resolve to 200000"
            assert is_default is False, f"Model {model} should have is_default=False"

    def test_force_fixed_window_0_uses_model_specific(self, monkeypatch):
        """With force_fixed_window=0, models use their config-specific windows."""
        def mock_config_int(path: str, default: int = 0) -> int:
            if path == "token_estimation.force_fixed_window":
                return 0
            if path == "token_estimation.context_windows.default":
                return 200000
            if path == "token_estimation.context_windows.sonnet_45":
                return 160000
            if path == "token_estimation.context_windows.composer":
                return 180000
            return default

        monkeypatch.setattr(
            "_model_utils.config_int",
            mock_config_int,
        )

        # Test that models resolve to their specific windows
        window, is_default = model_context_window_resolved("claude-sonnet-4.5")
        assert window == 160000, "sonnet-4.5 should resolve to 160000"
        assert is_default is False

        window, is_default = model_context_window_resolved("composer-2.5-fast")
        assert window == 180000, "composer should resolve to 180000"
        assert is_default is False

        # Unknown model falls back to default
        window, is_default = model_context_window_resolved("unknown-model")
        assert window == 200000, "unknown model should use default 200000"
        assert is_default is True

    def test_force_fixed_window_absent_uses_defaults(self, monkeypatch):
        """With force_fixed_window absent (not in config), use model-specific windows."""
        def mock_config_int(path: str, default: int = 0) -> int:
            # Simulate config key being absent by not explicitly setting force_fixed_window
            # which causes config_int to return default (0)
            if path == "token_estimation.context_windows.default":
                return 200000
            if path == "token_estimation.context_windows.sonnet_45":
                return 160000
            return default

        monkeypatch.setattr(
            "_model_utils.config_int",
            mock_config_int,
        )

        # Model-specific window should be used
        window, is_default = model_context_window_resolved("claude-sonnet-4.5")
        assert window == 160000
        assert is_default is False

        # Default for unknown
        window, is_default = model_context_window_resolved("unknown")
        assert window == 200000
        assert is_default is True

    def test_force_fixed_window_variant_values(self, monkeypatch):
        """Test various force_fixed_window values (not just 200000)."""
        test_values = [100000, 200000, 500000, 1000000]

        for force_value in test_values:
            def mock_config_int(path: str, default: int = 0) -> int:
                if path == "token_estimation.force_fixed_window":
                    return force_value
                if path == "token_estimation.context_windows.default":
                    return 200000
                return default

            monkeypatch.setattr(
                "_model_utils.config_int",
                mock_config_int,
            )

            window, is_default = model_context_window_resolved("sonnet-45")
            assert window == force_value, f"Should resolve to {force_value}"
            assert is_default is False

    def test_force_fixed_window_1m_mapped_sonnet(self, monkeypatch):
        """Test force_fixed_window with 1M-mapped sonnet models."""
        def mock_config_int(path: str, default: int = 0) -> int:
            if path == "token_estimation.force_fixed_window":
                return 200000
            if path == "token_estimation.context_windows.default":
                return 200000
            if path == "token_estimation.context_windows.sonnet_5_5":
                return 1000000  # Normally 1M for sonnet-5-5
            return default

        monkeypatch.setattr(
            "_model_utils.config_int",
            mock_config_int,
        )

        # Even with 1M config, force_fixed_window should override
        window, is_default = model_context_window_resolved("claude-sonnet-5-5-20250514")
        assert window == 200000, "Should override 1M window with forced 200000"
        assert is_default is False

    def test_context_pressure_check_uses_forced_window(self, monkeypatch, tmp_path):
        """Test that _context_pressure_check uses force_fixed_window in ratio calc."""
        # Create a minimal mock transcript
        transcript_path = tmp_path / "transcript.md"
        transcript_path.write_text("# Transcript\n")

        forced_window = 200000

        def mock_config_int(path: str, default: int = 0) -> int:
            if path == "token_estimation.force_fixed_window":
                return forced_window
            if path == "token_estimation.context_windows.default":
                return 200000
            return default

        def mock_config_get(path: str) -> Any:
            if path == "context_pressure.mode":
                return "warn"
            if path == "context_pressure.threshold":
                return 0.85
            return None

        # Mock the transcript reading function to return known tokens
        def mock_read_live_context_tokens(path: str) -> dict[str, Any] | None:
            return {
                "tokens": 150000,  # 75% of 200000
                "model": "claude-sonnet-4.5",
            }

        monkeypatch.setattr(
            "_model_utils.config_int",
            mock_config_int,
        )
        monkeypatch.setattr(
            "routing_resolver.config_get",
            mock_config_get,
        )
        monkeypatch.setattr(
            "routing_resolver._read_live_context_tokens",
            mock_read_live_context_tokens,
        )

        result = _context_pressure_check("session-123", str(transcript_path))

        # Should not exceed threshold (75% < 85%)
        assert result["should_deny"] is False
        assert result["would_deny"] is False
        assert result["context_pct"] == 0.75

    def test_context_pressure_check_threshold_with_forced_window(
        self, monkeypatch, tmp_path
    ):
        """Test that threshold is correctly applied with forced window."""
        transcript_path = tmp_path / "transcript.md"
        transcript_path.write_text("# Transcript\n")

        forced_window = 200000
        live_tokens = 180000  # 90% of 200000, exceeds 85% threshold

        def mock_config_int(path: str, default: int = 0) -> int:
            if path == "token_estimation.force_fixed_window":
                return forced_window
            if path == "token_estimation.context_windows.default":
                return 200000
            return default

        def mock_config_get(path: str) -> Any:
            if path == "context_pressure.mode":
                return "warn"
            if path == "context_pressure.threshold":
                return 0.85
            return None

        def mock_read_live_context_tokens(path: str) -> dict[str, Any] | None:
            return {
                "tokens": live_tokens,
                "model": "claude-sonnet-4.5",
            }

        monkeypatch.setattr(
            "_model_utils.config_int",
            mock_config_int,
        )
        monkeypatch.setattr(
            "routing_resolver.config_get",
            mock_config_get,
        )
        monkeypatch.setattr(
            "routing_resolver._read_live_context_tokens",
            mock_read_live_context_tokens,
        )

        result = _context_pressure_check("session-123", str(transcript_path))

        # Should trigger warning (90% >= 85%)
        assert result["should_deny"] is False
        assert result["would_deny"] is True
        assert abs(result["context_pct"] - 0.9) < 0.0001

    def test_model_context_window_shorthand(self, monkeypatch):
        """Test the model_context_window() shorthand returns the window value."""
        def mock_config_int(path: str, default: int = 0) -> int:
            if path == "token_estimation.force_fixed_window":
                return 200000
            if path == "token_estimation.context_windows.default":
                return 200000
            return default

        monkeypatch.setattr(
            "_model_utils.config_int",
            mock_config_int,
        )

        window = model_context_window("any-model")
        assert window == 200000

    def test_force_fixed_window_negative_treated_as_zero(self, monkeypatch):
        """Test that negative force_fixed_window is treated as 0 (not set)."""
        def mock_config_int(path: str, default: int = 0) -> int:
            if path == "token_estimation.force_fixed_window":
                return -1000  # Negative value
            if path == "token_estimation.context_windows.default":
                return 200000
            if path == "token_estimation.context_windows.sonnet_45":
                return 160000
            return default

        monkeypatch.setattr(
            "_model_utils.config_int",
            mock_config_int,
        )

        # Negative values should be treated as "not set" in the implementation
        # The code checks "if force_window > 0:" so negative is falsy
        window, is_default = model_context_window_resolved("claude-sonnet-4.5")
        assert window == 160000, "Negative force_fixed_window should be ignored"
        assert is_default is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
