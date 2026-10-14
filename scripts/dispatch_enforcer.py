#!/usr/bin/env python3
"""Dispatch enforcer — gate allow/deny primitives, message generators, scope
helpers, fix-loop detection, concurrency/wave/stall gates, and
event_explore_model_gate.

Extracted from check_routing.py. All callers in check_routing.py use
re-export shims; this module can also be imported directly.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    FAST_PATH_CALL_LIMIT,
    INLINE_MUTATION_DENY_THRESHOLD,
    INLINE_MUTATION_NUDGE_THRESHOLD,
    append_event,
    config_get,
    config_int,
    get_thresholds,
    load_json_state,
    save_json_state,
    session_id_of,
    subagent_id_of,
    _warn,
)
from transcript_scanner import (  # noqa: E402
    ROUTING_VERDICT_RE,
    _is_genuine_user_prompt,
    _read_transcript_tail,
    _record_content,
    _record_role,
    _text_from_content,
    _transcript_path,
)
from session_state import _bump, _save_state  # noqa: E402
from command_detector import _shell_command  # noqa: E402
from subagent_tracker import _is_subagent_context, _GLOBAL_SUBAGENT_KEY  # noqa: E402
import enforce_role_gates as _erg  # noqa: E402
from _model_utils import (  # noqa: E402
    is_claude_payload as _icp,
    tier_of_model,
    tier_model_slug,
)

# ---------------------------------------------------------------------------
# Allow / deny response builders
# ---------------------------------------------------------------------------


def _allow(is_claude: bool, message: str | None) -> dict[str, Any]:
    if is_claude:
        output: dict[str, Any] = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }
        if message:
            output["hookSpecificOutput"]["additionalContext"] = message
        return output
    payload: dict[str, Any] = {"permission": "allow"}
    if message:
        payload["agent_message"] = message
    return payload


def _deny(is_claude: bool, message: str) -> dict[str, Any]:
    if is_claude:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "additionalContext": message,
            },
            "systemMessage": message,
        }
    return {
        "permission": "deny",
        "agent_message": message,
    }


# ---------------------------------------------------------------------------
# Gate message generators — dispatch thresholds
# ---------------------------------------------------------------------------


def _dispatch_deny_message(count: int) -> str:
    return (
        f"[cost-router] {count} mechanical call(s) on a reasoning-tier model since the last Task dispatch — "
        "dispatch remaining work via Task(model: \"haiku\"), or include an explicit override "
        "(`direct override`, `no subagent dispatch`, `single-file direct`, or "
        "`no task dispatch`) with your Routing line."
    )


def _dispatch_nudge_message(count: int) -> str:
    return (
        f"[cost-router] {count} mechanical call(s) on a reasoning-tier model since the last Task dispatch — "
        "dispatch via Task(model: \"haiku\") or state why DIRECT is still correct."
    )


def _inline_mutation_nudge_message(count: int, deny_threshold: int) -> str:
    return (
        f"[cost-router] {count} inline mutations without Task dispatch — "
        f"dispatch remaining work via Task(model: haiku) to reduce cost. "
        f"This will become a deny at {deny_threshold}."
    )


def _inline_mutation_deny_message(count: int) -> str:
    return (
        f"[cost-router] INLINE MUTATION LIMIT — {count} inline mutations on this session "
        "without a Task dispatch. Emit Routing: FO and dispatch remaining work via Task(model: haiku)."
    )


def _shell_gate_nudge_message(count: int) -> str:
    return (
        f"[shell-gate] {count} inline shell calls this turn without Task dispatch — "
        "dispatch remaining shell work to a fast shell worker: "
        "Task(model=\"haiku\", subagent_type=\"general-purpose\", prompt=\"Run: <cmd>\")"
    )


def _shell_gate_deny_message(count: int) -> str:
    return (
        f"[shell-gate] {count} inline shell calls this turn without Task dispatch — "
        "dispatch remaining shell work to a fast shell worker: "
        "Task(model=\"haiku\", subagent_type=\"general-purpose\", prompt=\"Run: <cmd>\")"
    )


def _session_no_dispatch_deny_message(count: int) -> str:
    return (
        f"[cost-router] SESSION GATE: {count} mutating tool call(s) in this session with 0 Task dispatches. "
        "Multi-step work must route through Task dispatch regardless of tier. "
        "Emit 'Routing: FO — <reason>' and dispatch a Task worker before continuing."
    )


def _inline_dispatch_volume_deny_message(count: int, is_claude: bool) -> str:
    if is_claude:
        return (
            f"[cost-router] {count} inline mutations with no Task dispatch this window — "
            "dispatch this step via Task(model: \"haiku\") to reduce cost. Gate resets after your first dispatch."
        )
    return (
        f"[cost-router] {count} inline steps with no subagent dispatch this window — "
        "break this into a background agent call or split into a smaller focused turn. Gate resets after your first agent dispatch."
    )


def _churn_nudge_message(transitions: int) -> str:
    return (
        f"[cost-router] This session already switched tiers {transitions} times — "
        "commit to one tier for the rest of this task unless a fast attempt concretely failed."
    )


def _direct_on_large_write_nudge_message(line_count: int, byte_count: int) -> str:
    thresholds = get_thresholds()
    max_lines = thresholds["write_existing_max_lines"]
    max_kb = thresholds["write_existing_max_kb"]
    return (
        f"[cost-router] Routing: DIRECT claimed for a Write of {line_count} lines "
        f"({byte_count} bytes) — past the model-router rule's own >{max_lines}L/{max_kb}KB "
        "threshold for this size. Re-check: is this genuinely a judgment call "
        "you're making right now, or a fully-specified/mechanical rewrite that "
        "belongs on Task(model: \"haiku\") instead? If DIRECT is still correct "
        "(e.g. real judgment remains), proceed — this is advisory, not a deny."
    )


def _no_transcript_nudge_message() -> str:
    return (
        "[cost-router] No transcript available to verify routing this turn — emit a `Routing:` line "
        "before further tool calls if this is a multi-step task."
    )


def _fast_path_limit_deny_message(count: int) -> str:
    return (
        f"[cost-router] {count} mutating call(s) this turn already exceeds the Decision Table's own "
        f"'simple task' bound (<= {FAST_PATH_CALL_LIMIT} tool calls) that earned the fast-path exemption — "
        "emit a `Routing:` line to continue past a genuinely simple task, or dispatch remaining "
        "work via Task."
    )


def _fast_path_same_file_limit_deny_message(count: int, file_path: str) -> str:
    name = Path(file_path).name if file_path else "(unknown file)"
    return (
        f"[cost-router] {count} fast-path edit(s) already hit `{name}` this turn — "
        "batch all remaining edits to this file into ONE Task dispatch instead of another "
        "direct Edit/Write call. No override available."
    )


def _role_tool_mismatch_deny_message(role: str, tool: str) -> str:
    return (
        f"[cost-router] Routing: {role} says this work should be decomposed/dispatched, but `{tool}` "
        "is a direct, self-performed mutation, not a Task dispatch — either dispatch via Task, or "
        "emit `Routing: DIRECT`/`Routing: FW` if you are genuinely doing this call yourself."
    )


def _out_of_scope_deny_message(target: str, prompt_files: list[str]) -> str:
    named = ", ".join(prompt_files) if prompt_files else "(none)"
    target_name = Path(target).name
    return (
        f"[cost-router] This turn's verdict covers: {named}. "
        f"Mutation target `{target}` is not in scope. To proceed, either: "
        f"(a) mention `{target_name}` in a `Routing:` line before this tool call "
        f"(e.g. `Routing: DIRECT — creating {target_name}`), or "
        "(b) include the filename in your next user message so it lands in prompt scope."
    )


def _resume_forced_fo_deny_message(work_units: int, is_claude: bool) -> str:
    n = work_units or "multiple"
    return (
        f"[cost-router resume-gate] {n} work items were detected at session start. "
        "Dispatch via Task(model: 'haiku') FIRST — direct Edit/Write/Shell blocked until you do. "
        "Emit 'Routing: FO — resume gate' then call Task(...) as your very next tool."
    )


def _concurrency_cap_deny_message(active: int, cap: int) -> str:
    return (
        f"[cost-router concurrency] {active}/{cap} subagents already in flight. "
        "Hold this work unit and dispatch it once a slot frees up, or resume an "
        "already-running idle worker instead of spawning a new one."
    )


def _serialize_verify_deny_message(active: int) -> str:
    return (
        f"[cost-router concurrency] {active} subagent(s) still active — verification must run after "
        "the wave drains, not while sibling units are still in flight (BWS-T2-002). "
        "Wait for the active dispatches to finish, then run the suite once."
    )


def _stall_partial_report_message(max_stall: int | None) -> str:
    return (
        f"[cost-router progress] {max_stall} consecutive tool calls with no "
        "progress marker detected (BWS-T2-003). Stop and return a partial "
        "report now - state what is done and what remains - instead of "
        "continuing silently. Include a continuation-manifest JSON block "
        "(see dispatch-protocol.md). The orchestrator will resume or replan from "
        "this checkpoint."
    )


def _stall_time_partial_report_message(max_seconds: int, elapsed_seconds: float) -> str:
    minutes = max_seconds / 60.0
    return (
        f"[cost-router progress] {int(elapsed_seconds)}s elapsed with no "
        f"progress marker detected (BWS-T2-003 time-based check, "
        f"~{minutes:.1f} min ceiling). This applies to orchestrator workers "
        "on design/refactor steps as well as shell workers - low tool-call "
        "counts don't exempt a long-running step. Stop and return a partial "
        "report now - state what is done and what remains - instead of "
        "continuing silently. Include a continuation-manifest JSON block "
        "(see dispatch-protocol.md). The orchestrator will resume or replan from "
        "this checkpoint."
    )


def _stall_kill_deny_message(grace_count: int, denied_count: int) -> str:
    return (
        f"[cost-router kill] Checkpoint requested {denied_count}+ calls ago (grace period: {grace_count}) "
        f"and not acted upon (DGC-104). This worker is now denied all tool calls — respond with your final report. "
        f"SubagentStop will be invoked by the main session."
    )


# ---------------------------------------------------------------------------
# Scope / mutation helpers
# ---------------------------------------------------------------------------

# PROD-20 (break-018): unknown-and-potentially-mutating tools fail closed.
_MUTATION_KEYWORD_RE = re.compile(
    r"(?:^|[_.\-/])(?:write|edit|delete|remove|apply|deploy|create|update|patch|"
    r"modify|mutate|exec|install|publish|push|commit|merge|migrate|drop|"
    r"truncate|insert|upsert|rm|mv|overwrite)(?:$|[_.\-])",
    re.IGNORECASE,
)

# PROD-20 (break-020): file names explicitly named in the triggering prompt.
_PROMPT_FILE_RE = re.compile(r"[\w./-]+\.[A-Za-z]{1,10}\b")

# P12-11: DIRECT routing must carry a non-empty reason after the em-dash.
_ROUTING_REASON_RE = re.compile(
    r"(?i:routing:)\s*DIRECT\s*[—\-–]\s*\S"
)


def _looks_mutating(tool: str | None) -> bool:
    return bool(tool) and bool(_MUTATION_KEYWORD_RE.search(tool))


def _direct_has_reason(text: str) -> bool:
    """True if `Routing: DIRECT — <reason>` has a non-empty reason clause."""
    return bool(_ROUTING_REASON_RE.search(text))


def _direct_write_file_count(text: str) -> int:
    """Count file-like targets in the routing reason after `Routing: DIRECT`."""
    m = ROUTING_VERDICT_RE.search(text)
    if not m or m.group("role") != "DIRECT":
        return 0
    after = text[m.end():]
    return len(_PROMPT_FILE_RE.findall(after))


def _file_name_pattern(filepath: str) -> str:
    """Extract a grouping pattern from a filename.

    test_auth.py -> test_*.py
    test_utils.py -> test_*.py
    config_dev.json -> config_*.json
    auth.py -> *.py (fallback to extension-only)
    """
    name = Path(filepath).name
    stem = Path(filepath).stem
    ext = Path(filepath).suffix or ".unknown"

    for prefix in ("test_", "spec_", "conftest_", "fixture_", "mock_", "stub_"):
        if stem.startswith(prefix):
            return f"{prefix}*{ext}"

    for suffix in ("_test", "_spec", ".test", ".spec"):
        if stem.endswith(suffix):
            return f"*{suffix}{ext}"

    if "_" in stem:
        prefix = stem.rsplit("_", 1)[0]
        if len(prefix) >= 3:
            return f"{prefix}_*{ext}"

    return f"*{ext}"


def _structural_fingerprint(content: str) -> set[str]:
    """Extract a lightweight structural fingerprint from write content."""
    fingerprint = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("import "):
            fingerprint.add("import_module")
        elif stripped.startswith("from "):
            fingerprint.add("from_import")
        elif stripped.startswith(("def ", "class ", "async def ")):
            parts = stripped.split("(", 1)
            if len(parts) == 2:
                keyword = parts[0].split()[0]
                params = parts[1]
                fingerprint.add(f"{keyword}({params}")
            else:
                keyword = stripped.split()[0]
                fingerprint.add(f"{keyword}:")
        elif stripped.startswith("@"):
            fingerprint.add("decorator")
        elif stripped.startswith(("assert ", "self.assert", "expect(")):
            for prefix in ("assertEqual", "assertTrue", "assertFalse", "assertIn",
                           "assertRaises", "assert ", "expect("):
                if stripped.startswith(prefix) or ("." + prefix) in stripped:
                    fingerprint.add(f"assert_pattern:{prefix}")
                    break
    return fingerprint


def _jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Jaccard similarity between two sets. Returns 0.0-1.0."""
    if not set_a and not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def _direct_bulk_write_deny_message(limit: int) -> str:
    return (
        f"Routing: DIRECT with {limit}+ file targets — bulk mechanical writes belong in a "
        f"Task(fast worker). Re-emit `Routing: FO` and dispatch a Task instead."
    )


