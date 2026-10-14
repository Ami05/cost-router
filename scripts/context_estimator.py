"""Dispatch context token estimator — importable API.

Estimates infra + prompt + file token components with model-aware correction,
budget % vs the dispatched model's window, and optional resume suggestions
from dispatch_registry.json.

Extracted from estimate_dispatch_context.py. All symbols here are re-exported
by that module for backward compatibility.

Stdlib-only; fail-open on malformed paths or unreadable files.
All figures labeled (est.) unless a future --count-tokens mode is used.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402
import _model_utils  # noqa: E402
from _common import (  # noqa: E402
    CODE_MARKER_RATIO_THRESHOLD,
    DISPATCH_NUDGE_PCT_MIN,
    DISPATCH_OVER_BUDGET_PCT,
    PROMPT_BLOAT_THRESHOLD,
    _warn,
    config_get,
    config_int,
    load_json_state,
    now_iso,
    save_json_state,
)
from _model_utils import (  # noqa: E402
    corrected_estimate_tokens,
    dispatch_budget_pct,
    est_cold_write_cost_usd,
    estimate_tokens,
    infra_tokens,
    load_pricing,
    model_context_window,
    price_for,
    resume_ttl_seconds,
    token_correction_factor,
)
from path_range_utils import (  # noqa: E402
    _any_ref_overlap,
    _project_root,
    _region_aware_overlap_enabled,
    _split_path_and_range,
    resolve_safe_path,
)

# ---------------------------------------------------------------------------
# Constants / patterns
# ---------------------------------------------------------------------------

_AT_PATH_RE = re.compile(r"@([^\s@,;]+)")
_BACKTICK_PATH_RE = re.compile(r"`([^`\n]+)`")
_RUN_CMD_RE = re.compile(r"Run:\s*(.+?)(?:\n|$)", re.IGNORECASE | re.MULTILINE)
_SHELL_CMD_RE = re.compile(
    r"(?:^|\n)\s*(?:pytest\b[^\n]*|npm\s+test\b[^\n]*|npm\s+run\s+test\b[^\n]*)",
    re.IGNORECASE | re.MULTILINE,
)

_CODE_EXTS = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp",
    ".h", ".hpp", ".rb", ".php", ".sh", ".bash", ".zsh", ".swift", ".kt",
    ".scala", ".cs", ".m", ".mm", ".sql", ".r", ".lua",
})
_SYMBOL_EXTS = frozenset({
    ".json", ".map", ".yaml", ".yml", ".toml", ".xml", ".svg", ".lock",
    ".min.js", ".min.css",
})

EST_LABEL = "(est.)"

# Invariant token patterns (hook chain, common API, event shapes, gate results)
_INVARIANT_HOOK_CHAIN_RE = re.compile(
    r"estimate_tokens|check_routing|reward_signal|session_summary|graph_gc|compact_log"
)
_INVARIANT_API_RE = re.compile(
    r"config_get|config_int|config_float|load_json_state|save_json_state|append_event|session_id_of"
)
_INVARIANT_EVENT_RE = re.compile(
    r"routing_verdict|reward_signal_event|intent_event|session_summary_event|gate_result"
)
_INVARIANT_GATE_RE = re.compile(
    r"GateResult|gate_allow|gate_deny|emit_gate_result"
)

# Code fence pattern: ``` blocks
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n([\s\S]*?)```", re.MULTILINE)


# ---------------------------------------------------------------------------
# Plan-mode freeze check (uses role_gate_interface)
# ---------------------------------------------------------------------------


def _worker_frozen_in_plan_mode(
    main_transcript_path: str | None,
    agent_id: str,
    *,
    role_gate: "Any | None" = None,
) -> bool:
    """True if *agent_id*'s own transcript still privately believes Claude
    Code plan mode is active: an EnterPlanMode tool call with no later
    ExitPlanMode call among that worker's own turns.

    The registry's `status: idle` is orchestrator bookkeeping only — it does
    not know a resumed worker may still internally believe it is plan-mode
    gated. A message from the orchestrator or a peer worker is never a valid
    way to lift this belief for the worker; only a cold spawn resets it,
    which is why such a candidate must be excluded from resume rather than
    resumed and told to proceed.

    Args:
        main_transcript_path: Path to the parent session transcript.
        agent_id: The subagent's ID.
        role_gate: Optional RoleGateQuery (role_gate_interface.RoleGateQuery)
            for transcript-path resolution. When None, falls back to the
            production adapter from role_gate_interface.default_role_gate_query().

    Fail-open (False) when there is no transcript path, no per-agent
    transcript file, or the file is unreadable/malformed.
    """
    if not main_transcript_path or not agent_id:
        return False
    try:
        if role_gate is None:
            from role_gate_interface import default_role_gate_query  # noqa: PLC0415
            role_gate = default_role_gate_query()
        path = role_gate.subagent_transcript_path(main_transcript_path, agent_id)
        if path is None:
            path = Path(main_transcript_path)
        if not path.exists():
            return False
        entered = False
        for raw in path.read_text(errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else record.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                if name == "EnterPlanMode":
                    entered = True
                elif name == "ExitPlanMode":
                    entered = False
        return entered
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Path extraction + content kind
# ---------------------------------------------------------------------------


def extract_paths_from_prompt(prompt: str) -> list[str]:
    """Extract @paths and backtick file paths from a task prompt."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        cleaned = raw.strip().strip("'\"")
        if not cleaned or cleaned in seen:
            return
        if "." not in cleaned and "/" not in cleaned and "\\" not in cleaned:
            return
        seen.add(cleaned)
        found.append(cleaned)

    for match in _AT_PATH_RE.finditer(prompt):
        _add(match.group(1))
    for match in _BACKTICK_PATH_RE.finditer(prompt):
        candidate = match.group(1).strip()
        if "/" in candidate or "\\" in candidate or "." in candidate:
            _add(candidate)
    return found


