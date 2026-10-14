"""Schema contracts for cost-router data pipeline files.

Derived from write-path scripts — authoritative field contracts for all
append-only JSONL files produced by the hook chain.

Each schema:
  "required": {field: type_or_tuple_of_types}  — field must be present, isinstance check
  "invariants": [(field, predicate, description)]  — value-level rules

session_signals.jsonl is array-per-line (6-tuple), not a dict — handled separately.
"""
from __future__ import annotations

_Num = (int, float)
_StrOrNone = (str, type(None))
_NumOrNone = (int, float, type(None))

# Fields common to every usage.jsonl record type.
USAGE_COMMON: dict = {
    "required": {
        "type": str,
        "session_id": str,
        "ts": str,
    },
    "invariants": [
        ("session_id", lambda v: bool(v), "must be non-empty"),
    ],
}

# Per-type schemas for usage.jsonl — only type-specific fields (not common ones).
# Unknown types pass common-only validation.
USAGE_EVENT_SCHEMAS: dict[str, dict] = {
    "tool": {
        "required": {
            "tier": str,
            "model": _StrOrNone,
            "direction": str,
            "tool_name": str,
            "bytes": int,
            "est_tokens": int,
        },
        "invariants": [
            ("bytes", lambda v: v >= 0, "must be >= 0"),
            ("est_tokens", lambda v: v >= 0, "must be >= 0"),
        ],
    },
    "prompt": {
        "required": {
            "tier": str,
            "model": _StrOrNone,
            "direction": str,
            "bytes": int,
            "est_tokens": int,
        },
        "invariants": [
            ("bytes", lambda v: v >= 0, "must be >= 0"),
            ("est_tokens", lambda v: v >= 0, "must be >= 0"),
        ],
    },
    "response": {
        "required": {
            "tier": str,
            "model": _StrOrNone,
            "direction": str,
            "bytes": int,
            "est_tokens": int,
            "token_source": str,
        },
        "invariants": [
            ("bytes", lambda v: v >= 0, "must be >= 0"),
            ("est_tokens", lambda v: v >= 0, "must be >= 0"),
        ],
    },
    "session_summary": {
        "required": {
            "started_at": _StrOrNone,
            "ended_at": _StrOrNone,
            "est_input_tokens": int,
            "est_output_tokens": int,
            "est_context_tokens": int,
            "est_cost_usd": _Num,
            "subagent_dispatch_count": int,
            "subagent_unattributed_count": int,
            "tool_call_count": int,
            "tier_mix": dict,
            "reward": _Num,
            "has_real_tokens": bool,
            "session_type": str,
            "explore_without_haiku": int,
            "total_agents_spawned": int,
            "agent_model_waste_usd": _Num,
            "rd2_nudges": int,
            "rd2_override_rate": _Num,
            "resume_used_count": int,
        },
        "invariants": [
            ("est_input_tokens", lambda v: v >= 0, "must be >= 0"),
            ("est_output_tokens", lambda v: v >= 0, "must be >= 0"),
            ("est_context_tokens", lambda v: v >= 0, "must be >= 0"),
            ("est_cost_usd", lambda v: v >= 0, "must be >= 0"),
            ("rd2_override_rate", lambda v: 0.0 <= v <= 1.0, "must be in [0.0, 1.0]"),
            ("total_agents_spawned", lambda v: v >= 0, "must be >= 0"),
            ("agent_model_waste_usd", lambda v: v >= 0, "must be >= 0"),
        ],
    },
    "routing_declared": {
        "required": {
            "turn_id": str,
            "role": str,
            "model": str,
            "intent": str,
            "rule_id": str,
        },
        "invariants": [
            ("turn_id", lambda v: bool(v), "must be non-empty"),
            ("role", lambda v: bool(v), "must be non-empty"),
        ],
    },
    "subagent_dispatch": {
        "required": {
            "subagent_id": str,
            "tier": str,
            "model": _StrOrNone,
            "est_tokens": int,
        },
        "invariants": [
            ("subagent_id", lambda v: bool(v), "must be non-empty"),
            ("est_tokens", lambda v: v >= 0, "must be >= 0"),
        ],
    },
    "intent": {
        "required": {
            "turn_id": str,
            "intent": str,
        },
        "invariants": [
            ("turn_id", lambda v: bool(v), "must be non-empty"),
        ],
    },
    "config_error": {
        "required": {
            "error_type": str,
            "config_key": str,
        },
        "invariants": [
            ("error_type", lambda v: bool(v), "must be non-empty"),
            ("config_key", lambda v: bool(v), "must be non-empty"),
        ],
    },
    "role_distribution": {
        "required": {
            "counts": dict,
            "turns_total": int,
            "turns_missing_routing": int,
        },
        "invariants": [
            ("turns_total", lambda v: v >= 0, "must be >= 0"),
            ("turns_missing_routing", lambda v: v >= 0, "must be >= 0"),
        ],
    },
}