def _fw_scope_deny_message(file_count: int, limit: int) -> str:
    """RD-6: deny message for FW scope guard when edits exceed limit."""
    return (
        f"FW scope blocked: {file_count} files edited under a single FW declaration "
        f"(limit: {limit}). FW is for targeted, scoped fixes — not broad edits. "
        f"Split into FO + multiple FW workers scoped to ≤{limit} files each. "
        f"Emit 'Routing: FO — decompose' and dispatch via Task(model: 'haiku')."
    )


def _structural_similarity_nudge_message(similarity: float) -> str:
    """RD-2: nudge message for structural similarity detection."""
    return (
        f"Structural similarity detected ({similarity:.0%} overlap with a recent write). "
        f"This looks like mechanical/boilerplate generation. Consider dispatching to "
        f"FO with Task(haiku) for remaining similar writes."
    )


def _mechanical_intent_deny_message(intent: str, file_count: int) -> str:
    """RD-3: deny message for mechanical work unit intent classification."""
    return (
        f"Work classified as {intent} (spec loaded, pattern detected, {file_count} files written). "
        f"Mechanical work should be dispatched via FO with Task(haiku). "
        f"Re-emit `Routing: FO` and dispatch a Task instead."
    )


def _direct_no_reason_deny_message() -> str:
    return (
        "Routing: DIRECT requires a reason after the em-dash, "
        "e.g. `Routing: DIRECT — renaming one symbol`. Add a brief justification."
    )


