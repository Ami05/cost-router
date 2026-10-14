"""Dispatch budget enforcement and grouping gate.

Extracted from estimate_dispatch_context.py.
Provides budget nudge/deny message builders and the grouping enforcement
check that prevents redundant cold-start dispatches when a warm worker exists.

Supports two enforcement modes:
1. Band-based (DGC-203): min_useful_work, target_min/max, hard_limit thresholds.
   Activated when dispatch_context.bands.mode is 'warn' or 'enforce'.
2. Legacy percentage-based: budget_pct / over_budget_pct gates.
   Active when dispatch_context.bands.mode is 'off'.

Depends on RoleGateQuery from role_gate_interface (via context_estimator)
rather than importing enforce_role_gates directly.

Stdlib-only; fail-open on registry errors or missing config.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DISPATCH_NUDGE_PCT_MIN,
    DISPATCH_OVER_BUDGET_PCT,
)
from context_estimator import (  # noqa: E402
    classify_handoff,
    extract_paths_from_prompt,
    load_dispatch_registry,
    suggest_resume,
    _registry_workers,
)
from path_range_utils import (  # noqa: E402
    _project_root,
    _region_aware_overlap_enabled,
    _split_path_and_range,
    _any_ref_overlap,
    resolve_safe_path,
)
from affinity import (  # noqa: E402
    affinity_score,
    is_high_affinity,
    get_affinity_threshold,
)
from split_planner import (  # noqa: E402
    plan_split_for_dispatch,
)


# ---------------------------------------------------------------------------
# Band configuration and helpers (DGC-203)
# ---------------------------------------------------------------------------


def _get_bands_config() -> dict[str, Any]:
    """Load dispatch_context.bands from config.json.

    Returns an empty dict if config cannot be loaded; caller must handle
    gracefully (fail-open pattern).
    """
    try:
        config_path = Path(__file__).resolve().parent.parent / "config.json"
        if not config_path.exists():
            return {}
        with open(config_path, "r") as f:
            config = json.load(f)
        bands = config.get("dispatch_context", {}).get("bands", {})
        return bands
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def _get_bands_mode() -> str:
    """Get the bands mode from config.

    Returns: 'off', 'warn', 'enforce', or 'warn' (default if missing/invalid).
    """
    bands = _get_bands_config()
    mode = bands.get("mode", "warn")
    if mode not in ("off", "warn", "enforce"):
        return "warn"
    return mode


def _get_model_tier(est: dict[str, Any]) -> str:
    """Determine model tier from estimate.

    Returns: 'fast' or 'reasoning' (default).
    Heuristic: if infra_tokens match fast tier, classify as fast; otherwise reasoning.
    """
    bands = _get_bands_config()
    infra_fast = bands.get("subagent_infra_tokens", {}).get("fast", 40000)
    est_infra = est.get("infra_tokens", 0)
    if est_infra > 0 and est_infra <= infra_fast:
        return "fast"
    return "reasoning"


def _apply_bands_logic(est: dict[str, Any], prompt: str | None = None) -> tuple[str, str | None]:
    """Apply band-based budget checks per DGC-203.

    Returns: (action, message)
    where action is one of: 'allow', 'nudge', 'deny'
    and message is the nudge/deny text or None if allow-silent.

    Bands thresholds (tokens):
    - Below min_useful_work: nudge to batch or wait
    - target_min to target_max (140K-165K): allow silently
    - target_max to hard_limit (165K-180K): nudge to split
    - Above hard_limit (180K): deny

    When bands.mode is 'warn', deny becomes nudge (non-blocking).
    """
    if est.get("estimate_failed"):
        return ("allow", None)

    bands = _get_bands_config()
    mode = _get_bands_mode()

    if mode == "off":
        return ("legacy", None)  # Signal to use legacy logic

    total_tokens = int(est.get("total_tokens", 0))
    tier = _get_model_tier(est)

    min_useful = bands.get("min_useful_work_tokens", {}).get(
        tier, bands.get("min_useful_work_tokens", {}).get("default", 70000)
    )
    target_min = bands.get("target_min", 140000)
    target_max = bands.get("target_max", 165000)
    hard_limit = bands.get("hard_limit", 180000)

    # Below min_useful_work: nudge to batch or wait
    if total_tokens < min_useful:
        msg = (
            f"[cost-router] Dispatch too small (~{total_tokens:,} tokens, "
            f"min useful work is ~{min_useful:,}). Consider batching with related tasks "
            f"or waiting to accumulate more work."
        )
        return ("nudge", msg)

    # Within target band: silent allow
    if target_min <= total_tokens <= target_max:
        return ("allow", None)

    # Above target_max but below hard_limit: nudge to split
    if target_max < total_tokens < hard_limit:
        msg = (
            f"[cost-router] Dispatch context large (~{total_tokens:,} tokens). "
            f"Consider splitting into smaller units to stay within target range "
            f"({target_min:,}-{target_max:,} tokens)."
        )
        return ("nudge", msg)

    # Above hard_limit: deny (or nudge if mode is warn)
    if total_tokens >= hard_limit:
        # Generate split plan from prompt if available (DGC-204)
        split_plan = ""
        if prompt:
            try:
                split_plan = plan_split_for_dispatch(prompt, hard_limit=hard_limit)
            except Exception:
                split_plan = ""

        deny_msg = (
            f"[cost-router] Dispatch context exceeds hard limit (~{total_tokens:,} tokens "
            f"vs {hard_limit:,} max). Split the work into smaller units."
        )
        if split_plan:
            deny_msg += split_plan
        else:
            deny_msg += " (split plan not available)"

        if mode == "warn":
            # In warn mode, log would-deny and emit non-blocking nudge
            return ("nudge", deny_msg)
        else:  # enforce
            return ("deny", deny_msg)

    return ("allow", None)


# ---------------------------------------------------------------------------
# Budget message builders
# ---------------------------------------------------------------------------


def budget_nudge_message(est: dict[str, Any], *, advisory_only: bool = True, prompt: str | None = None) -> str | None:
    """Build a nudge message based on bands (if enabled) or legacy percentage logic.

    When bands.mode is 'warn' or 'enforce', band-based thresholds take precedence.
    When bands.mode is 'off', fall back to legacy percentage-based nudges.
    """
    if est.get("estimate_failed"):
        return None

    # Try band logic first
    action, msg = _apply_bands_logic(est, prompt=prompt)
    if action == "legacy":
        # Fall through to legacy logic below
        pass
    elif action == "nudge":
        return msg
    elif action == "allow":
        return None
    # For "deny" in band logic, this function returns None; caller handles deny separately
    elif action == "deny":
        return None

    # Legacy percentage-based logic (when bands.mode is off)
    pct = float(est.get("pct_of_window", 0))
    total = int(est.get("total_tokens", 0))
    window = int(est.get("context_window", est.get("window_tokens", 0)))
    if est.get("nudge") or (DISPATCH_NUDGE_PCT_MIN <= pct <= DISPATCH_OVER_BUDGET_PCT):
        return (
            f"[cost-router] Dispatch context ~{pct:.1f}% of model window "
            f"(~{total:,}/{window:,} est. tokens) — will likely trigger compaction; "
            "consider splitting, paths-only refs, or resume on a warm worker."
        )
    if pct > DISPATCH_OVER_BUDGET_PCT:
        prefix = "[cost-router] Dispatch context likely exceeds model window"
        if advisory_only:
            prefix += " (advisory — dispatch already started)"
        return (
            f"{prefix}: ~{total:,} est. tokens vs {window:,} window ({pct:.1f}%). "
            "Split the batch, use paths-only handoff, or resume a warm worker — "
            "include `dispatch-budget-override` in Routing if you accept the risk."
        )
    return None


def should_deny_by_bands(est: dict[str, Any]) -> bool:
    """Check if dispatch should be denied by band enforcement (enforce mode only).

    Returns True if bands.mode is 'enforce' and total_tokens >= hard_limit.
    Otherwise returns False.
    """
    mode = _get_bands_mode()
    if mode != "enforce":
        return False

    if est.get("estimate_failed"):
        return False

    bands = _get_bands_config()
    hard_limit = bands.get("hard_limit", 180000)
    total_tokens = int(est.get("total_tokens", 0))

    return total_tokens >= hard_limit


def would_deny_in_warn(est: dict[str, Any]) -> bool:
    """True if bands.mode is 'warn' and the estimate is at or above hard_limit.

    Lets callers log a would-deny event so the enforce flip is data-backed.
    """
    if _get_bands_mode() != "warn" or est.get("estimate_failed"):
        return False
    hard_limit = _get_bands_config().get("hard_limit", 180000)
    return int(est.get("total_tokens", 0)) >= hard_limit


def get_band_enforcement_result(est: dict[str, Any], *, prompt: str | None = None) -> dict[str, Any]:
    """Get band enforcement decision with action and message.

    Returns a dict with keys:
      - enforcement_active (bool): True if bands.mode is 'warn' or 'enforce'
      - action (str): 'allow', 'nudge', or 'deny'
      - message (str|None): nudge/deny message or None if allow-silent
      - mode (str): 'off', 'warn', or 'enforce'

    Useful for callers that need the full picture of band enforcement state.
    """
    mode = _get_bands_mode()
    enforcement_active = mode != "off"

    if not enforcement_active:
        return {
            "enforcement_active": False,
            "action": "legacy",
            "message": None,
            "mode": "off",
        }

    action, message = _apply_bands_logic(est, prompt=prompt)
    return {
        "enforcement_active": True,
        "action": action,
        "message": message,
        "mode": mode,
    }


def budget_deny_message(est: dict[str, Any], *, prompt: str | None = None) -> str:
    """Build a deny message based on bands (if enabled and enforce mode) or legacy logic.

    When bands.mode is 'enforce' and over hard_limit, use band deny message.
    When bands.mode is 'warn' or 'off', use legacy percentage-based deny message.
    """
    mode = _get_bands_mode()

    # Check band logic for enforce mode
    if mode == "enforce":
        action, msg = _apply_bands_logic(est, prompt=prompt)
        if action == "deny" and msg:
            return msg

    # Fall back to legacy percentage-based deny message
    total = int(est.get("total_tokens", 0))
    window = int(est.get("context_window", est.get("window_tokens", 0)))
    pct = float(est.get("pct_of_window", 0))
    return (
        f"[cost-router] Task dispatch blocked — corrected est. ~{total:,} tokens "
        f"exceeds model window ({pct:.1f}% of {window:,}). Split the work, "
        "drop inline file bodies, use paths-only, or resume a warm worker. "
        "To override, include `dispatch-budget-override` in your Routing line."
    )


# ---------------------------------------------------------------------------
# Grouping enforcement
# ---------------------------------------------------------------------------

DISPATCH_GROUP_OVERRIDE_RE = re.compile(r"dispatch-group-override", re.IGNORECASE)


def _grouping_enforce_enabled() -> bool:
    flag = os.environ.get("COST_ROUTER_GROUPING_ENFORCE", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def _has_dispatch_group_override(*texts: str) -> bool:
    # Self-service overrides disabled (P2 audit). Agent text cannot bypass gates.
    # Use !override in user prompt for manual bypass.
    return False


def _task_prompt_from_input(tool_input: dict[str, Any]) -> str:
    for key in ("prompt", "task", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def grouping_deny_message(resume_id: str, *, reason: str) -> str:
    if reason == "same_turn_shared_files":
        return (
            f"[dispatch-group] Cold-start blocked — warm worker {resume_id!r} is idle "
            "and matches this task type. Use "
            f"Task(resume={resume_id!r}) instead. "
            "(same-turn shared-file overlap detected)"
        )
    return (
        f"[dispatch-group] Cold-start blocked — warm worker {resume_id!r} is idle "
        "and matches this task type. Use "
        f"Task(resume={resume_id!r}) instead."
    )


def check_grouping_enforcement(
    tool_input: dict[str, Any],
    *,
    routing_text: str = "",
    session_entry: dict[str, Any] | None = None,
    turn_id: str | None = None,
    transcript_path: str | None = None,
) -> dict[str, Any]:
    """Hard push: deny fresh Task when resume would reuse similar context.

    Returns keys: should_deny (bool), message (str|None), resume (str|None), reason (str).
    Fail-open when enforcement disabled or inputs are incomplete.
    """
    empty = {"should_deny": False, "message": None, "resume": None, "reason": ""}
    if not _grouping_enforce_enabled():
        return empty

    resume_param = tool_input.get("resume")
    if isinstance(resume_param, str) and resume_param.strip():
        return empty

    prompt = _task_prompt_from_input(tool_input)
    subagent_type = tool_input.get("subagent_type")
    if isinstance(subagent_type, str):
        subagent_type = subagent_type.strip() or None
    else:
        subagent_type = None

    if _has_dispatch_group_override(prompt, routing_text):
        return empty

    files = extract_paths_from_prompt(prompt)
    handoff = classify_handoff(prompt, "auto", files)

    # Same-turn overlap: second cold-start with shared files in one turn.
    if session_entry is not None and turn_id:
        last_turn = session_entry.get("last_task_dispatch_turn_id")
        last_files = session_entry.get("last_task_dispatch_files") or []
        if (
            last_turn == turn_id
            and isinstance(last_files, list)
            and files
            and last_files
        ):
            region_aware = _region_aware_overlap_enabled()
            _same_turn_overlap: set[str] = set()
            _used_basename_fallback = False
            if not region_aware:
                _cge_root = _project_root()
                _res_last = {str(r) for f in last_files if (r := resolve_safe_path(str(f), _cge_root)) is not None}
                _res_curr = {str(r) for f in files if (r := resolve_safe_path(str(f), _cge_root)) is not None}
                _used_basename_fallback = False
                if _res_last and _res_curr:
                    _same_turn_overlap = _res_last & _res_curr
                else:
                    _used_basename_fallback = True
                    _same_turn_overlap = {Path(str(f)).name for f in last_files} & {Path(str(f)).name for f in files}
            else:
                _cge_root = _project_root()
                _refs_last: list[tuple[str, tuple[int, int] | None]] = []
                for f in last_files:
                    _bare, _rng = _split_path_and_range(str(f))
                    _r = resolve_safe_path(_bare, _cge_root)
                    if _r is not None:
                        _refs_last.append((str(_r), _rng))
                _refs_curr: list[tuple[str, tuple[int, int] | None]] = []
                for f in files:
                    _bare, _rng = _split_path_and_range(str(f))
                    _r = resolve_safe_path(_bare, _cge_root)
                    if _r is not None:
                        _refs_curr.append((str(_r), _rng))
                if _refs_last and _refs_curr:
                    _same_turn_overlap = _any_ref_overlap(_refs_last, _refs_curr)
                else:
                    _used_basename_fallback = True
                    _same_turn_overlap = bool(
                        {Path(str(f)).name for f in last_files} & {Path(str(f)).name for f in files}
                    )
            if _same_turn_overlap:
                resume_hint = session_entry.get("last_task_dispatch_agent_id")
                if not isinstance(resume_hint, str) or not resume_hint:
                    resume_hint = "<agent_id>"
                # Only deny if the prior worker is actually idle and resumable.
                # If it's still busy, the caller cannot resume it — fail-open so
                # the new cold-start Task is allowed instead of creating a deadlock.
                if resume_hint != "<agent_id>":
                    registry = load_dispatch_registry()
                    all_workers = _registry_workers(registry)
                    worker_map = {
                        (w.get("agent_id") or w.get("id")): w
                        for w in all_workers
                        if isinstance(w.get("agent_id") or w.get("id"), str)
                    }
                    prior = worker_map.get(resume_hint)
                    if prior is None or prior.get("status") == "busy":
                        return empty  # prior worker still running — allow cold-start
                return {
                    "should_deny": True,
                    "message": grouping_deny_message(resume_hint, reason="same_turn_shared_files"),
                    "resume": resume_hint if resume_hint != "<agent_id>" else None,
                    "reason": "same_turn_shared_files",
                    "path_resolution_fallback": _used_basename_fallback,
                }

    # Warm-worker resume push requires identifiable context: file overlap (below)
    # or a zero-handoff shell command. Empty/inline prompts with no paths
    # cannot be matched to "similar context" — fail open (RB escalation,
    # FO decomposition Task with prompt in Routing line only, etc.).
    if files or handoff == "zero":
        suggest_type = subagent_type
        if handoff == "zero" and not suggest_type:
            suggest_type = "shell"

        # Cross-turn fallback: suggest_resume() checks all idle workers in the
        # registry (including those from previous turns) and enforces TTL.
        suggestion = suggest_resume(
            subagent_type=suggest_type,
            files=files if files else None,
            handoff=handoff,
            transcript_path=transcript_path,
        )
        resume_id = suggestion.get("resume")
        if isinstance(resume_id, str) and resume_id:
            return {
                "should_deny": True,
                "message": grouping_deny_message(resume_id, reason="cross_turn_file_overlap"),
                "resume": resume_id,
                "reason": "cross_turn_file_overlap",
                "path_resolution_fallback": False,
            }

    return empty


# ---------------------------------------------------------------------------
# Cluster ID exposure for pending dispatches (DGC-201)
# ---------------------------------------------------------------------------


def get_pending_dispatch_clusters(
    pending_units: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """Partition pending dispatch units into affinity clusters.

    Args:
        pending_units: list of dicts with "files" or "file_paths" keys.

    Returns:
        dict mapping cluster_id (int) to list of units in that cluster.
        Units with affinity >= threshold are grouped together.

    Uses get_affinity_threshold() from config. Deterministic: same units
    produce same clustering across calls.
    """
    if not pending_units:
        return {}

    threshold = get_affinity_threshold()
    clusters: dict[int, list[dict[str, Any]]] = {}
    unit_to_cluster: dict[int, int] = {}  # unit index -> cluster id
    next_cluster_id = 0

    for i, unit_i in enumerate(pending_units):
        if i in unit_to_cluster:
            continue

        # Start a new cluster
        cluster_id = next_cluster_id
        next_cluster_id += 1
        clusters[cluster_id] = [unit_i]
        unit_to_cluster[i] = cluster_id

        # Greedily add subsequent units with high affinity to this cluster
        for j in range(i + 1, len(pending_units)):
            if j in unit_to_cluster:
                continue
            score = affinity_score(unit_i, pending_units[j])
            if score >= threshold:
                clusters[cluster_id].append(pending_units[j])
                unit_to_cluster[j] = cluster_id

    return clusters
