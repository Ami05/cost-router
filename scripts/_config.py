#!/usr/bin/env python3
"""Configuration access, path constants, and module-level thresholds.

Canonical home for: PLUGIN_ROOT, DATA_DIR, all config_* helpers, all
module-level constants derived from config, and the _warn helper (placed
here rather than _io.py so _io.py can import _warn without a circular dep).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_data_dir_override = os.environ.get("COST_ROUTER_DATA_DIR")
DATA_DIR = Path(_data_dir_override) if _data_dir_override else PLUGIN_ROOT / "data"


# ── config access ─────────────────────────────────────────────────────────────

def _get_nested(config: dict[str, Any], path: str) -> tuple[bool, Any]:
    """Return (found, value) for a dotted config path."""
    current: Any = config
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


_config_cache: dict[str, Any] | None = None


def _load_config_from_disk() -> dict[str, Any]:
    config: dict[str, Any] = {}
    for name in ("routing", "gates", "classifier", "reward", "session"):
        path = PLUGIN_ROOT / "config" / f"{name}.json"
        try:
            fragment = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(fragment, dict):
                config.update(fragment)
        except (OSError, json.JSONDecodeError) as e:
            _warn("config_load_failed", {"file": path.name, "error": str(e)})
    if not config:
        try:
            legacy = PLUGIN_ROOT / "config.json"
            file_config = json.loads(legacy.read_text(encoding="utf-8"))
            return file_config if isinstance(file_config, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            _warn("config_load_failed", {"file": "config.json", "error": str(e)})
    return config


def load_plugin_config() -> dict[str, Any]:
    """Load (and cache) configuration from config.json in the plugin root."""
    global _config_cache
    if _config_cache is None:
        _config_cache = _load_config_from_disk()
    return _config_cache


def _invalidate_config_cache() -> None:
    """Test helper — invalidates the cached config so the next call re-reads disk."""
    global _config_cache
    _config_cache = None


def config_get(path: str, default: Any = None, flat_key: str | None = None) -> Any:
    """Read a nested config value with optional flat-key backward compatibility."""
    config = load_plugin_config()
    found, value = _get_nested(config, path)
    if found:
        return value
    if flat_key is not None and flat_key in config:
        return config[flat_key]
    last_segment = path.rsplit(".", 1)[-1]
    if last_segment in config:
        return config[last_segment]
    return default


def config_int(path: str, default: int = 0, flat_key: str | None = None) -> int:
    """Read an integer config value; falls back to *default* on type errors."""
    raw = config_get(path, default, flat_key=flat_key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def config_float(path: str, default: float = 0.0, flat_key: str | None = None) -> float:
    """Read a float config value; falls back to *default* on type errors."""
    raw = config_get(path, default, flat_key=flat_key)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


GATE_MODES = ("off", "warn", "enforce")


def gate_mode(section: str) -> str:
    """Return the gate mode ("off" | "warn" | "enforce") for a config section.

    Reads ``<section>.mode`` (dotted section, e.g. "progress_ledger.kill").
    Missing, null, non-string or misspelled values return "warn".
    """
    found, value = _get_nested(load_plugin_config(), f"{section}.mode")
    if found and isinstance(value, str) and value in GATE_MODES:
        return value
    return "warn"


def validate_gate_config(config: dict[str, Any] | None = None) -> list[str]:
    """Validate DGC-002 threshold ordering; return a list of error strings (empty = valid)."""
    cfg = load_plugin_config() if config is None else config
    errors: list[str] = []

    def num(path: str) -> Any:
        found, v = _get_nested(cfg, path)
        return v if found and isinstance(v, (int, float)) and not isinstance(v, bool) else None

    t_min, t_max, hard = (num(f"dispatch_context.bands.{k}") for k in ("target_min", "target_max", "hard_limit"))
    if None in (t_min, t_max, hard):
        errors.append("dispatch_context.bands: target_min, target_max, hard_limit must be numbers")
    elif not (t_min < t_max <= hard):
        errors.append("dispatch_context.bands: require target_min < target_max <= hard_limit")

    for worker in ("shell", "orchestrator"):
        for key in ("max_stall_markers", "max_stall_seconds"):
            v = num(f"progress_ledger.strict_profile.{worker}.{key}")
            if v is None or v <= 0:
                errors.append(f"progress_ledger.strict_profile.{worker}.{key} must be > 0")

    warn_pct, threshold = num("context_pressure.warn_pct"), num("context_pressure.threshold")
    if warn_pct is None or threshold is None or not (warn_pct < threshold <= 1):
        errors.append("context_pressure: require warn_pct < threshold <= 1")

    ttl = num("dispatch_context.resume_ttl_seconds")
    if ttl is None or ttl > 300:
        errors.append("dispatch_context.resume_ttl_seconds must be <= 300")

    for key in ("fp_ceiling_hard_deny", "fp_ceiling_nudge", "fp_ceiling_default"):
        v = num(f"dispatch_context.enforce_decision.{key}")
        if v is None or not (0 < v < 1):
            errors.append(f"dispatch_context.enforce_decision.{key} must be in (0,1)")
    return errors


_THRESHOLD_DEFAULTS: dict[str, int] = {
    "read_full_max_lines": 300,
    "edit_dispatch_min_lines": 500,
    "write_existing_max_lines": 150,
    "write_existing_max_kb": 5,
    "role_read_max_lines": 50,
}


def get_thresholds() -> dict[str, int]:
    """Size limits from the config `thresholds` block, defaults filling gaps."""
    raw = config_get("thresholds", {}, flat_key="thresholds")
    out = dict(_THRESHOLD_DEFAULTS)
    if isinstance(raw, dict):
        for key in out:
            try:
                out[key] = int(raw[key])
            except (KeyError, TypeError, ValueError):
                pass
    return out


_FEEDBACK_LOOP_DEFAULTS: dict[str, Any] = {
    "promote_monitor_window": 20,
    "regression_threshold": 0.2,
    "improvement_threshold": 0.15,
    "max_rules_per_week": 3,
}


def get_feedback_loop_config() -> dict[str, Any]:
    """Return the feedback_loop config block with fallback defaults for missing keys."""
    overrides = config_get("feedback_loop", {})
    merged = dict(_FEEDBACK_LOOP_DEFAULTS)
    if isinstance(overrides, dict):
        merged.update(overrides)
    return merged


# ── file paths ────────────────────────────────────────────────────────────────

ROUTING_EVENTS_LOG = DATA_DIR / "routing_events.jsonl"
SESSION_EVENTS_LOG = DATA_DIR / "session_events.jsonl"
REWARD_EVENTS_LOG = DATA_DIR / "reward_events.jsonl"
TOKEN_EVENTS_LOG = DATA_DIR / "token_events.jsonl"
USAGE_LOG = ROUTING_EVENTS_LOG
USAGE_ARCHIVE = DATA_DIR / "usage_archive.jsonl"
SIGNAL_FILE = DATA_DIR / "signal.jsonl"
DEBUG_LOG = DATA_DIR / "debug.log"
TUNE_STATE_FILE = DATA_DIR / "tune_state.json"
DISPATCH_REGISTRY_FILE = DATA_DIR / "dispatch_registry.json"
INSTALLED_AT_FILE = PLUGIN_ROOT / ".installed_at"

WARNINGS_LOG = DATA_DIR / "warnings.jsonl"
_WARNED_THIS_PROCESS: set[str] = set()


def _warn(event: str, context: dict[str, Any] | None = None) -> None:
    """Log a structured warning to warnings.jsonl. Never raises."""
    _key = f"{event}:{str((context or {}).get('error', ''))}"
    if _key in _WARNED_THIS_PROCESS:
        return
    _WARNED_THIS_PROCESS.add(_key)
    try:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
        }
        if context:
            entry["context"] = context
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with WARNINGS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ── module-level constants ────────────────────────────────────────────────────

FLIP_FLOP_THRESHOLD = config_int("routing.flip_flop_threshold")
DISPATCH_NUDGE_THRESHOLD = config_int("routing.dispatch_nudge_threshold")
DISPATCH_DENY_THRESHOLD = config_int("routing.dispatch_deny_threshold")
INLINE_MUTATION_NUDGE_THRESHOLD = config_int("inline_mutation_gate.nudge_threshold")
INLINE_MUTATION_DENY_THRESHOLD = config_int("inline_mutation_gate.deny_threshold")
SHELL_NUDGE_THRESHOLD = config_int("shell_gate.nudge_threshold", 3)
SHELL_DENY_THRESHOLD = config_int("shell_gate.deny_threshold", 5)
SESSION_DISPATCH_DENY_THRESHOLD = config_int("routing.session_dispatch_deny_threshold")
RESUME_GATE_WORK_UNIT_THRESHOLD = config_int("resume_gate.work_unit_threshold", 3)
RESUME_GATE_EARLY_CALL_THRESHOLD = config_int("resume_gate.early_call_threshold", 5)
RESUME_GATE_SHORT_PROMPT_MAX_CHARS = config_int("resume_gate.short_prompt_max_chars", 30)
RESUME_GATE_MAX_TRACKED_SESSIONS = config_int("resume_gate.max_tracked_sessions", 200)
DISPATCH_GATE_THRESHOLD = config_int("routing.dispatch_gate_threshold", flat_key="dispatch_gate_threshold")
MAX_LOG_BYTES = config_int("logging.max_usage_log_bytes")
USAGE_LOG_COMPACT_TRIGGER_BYTES = config_int("logging.usage_log_compact_trigger_bytes")
MAX_SCENARIOS_PER_KEY = config_int("logging.max_scenarios_per_key")
MAX_COUNTERFACTUAL_LOG_BYTES = config_int("logging.max_counterfactual_log_bytes")
FAST_PATH_CALL_LIMIT = config_int("routing.fast_path_call_limit")
FAST_PATH_MAX_PROMPT_CHARS = config_int("routing.fast_path_max_prompt_chars")
MAX_TRACKED_SESSIONS = config_int("routing.max_tracked_sessions")
TRANSCRIPT_TAIL_BYTES = config_int("routing.transcript_tail_bytes")
SUBAGENT_TTL_SECONDS = config_int("routing.subagent_ttl_seconds")
ACTIVE_SUBAGENT_STALE_TTL_SECONDS = config_int("routing.active_subagent_stale_ttl_seconds", 1800)
LARGE_WRITE_LINE_THRESHOLD = config_int("routing.large_write_line_threshold")
LARGE_WRITE_BYTE_THRESHOLD = config_int("routing.large_write_byte_threshold")
TIER_HISTORY_CAP = config_int("routing.tier_history_cap")
ASSISTANT_SNIPPET_CHARS = config_int("routing.assistant_snippet_chars")
TASK_TARGET_HASH_CHARS = config_int("routing.task_target_hash_chars")
READ_REPEAT_MAX_TRACKED_SESSIONS = config_int("read_repeat.max_tracked_sessions")
DENY_AFTER_REPEAT_COUNT = config_int("read_repeat.deny_after_repeat_count")
PROMPT_BLOAT_THRESHOLD = config_float("dispatch_context.prompt_bloat_threshold")
DISPATCH_NUDGE_PCT_MIN = config_float("dispatch_context.nudge_pct_min")
DISPATCH_OVER_BUDGET_PCT = config_float("dispatch_context.over_budget_pct")
GATE_HEALTH_WARN_PCT = config_float("analyze_routing.gate_health_warn_pct")
ESCALATION_WINDOW = config_int("analyze_routing.escalation_window")
OVER_ROUTE_TOKEN_THRESHOLD = config_int("analyze_routing.over_route_token_threshold")
OVER_ROUTE_SESSION_TOKEN_CAP = config_int("analyze_routing.over_route_session_token_cap")
UNDISPATCHED_TOOL_THRESHOLD = config_int("analyze_routing.undispatched_tool_threshold")
ESCALATION_NUDGE_THRESHOLD = config_int("analyze_routing.escalation_nudge_threshold")
REPEAT_READ_NUDGE_THRESHOLD = config_int("analyze_routing.repeat_read_nudge_threshold")
UNDISPATCHED_NUDGE_THRESHOLD = config_int("analyze_routing.undispatched_nudge_threshold")
SESSION_CADENCE_THRESHOLD = config_int("analyze_routing.session_cadence_threshold")
DEFAULT_NUDGE_COOLDOWN_HOURS = config_float("analyze_routing.nudge_cooldown_hours")
DISPATCH_BUDGET_NUDGE_PCT = config_float("analyze_routing.dispatch_budget_nudge_pct")
_REASONING_PRICE_PER_1K = config_float("analyze_routing.pricing_per_1k.reasoning_output")
_FAST_PRICE_PER_1K = config_float("analyze_routing.pricing_per_1k.fast_output")
REPEAT_DISPATCH_TOKEN_THRESHOLD = config_int("analyze_routing.repeat_dispatch_token_threshold")
SESSION_OVER_ROUTE_TOOL_CALL_CAP = config_int("analyze_routing.session_over_route_tool_call_cap")
SESSION_OVER_ROUTE_MIN_COST_USD = config_float("analyze_routing.session_over_route_min_cost_usd")
QA_FULL_FILE_READ_THRESHOLD = config_int("analyze_routing.qa_full_file_read_threshold")
QA_LOW_TOOL_CALL_THRESHOLD = config_int("analyze_routing.qa_low_tool_call_threshold")
REWARD_OVERRIDE_PENALTY = config_float("reward.override_penalty")
REWARD_ESCALATION_PENALTY_CAP = config_float("reward.escalation_penalty_cap")
REWARD_ESCALATION_PENALTY_MULTIPLIER = config_float("reward.escalation_penalty_multiplier")
REWARD_ERROR_PENALTY_CAP = config_float("reward.error_penalty_cap")
REWARD_ERROR_PENALTY_MULTIPLIER = config_float("reward.error_penalty_multiplier")
REWARD_REVERT_PENALTY_CAP = config_float("reward.revert_penalty_cap")
REWARD_REVERT_PENALTY_MULTIPLIER = config_float("reward.revert_penalty_multiplier")
REWARD_CLEAN_COMPLETION_BONUS = config_float("reward.clean_completion_bonus")
REWARD_COST_PENALTY_CAP = config_float("reward.cost_penalty_cap")
REWARD_COST_PENALTY_DIVISOR_USD = config_float("reward.cost_penalty_divisor_usd")
REWARD_OVER_ROUTE_PENALTY_CAP = config_float("reward.over_route_penalty_cap")
REWARD_OVER_ROUTE_PENALTY_MULTIPLIER = config_float("reward.over_route_penalty_multiplier")
QA_WITH_READS_MAX_FILES = config_int("classify_intent.qa_with_reads_max_files")
QA_WITH_READS_MAX_LINES = config_int("classify_intent.qa_with_reads_max_lines")
QA_ARCHITECTURE_MAX_FILES = config_int("classify_intent.qa_architecture_max_files")
TUNE_SAVINGS_RULE_INJECT_FALLBACK_USD = config_float("tune_savings.rule_inject_fallback_usd")
TUNE_SAVINGS_RULE_INJECT_EVIDENCE_MULTIPLIER = config_float("tune_savings.rule_inject_evidence_multiplier")
TUNE_SAVINGS_SESSION_SUMMARY_PROPOSAL_MULTIPLIER = config_float("tune_savings.session_summary_proposal_multiplier")
POLICY_GC_NEAR_DUPLICATE_RATIO = config_float("policy_gc.near_duplicate_ratio")
CODE_MARKER_RATIO_THRESHOLD = config_float("estimate_dispatch_context.code_marker_ratio_threshold")
TARGET_HASH_HEX_CHARS = config_int("runtime.target_hash_hex_chars")
STDIN_SELECT_TIMEOUT_SECONDS = config_float("runtime.stdin_select_timeout_seconds")
TAIL_READ_CHUNK_BYTES = config_int("runtime.tail_read_chunk_bytes")
SESSION_LIMIT_FILE_HASH_GUARD = config_int("session_limits.file_hash_guard")
SESSION_LIMIT_AGENT_CALL_TRACKER = config_int("session_limits.agent_call_tracker")
SESSION_LIMIT_INJECT_RULE = config_int("session_limits.inject_rule")
SESSION_LIMIT_SESSION_SUMMARY_PROJECTS = config_int("session_limits.session_summary_projects")
POLICY_QUEUED_THRESHOLD = config_int("policy.queued_threshold")
POLICY_AUTO_APPLY_THRESHOLD = config_int("policy.auto_apply_threshold")
POLICY_MAX_TABLE_ROWS = config_int("policy.max_table_rows")
POLICY_DEAD_ROW_WINDOW = config_int("policy.dead_row_window")


def policy_auto_apply_enabled() -> bool:
    """True only when policy.auto_apply_enabled is explicitly true (default false)."""
    return config_get("policy.auto_apply_enabled", False) is True


def pipeline_trigger_gc_apply() -> bool:
    """True only when pipeline_trigger.gc_apply is explicitly true (default false)."""
    return config_get("pipeline_trigger.gc_apply", False) is True

GRAPH_TTL_DAYS = config_int("graph.ttl_days")