def _files_mentioned_in_prompt(prompt: str) -> list[str]:
    return sorted({Path(m).name for m in _PROMPT_FILE_RE.findall(prompt)})


def _expand_scope_names(names: list[str]) -> set[str]:
    """Derive implied filenames from test-file naming conventions."""
    expanded: set[str] = set(names)
    for name in names:
        p = Path(name)
        stem, suffix = p.stem, p.suffix
        if stem.startswith("test_"):
            expanded.add(stem[5:] + suffix)
        if stem.endswith("_test"):
            expanded.add(stem[:-5] + suffix)
        if ".test" in stem:
            expanded.add(stem.replace(".test", "") + suffix)
    return expanded


def _files_in_current_turn_assistant_text(payload: dict[str, Any]) -> set[str]:
    """Collect all filenames from the current-turn assistant messages."""
    raw_path = _transcript_path(payload)
    if not raw_path:
        files: set[str] = set()
        for key in ("agent_message", "text", "last_assistant_message"):
            value = payload.get(key)
            if isinstance(value, str):
                files.update(Path(m).name for m in _PROMPT_FILE_RE.findall(value))
        return files
    try:
        path = Path(raw_path)
        if not path.is_file():
            return set()
        lines = _read_transcript_tail(path)
    except Exception:  # noqa: BLE001
        return set()
    files = set()
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if _is_genuine_user_prompt(record):
            break
        if _record_role(record) == "assistant":
            text = _text_from_content(_record_content(record))
            if text:
                files.update(Path(m).name for m in _PROMPT_FILE_RE.findall(text))
    return files