SESSION_LOG_COMMON: dict = {
    "required": {
        "session_id": str,
        "ts": str,
    },
    "invariants": [
        ("session_id", lambda v: bool(v), "must be non-empty"),
    ],
}

SESSION_LOG_TURN_SCHEMA: dict = {
    "required": {
        "turn_id": str,
        "model_key": str,
        "est_output_tokens": _Num,
        "tier": str,
        "content_kind": str,
    },
    "invariants": [],
}

SESSION_LOG_EVENT_SCHEMA: dict = {
    "required": {
        "event_type": str,
    },
    "invariants": [
        ("event_type", lambda v: bool(v), "must be non-empty"),
    ],
}

SCHEMAS: dict[str, dict] = {
    "shadow_routes.jsonl": {
        "required": {
            "session_id": str,
            "timestamp": str,
            "rule_id": str,
            "actual_role": str,
            "actual_model": str,
            "candidate_role": str,
            "candidate_model": str,
            "reason": str,
        },
        "invariants": [
            ("session_id", lambda v: bool(v), "must be non-empty"),
            ("actual_role", lambda v: bool(v), "must be non-empty"),
            ("candidate_role", lambda v: bool(v), "must be non-empty"),
        ],
    },
    "reward_log.jsonl": {
        "required": {
            "rule_id": str,
            "routing_role": str,
            "actual_model": str,
            "base_reward_value": _Num,
            "quality_multiplier": _Num,
            "revert_rate_at_compute": _Num,
            "reward_value": _Num,
            "timestamp": str,
            "session_id": str,
        },
        "invariants": [
            ("session_id", lambda v: bool(v), "must be non-empty"),
            ("quality_multiplier", lambda v: v >= 0, "must be >= 0"),
            ("revert_rate_at_compute", lambda v: 0.0 <= v <= 1.0, "must be in [0.0, 1.0]"),
        ],
    },
    "calibration.jsonl": {
        "required": {
            "timestamp": str,
            "session_id": str,
            "model_id": str,
            "content_kind": str,
            "estimated_tokens": int,
            "actual_tokens": int,
            "ratio": _NumOrNone,
        },
        "invariants": [
            ("session_id", lambda v: bool(v), "must be non-empty"),
            ("estimated_tokens", lambda v: v >= 0, "must be >= 0"),
            ("actual_tokens", lambda v: v >= 0, "must be >= 0"),
        ],
    },
    "policy_changelog.jsonl": {
        "required": {
            "timestamp": str,
            "event_type": str,
            "rule_id": str,
        },
        "invariants": [
            ("event_type", lambda v: bool(v), "must be non-empty"),
            ("rule_id", lambda v: bool(v), "must be non-empty"),
        ],
    },
}

# session_signals.jsonl: each line is a JSON array [signals, outcome, confidence,
# duration_seconds, reward, retried_intent] — validated structurally, not as a dict.
SESSION_SIGNALS_SCHEMA = {
    "length": 6,
    "indices": {
        0: (list, "signals must be a list"),
        1: (str, "outcome must be a string"),
        2: (str, "confidence must be a string"),
        3: (_Num + (type(None),), "duration_seconds must be a number or null"),
        4: (_NumOrNone, "reward must be a number or null"),
        5: (_StrOrNone, "retried_intent must be a string or null"),
    },
    "valid_outcomes": {"retried", "abandoned", "clean_success", "unknown"},
    "valid_confidences": {"high", "medium", "low"},
}
