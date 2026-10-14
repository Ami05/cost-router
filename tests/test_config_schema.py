"""DGC-002: gate_mode helper, new config keys, and ordering validation."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import _config  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_cache():
    _config._invalidate_config_cache()
    yield
    _config._invalidate_config_cache()


def _set_config(monkeypatch, cfg):
    monkeypatch.setattr(_config, "_config_cache", cfg)


def _runtime():
    return copy.deepcopy(_config.load_plugin_config())


# ── gate_mode ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("off", "off"), ("warn", "warn"), ("enforce", "enforce"),
    (None, "warn"), ("enforse", "warn"), ("ENFORCE", "warn"), (1, "warn"), (True, "warn"), ({}, "warn"),
])
def test_gate_mode_values(monkeypatch, value, expected):
    _set_config(monkeypatch, {"x": {"mode": value}})
    assert _config.gate_mode("x") == expected


def test_gate_mode_missing(monkeypatch):
    _set_config(monkeypatch, {"x": {}})
    assert _config.gate_mode("x") == "warn"
    assert _config.gate_mode("nope") == "warn"
    assert _config.gate_mode("x.y.z") == "warn"


def test_gate_mode_ignores_unrelated_top_level_mode(monkeypatch):
    _set_config(monkeypatch, {"mode": "enforce"})
    assert _config.gate_mode("progress_ledger.kill") == "warn"


@pytest.mark.parametrize("section", [
    "dispatch_context.bands", "progress_ledger", "progress_ledger.kill",
    "continuation", "context_pressure",
])
def test_runtime_gate_modes_default_warn(section):
    assert _config.gate_mode(section) == "warn"


# ── new keys present in loaded runtime config (fragments) ────────────────────

@pytest.mark.parametrize("path", [
    "dispatch_context.bands.target_min", "dispatch_context.bands.target_max",
    "dispatch_context.bands.hard_limit", "dispatch_context.bands.safety_reserve_tokens",
    "dispatch_context.bands.mode", "dispatch_context.bands.min_useful_work_tokens.fast",
    "dispatch_context.bands.expected_output_tokens.reasoning",
    "dispatch_context.affinity.threshold",
    "dispatch_context.enforce_decision.fp_ceiling_hard_deny",
    "dispatch_context.enforce_decision.min_runs_per_worker_type",
    "progress_ledger.mode", "progress_ledger.kill.grace_denied_calls", "progress_ledger.kill.mode",
    "progress_ledger.strict_profile.shell.max_stall_markers",
    "progress_ledger.strict_profile.shell.max_stall_seconds",
    "progress_ledger.strict_profile.orchestrator.max_stall_markers",
    "progress_ledger.strict_profile.orchestrator.max_stall_seconds",
    "continuation.max_depth", "continuation.max_compact_chars",
    "continuation.max_remaining_items", "continuation.no_progress_halts",
    "continuation.manifest_required", "continuation.mode",
])
def test_new_key_in_runtime_and_legacy(path):
    found, _ = _config._get_nested(_config.load_plugin_config(), path)
    assert found, f"{path} missing from runtime fragments"
    legacy = json.loads((ROOT / "config.json").read_text())
    assert _config._get_nested(legacy, path)[0], f"{path} missing from config.json"


def test_no_token_governor_keys():
    assert "token_governor" not in _config.load_plugin_config()


def test_existing_values_unchanged():
    legacy = json.loads((ROOT / "config.json").read_text())
    assert legacy["dispatch_context"]["subagent_infra_tokens"] == {"fast": 40000, "reasoning": 70000, "default": 70000}
    assert legacy["dispatch_context"]["resume_ttl_seconds"] == 300
    pl = legacy["progress_ledger"]
    assert pl["max_stall_markers"] == 6 and pl["max_stall_seconds"] == 180
    assert pl["worker_overrides"]["mutation"] == {"max_stall_seconds": 600}
    assert pl["worker_overrides"]["read"] == {"max_stall_seconds": 600}
    assert pl["strict_profile"]["shell"] == {"max_stall_seconds": 90, "max_stall_markers": 3, "heartbeat_seconds": 30}
    assert pl["strict_profile"]["orchestrator"] == {"max_stall_seconds": 90, "max_stall_markers": 3, "heartbeat_seconds": 30}
    runtime = _config.load_plugin_config()
    assert runtime["dispatch_context"]["subagent_infra_tokens"] == legacy["dispatch_context"]["subagent_infra_tokens"]
    assert runtime["progress_ledger"]["max_stall_markers"] == 6
    assert runtime["context_pressure"]["warn_pct"] == 0.70 and runtime["context_pressure"]["threshold"] == 0.85


def test_runtime_config_valid():
    assert _config.validate_gate_config() == []
    assert _config.validate_gate_config(json.loads((ROOT / "config.json").read_text())) == []


# ── ordering violations rejected ─────────────────────────────────────────────

def _mutate(path, value):
    cfg = _runtime()
    cur = cfg
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur[p]
    cur[parts[-1]] = value
    return cfg


@pytest.mark.parametrize("path,value,needle", [
    ("dispatch_context.bands.target_min", 165000, "target_min < target_max"),
    ("dispatch_context.bands.target_min", 170000, "target_min < target_max"),
    ("dispatch_context.bands.target_max", 190000, "target_min < target_max"),
    ("dispatch_context.bands.hard_limit", 150000, "target_min < target_max"),
    ("progress_ledger.strict_profile.shell.max_stall_markers", 0, "shell.max_stall_markers"),
    ("progress_ledger.strict_profile.shell.max_stall_seconds", -1, "shell.max_stall_seconds"),
    ("progress_ledger.strict_profile.orchestrator.max_stall_markers", 0, "orchestrator.max_stall_markers"),
    ("progress_ledger.strict_profile.orchestrator.max_stall_seconds", None, "orchestrator.max_stall_seconds"),
    ("context_pressure.warn_pct", 0.9, "warn_pct < threshold"),
    ("context_pressure.warn_pct", 0.85, "warn_pct < threshold"),
    ("context_pressure.threshold", 1.2, "warn_pct < threshold"),
    ("dispatch_context.resume_ttl_seconds", 301, "resume_ttl_seconds"),
    ("dispatch_context.enforce_decision.fp_ceiling_hard_deny", 0, "fp_ceiling_hard_deny"),
    ("dispatch_context.enforce_decision.fp_ceiling_nudge", 1, "fp_ceiling_nudge"),
    ("dispatch_context.enforce_decision.fp_ceiling_default", 1.5, "fp_ceiling_default"),
])
def test_validation_rejects(path, value, needle):
    errors = _config.validate_gate_config(_mutate(path, value))
    assert any(needle in e for e in errors), errors


def test_context_pressure_threshold_equal_one_ok():
    assert _config.validate_gate_config(_mutate("context_pressure.threshold", 1)) == []


def test_bands_hard_limit_equal_target_max_ok():
    assert _config.validate_gate_config(_mutate("dispatch_context.bands.hard_limit", 165000)) == []