def _target_is_in_scope(
    target: str, prompt_files: list[str], payload: dict[str, Any]
) -> bool:
    """True when *target* is within the legitimate scope of the current turn."""
    target_name = Path(target).name
    target_stem = Path(target).stem

    if target_name in prompt_files:
        return True
    if target_name in _expand_scope_names(prompt_files):
        return True
    for mentioned in prompt_files:
        mentioned_stem = Path(mentioned).stem
        if target_stem and (target_stem in mentioned_stem or mentioned_stem in target_stem):
            return True
    assistant_files = _files_in_current_turn_assistant_text(payload)
    if target_name in assistant_files:
        return True
    for mentioned in assistant_files:
        mentioned_stem = Path(mentioned).stem
        if target_stem and (target_stem in mentioned_stem or mentioned_stem in target_stem):
            return True
    return False


def _mutation_target_file(payload: dict[str, Any]) -> str | None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = payload.get("toolInput")
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "path", "target"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# Fix-loop detection (RD-5)
# ---------------------------------------------------------------------------


def _fix_loop_nudge_message(count: int) -> str:
    return (
        f"Fix-loop detected: {count} consecutive edit-test-fail cycles on the same files. "
        f"Consider dispatching to FO with Task(haiku) or stepping back to reconsider the approach."
    )


def _fix_loop_deny_message(count: int) -> str:
    return (
        f"Fix-loop blocked: {count} consecutive edit-test-fail cycles on the same files. "
        f"Dispatch remaining work via Task(model: 'haiku') with a different strategy, "
        f"or emit 'Routing: DIRECT — new approach: <concrete reason>' before resuming edits."
    )


