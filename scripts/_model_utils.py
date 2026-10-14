#!/usr/bin/env python3
"""Model, token, and pricing utilities for cost-router hook scripts.

Separated from _common.py so infra-only hook scripts (graph, state, log)
never load model/pricing logic. Pure stdlib, no external deps.

Imports config primitives from _common; never imports IO helpers from it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# ── constants ─────────────────────────────────────────────────────────────────
# Defined before _common import to break circular import:
# _common (L590) re-imports FAST_MARKERS from this module; placing these
# constants after the _common import would leave them undefined when _common
# tries to read them from the partially-initialized _model_utils module.

PRICING_FILE = Path(__file__).resolve().parent / "pricing.json"

# Substrings used to classify a model name into a rough cost tier.
# Extend these lists as new model names appear in hook payloads.
FAST_MARKERS = ("fast", "composer", "haiku", "mini")
REASONING_MARKERS = ("sonnet", "opus", "thinking", "gpt-5", "o1", "o3")

from _config import config_float, config_int, load_plugin_config, _warn  # noqa: E402

# ── rate-limited warning (TICKET-056) ────────────────────────────────────────
# TICKET-056: Track warn emissions per (field, failure_mode) pair for process
# lifetime rate-limiting. Prevents log flooding from repeated unrecognized models.

_WARN_RATE_LIMIT: dict[tuple[str, str], dict[str, int]] = {}


def _warn_rate_limited(field: str, failure_mode: str, details: dict | None = None) -> None:
    """Emit a warning once per unique (field, failure_mode) pair per process lifetime.

    Tracks suppressed_count so total occurrences remain observable without spamming.
    Call sites should pass field (e.g. "model") and failure_mode (e.g. "tier_unrecognized")
    to enable deduplication.

    TICKET-056: rate-limit _warn to once per unique (field, failure_mode) pair.
    """
    key = (field, failure_mode)
    if key not in _WARN_RATE_LIMIT:
        _WARN_RATE_LIMIT[key] = {"count": 0, "emitted": False}

    entry = _WARN_RATE_LIMIT[key]
    entry["count"] += 1

    if not entry["emitted"]:
        entry["emitted"] = True
        suppressed = entry["count"] - 1
        msg_parts = [f"{field}={failure_mode!r}"]
        if suppressed > 0:
            msg_parts.append(f"suppressed_count={suppressed}")
        if details:
            for k, v in details.items():
                msg_parts.append(f"{k}={v!r}")
        try:
            _warn(" ".join(msg_parts))
        except Exception:  # noqa: BLE001
            pass
    else:
        entry["count"] += 1


# ── pricing singleton ─────────────────────────────────────────────────────────

_pricing_cache: dict[str, Any] | None = None


def load_pricing() -> dict[str, Any]:
    global _pricing_cache
    if _pricing_cache is None:
        try:
            _pricing_cache = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _warn("pricing_load_failed", {"file": "_model_utils.py", "error": str(e)})
            _pricing_cache = {}
    return _pricing_cache


def _invalidate_pricing_cache() -> None:
    """Test helper — invalidates the cached pricing so the next call re-reads disk."""
    global _pricing_cache
    _pricing_cache = None


def check_model_key_none_threshold(
    none_count: int, total_count: int, threshold_pct: float = 2.0
) -> tuple[bool, float]:
    """TICKET-046: Check if model_key=None rate exceeds threshold.

    Args:
        none_count: Number of events with model_key=None.
        total_count: Total number of events in the window.
        threshold_pct: Maximum acceptable None rate as a percentage (default 2%).

    Returns:
        (should_alert, actual_pct): True if threshold exceeded, and actual None rate.
    """
    if total_count <= 0:
        return False, 0.0
    actual_pct = (none_count / total_count) * 100.0
    return actual_pct > threshold_pct, actual_pct


# ── env-or-config helpers (Template Method) ───────────────────────────────────

def _env_or_config_int(env_var: str, config_path: str, *, min_val: int = 0) -> int:
    raw = os.environ.get(env_var)
    if raw:
        try:
            return max(min_val, int(raw))
        except ValueError:
            pass
    return config_int(config_path)


def _env_or_config_float(
    env_var: str,
    config_path: str,
    *,
    lo: float = 0.0,
    hi: float = float("inf"),
) -> float:
    raw = os.environ.get(env_var)
    if raw:
        try:
            value = float(raw)
            if lo <= value <= hi:
                return value
        except ValueError:
            pass
    return config_float(config_path)


# ── dispatch parameter accessors ──────────────────────────────────────────────

def infra_tokens(model: str | None = None) -> int:
    """Fixed host-context prefix size charged on a cold subagent spawn.

    Per-tier values: fast (haiku) = 50K, reasoning (sonnet) = 70K.
    Env var COST_ROUTER_SUBAGENT_INFRA_TOKENS overrides all tiers.
    """
    raw = os.environ.get("COST_ROUTER_SUBAGENT_INFRA_TOKENS")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    tier = tier_of_model(model)
    if tier == "unknown":
        tier = "default"
    val = config_int(f"dispatch_context.subagent_infra_tokens.{tier}", 0)
    if val > 0:
        return val
    # Fallback tier 1: default config
    default_val = config_int("dispatch_context.subagent_infra_tokens.default", 0)
    if default_val > 0:
        try:
            _warn(f'infra_tokens resolution_source=default_config tier={tier}')
        except Exception:
            pass
        return default_val
    # Fallback tier 2: hardcoded fallback
    try:
        _warn(f'infra_tokens resolution_source=hardcoded_fallback tier={tier}')
    except Exception:
        pass
    return 70000


RESUME_TTL_MAX_SECONDS = 300


def resume_ttl_seconds() -> int:
    """Sliding warm-cache TTL for resume reuse, hard-capped at 300s (cache TTL)."""
    return min(
        RESUME_TTL_MAX_SECONDS,
        _env_or_config_int(
            "COST_ROUTER_RESUME_TTL_SECONDS", "dispatch_context.resume_ttl_seconds"
        ),
    )


def dispatch_budget_pct() -> float:
    """Headroom target as a fraction of the dispatched model's context window."""
    return _env_or_config_float(
        "COST_ROUTER_DISPATCH_CONTEXT_BUDGET_PCT",
        "dispatch_context.budget_pct",
        lo=1e-9,
        hi=1.0,
    )