extract_file_paths = extract_paths_from_prompt


def content_kind_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _SYMBOL_EXTS or suffix.endswith(".min.js") or suffix.endswith(".min.css"):
        return "symbol"
    if suffix in _CODE_EXTS:
        return "code"
    return "prose"


def content_kind_for_prompt(prompt: str, handoff: str) -> str:
    if handoff in ("zero", "paths-only"):
        return "prose"
    if re.search(r"```", prompt):
        return "code"
    code_markers = sum(1 for ch in prompt if ch in "{}();[]=<>")
    if len(prompt) > 0 and code_markers / len(prompt) > CODE_MARKER_RATIO_THRESHOLD:
        return "code"
    return "prose"


def file_tokens_for_path(path: Path, model: str | None) -> int:
    """Byte-length-only file estimate with model-aware correction."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= 0:
        return 0
    kind = content_kind_for_path(path)
    base = estimate_tokens("x" * size)
    factor = token_correction_factor(kind, model)
    return max(0, round(base * factor))


# ---------------------------------------------------------------------------
# Handoff classification
# ---------------------------------------------------------------------------


def extract_shell_command(prompt: str) -> str | None:
    run_match = _RUN_CMD_RE.search(prompt)
    if run_match:
        return run_match.group(1).strip()
    shell_match = _SHELL_CMD_RE.search(prompt)
    if shell_match:
        return shell_match.group(0).strip()
    return None


def classify_handoff(prompt: str, handoff: str, file_paths: list[str]) -> str:
    if handoff != "auto":
        return handoff
    command = extract_shell_command(prompt)
    if command and len(prompt.strip()) <= len(command) + 20:
        return "zero"
    if command and len(prompt) > len(command) * 2:
        return "inline"
    if file_paths and not command:
        return "paths-only"
    if command:
        return "zero"
    return "inline"


def effective_prompt_text(prompt: str, handoff: str) -> str:
    if handoff == "zero":
        command = extract_shell_command(prompt)
        return command if command else prompt
    if handoff == "paths-only":
        paths = extract_paths_from_prompt(prompt)
        lines = [f"FILES: {', '.join(paths)}"] if paths else []
        scope_match = re.search(r"(?i)scope:\s*(.+)", prompt)
        if scope_match:
            lines.append(f"SCOPE: {scope_match.group(1).strip()}")
        return "\n".join(lines) if lines else prompt
    return prompt


def prompt_bloat_ratio(prompt: str, prompt_tokens: int) -> float | None:
    command = extract_shell_command(prompt)
    if not command:
        return None
    command_tokens = corrected_estimate_tokens(command, "prose")
    if command_tokens <= 0:
        return None
    return round(prompt_tokens / command_tokens, 2)


# ---------------------------------------------------------------------------
# Registry read/write helpers
# ---------------------------------------------------------------------------


def _dispatch_registry_file() -> Path:
    return _common.DATA_DIR / "dispatch_registry.json"


def load_dispatch_registry() -> dict[str, Any]:
    return load_json_state(_dispatch_registry_file())


def _registry_workers(registry: dict[str, Any]) -> list[dict[str, Any]]:
    workers = registry.get("workers")
    if isinstance(workers, list):
        return [w for w in workers if isinstance(w, dict)]
    if isinstance(registry.get("agents"), list):
        return [w for w in registry["agents"] if isinstance(w, dict)]
    sessions = registry.get("sessions")
    if isinstance(sessions, dict):
        collected: list[dict[str, Any]] = []
        for entry in sessions.values():
            if not isinstance(entry, dict):
                continue
            agents = entry.get("agents")
            if isinstance(agents, dict):
                collected.extend(w for w in agents.values() if isinstance(w, dict))
            elif isinstance(agents, list):
                collected.extend(w for w in agents if isinstance(w, dict))
        if collected:
            return collected
    return []


def suggest_resume(
    *,
    subagent_type: str | None = None,
    files: list[str] | None = None,
    handoff: str = "auto",
    transcript_path: str | None = None,
) -> dict[str, Any]:
    """Return best idle worker to resume, or null when none qualifies."""
    registry = load_dispatch_registry()
    workers = _registry_workers(registry)
    if not workers:
        return {"resume": None, "reason": "no registry"}

    root = _project_root()
    region_aware = _region_aware_overlap_enabled() and bool(files)
    normalized_refs: list[tuple[str, tuple[int, int] | None]] | None = None
    normalized_files: set[str] | None = None
    if not region_aware:
        if files:
            normalized_files = set()
            for fp in files:
                resolved = resolve_safe_path(fp, root)
                if resolved is not None:
                    normalized_files.add(str(resolved))
    else:
        normalized_refs = []
        for fp in files:
            bare, rng = _split_path_and_range(fp)
            r = resolve_safe_path(bare, root)
            if r is not None:
                normalized_refs.append((str(r), rng))

    now_ts = datetime.now(timezone.utc).timestamp()
    ttl = resume_ttl_seconds()

    candidates: list[tuple[int, dict[str, Any]]] = []
    for worker in workers:
        agent_id = worker.get("agent_id") or worker.get("id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        if worker.get("status") not in (None, "idle"):
            continue
        # Registry idle only reflects orchestrator bookkeeping; a worker may
        # privately still believe plan mode is active. See
        # _worker_frozen_in_plan_mode() docstring — only a cold spawn can
        # lift that belief, so exclude it as a resume candidate here.
        if _worker_frozen_in_plan_mode(transcript_path, agent_id):
            continue
        # TTL check: skip workers whose warm window has expired.
        warm_until_raw = worker.get("warm_until")
        if warm_until_raw is not None:
            try:
                dt = datetime.fromisoformat(str(warm_until_raw))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                warm_until_ts = dt.timestamp()
                if now_ts > warm_until_ts:
                    continue
            except (ValueError, TypeError):
                pass  # malformed timestamp — fail-open, allow candidate
        elif ttl > 0:
            # No warm_until stored; fall back to finished_at + ttl.
            finished_raw = worker.get("finished_at")
            if finished_raw is not None:
                try:
                    dt = datetime.fromisoformat(str(finished_raw))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    finished_ts = dt.timestamp()
                    if now_ts > finished_ts + ttl:
                        continue
                except (ValueError, TypeError):
                    pass  # fail-open
        worker_type = worker.get("subagent_type") or worker.get("type")
        if subagent_type and worker_type != subagent_type:
            continue
        if handoff == "zero" and worker_type != "shell":
            continue
        if not region_aware:
            if normalized_files is not None:
                touched = worker.get("files_touched") or worker.get("files") or []
                if not isinstance(touched, list):
                    continue
                worker_files = set()
                for item in touched:
                    if not isinstance(item, str):
                        continue
                    resolved = resolve_safe_path(item, root)
                    worker_files.add(str(resolved) if resolved else item)
                if not normalized_files & worker_files:
                    continue
        elif normalized_refs is not None:
            touched = worker.get("files_touched") or worker.get("files") or []
            if not isinstance(touched, list):
                continue
            worker_refs = []
            for item in touched:
                if not isinstance(item, str):
                    continue
                bare, rng = _split_path_and_range(item)
                r = resolve_safe_path(bare, root)
                worker_refs.append((str(r) if r else bare, rng))
            if not _any_ref_overlap(normalized_refs, worker_refs):
                continue
        score = int(worker.get("est_context_tokens") or 0)
        candidates.append((score, worker))

    if not candidates:
        return {"resume": None, "reason": "no matching idle worker"}

    candidates.sort(key=lambda item: item[0])
    best = candidates[0][1]
    agent_id = best.get("agent_id") or best.get("id")
    return {"resume": agent_id, "reason": "idle worker with matching context"}


def cc_resume_suggest(
    *,
    session_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Stub for Claude Code resume readiness.

    Returns suggest_resume=True only when COST_ROUTER_CC_RESUME_ENABLED=1 AND
    a mock/real agent_id is available. Currently always returns suggest_resume=False
    because Claude Code does not expose resume semantics yet.

    Activate via: COST_ROUTER_CC_RESUME_ENABLED=1 (default: off).
    """
    enabled = os.environ.get("COST_ROUTER_CC_RESUME_ENABLED", "0").strip() == "1"
    if not enabled:
        return {
            "suggest_resume": False,
            "agent_id": None,
            "reason": "COST_ROUTER_CC_RESUME_ENABLED not set — Claude Code does not expose resume",
        }
    # Flag-enabled path: in a real implementation this would query the registry
    # for a warm worker. For now returns a stub shape for testing.
    return {
        "suggest_resume": True,
        "agent_id": f"stub-{session_id or 'unknown'}-{task_id or 'task'}",
        "reason": "stub — replace with real registry lookup when API available",
    }