def _check_fix_loop(
    state: dict[str, Any],
    entry: dict[str, Any],
    payload: dict[str, Any],
    tool: str,
    is_claude: bool,
) -> dict[str, Any] | None:
    """RD-5: detect edit-test-edit loops on the same file set."""
    if _is_subagent_context(payload):
        return None

    _SHELL_TOOLS = frozenset({"Shell", "Bash"})

    if tool in _SHELL_TOOLS:
        cmd = _shell_command(payload)
        test_keywords = (
            "pytest", "python -m pytest", "npm test", "cargo test",
            "go test", "make test", "yarn test",
        )
        if any(kw in cmd for kw in test_keywords):
            entry["fix_loop_test_ran"] = True
        return None

    if tool not in ("Edit", "Write", "StrReplace"):
        return None

    target = _mutation_target_file(payload)
    if not target:
        return None

    file_set = set(entry.get("fix_loop_file_set", []))
    test_ran = entry.get("fix_loop_test_ran", False)

    if test_ran and target in file_set:
        entry["fix_loop_fail_count"] = entry.get("fix_loop_fail_count", 0) + 1
        entry["fix_loop_test_ran"] = False
        fail_count = entry["fix_loop_fail_count"]
        if fail_count >= 5:
            _bump(state, "gate_deny_fix_loop")
            _save_state(state)
            return _deny(is_claude, _fix_loop_deny_message(fail_count))
        elif fail_count >= 3:
            _bump(state, "gate_nudge_fix_loop")
            _save_state(state)
            return _allow(is_claude, _fix_loop_nudge_message(fail_count))
    elif test_ran:
        entry["fix_loop_test_ran"] = False

    if target not in file_set:
        file_set.add(target)
        entry["fix_loop_file_set"] = sorted(file_set)

    return None


# ---------------------------------------------------------------------------
# Candidate table helpers
# ---------------------------------------------------------------------------

_CANDIDATE_TABLE_PATH = Path(__file__).resolve().parent.parent / "data" / "candidate_table.json"


def _load_candidate_table() -> dict:
    import sys as _sys  # noqa: PLC0415
    try:
        from _common import DATA_DIR as _DATA_DIR  # noqa: PLC0415
        data = json.loads(_CANDIDATE_TABLE_PATH.read_text())
        try:
            pd = json.loads((_DATA_DIR / "pinned_rules.json").read_text())
            data["rules"] = data.get("rules", []) + pd.get("rules", [])
        except Exception:
            pass
        rules = data.get("rules", [])
        if not rules:
            print("warn: candidate_table.json has no rules — routing candidates disabled", file=_sys.stderr)
        return data
    except Exception:
        print("warn: candidate_table.json missing or unreadable", file=_sys.stderr)
        return {}


def _candidate_role(intent: str | None, candidate_table: dict) -> str | None:
    for rule in candidate_table.get("rules", []):
        if rule.get("intent") == intent:
            return rule.get("suggested_role")
    return None


# ---------------------------------------------------------------------------
# Concurrency / wave / stall gates
# ---------------------------------------------------------------------------

_PROGRESS_MARKER_RE = re.compile(r"(?i)\b(done|completed|remaining|progress)\s*:")


def _optional_positive_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _dispatch_worker_type_for(payload: dict[str, Any]) -> str | None:
    """TICKET-039: resolve the dispatch-schema `worker` field (mutation,
    read, shell, orchestrator, ...) for the subagent making *this* tool
    call, so the stall gate can apply a per-worker-type threshold instead of
    one blanket cadence.

    Looked up from dispatch_registry.json, keyed by session_id -> agents ->
    agent_id -> "dispatch_worker" (recorded at SubagentStart via
    mark_registry_busy, see check_routing.py ~L976). Best-effort: any
    missing session/agent/registry state returns None, and callers must
    treat None as "use the top-level default" rather than erroring.
    """
    agent_id = subagent_id_of(payload)
    if not agent_id:
        return None
    session_id = session_id_of(payload)
    if not session_id:
        return None
    try:
        from context_estimator import load_dispatch_registry  # noqa: PLC0415
        registry = load_dispatch_registry()
    except Exception:
        return None
    sessions = registry.get("sessions") if isinstance(registry, dict) else None
    session_entry = sessions.get(session_id) if isinstance(sessions, dict) else None
    agents = session_entry.get("agents") if isinstance(session_entry, dict) else None
    agent_entry = agents.get(agent_id) if isinstance(agents, dict) else None
    worker = agent_entry.get("dispatch_worker") if isinstance(agent_entry, dict) else None
    if isinstance(worker, str) and worker.strip():
        return worker.strip().lower()
    return None


