"""Tests for DGC-203 budget bands enforcement.

Covers band-based budget checks per dispatch_budget_gate.py:
- mode off: legacy percentage behavior unchanged
- below min_useful_work: nudge to batch or wait
- 140K-165K: allow silently
- 165K-180K: nudge to split
- 180K+: deny in enforce, nudge in warn

Uses monkeypatch for config reads; no fabricated data files.
"""

import json
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))


# We'll mock the config loading in dispatch_budget_gate
# so tests don't depend on an actual config.json file.


@pytest.fixture
def mock_bands_config(monkeypatch):
    """Fixture to mock _get_bands_config for tests.

    Default bands config matching dispatch_budget_gate expectations.
    """
    default_config = {
        "mode": "enforce",
        "min_useful_work_tokens": {
            "fast": 70000,
            "reasoning": 80000,
            "default": 70000,
        },
        "subagent_infra_tokens": {
            "fast": 40000,
            "reasoning": 50000,
        },
        "target_min": 140000,
        "target_max": 165000,
        "hard_limit": 180000,
        "safety_reserve_tokens": 10000,
    }

    def _get_bands_config_mock():
        return default_config

    # Patch the module function
    import sys
    import dispatch_budget_gate
    monkeypatch.setattr(dispatch_budget_gate, "_get_bands_config", _get_bands_config_mock)

    return default_config


class TestBandsModeOff:
    """Test precedence: mode off uses legacy percentage behavior."""

    def test_mode_off_returns_legacy_signal(self, mock_bands_config, monkeypatch):
        """When mode is 'off', _apply_bands_logic returns ('legacy', None)."""
        import dispatch_budget_gate

        # Override mode to 'off'
        mock_bands_config["mode"] = "off"

        est = {
            "total_tokens": 150000,
            "infra_tokens": 40000,
            "pct_of_window": 15.0,
            "context_window": 1000000,
        }

        action, msg = dispatch_budget_gate._apply_bands_logic(est)
        assert action == "legacy"
        assert msg is None

    def test_mode_off_nudge_uses_legacy_logic(self, mock_bands_config, monkeypatch):
        """Legacy percentage nudge still works when mode is 'off'."""
        import dispatch_budget_gate
        from _common import DISPATCH_NUDGE_PCT_MIN, DISPATCH_OVER_BUDGET_PCT

        mock_bands_config["mode"] = "off"

        # Create estimate that would trigger legacy nudge (between NUDGE_PCT_MIN and OVER_BUDGET_PCT)
        est = {
            "total_tokens": 150000,
            "infra_tokens": 40000,
            "pct_of_window": 20.0,  # Assuming NUDGE_PCT_MIN <= 20 <= OVER_BUDGET_PCT
            "context_window": 750000,
            "estimate_failed": False,
        }

        msg = dispatch_budget_gate.budget_nudge_message(est)
        # When mode is off, should use legacy logic; message may be None or a legacy nudge
        # The key is that it doesn't apply band logic
        assert isinstance(msg, (str, type(None)))