# ---------------------------------------------------------------------------
# Session-scoped registry writes
# ---------------------------------------------------------------------------


def _session_agents(registry: dict[str, Any], session_id: str) -> dict[str, Any]:
    sessions = registry.setdefault("sessions", {})
    entry = sessions.get(session_id)
    if not isinstance(entry, dict):
        entry = {"agents": {}}
        sessions[session_id] = entry
    if not isinstance(entry.get("agents"), dict):
        entry["agents"] = {}
    return entry["agents"]


def mark_registry_busy(
    session_id: str,
    subagent_id: str,
    *,
    subagent_type: str | None = None,
    model: str | None = None,
    task_hash: str | None = None,
    files: list[str] | None = None,
    composition: dict[str, int | float] | None = None,
    dispatch_worker: str | None = None,
    dispatch_intent: str | None = None,
    requested_model: str | None = None,
    dispatch_context_estimate: int | None = None,
) -> None:
    path = _dispatch_registry_file()
    with open(path, "a+b") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            registry = load_dispatch_registry()
            agents = _session_agents(registry, session_id)
            file_names = [Path(f).name for f in (files or [])]
            entry = {
                "agent_id": subagent_id,
                "subagent_id": subagent_id,
                "subagent_type": subagent_type,
                "type": subagent_type,
                "model": model,
                "task_hash": task_hash,
                "files": file_names,
                "files_touched": list(files or []),
                "status": "busy",
                "started_at": now_iso(),
                "dispatch_worker": dispatch_worker,
                "dispatch_intent": dispatch_intent,
            }
            if composition is not None:
                entry["composition"] = composition
            if requested_model is not None:
                entry["requested_model"] = requested_model
            if dispatch_context_estimate is not None:
                entry["dispatch_context_estimate"] = dispatch_context_estimate
            agents[subagent_id] = entry
            save_json_state(path, registry)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def mark_registry_idle(
    session_id: str,
    subagent_id: str,
    *,
    est_context_tokens: int | None = None,
    result_status: str | None = None,
    remaining: list[dict[str, Any]] | None = None,
    tokens_used: int | None = None,
    depth: int | None = None,
    manifest_source: str | None = None,
    stall_killed: bool | None = None,
) -> None:
    path = _dispatch_registry_file()
    with open(path, "a+b") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            registry = load_dispatch_registry()
            agents = _session_agents(registry, session_id)
            entry = agents.get(subagent_id)
            if not isinstance(entry, dict):
                entry = {"agent_id": subagent_id, "subagent_id": subagent_id}
                agents[subagent_id] = entry
            finished = datetime.now(timezone.utc)
            warm_until = finished.timestamp() + resume_ttl_seconds()
            warm_until_iso = datetime.fromtimestamp(warm_until, tz=timezone.utc).isoformat(
                timespec="seconds"
            )
            entry["status"] = "idle"
            entry["finished_at"] = finished.isoformat(timespec="seconds")
            entry["warm_until"] = warm_until_iso
            if est_context_tokens is not None:
                entry["est_context_tokens"] = est_context_tokens

            # DGC-106: Add manifest-related fields
            entry["manifest_schema_version"] = 1
            if result_status is not None:
                entry["result_status"] = result_status
            if tokens_used is not None:
                entry["tokens_used"] = tokens_used
            if depth is not None:
                entry["depth"] = depth
            if manifest_source is not None:
                entry["manifest_source"] = manifest_source
            if stall_killed is not None:
                entry["stall_killed"] = stall_killed
            if remaining is not None:
                # Cap to 20 items per spec
                entry["remaining"] = remaining[:20] if isinstance(remaining, list) else remaining

            # Update manifest_stats at registry top level
            if not isinstance(registry.get("manifest_stats"), dict):
                registry["manifest_stats"] = {"total": 0, "unknown": 0, "by_status": {}}

            stats = registry["manifest_stats"]
            stats["total"] = stats.get("total", 0) + 1
            if result_status == "UNKNOWN":
                stats["unknown"] = stats.get("unknown", 0) + 1
            if result_status:
                if "by_status" not in stats:
                    stats["by_status"] = {}
                stats["by_status"][result_status] = stats["by_status"].get(result_status, 0) + 1

            save_json_state(path, registry)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# DGC-106: Manifest readers and utilities