def _stall_thresholds_for(worker_type: str | None) -> tuple[int | None, int | None]:
    """TICKET-039: resolve (max_stall_markers, max_stall_seconds) for a
    given dispatch worker type, applying `progress_ledger.worker_overrides.
    <worker_type>` on top of the top-level defaults when present. Unknown/
    unresolved worker types (worker_type is None, or no matching override)
    fall back to the plain top-level keys, so shell/orchestrator dispatches
    and any worker whose type couldn't be resolved keep the pre-TICKET-039
    behavior exactly.
    """
    max_stall = _optional_positive_int(config_get("progress_ledger.max_stall_markers", None))
    max_seconds = _optional_positive_int(config_get("progress_ledger.max_stall_seconds", None))
    if worker_type:
        override_stall = config_get(
            f"progress_ledger.worker_overrides.{worker_type}.max_stall_markers", None
        )
        override_seconds = config_get(
            f"progress_ledger.worker_overrides.{worker_type}.max_stall_seconds", None
        )
        if override_stall is not None:
            max_stall = _optional_positive_int(override_stall)
        if override_seconds is not None:
            max_seconds = _optional_positive_int(override_seconds)
    return max_stall, max_seconds


def _progress_ledger_mode() -> str:
    """Return the progress ledger mode: off, warn, or enforce.
    Absent or invalid keys default to warn."""
    mode = config_get("progress_ledger.mode", "warn")
    if mode in ("off", "warn", "enforce"):
        return mode
    return "warn"


def _stall_kill_mode() -> str:
    """DGC-104: Return the stall kill mode: off, warn, or enforce.
    Absent or invalid keys default to warn."""
    mode = config_get("progress_ledger.kill.mode", "warn")
    if mode in ("off", "warn", "enforce"):
        return mode
    return "warn"


def _stall_grace_denied_calls() -> int:
    """DGC-104: Return the grace period (in denied calls) before hard-deny kill.
    Default is 3 calls."""
    grace = _optional_positive_int(config_get("progress_ledger.kill.grace_denied_calls", 3))
    return grace if grace is not None else 3


def _strict_stall_profile_for(worker_type: str | None) -> dict[str, int] | None:
    """DGC-103: resolve strict stall profile for a worker type.
    Returns None if worker is unresolved, mode is off, or any required key is missing.

    A miss is counted each time elapsed exceeds heartbeat_seconds without a progress marker.
    When miss_count reaches max_stall_markers or elapsed reaches max_stall_seconds,
    the gate denies (enforce mode) or warns (warn mode).
    """
    if not worker_type or _progress_ledger_mode() == "off":
        return None

    profile_key = f"progress_ledger.strict_profile.{worker_type}"
    max_markers = _optional_positive_int(config_get(f"{profile_key}.max_stall_markers", None))
    max_seconds = _optional_positive_int(config_get(f"{profile_key}.max_stall_seconds", None))
    heartbeat = _optional_positive_int(config_get(f"{profile_key}.heartbeat_seconds", None))

    if max_markers is None or max_seconds is None or heartbeat is None:
        return None

    return {
        "max_stall_markers": max_markers,
        "max_stall_seconds": max_seconds,
        "heartbeat_seconds": heartbeat,
    }