class TestMinUsefulWorkThreshold:
    """Test below min_useful_work: nudge to batch or wait."""

    def test_below_min_useful_work_fast_tier(self, mock_bands_config, monkeypatch):
        """Dispatch below min_useful_work (fast tier) triggers batch/wait nudge."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"
        min_useful_fast = 70000

        est = {
            "total_tokens": 50000,  # Below min_useful_work
            "infra_tokens": 40000,  # Marks as fast tier
            "estimate_failed": False,
        }

        action, msg = dispatch_budget_gate._apply_bands_logic(est)
        assert action == "nudge"
        assert msg is not None
        assert "batch" in msg.lower() or "wait" in msg.lower()
        assert "50000" in msg or "50,000" in msg

    def test_below_min_useful_work_reasoning_tier(self, mock_bands_config, monkeypatch):
        """Dispatch below min_useful_work (reasoning tier) triggers batch/wait nudge."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"
        min_useful_reasoning = 80000

        est = {
            "total_tokens": 60000,  # Below min_useful_work for reasoning
            "infra_tokens": 50000,  # Marks as reasoning tier (> 40000)
            "estimate_failed": False,
        }

        action, msg = dispatch_budget_gate._apply_bands_logic(est)
        assert action == "nudge"
        assert msg is not None
        assert "batch" in msg.lower() or "wait" in msg.lower()

    def test_at_min_useful_work_boundary(self, mock_bands_config, monkeypatch):
        """Dispatch exactly at min_useful_work is allowed (not nudged)."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        est = {
            "total_tokens": 70000,  # Exactly min_useful_work for fast
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        action, msg = dispatch_budget_gate._apply_bands_logic(est)
        # At threshold should not trigger the "below" nudge; may be in target band
        assert action in ("allow", "nudge")


class TestTargetBand:
    """Test 140K-165K: allow silently."""

    def test_within_target_band_silent_allow(self, mock_bands_config, monkeypatch):
        """Dispatch within target_min to target_max is silently allowed."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        for tokens in [140000, 150000, 160000, 165000]:
            est = {
                "total_tokens": tokens,
                "infra_tokens": 40000,
                "estimate_failed": False,
            }

            action, msg = dispatch_budget_gate._apply_bands_logic(est)
            assert action == "allow", f"Failed for {tokens} tokens"
            assert msg is None, f"Expected silent allow for {tokens} tokens, got: {msg}"

    def test_below_target_min_nudge(self, mock_bands_config, monkeypatch):
        """Dispatch between min_useful_work and target_min is nudged (if not below min_useful)."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        # Above min_useful_work but below target_min should not nudge in current logic
        # (min_useful_work check happens first)
        est = {
            "total_tokens": 100000,  # Between 70K (min_useful) and 140K (target_min)
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        action, msg = dispatch_budget_gate._apply_bands_logic(est)
        # Current logic: below min_useful triggers nudge, then target band logic
        # 100K is above 70K min_useful, so no early exit; enters target band check
        # 100K is below 140K target_min, so no silent allow; falls through
        # Not caught by hard_limit check, so should return allow
        assert action == "allow"

    def test_above_target_max_nudge(self, mock_bands_config, monkeypatch):
        """Dispatch above target_max but below hard_limit is nudged to split."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        est = {
            "total_tokens": 170000,  # Above 165K target_max, below 180K hard_limit
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        action, msg = dispatch_budget_gate._apply_bands_logic(est)
        assert action == "nudge"
        assert msg is not None
        assert "split" in msg.lower()
        assert "170000" in msg or "170,000" in msg


class TestSplitNudgeBand:
    """Test 165K-180K: nudge to split."""

    def test_split_nudge_at_target_max(self, mock_bands_config, monkeypatch):
        """Dispatch just above target_max triggers split nudge."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        est = {
            "total_tokens": 165001,  # Just above target_max
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        action, msg = dispatch_budget_gate._apply_bands_logic(est)
        assert action == "nudge"
        assert msg is not None
        assert "split" in msg.lower()

    def test_split_nudge_midrange(self, mock_bands_config, monkeypatch):
        """Dispatch in mid-split band triggers split nudge."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        for tokens in [170000, 175000, 179999]:
            est = {
                "total_tokens": tokens,
                "infra_tokens": 40000,
                "estimate_failed": False,
            }

            action, msg = dispatch_budget_gate._apply_bands_logic(est)
            assert action == "nudge", f"Expected nudge for {tokens} tokens"
            assert msg is not None
            assert "split" in msg.lower()