# ---------------------------------------------------------------------------


def get_last_manifest(agent_id: str) -> dict[str, Any] | None:
    """Read manifest entry for a given agent_id from registry.

    Returns the entry dict if found and manifest_schema_version=1; else None.
    Legacy entries (missing manifest_schema_version) return UNKNOWN status.
    """
    registry = load_dispatch_registry()
    sessions = registry.get("sessions", {})
    for session_entry in sessions.values():
        if not isinstance(session_entry, dict):
            continue
        agents = session_entry.get("agents", {})
        if not isinstance(agents, dict):
            continue
        entry = agents.get(agent_id)
        if isinstance(entry, dict) and entry.get("manifest_schema_version") == 1:
            return entry
    return None


def get_manifest_stats() -> dict[str, Any]:
    """Get manifest statistics from registry top-level key.

    Returns: {total, unknown, unknown_rate}
    """
    registry = load_dispatch_registry()
    stats = registry.get("manifest_stats", {})
    total = stats.get("total", 0)
    unknown = stats.get("unknown", 0)
    unknown_rate = (unknown / total * 100) if total > 0 else 0
    return {
        "total": total,
        "unknown": unknown,
        "unknown_rate": unknown_rate,
    }


def reconcile_stuck_busy(
    registry: dict[str, Any],
    now: datetime,
    exclude_ids: set[str] | None = None,
    min_age_hours: int = 6,
) -> None:
    """Mark stuck busy entries as idle.

    Scans registry for status=="busy" entries older than min_age_hours that are
    not in exclude_ids set; marks them idle with result_status=UNKNOWN and
    reconciled=True. Caller must hold the same flock. Function only; never
    invoked on production data.

    Args:
        registry: dispatch_registry dict
        now: current datetime (timezone-aware UTC)
        exclude_ids: set of agent_ids to skip
        min_age_hours: age threshold in hours
    """
    if exclude_ids is None:
        exclude_ids = set()

    sessions = registry.get("sessions", {})
    if not isinstance(sessions, dict):
        return

    min_age_seconds = min_age_hours * 3600
    now_ts = now.timestamp()

    for session_entry in sessions.values():
        if not isinstance(session_entry, dict):
            continue
        agents = session_entry.get("agents", {})
        if not isinstance(agents, dict):
            continue

        for agent_id, entry in agents.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("status") != "busy":
                continue
            if agent_id in exclude_ids:
                continue

            started_at_str = entry.get("started_at")
            if not started_at_str:
                continue
            try:
                started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
                age_seconds = now_ts - started_at.timestamp()
            except (ValueError, AttributeError):
                continue

            if age_seconds >= min_age_seconds:
                entry["status"] = "idle"
                entry["result_status"] = "UNKNOWN"
                entry["finished_at"] = now.isoformat(timespec="seconds")
                entry["reconciled"] = True
                if "warm_until" in entry:
                    del entry["warm_until"]