def _subagent_progress_gate(
    payload: dict[str, Any], state: dict[str, Any], is_claude: bool
) -> dict[str, Any] | None:
    """BWS-T2-003: track consecutive non-progressing tool calls per dispatched
    worker (call-count based), plus a wall-clock check (progress_ledger.
    max_stall_seconds) so a low-tool-call-count design/refactor orchestrator
    step (TICKET-037) can't silently run past the 2-3 minute marker cadence
    just because it hasn't made many tool calls yet. Both checks are
    worker-type agnostic by construction (shell, mutation, read,
    orchestrator all funnel through this same gate) - the elapsed-time
    check is what actually closes the gap for orchestrator design/refactor
    steps, since those steps make far fewer tool calls per minute than
    shell/mutation workers.

    TICKET-039: mutation/read workers legitimately run longer
    diagnose-then-edit or search-then-report cycles without narrating every
    step, so a blanket 2-3 minute cadence under-fits them. Thresholds are
    now resolved per dispatch worker type via
    `progress_ledger.worker_overrides.<worker>` (see
    `_stall_thresholds_for`/`_dispatch_worker_type_for`) - e.g. mutation/read
    get a longer, "10-minute-class" wall-clock ceiling in config.json,
    while shell/orchestrator keep the tighter default. Worker type is
    best-effort (registry lookup); when it can't be resolved, this is a
    no-op fallback to the original top-level config keys.

    DGC-103: strict stall profiles allow per-worker-type heartbeat-based
    thresholds on top of the call-count and wall-clock checks. When a strict
    profile exists (progress_ledger.strict_profile.<worker>.{heartbeat,
    max_stall_markers, max_stall_seconds}), each tool call where elapsed
    exceeds heartbeat_seconds increments miss_count; when miss_count reaches
    max_stall_markers or elapsed reaches max_stall_seconds, the gate denies
    (enforce mode) or warns (warn mode). Mode is controlled by
    progress_ledger.mode (off/warn/enforce, default warn).

    DGC-104: stall escalation to hard-deny kill. After a checkpoint request
    (partial report message), count further tool calls. After grace_denied_calls
    denials, deny every tool call (stall_killed). Checkpoint received (marker
    in next assistant text) clears the kill path. Mode is controlled by
    progress_ledger.kill.mode (off/warn/enforce, default warn).
    """
    worker_type = _dispatch_worker_type_for(payload)
    max_stall, max_seconds = _stall_thresholds_for(worker_type)
    if max_stall is None and max_seconds is None:
        return None
    agent_id = subagent_id_of(payload)
    if not agent_id:
        return None
    text = _subagent_recent_assistant_text(payload)
    if not text:
        return None
    now = time.time()
    ledger = state.setdefault("subagent_progress", {})
    entry = ledger.setdefault(agent_id, {"stall_count": 0, "last_marker_ts": now, "miss_count": 0})
    entry.setdefault("last_marker_ts", now)
    entry.setdefault("miss_count", 0)

    # DGC-104: Check if worker is in kill state and has received a checkpoint request
    checkpoint_requested_ts = entry.get("checkpoint_requested_ts")
    if checkpoint_requested_ts is not None:
        # Checkpoint was requested; check if checkpoint has been received (marker in text)
        if _PROGRESS_MARKER_RE.search(text):
            # Checkpoint received — clear the kill path
            entry["checkpoint_requested_ts"] = None
            entry["grace_denied_calls"] = 0
            entry["stall_count"] = 0
            entry["last_marker_ts"] = now
            entry["miss_count"] = 0
            entry["stall_killed"] = False
            return None
        # Still in kill grace period — count denied calls
        grace_denied = int(entry.get("grace_denied_calls", 0)) + 1
        entry["grace_denied_calls"] = grace_denied
        grace_limit = _stall_grace_denied_calls()
        kill_mode = _stall_kill_mode()

        if grace_denied >= grace_limit:
            # Grace period exceeded — enforce hard deny
            if kill_mode == "enforce":
                entry["stall_killed"] = True
                _bump(state, "gate_deny_stall_kill")
                return _deny(is_claude, _stall_kill_deny_message(grace_limit, grace_denied))
            elif kill_mode == "warn":
                _bump(state, "gate_warn_stall_kill")
                _warn(
                    "stall_kill_would_deny",
                    {
                        "agent_id": agent_id,
                        "grace_denied_calls": grace_denied,
                        "grace_limit": grace_limit,
                    },
                )
                return None
    # End DGC-104 kill check

    if _PROGRESS_MARKER_RE.search(text):
        entry["stall_count"] = 0
        entry["last_marker_ts"] = now
        entry["miss_count"] = 0
        return None
    entry["stall_count"] = int(entry.get("stall_count", 0)) + 1
    elapsed = now - float(entry.get("last_marker_ts", now))

    # Strict stall profile check (DGC-103)
    strict_profile = _strict_stall_profile_for(worker_type)
    if strict_profile and elapsed > strict_profile["heartbeat_seconds"]:
        entry["miss_count"] = int(entry.get("miss_count", 0)) + 1

    count_stalled = max_stall is not None and entry["stall_count"] >= max_stall
    time_stalled = max_seconds is not None and elapsed >= max_seconds
    strict_stalled = strict_profile and (
        entry["miss_count"] >= strict_profile["max_stall_markers"]
        or elapsed >= strict_profile["max_stall_seconds"]
    )

    # Existing top-level deny (unchanged) and DGC-104 checkpoint request
    if count_stalled or time_stalled:
        entry["stall_count"] = 0
        entry["last_marker_ts"] = now
        entry["miss_count"] = 0
        # DGC-104: Set checkpoint_requested_ts to initiate grace period (only if not already set)
        if entry.get("checkpoint_requested_ts") is None:
            entry["checkpoint_requested_ts"] = now
            entry["grace_denied_calls"] = 0
        if time_stalled and not count_stalled:
            return _deny(is_claude, _stall_time_partial_report_message(max_seconds, elapsed))
        return _deny(is_claude, _stall_partial_report_message(max_stall))

    # Strict profile handling (DGC-103)
    if strict_stalled:
        # Capture miss_count before reset for logging
        miss_count_value = int(entry.get("miss_count", 0))

        entry["stall_count"] = 0
        entry["last_marker_ts"] = now
        entry["miss_count"] = 0
        mode = _progress_ledger_mode()

        # Determine what caused the trip
        strict_count = miss_count_value >= strict_profile["max_stall_markers"]
        strict_time = elapsed >= strict_profile["max_stall_seconds"]

        if mode == "enforce":
            # DGC-104: Set checkpoint_requested_ts to initiate grace period (only if not already set)
            if entry.get("checkpoint_requested_ts") is None:
                entry["checkpoint_requested_ts"] = now
                entry["grace_denied_calls"] = 0
            # Choose message based on trip cause: miss-count message if strict_count is true,
            # time message only when strict_time alone tripped
            if strict_count and not strict_time:
                return _deny(is_claude, _stall_partial_report_message(strict_profile["max_stall_markers"]))
            else:
                return _deny(is_claude, _stall_time_partial_report_message(strict_profile["max_stall_seconds"], elapsed))
        elif mode == "warn":
            _bump(state, "gate_warn_strict_stall")
            _warn(
                "strict_stall_would_deny",
                {
                    "agent_id": agent_id,
                    "miss_count": miss_count_value,
                    "elapsed": elapsed,
                    "max_markers": strict_profile["max_stall_markers"],
                    "max_seconds": strict_profile["max_stall_seconds"],
                },
            )
            return None
    return None