# ── payload helpers ───────────────────────────────────────────────────────────

def is_claude_payload(payload: dict[str, Any]) -> bool:
    """True when the hook payload originates from Claude Code (not Cursor).

    Claude Code sets hook_event_name; Cursor also sets cursor_version, which
    distinguishes the two when both fields are present."""
    return bool(payload.get("hook_event_name")) and not payload.get("cursor_version")


def tool_input_of(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the tool_input dict from a hook payload, normalising camelCase."""
    ti = payload.get("tool_input")
    if isinstance(ti, dict):
        return ti
    ti = payload.get("toolInput")
    return ti if isinstance(ti, dict) else {}


def extract_file_path(payload: dict[str, Any]) -> str | None:
    """Return the first non-empty file path from a hook payload, or None.

    Checks tool_input keys (path, file_path, target_file) first, then top-level
    payload keys (path, file_path) as a fallback for older hook shapes."""
    ti = tool_input_of(payload)
    for key in ("path", "file_path", "target_file"):
        value = ti.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("path", "file_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def model_of(payload: dict[str, Any]) -> str | None:
    for key in ("model", "model_name", "modelName"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ── model tier ────────────────────────────────────────────────────────────────

def _model_segments(model: str) -> frozenset[str]:
    """Split a model name on common separators for exact-segment matching."""
    return frozenset(re.split(r"[-/._]", model.lower()))


def tier_of_model(model: str | None) -> str:
    """Rough cost tier classification: fast / reasoning / unknown.

    CRITICAL: Check FAST before REASONING. "haiku-thinking" contains both markers;
    must be classified as fast (haiku is cheaper regardless of thinking).

    Returns "unknown" for two distinct failure modes — both require append_event()
    emission by callers (NOT via _warn here):
    - model_none: model is None or empty (extraction failure upstream)
    - tier_unrecognized: non-empty model string matches no known tier marker

    TICKET-033: extraction_fallback/tier_unrecognized now delegated to append_event()
    callers (check_routing.py _tier_of_model_with_event) for structured routing_events.jsonl
    logging. _warn() removed to prevent lossy stderr-only emission.
    """
    if not model:
        return "unknown"
    segs = _model_segments(model)
    # Check FAST first to prevent "haiku-thinking" from being misclassified as reasoning
    if segs & frozenset(FAST_MARKERS):
        return "fast"
    if segs & frozenset(REASONING_MARKERS):
        return "reasoning"
    return "unknown"


def is_valid_subagent_model(model: str | None) -> bool:
    """Check if model is a valid subagent tier (fast or reasoning, never opus)."""
    if not model or re.search(r"\bopus\b", model, re.IGNORECASE):
        return False
    return tier_of_model(model) in ("fast", "reasoning")


def routing_platform(payload: dict[str, Any] | None) -> str:
    """Return config host key for model_tiers lookup: claude or cursor."""
    if payload and is_claude_payload(payload):
        return "claude"
    return "cursor"


def tier_model_slug(tier: str, platform: str = "cursor") -> str:
    """Resolve a model_tiers tier name to the platform-specific Task model slug."""
    tiers = load_plugin_config().get("model_tiers", {})
    entry = tiers.get(tier) if isinstance(tiers, dict) else None
    if isinstance(entry, dict):
        slug = entry.get(platform) or entry.get("claude") or entry.get("cursor")
        if slug:
            return str(slug)
    return "haiku" if tier == "fast" else "sonnet"


def subagent_model_hint(platform: str = "cursor") -> str:
    """Human-readable allowed-model hint for gate denial messages."""
    fast = tier_model_slug("fast", platform)
    reasoning = tier_model_slug("reasoning", platform)
    extra = ", claude-4.5-haiku-thinking" if platform == "cursor" else ""
    return (
        f"Use a fast-tier slug (e.g. model='{fast}'{extra}) "
        f"or a reasoning-tier slug (e.g. model='{reasoning}')."
    )


# ── token estimation ──────────────────────────────────────────────────────────

def estimate_tokens(text: str, model: str | None = None, content_kind: str = "prose") -> int:
    """Rough heuristic: ~4 chars per token, with model-aware correction.

    When *model* is provided, applies token_correction_factor() from config
    (default multipliers: 1.5× prose, 2.0× code, 2.5× symbol).
    This is a local (est.) figure only — never present this as a billing number.
    """
    if not text:
        return 0
    base = max(1, round(len(text) / 4))
    if model:
        return max(1, round(base * token_correction_factor(content_kind, model)))
    return base


# ── model version registry (Strategy/Registry) ────────────────────────────────
#
# Adding a new version = one dict entry. No new function, no new guard chain.

_MODEL_VERSION_PATTERNS: dict[str, re.Pattern[str]] = {
    "sonnet_45": re.compile(r"sonnet.*4[-/._]?5(?!\d)|sonnet.*45(?!\d)"),
    "sonnet_46": re.compile(r"sonnet.*4[-/._]?6(?!\d)|4\.6|4-6(?!\d)"),
    "sonnet_5":  re.compile(r"sonnet[-/._]?5(?!\d)"),
}


def _match_model_version(model: str | None, key: str) -> bool:
    if not model or "sonnet" not in model.lower():
        return False
    return bool(_MODEL_VERSION_PATTERNS[key].search(model.lower()))


def _is_sonnet_45_model(model: str | None) -> bool:
    return _match_model_version(model, "sonnet_45")


def _is_sonnet_46_model(model: str | None) -> bool:
    if _is_sonnet_45_model(model):
        return False
    return _match_model_version(model, "sonnet_46")


def _is_sonnet_5_model(model: str | None) -> bool:
    if _is_sonnet_45_model(model) or _is_sonnet_46_model(model):
        return False
    return _match_model_version(model, "sonnet_5")


# ── token correction ──────────────────────────────────────────────────────────

def token_correction_factor(content_kind: str, model: str | None) -> float:
    """Model-aware multiplier applied after chars/4 heuristic.

    Uses _MODEL_VERSION_PATTERNS registry to resolve model → correction bucket.
    First match in registry order wins. Falls back to 'default' bucket if no
    pattern matches or bucket key is absent from config.

    Override any kind via COST_ROUTER_TOKEN_CORRECTION_PROSE/CODE/SYMBOL env vars.
    """
    env_keys = {
        "prose": "COST_ROUTER_TOKEN_CORRECTION_PROSE",
        "code": "COST_ROUTER_TOKEN_CORRECTION_CODE",
        "symbol": "COST_ROUTER_TOKEN_CORRECTION_SYMBOL",
    }
    env_key = env_keys.get(content_kind, env_keys["prose"])
    raw = os.environ.get(env_key)
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass

    config = load_plugin_config()
    correction = config.get("token_estimation", {}).get("correction", {})

    # Iterate registry in definition order; first match wins
    if model:
        lower = model.lower()
        for key, pattern in _MODEL_VERSION_PATTERNS.items():
            if pattern.search(lower):
                bucket = correction.get(key)
                if bucket:
                    return float(bucket.get(content_kind, bucket.get("prose", 1.5)))

    # No registry match or no config key → conservative unknown default
    defaults = correction.get("default", {"prose": 1.5, "code": 2.0, "symbol": 2.5})
    return float(defaults.get(content_kind, defaults.get("prose", 1.5)))


def model_context_window_resolved(model: str | None) -> tuple[int, bool]:
    """Return (window, is_default). is_default is True when the model matched no specific
    context_windows key and fell back to the `default` entry. If token_estimation.force_fixed_window is set, that value always wins."""
    force_window = config_int("token_estimation.force_fixed_window", 0)
    if force_window > 0:
        return force_window, False
    default_window = config_int("token_estimation.context_windows.default", 200_000)
    if not model:
        return default_window, True
    lower = model.lower()
    segs = _model_segments(model)
    if "composer" in lower or "composer" in segs:
        return config_int("token_estimation.context_windows.composer", default_window), False
    if "sonnet" in lower or "sonnet" in segs:
        if _is_sonnet_45_model(model):
            return config_int("token_estimation.context_windows.sonnet_45", default_window), False
        if _is_sonnet_5_model(model):
            return config_int("token_estimation.context_windows.sonnet_5_5", default_window), False
        if _is_sonnet_46_model(model):
            return config_int("token_estimation.context_windows.sonnet_46_5", default_window), False
    return default_window, True


def model_context_window(model: str | None) -> int:
    """Per-model context window for budget math (community figures, labeled est.)."""
    return model_context_window_resolved(model)[0]


# ── pricing ───────────────────────────────────────────────────────────────────

def price_for(model: str | None, pricing: dict[str, Any]) -> dict[str, float]:
    """Look up a $/1K-token price entry by segment match on model name,
    falling back to the pricing table's "default" entry."""
    default = pricing.get("default", {"input_per_1k": 0.003, "output_per_1k": 0.015})
    if not model:
        return default
    segs = _model_segments(model)
    for key, value in pricing.items():
        if key == "default":
            continue
        if _model_segments(key) & segs:
            return value
    return default


def est_cold_write_cost_usd(model: str | None, infra: int | None = None) -> float:
    """(est.) cold-write cost for the infra prefix using cache_write_per_1k."""
    tokens = infra if infra is not None else infra_tokens(model)
    if tokens <= 0:
        return 0.0
    pricing = load_pricing()
    price = price_for(model, pricing)
    per_1k = price.get("cache_write_per_1k")
    if not isinstance(per_1k, (int, float)):
        input_per_1k = price.get("input_per_1k", 0.003)
        per_1k = float(input_per_1k) * 1.25
    return (tokens / 1000.0) * float(per_1k)


def explore_sonnet_waste_usd() -> float:
    """(est.) cost of one Explore-agent dispatch that ran on a reasoning model
    instead of haiku, derived from live scripts/pricing.json rather than a
    hardcoded dollar figure (MVP-1). Assumes a typical Explore-agent dispatch
    produces roughly EXPLORE_AVG_OUTPUT_TOKENS output tokens (configurable via
    config.json's tune_savings.explore_avg_tokens, default below) at the
    reasoning tier's real output price — so this figure moves automatically
    if pricing.json is edited, instead of silently going stale.
    """
    avg_tokens = config_int("tune_savings.explore_avg_tokens", 3000)
    pricing = load_pricing()
    sonnet_price = price_for("sonnet", pricing).get("output_per_1k", 0.015)
    return round(avg_tokens / 1000 * sonnet_price, 4)


def corrected_estimate_tokens(
    text: str,
    content_kind: str = "prose",
    model: str | None = None,
) -> int:
    """Apply model-aware correction to the local chars/4 heuristic."""
    if not text:
        return 0
    base = estimate_tokens(text)
    factor = token_correction_factor(content_kind, model)
    return max(0, round(base * factor))