# ---------------------------------------------------------------------------
# Prompt composition classification
# ---------------------------------------------------------------------------


def classify_prompt_composition(
    prompt: str, prompt_tokens: int, model: str | None = None
) -> dict[str, int | float]:
    """Classify prompt tokens by category: invariant, incremental, task_spec.

    Returns: {
        "invariant_tokens": int,
        "incremental_tokens": int,
        "task_spec_tokens": int,
        "invariant_ratio": float (0.0 if prompt_tokens==0)
    }

    Fail-open: on any exception return safe defaults.
    """
    try:
        if not prompt or prompt_tokens <= 0:
            return {
                "invariant_tokens": 0,
                "incremental_tokens": 0,
                "task_spec_tokens": prompt_tokens,
                "invariant_ratio": 0.0,
            }

        invariant_tokens = 0
        incremental_tokens = 0

        # Check for invariant patterns in the entire prompt text
        if (
            _INVARIANT_HOOK_CHAIN_RE.search(prompt)
            or _INVARIANT_API_RE.search(prompt)
            or _INVARIANT_EVENT_RE.search(prompt)
            or _INVARIANT_GATE_RE.search(prompt)
        ):
            # Rough estimate: count lines containing invariant patterns
            lines = prompt.split("\n")
            for line in lines:
                if (
                    _INVARIANT_HOOK_CHAIN_RE.search(line)
                    or _INVARIANT_API_RE.search(line)
                    or _INVARIANT_EVENT_RE.search(line)
                    or _INVARIANT_GATE_RE.search(line)
                ):
                    # Estimate tokens for this line segment
                    line_tokens = corrected_estimate_tokens(line, "prose", model)
                    invariant_tokens += line_tokens

        # Check for incremental code blocks (>10 lines, not preceded by CONTEXT:/FILES:)
        for match in _CODE_FENCE_RE.finditer(prompt):
            block_content = match.group(1)
            lines = block_content.split("\n")
            if len(lines) > 10:
                # Check if preceded by CONTEXT: or FILES: within 3 lines before
                start_pos = match.start()
                before_text = prompt[:start_pos]
                before_lines = before_text.split("\n")
                is_preceded_by_header = False
                if len(before_lines) >= 1:
                    for i in range(max(0, len(before_lines) - 3), len(before_lines)):
                        if (
                            "CONTEXT:" in before_lines[i]
                            or "FILES:" in before_lines[i]
                        ):
                            is_preceded_by_header = True
                            break

                if not is_preceded_by_header:
                    block_tokens = corrected_estimate_tokens(
                        block_content, "code", model
                    )
                    incremental_tokens += block_tokens

        # Calculate task_spec as remainder
        task_spec_tokens = max(0, prompt_tokens - invariant_tokens - incremental_tokens)

        invariant_ratio = (
            invariant_tokens / prompt_tokens if prompt_tokens > 0 else 0.0
        )
        invariant_ratio = round(invariant_ratio, 4)

        return {
            "invariant_tokens": invariant_tokens,
            "incremental_tokens": incremental_tokens,
            "task_spec_tokens": task_spec_tokens,
            "invariant_ratio": invariant_ratio,
        }

    except Exception as e:
        # Fail-open: return safe defaults on any exception
        _warn("prompt_classification_failed", {"file": "context_estimator.py", "error": str(e)})
        return {
            "invariant_tokens": 0,
            "incremental_tokens": 0,
            "task_spec_tokens": prompt_tokens,
            "invariant_ratio": 0.0,
        }