def _subagent_recent_assistant_text(payload: dict[str, Any]) -> str:
    """Read the most recent assistant text from the subagent's transcript."""
    raw_path = _transcript_path(payload)
    agent_id = subagent_id_of(payload)
    if not raw_path or not agent_id:
        return ""
    per_agent = _erg._subagent_transcript_path(raw_path, agent_id)
    path = per_agent if per_agent is not None else Path(raw_path)
    if not path.is_file():
        return ""
    try:
        lines = _read_transcript_tail(path)
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if _is_genuine_user_prompt(record):
                return ""
            if _record_role(record) == "assistant":
                return _text_from_content(_record_content(record))
    except Exception:
        pass
    return ""


def _expire_dead_subagents(state: dict[str, Any]) -> list[str]:
    """RC-6: Expire subagents whose deadline has passed."""
    wave_timeout = config_int("concurrency.wave_timeout_seconds", 180)
    now = time.time()
    expired: list[str] = []
    deadlines = state.get("subagent_deadlines", {})
    for sid, deadline_epoch in list(deadlines.items()):
        if now >= deadline_epoch:
            expired.append(sid)
            del deadlines[sid]
            _warn("wave_timeout_expired_subagent", {
                "subagent_id": sid,
                "wave_timeout_seconds": wave_timeout,
            })
    return expired


def _wave_barrier_check(
    payload: dict[str, Any], state: dict[str, Any], is_claude: bool
) -> dict[str, Any] | None:
    """RC-3: Enforce concurrency cap on Task/Agent dispatch from subagents."""
    expired = _expire_dead_subagents(state)
    if expired:
        partial_policy = config_get("concurrency.partial_wave_policy", "allow")
        _warn("wave_barrier_partial_after_expiry", {
            "expired_count": len(expired),
            "policy": partial_policy,
            "expired_ids": expired[:3],
        })
        if partial_policy != "allow":
            active = len([k for k in state.get("subagents", {}) if k != _GLOBAL_SUBAGENT_KEY])
            cap = config_int("concurrency.max_inflight_subagents", 3)
            if active >= cap:
                return _deny(is_claude, _concurrency_cap_deny_message(active, cap))
        return None

    active = len([k for k in state.get("subagents", {}) if k != _GLOBAL_SUBAGENT_KEY])
    cap = config_int("concurrency.max_inflight_subagents", 3)
    if active >= cap:
        return _deny(is_claude, _concurrency_cap_deny_message(active, cap))
    return None


# ---------------------------------------------------------------------------
# Explore model gate (P12-9)
# ---------------------------------------------------------------------------


def event_explore_model_gate(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """P12-9: Block Task dispatches for Explore subagents that don't declare model in fast tier."""
    tool_input = payload.get("tool_input") or {}
    subagent_type = str(tool_input.get("subagent_type") or "")
    if "explore" not in subagent_type.lower():
        return _allow(is_claude, None)
    model = str(tool_input.get("model") or "")
    if tier_of_model(model) == "fast":
        return _allow(is_claude, None)
    try:
        append_event({
            "type": "gate_violation",
            "subtype": "model_tier_violation",
            "gate_id": "explore_model_tier_gate",
            "session_id": session_id_of(payload),
            "turn_id": payload.get("prompt_id") or payload.get("generation_id"),
            "declared_model": model or "(none)",
            "subagent_type": subagent_type,
        })
    except OSError as e:
        _warn("routing_state_io_failed", {"file": "dispatch_enforcer.py", "error": str(e)})
    platform = "claude" if _icp(payload) else "cursor"
    fast_slug = tier_model_slug("fast", platform)
    return _deny(
        is_claude,
        f"Explore subagents must declare a fast-tier model (e.g. model: \"{fast_slug}\") — "
        f"got '{model or '(none)'}'. Set it explicitly in the Task call "
        f"to avoid Sonnet cold-start cost (~$0.56 per spawn).",
    )