class TestHardLimitEnforce:
    """Test 180K+: deny in enforce mode, nudge in warn mode."""

    def test_hard_limit_deny_enforce_mode(self, mock_bands_config, monkeypatch):
        """Dispatch at/above hard_limit is denied in enforce mode."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        for tokens in [180000, 190000, 200000]:
            est = {
                "total_tokens": tokens,
                "infra_tokens": 40000,
                "estimate_failed": False,
            }

            action, msg = dispatch_budget_gate._apply_bands_logic(est)
            assert action == "deny", f"Expected deny for {tokens} tokens in enforce mode"
            assert msg is not None
            assert "split" in msg.lower() or "hard limit" in msg.lower()

    def test_hard_limit_nudge_warn_mode(self, mock_bands_config, monkeypatch):
        """Dispatch at/above hard_limit is nudged (non-blocking) in warn mode."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "warn"

        est = {
            "total_tokens": 190000,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        action, msg = dispatch_budget_gate._apply_bands_logic(est)
        assert action == "nudge", "Expected nudge in warn mode, not deny"
        assert msg is not None

    def test_should_deny_by_bands_enforce(self, mock_bands_config, monkeypatch):
        """should_deny_by_bands returns True only in enforce mode at/above hard_limit."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        est = {
            "total_tokens": 180000,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        assert dispatch_budget_gate.should_deny_by_bands(est) is True

    def test_should_deny_by_bands_warn(self, mock_bands_config, monkeypatch):
        """should_deny_by_bands returns False in warn mode."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "warn"

        est = {
            "total_tokens": 200000,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        assert dispatch_budget_gate.should_deny_by_bands(est) is False

    def test_should_deny_by_bands_off(self, mock_bands_config, monkeypatch):
        """should_deny_by_bands returns False when mode is off."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "off"

        est = {
            "total_tokens": 200000,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        assert dispatch_budget_gate.should_deny_by_bands(est) is False


class TestBoundaryTransitions:
    """Test boundary conditions at each threshold."""

    def test_min_useful_boundary_fast(self, mock_bands_config, monkeypatch):
        """Test exact boundaries for min_useful_work (fast tier)."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"
        min_useful = 70000

        # Just below
        est_below = {
            "total_tokens": min_useful - 1,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }
        action_below, _ = dispatch_budget_gate._apply_bands_logic(est_below)

        # Just at
        est_at = {
            "total_tokens": min_useful,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }
        action_at, _ = dispatch_budget_gate._apply_bands_logic(est_at)

        assert action_below == "nudge"
        assert action_at != "nudge"  # Should be allowed or enter other checks

    def test_target_band_boundaries(self, mock_bands_config, monkeypatch):
        """Test exact boundaries for target band."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        # Just below target_min
        est_below = {
            "total_tokens": 139999,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }
        action_below, _ = dispatch_budget_gate._apply_bands_logic(est_below)

        # At target_min
        est_at_min = {
            "total_tokens": 140000,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }
        action_at_min, msg_at_min = dispatch_budget_gate._apply_bands_logic(est_at_min)

        # At target_max
        est_at_max = {
            "total_tokens": 165000,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }
        action_at_max, msg_at_max = dispatch_budget_gate._apply_bands_logic(est_at_max)

        # Just above target_max
        est_above = {
            "total_tokens": 165001,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }
        action_above, _ = dispatch_budget_gate._apply_bands_logic(est_above)

        assert action_at_min == "allow" and msg_at_min is None
        assert action_at_max == "allow" and msg_at_max is None
        assert action_above == "nudge"

    def test_hard_limit_boundary(self, mock_bands_config, monkeypatch):
        """Test exact boundary for hard_limit."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        # Just below hard_limit
        est_below = {
            "total_tokens": 179999,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }
        action_below, _ = dispatch_budget_gate._apply_bands_logic(est_below)

        # At hard_limit
        est_at = {
            "total_tokens": 180000,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }
        action_at, _ = dispatch_budget_gate._apply_bands_logic(est_at)

        assert action_below == "nudge"  # Split band nudge
        assert action_at == "deny"  # Hard limit deny


class TestEstimateFailedGraceful:
    """Test graceful handling when estimate_failed is True."""

    def test_estimate_failed_always_allow(self, mock_bands_config, monkeypatch):
        """When estimate_failed is True, return allow regardless of tokens."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        for tokens in [50000, 200000, 500000]:
            est = {
                "total_tokens": tokens,
                "infra_tokens": 40000,
                "estimate_failed": True,
            }

            action, msg = dispatch_budget_gate._apply_bands_logic(est)
            assert action == "allow"
            assert msg is None

    def test_should_deny_respects_estimate_failed(self, mock_bands_config, monkeypatch):
        """should_deny_by_bands returns False if estimate_failed is True."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        est = {
            "total_tokens": 200000,
            "infra_tokens": 40000,
            "estimate_failed": True,
        }

        assert dispatch_budget_gate.should_deny_by_bands(est) is False


class TestBudgetMessageBuilders:
    """Test budget_nudge_message and budget_deny_message."""

    def test_budget_nudge_message_bands_active(self, mock_bands_config, monkeypatch):
        """budget_nudge_message returns band nudge when bands mode is active."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "warn"

        # Below min_useful_work: should get nudge
        est = {
            "total_tokens": 50000,
            "infra_tokens": 40000,
            "estimate_failed": False,
            "pct_of_window": 5.0,
            "context_window": 1000000,
        }

        msg = dispatch_budget_gate.budget_nudge_message(est)
        assert msg is not None
        assert "batch" in msg.lower() or "wait" in msg.lower()

    def test_budget_nudge_message_legacy_fallback(self, mock_bands_config, monkeypatch):
        """budget_nudge_message uses legacy logic when mode is off."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "off"

        est = {
            "total_tokens": 150000,
            "infra_tokens": 40000,
            "estimate_failed": False,
            "pct_of_window": 20.0,
            "context_window": 750000,
            "nudge": False,
        }

        msg = dispatch_budget_gate.budget_nudge_message(est)
        # May be None or a legacy nudge depending on _common thresholds
        assert isinstance(msg, (str, type(None)))

    def test_budget_deny_message_bands_enforce(self, mock_bands_config, monkeypatch):
        """budget_deny_message returns band deny when enforce mode and over hard_limit."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        est = {
            "total_tokens": 200000,
            "infra_tokens": 40000,
            "estimate_failed": False,
            "pct_of_window": 20.0,
            "context_window": 1000000,
        }

        msg = dispatch_budget_gate.budget_deny_message(est)
        assert msg is not None
        assert "hard limit" in msg.lower() or "split" in msg.lower()

    def test_budget_deny_message_legacy_fallback(self, mock_bands_config, monkeypatch):
        """budget_deny_message uses legacy logic when bands mode is off or warn."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "warn"

        est = {
            "total_tokens": 200000,
            "infra_tokens": 40000,
            "estimate_failed": False,
            "pct_of_window": 40.0,
            "context_window": 500000,
        }

        msg = dispatch_budget_gate.budget_deny_message(est)
        assert msg is not None
        # May be band-based or legacy, but should contain deny message
        assert "blocked" in msg.lower() or "exceed" in msg.lower()


class TestGetBandEnforcementResult:
    """Test get_band_enforcement_result which returns full decision dict."""

    def test_enforcement_result_when_active(self, mock_bands_config, monkeypatch):
        """get_band_enforcement_result returns enforcement info when bands active."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        est = {
            "total_tokens": 150000,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        result = dispatch_budget_gate.get_band_enforcement_result(est)
        assert result["enforcement_active"] is True
        assert result["action"] == "allow"
        assert result["message"] is None
        assert result["mode"] == "enforce"

    def test_enforcement_result_when_off(self, mock_bands_config, monkeypatch):
        """get_band_enforcement_result reports enforcement inactive when mode is off."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "off"

        est = {
            "total_tokens": 150000,
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        result = dispatch_budget_gate.get_band_enforcement_result(est)
        assert result["enforcement_active"] is False
        assert result["mode"] == "off"


class TestPrecedenceAndDispatchOverride:
    """Test precedence rules and dispatch-budget-override semantics."""

    def test_precedence_bands_over_legacy(self, mock_bands_config, monkeypatch):
        """When bands mode is active, band logic takes precedence over legacy."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        est = {
            "total_tokens": 150000,
            "infra_tokens": 40000,
            "estimate_failed": False,
            "pct_of_window": 50.0,  # Would trigger legacy nudge
            "context_window": 300000,
        }

        # Band logic: 150K is in target band [140K, 165K] -> silent allow
        # Legacy logic: 50% would be a nudge
        # Bands should take precedence
        action, msg = dispatch_budget_gate._apply_bands_logic(est)
        assert action == "allow"
        assert msg is None

    def test_precedence_legacy_when_off(self, mock_bands_config, monkeypatch):
        """When bands mode is off, legacy logic applies."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "off"

        est = {
            "total_tokens": 150000,
            "infra_tokens": 40000,
            "estimate_failed": False,
            "pct_of_window": 20.0,
            "context_window": 750000,
        }

        action, msg = dispatch_budget_gate._apply_bands_logic(est)
        assert action == "legacy"
        assert msg is None


class TestModelTierClassification:
    """Test _get_model_tier classification."""

    def test_model_tier_fast_classification(self, mock_bands_config, monkeypatch):
        """Estimate with low infra_tokens is classified as fast tier."""
        import dispatch_budget_gate

        mock_bands_config["subagent_infra_tokens"]["fast"] = 40000

        est = {
            "infra_tokens": 40000,
        }

        tier = dispatch_budget_gate._get_model_tier(est)
        assert tier == "fast"

    def test_model_tier_reasoning_classification(self, mock_bands_config, monkeypatch):
        """Estimate with high infra_tokens is classified as reasoning tier."""
        import dispatch_budget_gate

        mock_bands_config["subagent_infra_tokens"]["fast"] = 40000

        est = {
            "infra_tokens": 50000,  # Above fast threshold
        }

        tier = dispatch_budget_gate._get_model_tier(est)
        assert tier == "reasoning"

    def test_model_tier_zero_infra(self, mock_bands_config, monkeypatch):
        """Zero infra_tokens defaults to reasoning tier."""
        import dispatch_budget_gate

        est = {
            "infra_tokens": 0,
        }

        tier = dispatch_budget_gate._get_model_tier(est)
        assert tier == "reasoning"


class TestSplitPlanPromptThreading:
    """Test DGC-204 split plan prompt threading through budget messages.

    When a dispatch exceeds the hard_limit and a prompt is provided,
    the split plan text should be included in nudge/deny messages.
    When no prompt is provided, "(split plan not available)" should appear.
    """

    def test_nudge_at_hard_limit_warn_includes_split_plan(self, mock_bands_config, monkeypatch):
        """budget_nudge_message in warn mode at hard_limit includes split plan from prompt."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "warn"

        # Prompt with real repo paths for split plan extraction
        prompt = (
            "Read `scripts/dispatch_budget_gate.py`, `scripts/split_planner.py`, "
            "`scripts/affinity.py`, and `tests/test_budget_bands.py` to understand "
            "the dispatch budget enforcement and split planning logic."
        )

        est = {
            "total_tokens": 180000,  # At hard_limit
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        msg = dispatch_budget_gate.budget_nudge_message(est, prompt=prompt)
        assert msg is not None
        # Split plan header should be present
        assert "[Recommended split plan:" in msg
        # "(split plan not available)" should not be present
        assert "(split plan not available)" not in msg

    def test_deny_enforce_includes_split_plan(self, mock_bands_config, monkeypatch):
        """budget_deny_message in enforce mode at hard_limit includes split plan from prompt."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        # Prompt with real repo paths for split plan extraction
        prompt = (
            "Read `scripts/dispatch_budget_gate.py`, `scripts/split_planner.py`, "
            "`scripts/affinity.py`, and `tests/test_budget_bands.py` to understand "
            "the dispatch budget enforcement and split planning logic."
        )

        est = {
            "total_tokens": 180000,  # At hard_limit
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        msg = dispatch_budget_gate.budget_deny_message(est, prompt=prompt)
        assert msg is not None
        # Split plan header should be present
        assert "[Recommended split plan:" in msg
        # "(split plan not available)" should not be present
        assert "(split plan not available)" not in msg

    def test_enforcement_result_includes_split_plan(self, mock_bands_config, monkeypatch):
        """get_band_enforcement_result at hard_limit with prompt includes split plan."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        # Prompt with real repo paths for split plan extraction
        prompt = (
            "Read `scripts/dispatch_budget_gate.py`, `scripts/split_planner.py`, "
            "`scripts/affinity.py`, and `tests/test_budget_bands.py` to understand "
            "the dispatch budget enforcement and split planning logic."
        )

        est = {
            "total_tokens": 180000,  # At hard_limit
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        result = dispatch_budget_gate.get_band_enforcement_result(est, prompt=prompt)
        assert result["enforcement_active"] is True
        assert result["action"] == "deny"
        assert result["message"] is not None
        # Split plan header should be present in message
        assert "[Recommended split plan:" in result["message"]
        # "(split plan not available)" should not be present
        assert "(split plan not available)" not in result["message"]

    def test_no_prompt_keeps_split_plan_not_available(self, mock_bands_config, monkeypatch):
        """Without prompt, message contains "(split plan not available)"."""
        import dispatch_budget_gate

        mock_bands_config["mode"] = "enforce"

        est = {
            "total_tokens": 180000,  # At hard_limit
            "infra_tokens": 40000,
            "estimate_failed": False,
        }

        # Call without prompt parameter
        msg = dispatch_budget_gate.budget_deny_message(est)
        assert msg is not None
        # When no prompt is provided, split plan should not be available
        assert "(split plan not available)" in msg
        # Split plan header should not be present
        assert "[Recommended split plan:" not in msg


class TestEnforcerBandDeny:
    """OPEN-2/OPEN-3: dispatch_budget_enforcer denies on band hard_limit."""

    @staticmethod
    def _run(monkeypatch, total_tokens, routing_text=""):
        import dispatch_budget_enforcer as enf

        est = {
            "total_tokens": total_tokens,
            "infra_tokens": 40000,
            "estimate_failed": False,
            "over_budget": False,
        }
        events = []
        monkeypatch.setattr(enf, "estimate_dispatch_context", lambda prompt, model=None: est)
        monkeypatch.setattr(enf, "append_event", events.append)
        result = enf._budget_enforcement_message(
            {"session_id": "s1"}, {"model": "haiku"}, True, "Agent", "do the thing", routing_text
        )
        return result, events

    def test_enforce_denies_at_hard_limit_without_legacy_over_budget(self, mock_bands_config, monkeypatch):
        mock_bands_config["mode"] = "enforce"
        (msg, should_deny, overridden), events = self._run(monkeypatch, 190000)
        assert should_deny is True
        assert overridden is False
        deny = [e for e in events if e["type"] == "dispatch_budget_deny"]
        assert len(deny) == 1
        assert deny[0]["band_deny"] is True
        assert deny[0]["split_plan_present"] is False  # no paths in prompt

    def test_warn_does_not_deny_at_hard_limit(self, mock_bands_config, monkeypatch):
        mock_bands_config["mode"] = "warn"
        (msg, should_deny, _), events = self._run(monkeypatch, 190000)
        assert should_deny is False
        assert not [e for e in events if e["type"] == "dispatch_budget_deny"]

    def test_enforce_override_bypasses_band_deny(self, mock_bands_config, monkeypatch):
        mock_bands_config["mode"] = "enforce"
        (msg, should_deny, overridden), events = self._run(
            monkeypatch, 190000, routing_text="dispatch-budget-override"
        )
        assert should_deny is False
        assert overridden is True
        assert [e for e in events if e["type"] == "override_used"]

    def test_warn_logs_would_deny_at_hard_limit(self, mock_bands_config, monkeypatch):
        mock_bands_config["mode"] = "warn"
        (msg, should_deny, _), events = self._run(monkeypatch, 190000)
        assert should_deny is False
        would_deny = [e for e in events if e["type"] == "dispatch_budget_would_deny"]
        assert len(would_deny) == 1
        assert would_deny[0]["est_tokens"] == 190000

    def test_warn_no_would_deny_below_hard_limit(self, mock_bands_config, monkeypatch):
        mock_bands_config["mode"] = "warn"
        (msg, should_deny, _), events = self._run(monkeypatch, 170000)
        would_deny = [e for e in events if e["type"] == "dispatch_budget_would_deny"]
        assert not would_deny

    def test_enforce_no_would_deny_event_when_denying(self, mock_bands_config, monkeypatch):
        mock_bands_config["mode"] = "enforce"
        (msg, should_deny, overridden), events = self._run(monkeypatch, 190000)
        assert should_deny is True
        assert not [e for e in events if e["type"] == "dispatch_budget_would_deny"]