def verify_template_hash(raise_on_mismatch: bool = False) -> dict[str, Any]:
    """Verify that the dispatch context template hash matches the current _common.py API."""
    import hashlib
    import re as _re

    try:
        hash_file = _common.DATA_DIR.parent / "data" / "dispatch_template_hash.txt"
        if not hash_file.exists():
            return {
                "match": True,
                "error": f"template hash file not found: {hash_file}",
            }

        hash_text = hash_file.read_text(encoding="utf-8").strip()

        expected_hash = None
        for line in hash_text.split("\n"):
            if line.startswith("sha256:"):
                expected_hash = line.replace("sha256:", "").strip()
                break

        if not expected_hash:
            return {
                "match": True,
                "error": "no sha256: line found in template hash file",
            }

        common_file = _common.PLUGIN_ROOT / "scripts" / "_common.py"
        common_content = common_file.read_text(encoding="utf-8")

        pattern = r"^def ([a-zA-Z_]\w+)\("
        matches = _re.findall(pattern, common_content, _re.MULTILINE)
        public_funcs = sorted([m for m in matches if not m.startswith("_")])

        func_string = "\n".join(public_funcs)
        actual_hash = hashlib.sha256(func_string.encode()).hexdigest()

        match = expected_hash == actual_hash
        result = {
            "match": match,
            "expected": expected_hash,
            "actual": actual_hash,
            "exports": public_funcs,
        }

        if not match and raise_on_mismatch:
            raise ValueError(
                f"Template hash mismatch: expected {expected_hash}, got {actual_hash}. "
                f"Update data/dispatch_template_hash.txt if _common.py public API changed."
            )

        return result

    except Exception as e:
        _warn("template_hash_verify_failed", {"file": "context_estimator.py", "error": str(e)})
        return {
            "match": True,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Core estimator
# ---------------------------------------------------------------------------


def _est_context_input_cost_usd(tokens: int, model: str | None) -> float:
    if tokens <= 0:
        return 0.0
    pricing = load_pricing()
    price = price_for(model, pricing)
    per_1k = float(price.get("input_per_1k", 0.003))
    return (tokens / 1000.0) * per_1k


def estimate_dispatch_context(
    task_prompt: str,
    file_paths: list[str] | None = None,
    model: str | None = None,
    resume: bool = False,
    handoff: str = "auto",
) -> dict[str, Any]:
    """Estimate dispatch context components and budget posture."""
    prompt = task_prompt or ""
    root = _project_root()

    extracted = extract_paths_from_prompt(prompt)
    explicit_paths = list(file_paths or [])
    all_paths: list[str] = []
    seen_paths: set[str] = set()
    for raw in explicit_paths + extracted:
        if raw not in seen_paths:
            seen_paths.add(raw)
            all_paths.append(raw)

    resolved_handoff = classify_handoff(prompt, handoff, all_paths)
    effective_prompt = effective_prompt_text(prompt, resolved_handoff)
    prompt_kind = content_kind_for_prompt(effective_prompt, resolved_handoff)
    prompt_tokens = corrected_estimate_tokens(effective_prompt, prompt_kind, model)

    file_token_total = 0
    resolved_files: list[str] = []
    rejected_paths: list[str] = []
    # Only count file tokens for explicitly passed file_paths (CLI --files),
    # not for path references extracted from prompt text. Agents read
    # referenced files on-demand via tools; they're not inlined at dispatch.
    for raw in explicit_paths:
        resolved = resolve_safe_path(raw, root)
        if resolved is None:
            rejected_paths.append(raw)
            continue
        resolved_files.append(str(resolved))
        file_token_total += file_tokens_for_path(resolved, model)

    infra = 0 if resume else infra_tokens(model)

    # Safety reserve: fixed buffer held back from the window (DGC-202).
    # Default 10000 tokens is unmeasured placeholder; revisit once DGC-004
    # reports estimate-vs-outcome data. Resume dispatches skip reserve (like infra).
    bands_mode = config_get("dispatch_context.bands.mode", "warn")
    safety_reserve_tokens_default = 10000
    safety_reserve = 0
    if bands_mode != "off" and not resume:
        safety_reserve = config_int(
            "dispatch_context.bands.safety_reserve_tokens",
            safety_reserve_tokens_default
        )

    # In enforce mode, include safety_reserve in gated total_tokens; in warn/off modes,
    # reserve is always reported (above) but excluded from total to keep warn mode non-blocking.
    total_tokens = infra + prompt_tokens + file_token_total
    if bands_mode == "enforce":
        total_tokens += safety_reserve

    window = model_context_window(model)
    budget_pct = dispatch_budget_pct()
    budget_tokens = round(window * budget_pct)
    pct_of_window = round((total_tokens / window) * 100, 2) if window > 0 else 0.0
    pct_of_budget = round((total_tokens / budget_tokens) * 100, 2) if budget_tokens > 0 else 0.0

    context_tokens = prompt_tokens + file_token_total
    est_usd = _est_context_input_cost_usd(context_tokens, model)
    if not resume and infra > 0:
        est_usd += est_cold_write_cost_usd(model, infra)
    est_usd = round(est_usd, 6)

    bloat = prompt_bloat_ratio(prompt, corrected_estimate_tokens(prompt, prompt_kind, model))
    shell_command = extract_shell_command(prompt)

    # Classify prompt composition by token category
    composition = classify_prompt_composition(prompt, prompt_tokens, model)

    return {
        "label": EST_LABEL,
        "model": model,
        "handoff": resolved_handoff,
        "resume": resume,
        "infra_tokens": infra,
        "prompt_tokens": prompt_tokens,
        "file_tokens": file_token_total,
        "safety_reserve": safety_reserve,
        "total_tokens": total_tokens,
        "context_window": window,
        "budget_pct": budget_pct,
        "budget_tokens": budget_tokens,
        "pct_of_window": pct_of_window,
        "pct_of_budget": pct_of_budget,
        "over_budget": pct_of_window > DISPATCH_OVER_BUDGET_PCT,
        "nudge": DISPATCH_NUDGE_PCT_MIN <= pct_of_window <= DISPATCH_OVER_BUDGET_PCT,
        "est_usd": est_usd,
        "files": resolved_files,
        "paths_rejected": rejected_paths,
        "prompt_bloat_ratio": bloat,
        "prompt_bloat_warning": (
            bloat is not None and bloat > PROMPT_BLOAT_THRESHOLD
        ),
        "shell_command_detected": shell_command is not None,
        "shell_command": shell_command,
        "estimate_failed": bool(rejected_paths),
        "invariant_tokens": composition["invariant_tokens"],
        "incremental_tokens": composition["incremental_tokens"],
        "task_spec_tokens": composition["task_spec_tokens"],
        "invariant_ratio": composition["invariant_ratio"],
    }
