#!/usr/bin/env python3
"""cost-router hook: enforce model-router invoke checkpoint before mutating tools.

Events: reset, scan, gate, subagent_start, subagent_stop (see --event).
Gate derives the current turn verdict from inline text or transcript at
decision time; persisted verdict is a turn-tagged cache only.
State: data/routing_state.json. Stdlib-only; fail-open on error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Optional, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    ASSISTANT_SNIPPET_CHARS,
    DATA_DIR,
    DISPATCH_DENY_THRESHOLD,
    DISPATCH_GATE_THRESHOLD,
    DISPATCH_NUDGE_THRESHOLD,
    FAST_PATH_CALL_LIMIT,
    FAST_PATH_MAX_PROMPT_CHARS,
    FLIP_FLOP_THRESHOLD,
    INLINE_MUTATION_DENY_THRESHOLD,
    INLINE_MUTATION_NUDGE_THRESHOLD,
    SHELL_DENY_THRESHOLD,
    SHELL_NUDGE_THRESHOLD,
    LARGE_WRITE_BYTE_THRESHOLD,
    LARGE_WRITE_LINE_THRESHOLD,
    MAX_TRACKED_SESSIONS,
    SESSION_DISPATCH_DENY_THRESHOLD,
    ACTIVE_SUBAGENT_STALE_TTL_SECONDS,
    SUBAGENT_TTL_SECONDS,
    TASK_TARGET_HASH_CHARS,
    TIER_HISTORY_CAP,
    TRANSCRIPT_TAIL_BYTES,
    append_event,
    config_get,
    config_int,
    hash_target,
    load_json_state,
    now_iso,
    read_events,
    read_stdin_json,
    save_json_state,
    session_id_of,
    subagent_id_of,
    _warn,
)
from _model_utils import est_cold_write_cost_usd, infra_tokens, model_of, tier_of_model
from _payload import NormalizedPayload, normalize_payload  # noqa: E402
from estimate_dispatch_context import (  # noqa: E402
    budget_nudge_message,
    check_grouping_enforcement,
    classify_handoff,
    estimate_dispatch_context,
    extract_paths_from_prompt,
    load_dispatch_registry,
    mark_registry_busy,
    mark_registry_idle,
)
from classify_intent import (  # noqa: E402
    _AGENTIC_VERBS,
    _ARCHITECTURE_SIGNALS,
    _TARGETED_READ_SIGNALS,
    classify_intent,
    classify_work_unit_intent,
)
import enforce_role_gates  # noqa: E402
import continuation_manifest  # noqa: E402
from session_state import (  # noqa: E402
    ROUTING_STATE_FILE,
    GATE_COUNTERS_FILE,
    SUBAGENT_STATE_FILE,
    RESUME_STATE_FILE,
)


RESUME_GATE_MAX_DENIALS = 3  # auto-lapse after this many consecutive denials

# TICKET-079: TTL for verdict_created_at entries (1 hour in seconds)
VERDICT_TTL = 3600

# Require a real role token after `Routing:` (.claude/rules/model-router.md).
# PROD-20/18: fixed role list only; named group checks role fits mutation.
ROUTING_VERDICT_RE = re.compile(
    r"(?i:routing:)\s*(?P<role>DIRECT|FO|FW|RB|FV|YIELD)\b", re.IGNORECASE
)

MUTATING_TOOLS = frozenset({
    "Write",
    "StrReplace",
    "Edit",
    "Shell",
    "Bash",  # ENF-3: Claude Code reports tool name "Bash", not "Shell"
    "Task",
    "Delete",
    "NotebookEdit",  # TICKET-017: explicit entry, camelCase regex now also matches
})

# TICKET-041: Exact enumeration for Cursor platform detection via hook_event_name.
# Never use substring matching on hook_event_name — future events like "precursor_check"
# or "cursor_action" would mis-classify. Hook event names are provided by the client
# and must be explicitly enumerated here when new ones are added.
CURSOR_EVENT_NAMES = frozenset({
    "cursor_precursor_execute",
    "cursor_action",
    "cursor_write",
})

DISPATCH_MECHANICAL_TOOLS = MUTATING_TOOLS - {"Task"}

# PROD-19: matches the decision table's "run test suite / lint / verification
# command after an edit -> Task(fast model); never: run inline" row.
_VERIFICATION_COMMAND_RE = re.compile(
    r"\b(pytest|py\.test|python3?\s+-m\s+(?:pytest|unittest)|unittest\b|"
    r"npm\s+(?:run\s+)?test|yarn\s+test|pnpm\s+test|go\s+test|cargo\s+test|"
    r"shellcheck|eslint|ruff\b|flake8|mypy\b|tsc\b)",
    re.IGNORECASE,
)




def _is_verdict_expired(entry: dict) -> bool:
    """TICKET-079: Check if verdict_created_at timestamp is older than TTL.

    Returns True if entry has a verdict timestamp and it has expired (> VERDICT_TTL seconds old).
    """
    if not entry.get("verdict"):
        return False
    ts = entry.get("verdict_created_at")
    if ts is None:
        return False
    return (time.time() - ts) > VERDICT_TTL







def _safe_dispatch_debt_active(session_id: str) -> bool:
    """Wrapper around _dispatch_debt_active that prevents exception propagation.

    Returns False on any exception and emits a config_error warning so the
    call sites never crash even when config or state I/O is broken.
    """
    try:
        return _dispatch_debt_active(session_id)
    except Exception as exc:  # noqa: BLE001
        try:
            append_event(
                {
                    "type": "config_error",
                    "session_id": session_id,
                    "error_type": type(exc).__name__,
                    "config_key": "_safe_dispatch_debt_active",
                }
            )
        except Exception:  # noqa: BLE001
            pass
        return False


def _safe_session_dispatch_cap_active(session_id: str) -> bool:
    """Wrapper around _session_dispatch_cap_active that fails open on any
    exception, matching the pattern of _safe_dispatch_debt_active above."""
    try:
        return _session_dispatch_cap_active(session_id)
    except Exception as exc:  # noqa: BLE001
        try:
            append_event(
                {
                    "type": "config_error",
                    "session_id": session_id,
                    "error_type": type(exc).__name__,
                    "config_key": "_safe_session_dispatch_cap_active",
                }
            )
        except Exception:  # noqa: BLE001
            pass
        return False


def _tier_of_model_with_event(model: str | None, session_id: str | None = None) -> str:
    """Call tier_of_model and emit a telemetry event when the tier is unknown."""
    tier = tier_of_model(model)
    if tier == "unknown":
        try:
            if not model:
                append_event({"type": "extraction_fallback", "reason": "model_none"})
            else:
                append_event({"type": "tier_unrecognized", "model": model})
        except Exception:  # noqa: BLE001
            pass
    return tier


def _model_of_with_event_fallback(payload: dict, session_id: str | None = None) -> str | None:
    """Return model from payload; if absent and session_id given, scan event log for last
    routing_declared event for this session that has a real model value."""
    m = model_of(payload)
    if m:
        return m
    if not session_id:
        return None
    try:
        from scripts._events import read_events  # noqa: PLC0415
        for evt in reversed(read_events()):
            if (
                evt.get("type") == "routing_declared"
                and evt.get("session_id") == session_id
                and evt.get("model")
                and evt.get("model") != "unknown"
            ):
                return evt["model"]
    except Exception:  # noqa: BLE001
        pass
    return None








# PROD-21: bulk verification Shell/Bash gate — structured handoff via bulk-verify: tokens.
_SHELL_TOOLS = frozenset({"Bash", "Shell"})
DISPATCH_BUDGET_OVERRIDE_RE = re.compile(r"dispatch-budget-override", re.IGNORECASE)
SESSION_BUDGET_OVERRIDE_RE = re.compile(r"session-budget-override", re.IGNORECASE)
DISPATCH_GROUP_OVERRIDE_RE = re.compile(r"dispatch-group-override", re.IGNORECASE)
_BULK_USER_LOCAL_PROMPT_SIGNALS = (
    "run it myself",
    "i'll run",
    "run locally",
    "my terminal",
    "iterm",
    "paste the output",
    "run it local",
)
_BULK_AGENT_PROMPT_SIGNALS = (
    "run the full",
    "run pytest",
    "you run",
    "go ahead and run",
    "run the suite",
    "run tests now",
    "run the tests",
)




def _extract_command_type(command: str) -> str:
    """Return a normalized key for a shell command, lowercased.

    Strips leading punctuation (e.g. ``!``, ``$``) before splitting. Skips
    flag tokens (starting with ``-``) and uses the first non-flag token as the
    base command, then appends the first path/subpath argument (if any) so that
    e.g. ``pytest tests/unit`` and ``pytest tests/e2e`` produce different keys.
    The result is capped at 60 characters. Returns ``'unknown'`` for empty or
    whitespace-only input so callers can fail-closed.
    """
    if not command or not command.strip():
        return "unknown"
    tokens = command.strip().split()
    # Strip shell sigils from the first token only
    tokens[0] = tokens[0].lstrip("!$#@%^&*(){}[]|<>")
    non_flag = [t for t in tokens if t and not t.startswith("-")]
    if not non_flag:
        return "unknown"
    base = non_flag[0].lower()
    # Append first path argument if present
    if len(non_flag) > 1:
        key = f"{base}:{non_flag[1]}"
    else:
        key = base
    return key[:60]


def _shell_same_type_deny_message(cmd_type: str, count: int) -> str:
    return (
        f"Shell gate: [{cmd_type}] called {count} times — "
        "batch into one command instead of repeating the same tool type."
    )
















# PROD-18: matches .claude/rules/model-router.md's own stated Write self-check
# threshold ("state 1-line reason before Write on existing >5KB/150L").






# Host detect: cursor_version marks Cursor; hook_event_name alone is ambiguous.
















# ---------------------------------------------------------------------------
# Transcript-derived verdict detection (removes the reset→scan→gate race)
# ---------------------------------------------------------------------------



































# PROD-6: Cursor worker tool payloads lack parent linkage; use a global active counter.






# PROD-19: classify dispatched Task prompt as mechanical/judgment for tier nudges.
_DISPATCH_JUDGMENT_SIGNALS = frozenset(_ARCHITECTURE_SIGNALS) | frozenset({
    "design", "decide", "trade-off", "tradeoff", "brainstorm", "evaluate",
    "strategy", "ambiguous", "which approach", "recommend", "pros and cons",
    "judgment", "explore options",
})

_DISPATCH_MECHANICAL_SIGNALS = frozenset(_AGENTIC_VERBS) | frozenset(_TARGETED_READ_SIGNALS)


_BULK_EDIT_FILE_THRESHOLD = 5

_BULK_EDIT_PHRASE_SIGNALS = frozenset({
    "all files", "every file", "all test files", "across all",
})

_FILE_PATH_PATTERN = re.compile(
    r"(?:[a-zA-Z0-9_\-]+/)+[a-zA-Z0-9_\-]+\.[a-zA-Z]{1,6}"  # paths with slashes
    r"|"
    r"\b[a-zA-Z0-9_\-]+\.[a-zA-Z]{2,6}\b"  # bare filenames like file.py
)










_DISPATCH_WORKER_NOT_APPLICABLE = "not_json_schema"


def _parse_dispatch_worker_intent(task_text: str) -> tuple[str | None, str | None]:
    """Best-effort parse of the `worker`/`intent` fields from a dispatched
    Task's own JSON prompt (see the mutation/read/create/shell/orchestrator
    dispatch schema). Never raises. When the prompt isn't a recognized
    worker-schema JSON payload (thin/plain-text dispatch, or JSON missing/
    with an invalid `worker` value), `worker` is the explicit sentinel
    `_DISPATCH_WORKER_NOT_APPLICABLE` rather than `None` — this disambiguates
    "known not to be a schema dispatch" from "field absent" (pre-fix records
    that never had this key at all) when consumers read the value downstream.
    """
    if not task_text:
        return (_DISPATCH_WORKER_NOT_APPLICABLE, None)
    try:
        data = json.loads(task_text)
    except (ValueError, TypeError):
        return (_DISPATCH_WORKER_NOT_APPLICABLE, None)
    if not isinstance(data, dict):
        return (_DISPATCH_WORKER_NOT_APPLICABLE, None)
    worker = data.get("worker")
    intent = data.get("intent")
    worker = worker.strip() if isinstance(worker, str) and worker.strip() else _DISPATCH_WORKER_NOT_APPLICABLE
    intent = intent.strip() if isinstance(intent, str) and intent.strip() else None
    return (worker, intent)


# Correlation queue between PreToolUse(Task/Agent) (where the real dispatch
# JSON lives) and SubagentStart (whose payload never carries it — see
# event_subagent_start). Bounded by both entry count and per-entry age so a
# dispatch that's denied or never actually starts a subagent can't leave a
# stale entry around to be mismatched against a later, unrelated one.
_DISPATCH_WORKER_QUEUE_MAX_SIZE = 12
_DISPATCH_WORKER_QUEUE_TTL_SECONDS = 120


def _push_pending_dispatch_worker_intent(
    entry: dict[str, Any],
    *,
    worker: str | None,
    intent: str | None,
    resume_agent_id: str | None,
    now: float,
    task_text: str | None = None,
    requested_model: str | None = None,
) -> None:
    """Record a parsed worker/intent at PreToolUse(Task/Agent) time for a
    later SubagentStart to consume. Drops expired entries first so the queue
    can't accumulate stale garbage from denied/abandoned dispatches.

    Also carries task_text/requested_model — PreToolUse(Task/Agent) is the
    only hook with the real dispatch JSON (see event_subagent_start), so
    PROD-19's tier-vs-work check at SubagentStart time must recover these
    from here rather than re-deriving from the SubagentStart payload."""
    queue = entry.setdefault("pending_dispatch_worker_intent", [])
    queue[:] = [q for q in queue if now - float(q.get("ts", 0)) <= _DISPATCH_WORKER_QUEUE_TTL_SECONDS]
    queue.append({
        "worker": worker,
        "intent": intent,
        "resume_agent_id": resume_agent_id,
        "ts": now,
        "task_text": task_text,
        "requested_model": requested_model,
    })
    if len(queue) > _DISPATCH_WORKER_QUEUE_MAX_SIZE:
        del queue[: len(queue) - _DISPATCH_WORKER_QUEUE_MAX_SIZE]


def _pop_pending_dispatch_worker_intent(
    entry: dict[str, Any],
    *,
    subagent_id: str | None,
    now: float,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Consume the queue entry that best corresponds to this SubagentStart.

    Priority: (1) exact resume_agent_id match — a resumed dispatch's agent_id
    is known up front, so this is unambiguous; (2) the closest-timestamp
    non-expired entry among the rest, not a blind FIFO pop, since BWS allows
    up to `concurrency.max_inflight_subagents` concurrent dispatches and
    SubagentStart events are not guaranteed to arrive in dispatch order.
    Falls back to the existing sentinel when nothing qualifies, rather than
    guessing — a wrong match would be silently-wrong telemetry, worse than
    an honest "unknown".

    Returns (worker, intent, task_text, requested_model) — the latter two
    are the carried PreToolUse-time values (may be None if not queued).
    """
    queue = entry.get("pending_dispatch_worker_intent")
    if not isinstance(queue, list) or not queue:
        return (_DISPATCH_WORKER_NOT_APPLICABLE, None, None, None)

    live = [q for q in queue if now - float(q.get("ts", 0)) <= _DISPATCH_WORKER_QUEUE_TTL_SECONDS]

    match = None
    if subagent_id:
        for q in live:
            if q.get("resume_agent_id") == subagent_id:
                match = q
                break

    if match is None and live:
        match = min(live, key=lambda q: abs(now - float(q.get("ts", 0))))

    if match is not None:
        live.remove(match)
    entry["pending_dispatch_worker_intent"] = live

    if match is None:
        return (_DISPATCH_WORKER_NOT_APPLICABLE, None, None, None)
    return (
        match.get("worker"),
        match.get("intent"),
        match.get("task_text"),
        match.get("requested_model"),
    )


_MODEL_COST_RANK: dict[str, int] = {"haiku": 0, "sonnet": 1, "opus": 2}




































def _analytical_qa_nudge_message() -> str:
    return (
        "[cost-router] Analytical Q&A detected — dispatch to a haiku subagent via "
        "Agent(model=\"haiku\") instead of answering inline on sonnet to save ~$0.15."
    )














def _no_verdict_nudge_message() -> str:
    return (
        "[cost-router] No routing verdict found for this turn — emit a `Routing:` line "
        "before further tool calls (e.g. `Routing: DIRECT — <reason>` or `Routing: FO — <reason>`)."
    )


def _inline_narration_nudge_message() -> str:
    return (
        "Inline re-narration of subagent results detected. Output 3 sentences max or a "
        "Key findings: bullet list - do NOT re-render tables or reports the subagent already "
        "produced. Restructure before the next tool call."
    )


def _dispatch_justification_nudge_message(count: int) -> str:
    return (
        f"[cost-router] {count} consecutive turns of dispatch justification without execution — "
        "stop explaining and execute: emit `Routing:` and dispatch via Task."
    )






# PROD-20 (break-005): cap fast-path exemption at FAST_PATH_CALL_LIMIT mutating calls.

# PROD-20 (break-018): MUTATING_TOOLS is an allowlist match. An unrecognized
# tool name (e.g. a custom MCP tool) that plainly LOOKS like it mutates state
# must not silently default-allow just because it's absent from that fixed
# list — unknown-and-potentially-mutating should fail closed, not open.




# PROD-20 (break-020): file names explicitly named in the triggering prompt,
# used as a soft scope bound for what a single verdict legitimately covers —
# see the out-of-scope check in `event_gate`.

# P12-11: DIRECT routing must carry a non-empty reason after the em-dash.






































_CANDIDATE_TABLE_PATH = Path(__file__).resolve().parent.parent / "data" / "candidate_table.json"






def event_reset(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    session_id = session_id_of(payload)
    prompt = _text_for_prompt(payload)
    state = _load_state()
    sessions = state["sessions"]
    entry = _get_session(sessions, session_id)
    entry["turn"] = int(entry.get("turn", 0)) + 1
    entry["turn_id"] = _turn_id(payload)
    entry["verdict"] = False
    entry["verdict_turn_id"] = None
    entry["fast_path_exempt"] = is_fast_path_exempt(prompt)
    entry["fast_path_calls"] = 0
    entry["fast_path_calls_by_file"] = {}
    entry["pending_batch_reminder"] = None
    entry["task_dispatched_turn_id"] = None
    entry["prompt_files"] = _files_mentioned_in_prompt(prompt)
    entry["bulk_shell_choice"] = None
    entry["bulk_shell_pending_hash"] = None
    entry["shell_calls_since_dispatch"] = 0
    # RG-03: Reset inline mutation counter on new turn so stale counts don't persist.
    entry["inline_mutations_since_dispatch"] = 0
    # RD-7: Reset cumulative bulk-write counter on new turn (event_reset).
    entry["bulk_write_file_types"] = {}
    entry["bulk_write_last_role"] = None
    # RD-6: Reset FW scope files on new turn (event_reset).
    entry["fw_scope_files"] = []
    # RD-5: Reset fix-loop tracking on new turn
    entry["fix_loop_file_set"] = []
    entry["fix_loop_fail_count"] = 0
    entry["fix_loop_test_ran"] = False
    # RD-2: Reset structural similarity tracking on new turn (event_reset).
    entry["last_write_fingerprints"] = []
    entry["mechanical_similarity_flag"] = False
    # RD-3: Store prompt-level intent and reset work unit tracking
    _prompt_intent = classify_intent(prompt) if prompt else "unknown"
    # Visibility maintained via the intent event (JSONL sink). Removed dual-sink _warn() call to prevent silent divergence.
    entry["prompt_intent"] = _prompt_intent
    append_event({"type": "intent", "session_id": session_id, "turn_id": _turn_id(payload), "intent": _prompt_intent})
    entry["read_count"] = 0
    entry["work_unit_intent"] = None
    pre_choice = _bulk_choice_from_prompt(prompt)
    if pre_choice:
        entry["bulk_shell_choice"] = pre_choice
    _save_state(state)

    if is_claude:
        return {"continue": True}

    output: dict[str, Any] = {"continue": True}
    if prompt and not entry["fast_path_exempt"] and classify_intent(prompt) == "agentic_action":
        output["additional_context"] = _PROACTIVE_HINT
    return output


def event_scan(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    text = _text_for_assistant(payload)
    session_id = session_id_of(payload)
    # P12-2: PostToolUse payloads lack inline assistant text — scan transcript
    # to find the Routing: verdict and emit routing_declared mid-session so
    # subsequent PreToolUse gate calls skip the expensive transcript scan (P12-3).
    if not text:
        raw_path = _transcript_path(payload)
        if raw_path:
            try:
                path = Path(raw_path)
                if path.is_file():
                    role = _scan_role_backward(_read_transcript_tail(path))
                    if role:
                        _model = _model_of_with_event_fallback(payload, session_id)
                        _state_t = _load_state()
                        _entry_t = _get_session(_state_t["sessions"], session_id)
                        _intent = _entry_t.get("prompt_intent", "unknown")
                        _mrm_1 = "primary"
                        if not _model:
                            _rm = get_routing_for_intent(_intent)
                            _model = _rm.get("worker") if role == "FW" else _rm.get("orchestrator")
                            _mrm_1 = "fallback" if _model else "unknown"
                        append_event({
                            "type": "routing_declared",
                            "session_id": session_id,
                            "turn_id": _turn_id(payload),
                            "role": role,
                            "model": _model or "unknown",
                            "intent": _intent,
                            "rule_id": f"{role}:{_intent}",
                            "model_resolution_method": _mrm_1,
                        })
            except Exception:  # noqa: BLE001
                pass
        # TICKET-005: emit tool_invocation event for PostToolUse so tool_name
        # is queryable in routing_events.jsonl without a transcript scan.
        _tool = _tool_name(payload)
        if _tool:
            try:
                append_event({
                    "type": "tool_invocation",
                    "session_id": session_id,
                    "turn_id": _turn_id(payload),
                    "tool_name": _tool,
                })
            except Exception:  # noqa: BLE001
                pass
        if payload.get("hook_event_name") == "Stop":
            raw_path = _transcript_path(payload)
            if raw_path:
                session_output_tokens = _read_session_output_tokens(raw_path)
                _state_stop = _load_state()
                _entry_stop = _get_session(_state_stop["sessions"], session_id)
                _entry_stop["session_output_tokens"] = session_output_tokens
                _save_state(_state_stop)
                if session_output_tokens > 50000:
                    append_event({
                        "type": "session_output_high",
                        "session_id": session_id,
                        "session_output_tokens": session_output_tokens,
                    })
        return {}
    state = _load_state()
    sessions = state["sessions"]
    entry = _get_session(sessions, session_id)

    if text:
        entry["last_assistant_snippet"] = text[:ASSISTANT_SNIPPET_CHARS]
        # PROD-20 (break-003): scan ran this turn — record real "no verdict", not absent detection.
        entry["scan_seen_turn_id"] = _turn_id(payload)

    nudge: str | None = None
    model = _model_of_with_event_fallback(payload, session_id)

    if text and has_routing_verdict(text):
        entry["verdict"] = True
        entry["verdict_turn"] = int(entry.get("turn", 0))
        entry["verdict_turn_id"] = _turn_id(payload)
        entry["verdict_created_at"] = time.time()  # TICKET-042: record TTL for eviction
        # P12-13: emit routing_declared event for role distribution tracking.
        _m_role = ROUTING_VERDICT_RE.search(text)
        if _m_role:
            entry["last_routing_role"] = _m_role.group("role")
            try:
                _intent = entry.get("prompt_intent", "unknown")
                _role = _m_role.group("role")
                _model_resolved = model
                _mrm_2 = "primary"
                if not _model_resolved:
                    _rm = get_routing_for_intent(_intent)
                    _model_resolved = _rm.get("worker") if _role == "FW" else _rm.get("orchestrator")
                    _mrm_2 = "fallback" if _model_resolved else "unknown"
                append_event({
                    "type": "routing_declared",
                    "session_id": session_id,
                    "turn_id": _turn_id(payload),
                    "role": _role,
                    "model": _model_resolved or "unknown",
                    "intent": _intent,
                    "rule_id": f"{_role}:{_intent}",
                    "model_resolution_method": _mrm_2,
                })
            except OSError as e:
                _warn("routing_state_io_failed", {"file": "check_routing.py", "error": str(e)})

    if text:
        bulk_choice = _parse_bulk_verify_choice(text)
        if bulk_choice:
            entry["bulk_shell_choice"] = bulk_choice
    tier = _tier_of_model_with_event(model)
    if tier in ("fast", "reasoning"):
        entry["tier"] = tier
    if tier in ("fast", "reasoning"):
        history = list(entry.get("tier_history") or [])
        if not history or history[-1] != tier:
            history.append(tier)
            entry["tier_history"] = history[-TIER_HISTORY_CAP:]
            transitions = _tier_transitions(entry["tier_history"])
            if transitions >= FLIP_FLOP_THRESHOLD and not entry.get("churn_nudged"):
                entry["churn_nudged"] = True
                nudge = _churn_nudge_message(transitions)

    # CF-03: Increment orchestrator turn counter so agent_call_tracker can
    # compute context-reload cost without re-scanning the transcript.
    entry["orchestrator_turns"] = int(entry.get("orchestrator_turns", 0)) + 1

    _save_state(state)

    # ── exploration pattern: inline Sonnet reads without Haiku dispatch ───
    events = read_events()
    _session_evs = [e for e in events if e.get("session_id") == session_id]
    _prompt_indices = [i for i, e in enumerate(_session_evs) if e.get("type") == "prompt"]
    _turn_start = _prompt_indices[-1] if _prompt_indices else 0
    _turn_evs = _session_evs[_turn_start:]
    _inline_reads = {
        e["target_hash"]
        for e in _turn_evs
        if e.get("tool_name") in ("Read", "read_file") and e.get("target_hash")
    }
    _has_dispatch = any(e.get("tool_name") in ("Task", "Agent") for e in _turn_evs)
    if len(_inline_reads) >= 3 and not _has_dispatch and not nudge:
        _n = len(_inline_reads)
        nudge = (
            f"[cost-router] {_n} distinct files read inline on Sonnet this turn "
            f"without a Haiku Explore dispatch. "
            f"Next exploration batch → Agent(model=\"haiku\", subagent_type=\"Explore\", ...). "
            f"See model-router rule: Reading — exploration phase."
        )

    # ── shadow mode: log candidate route vs actual ────────────────────────
    if os.environ.get("SHADOW_MODE") == "1" and text:
        m = ROUTING_VERDICT_RE.search(text)
        if m:
            actual_role = m.group("role")
            session_intents = [
                e.get("intent") for e in events
                if e.get("type") == "intent" and e.get("session_id") == session_id
                and isinstance(e.get("intent"), str)
            ]
            current_intent = session_intents[-1] if session_intents else None
            candidate_table = _load_candidate_table()
            c_role = _candidate_role(current_intent, candidate_table)
            if c_role:
                try:
                    append_event({
                        "type": "shadow_route",
                        "session_id": session_id,
                        "turn_id": _turn_id(payload),
                        "actual_role": actual_role,
                        "candidate_role": c_role,
                        "session_intent": current_intent,
                        "model": model,
                        "diverged": actual_role != c_role,
                    })
                except OSError as e:
                    _warn("routing_state_io_failed", {"file": "check_routing.py", "error": str(e)})

    if nudge:
        if is_claude:
            # hookSpecificOutput.additionalContext is valid for PostToolUse only;
            # Stop hooks reject it with JSON validation error.
            if payload.get("hook_event_name") == "Stop":
                return {}
            return {"hookSpecificOutput": {"additionalContext": nudge}}
        return {"additional_context": nudge}
    return {} if is_claude else {"additional_context": ""}


def _subagent_start_output(is_claude: bool, message: str | None) -> dict[str, Any]:
    if is_claude:
        # SubagentStart's hookSpecificOutput schema (Claude Code) has no
        # confirmed additionalContext/message channel the way PreToolUse
        # does — and Claude Code's SubagentStart payload doesn't carry the
        # signals needed to compute a nudge anyway (see
        # _dispatch_task_text/_dispatch_requested_model), so `message` is
        # always None on that host in practice.
        return {"continue": True}
    output: dict[str, Any] = {"permission": "allow"}
    if message:
        output["agent_message"] = message
    return output


def event_subagent_start(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    state = _load_state()
    conversation = _conversation_key(payload)
    subagents = state["subagents"]
    existing = subagents.get(conversation)
    count = int(existing.get("count", 0)) if isinstance(existing, dict) else 0
    subagents[conversation] = {"count": count + 1, "ts": time.time()}

    # PROD-6 (2026-08-03): also track a global, non-conversation-keyed count on
    # Cursor — see `_GLOBAL_SUBAGENT_KEY`'s docstring-comment for why the
    # per-conversation entry above can never match a Cursor worker's own tool
    # calls.
    if not is_claude:
        global_existing = subagents.get(_GLOBAL_SUBAGENT_KEY)
        global_count = int(global_existing.get("count", 0)) if isinstance(global_existing, dict) else 0
        subagents[_GLOBAL_SUBAGENT_KEY] = {"count": global_count + 1, "ts": time.time()}

    nudges: list[str] = []
    task_text = _dispatch_task_text(payload)
    requested_model = _dispatch_requested_model(payload)
    subagent_id = _subagent_id(payload)
    subagent_type = payload.get("subagent_type") or payload.get("agent_type")
    if isinstance(subagent_type, str):
        subagent_type = subagent_type.strip() or None
    else:
        subagent_type = None

    # SubagentStart's own payload never carries the dispatch JSON (confirmed
    # live: it's {session_id, transcript_path, cwd, scratchpad_dir, prompt_id,
    # agent_id, agent_type, hook_event_name} — no task/prompt/tool_input), so
    # _parse_dispatch_worker_intent(task_text) here would always see task_text
    # == "" and return the sentinel. The real worker/intent was parsed and
    # queued at PreToolUse(Task/Agent) time instead — recover it from there.
    entry = _get_session(state["sessions"], session_id_of(payload))
    dispatch_worker, dispatch_intent, queued_task_text, queued_requested_model = _pop_pending_dispatch_worker_intent(
        entry, subagent_id=subagent_id, now=time.time()
    )

    if subagent_id:
        files = extract_paths_from_prompt(task_text) if task_text else []
        # DGC-006b: Extract dispatch context estimate from session entry
        dispatch_context_estimate_value = None
        if isinstance(entry, dict):
            last_est = entry.get("last_dispatch_context_estimate")
            if isinstance(last_est, dict) and isinstance(last_est.get("total_tokens"), int):
                dispatch_context_estimate_value = last_est.get("total_tokens")
        try:
            mark_registry_busy(
                conversation,
                subagent_id,
                subagent_type=subagent_type,
                # F7: SubagentStart carries no model; use the one queued at PreToolUse.
                model=requested_model or queued_requested_model,
                task_hash=hash_target(task_text) if task_text else None,
                files=files,
                dispatch_worker=dispatch_worker,
                dispatch_intent=dispatch_intent,
                requested_model=requested_model or queued_requested_model,
                dispatch_context_estimate=dispatch_context_estimate_value,
            )
        except OSError as e:
            _warn("routing_state_io_failed", {"file": "check_routing.py", "error": str(e)})
        entry["last_task_dispatch_agent_id"] = subagent_id
        append_event({
            "type": "dispatch_worker_declared",
            "session_id": conversation,
            "turn_id": _turn_id(payload),
            "subagent_id": subagent_id,
            "worker": dispatch_worker,
            "intent": dispatch_intent,
        })

        # RC-6: Track per-subagent deadline for wave timeout expiry.
        wave_timeout = config_int("concurrency.wave_timeout_seconds", 180)
        now = time.time()
        deadlines = state.setdefault("subagent_deadlines", {})
        deadlines[subagent_id] = now + wave_timeout

    # PROD-19: tier-vs-work mismatch enforcement on subagentStart.
    # Hard deny: mechanical work on a reasoning-tier model (opus on mechanical).
    # Nudge only: bulk-edit on sonnet (allow + message — cost suggestion, not a block).
    # Nudge only: judgment work on a fast-tier model.
    # SubagentStart's own payload has no task_text/requested_model (see comment
    # above), so use the values carried through the PreToolUse dispatch queue.
    # If no queue entry matched, there's nothing real to check — skip, as before.
    prod19_task_text = queued_task_text
    prod19_requested_model = queued_requested_model
    if not (prod19_task_text and prod19_requested_model):
        digest = (
            hashlib.sha256(prod19_task_text.encode("utf-8")).hexdigest()[:8]
            if prod19_task_text
            else None
        )
        append_event({
            "type": "prod19_check_skipped",
            "session_id": conversation,
            "has_task_text": bool(prod19_task_text),
            "has_requested_model": bool(prod19_requested_model),
            "task_text_digest": digest,
        })
    if prod19_task_text and prod19_requested_model:
        label = classify_dispatch_work(prod19_task_text)
        matrix_worker_entry = get_routing_for_intent(label)
        expected_worker = matrix_worker_entry.get("worker", "sonnet")
        requested_rank = _model_cost_rank(prod19_requested_model)
        expected_rank = _model_cost_rank(expected_worker)
        append_event({
            "type": "prod19_check_ran",
            "session_id": conversation,
            "label": label,
            "requested_model": prod19_requested_model,
            "expected_worker": expected_worker,
        })
        if label == "mechanical" and requested_rank > expected_rank:
            _bump(state, "dispatch_deny_mechanical_on_reasoning")
            _save_state(state)
            return _deny(
                is_claude,
                _dispatch_tier_mismatch_deny_message(label, prod19_requested_model, expected_worker),
            )
        elif label == "bulk-edit" and requested_rank > expected_rank:
            _bump(state, "dispatch_nudge_bulk_edit_on_sonnet")
            nudges.append(_dispatch_bulk_edit_on_sonnet_nudge_message(prod19_requested_model))
        elif label == "judgment" and _tier_of_model_with_event(prod19_requested_model) == "fast":
            _bump(state, "dispatch_nudge_judgment_on_fast")
            nudges.append(_dispatch_judgment_on_fast_nudge_message(prod19_requested_model))

    if task_text and requested_model:
        entry = _get_session(state["sessions"], session_id_of(payload))
        budget_nudge = _dispatch_budget_nudge_and_log(
            payload, task_text, requested_model, entry
        )
        if budget_nudge:
            _bump(state, "dispatch_nudge_budget")
            nudges.append(budget_nudge)

    _save_state(state)
    nudge = "\n\n".join(nudges) if nudges else None
    return _subagent_start_output(is_claude, nudge)


# Shared with enforce_role_gates.py via _common.subagent_id_of (kept as a
# local alias so the existing _subagent_id(...) call sites in this file are
# untouched).
_subagent_id = subagent_id_of






def _registry_agent_entry(conversation: str, subagent_id: str | None) -> dict[str, Any]:
    """Registry entry for this agent (sessions[sid]["agents"][id]); {} if absent. Never raises."""
    try:
        agents = load_dispatch_registry().get("sessions", {}).get(conversation, {}).get("agents", {})
        entry = agents.get(subagent_id) if isinstance(agents, dict) else None
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


def _payload_agent_type(payload: dict[str, Any]) -> str | None:
    """agent_type as reported by the host on this subagent event; None if absent."""
    value = payload.get("agent_type") or payload.get("subagent_type")
    return (value.strip() or None) if isinstance(value, str) else None


def _subagent_usage_record(payload: dict[str, Any], conversation: str) -> dict[str, Any]:
    """Build a usage.jsonl record for one finished Task dispatch (see PROD-15).

    Attributed to the parent session via `conversation` (same correlation id
    `_conversation_key` uses for gate exemption) so session_summary.py can roll
    it into the parent's total. Real cost is sourced from `agent_transcript_path`
    (see `_read_subagent_transcript_usage`) since the SubagentStop payload
    itself carries no usage field on Claude Code (confirmed live 2026-07-31 —
    see knowledge/host-payloads.md); Cursor's shape is still unconfirmed
    (PROD-2), so the inline-field check stays as a defensive fallback for that
    host. Dispatches with neither source are marked cost-unknown rather than
    silently omitted or estimated.
    """
    subagent_id = _subagent_id(payload)
    # SubagentStop carries no model; fall back to the one recorded at SubagentStart.
    registry_entry = _registry_agent_entry(conversation, subagent_id)
    model = normalize_payload(payload).model or registry_entry.get("model")
    # SubagentStop carries no agent_type; fall back to the one recorded at SubagentStart.
    agent_type = _payload_agent_type(payload) or registry_entry.get("subagent_type") or registry_entry.get("type")
    record: dict[str, Any] = {
        "type": "subagent_dispatch",
        "session_id": conversation,
        "subagent_id": subagent_id,
        "agent_type": agent_type,
        "tier": _tier_of_model_with_event(model),
        "model": model,
        # append_event only dedupes against the immediately preceding line, on
        # a fixed key tuple that includes target_hash. Two distinct dispatches
        # finishing back-to-back with no usage data would otherwise be
        # indistinguishable (same session_id/type/est_tokens=0, no
        # direction/bytes/tool_name) and the second would be dropped as a
        # false duplicate — exactly the undercounting this fix exists to stop.
        # A per-dispatch nonce here is a deliberate dedup escape hatch, not a
        # real content hash.
        "target_hash": hash_target(f"{conversation}:{subagent_id or 'unknown'}:{time.time()}"),
    }
    usage = None
    for key in ("usage", "total_usage", "token_usage"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            usage = candidate
            break
    if isinstance(usage, dict) and "output_tokens" in usage:
        record["est_tokens"] = usage.get("output_tokens", 0)
        record["est_input_tokens_real"] = usage.get("input_tokens", 0)
        record["est_cache_read_tokens"] = usage.get("cache_read_input_tokens", 0)
        record["token_source"] = "real"
        record["usage_source"] = "payload"
        return record

    transcript_usage = _read_subagent_transcript_usage(payload.get("agent_transcript_path"))
    if transcript_usage is not None:
        record["est_tokens"] = transcript_usage["output_tokens"]
        record["est_input_tokens_real"] = transcript_usage["input_tokens"]
        record["est_cache_read_tokens"] = transcript_usage["cache_read_input_tokens"]
        record["token_source"] = "real"
        record["usage_source"] = "transcript"
        return record

    record["est_tokens"] = 0
    record["token_source"] = "unavailable"
    record["usage_source"] = "unavailable"
    return record


def _build_subagent_stop_telemetry_row(
    payload: dict[str, Any], conversation: str, state: dict[str, Any]
) -> dict[str, Any]:
    """Build a telemetry row for subagent runtime and usage at SubagentStop.

    Fields: timestamp (auto), agent_id, depth, status, model, outcome, startup_estimate, stall_events.
    result_status, outcome_source. No token fields. Never raises.
    DGC-006b: Outcome derivation from result_status + stall_killed; model/startup_estimate from registry.
    """
    subagent_id = _subagent_id(payload)
    np = normalize_payload(payload)
    session_id = session_id_of(payload)
    entry = _get_session(state["sessions"], session_id)

    # Extract status from payload
    status = payload.get("status", "unknown")

    # Extract depth from continuation context or default to 0
    depth = 0
    if isinstance(entry, dict):
        continuation = entry.get("continuation_manifest")
        if isinstance(continuation, dict):
            depth = int(continuation.get("depth", 0))

    # Count stall events from subagent_progress
    stall_events = 0
    progress_entry = state.get("subagent_progress", {}).get(subagent_id, {})
    if isinstance(progress_entry, dict):
        stall_events = int(progress_entry.get("stall_count", 0))

    # DGC-006b: Read model, startup_estimate from PRIMARY sources (payload/state);
    # result_status, stall_killed from manifest + progress_entry (not registry, which isn't populated yet)
    model = payload.get("model")
    startup_estimate = None
    result_status = None
    stall_killed = False
    outcome_source = "none"

    # Read startup_estimate from session entry as fallback source
    session_est = None
    if isinstance(entry, dict):
        dispatch_est = entry.get("last_dispatch_context_estimate")
        if isinstance(dispatch_est, dict):
            session_est = dispatch_est.get("total_tokens")

    # Read stall_killed from progress_entry (must happen BEFORE registry read)
    stall_killed = bool(progress_entry.get("stall_killed", False)) if isinstance(progress_entry, dict) else False

    # Compute result_status from manifest (matching event_subagent_stop logic)
    report_text = None
    try:
        transcript_path = payload.get("agent_transcript_path")
        if transcript_path:
            report_text = _extract_report_from_transcript(transcript_path)
    except Exception:
        pass

    if not report_text:
        report_text = payload.get("last_assistant_message") or payload.get("result")

    # Parse manifest and extract result_status (DGC-106 logic)
    manifest_status = "UNKNOWN"
    if report_text:
        try:
            manifest = continuation_manifest.parse_manifest(report_text)
            if isinstance(manifest, dict):
                manifest_status = manifest.get("status", "UNKNOWN")
        except Exception:
            pass

    # Calculate result_status from manifest_status and stall_killed (matching event_subagent_stop)
    # Track whether stall_killed was the driver of result_status being PARTIAL
    stall_caused_partial = False
    if manifest_status in ("COMPLETE", "PARTIAL"):
        result_status = manifest_status
        if stall_killed:
            result_status = "PARTIAL"
            stall_caused_partial = True
    else:
        result_status = "UNKNOWN"

    agent_type = _payload_agent_type(payload)
    try:
        registry = load_dispatch_registry()
        session_registry = registry.get("sessions", {}).get(conversation, {})
        # Registry layout is sessions[sid]["agents"][agent_id] (see _session_agents).
        agents_in_session = session_registry.get("agents", {}) if isinstance(session_registry, dict) else {}
        if isinstance(agents_in_session, dict):
            agent_entry = agents_in_session.get(subagent_id)
            if isinstance(agent_entry, dict):
                # Fallback for model if not in payload
                if not model:
                    model = agent_entry.get("model")
                # Fallback for agent_type if not in payload
                if not agent_type:
                    agent_type = agent_entry.get("subagent_type") or agent_entry.get("type")
                # Registry dispatch_context_estimate wins; session entry is fallback
                registry_est = agent_entry.get("dispatch_context_estimate")
                if registry_est is not None:
                    startup_estimate = registry_est
    except Exception:
        # Fail open: missing registry data → None values
        pass

    # Fallback to session entry if registry had no estimate
    if startup_estimate is None:
        startup_estimate = session_est

    # DGC-006b: Outcome derivation
    # 1. payload["status"] wins if it is one of the valid values
    valid_outcomes = {"success", "partial", "failed", "retried"}
    if isinstance(status, str) and status in valid_outcomes:
        outcome = status
        outcome_source = "payload"
    # 2. result_status COMPLETE → "success"
    elif result_status == "COMPLETE":
        outcome = "success"
        outcome_source = "result_status"
    # 3. result_status PARTIAL → "partial" (but may be from stall_killed)
    elif result_status == "PARTIAL":
        outcome = "partial"
        outcome_source = "stall" if stall_caused_partial else "result_status"
    # 4. stall_killed true → "failed"
    elif stall_killed:
        outcome = "failed"
        outcome_source = "stall"
    # 5. otherwise "unknown"
    else:
        outcome = "unknown"
        outcome_source = "none"

    # Snapshot of per-gate would-deny counters (naming: would_deny:<gate>).
    counters = state.get("counters")
    would_deny_counters = {
        k: v
        for k, v in (counters if isinstance(counters, dict) else {}).items()
        if isinstance(k, str) and k.startswith("would_deny:")
    }

    # Build the row
    row: dict[str, Any] = {
        "type": "subagent_stop_telemetry",
        "session_id": conversation,
        "agent_id": subagent_id,
        "agent_type": agent_type,
        "depth": depth,
        "status": status,
        "model": model,
        "outcome": outcome,
        "outcome_source": outcome_source,
        "result_status": result_status,
        "startup_estimate": startup_estimate,
        "stall_events": stall_events,
        "would_deny_counters": would_deny_counters,
    }

    return row


def _extract_report_from_transcript(agent_transcript_path: str | None) -> str | None:
    """Extract last assistant message from subagent's JSONL transcript.

    Reuses the JSONL reader pattern from _read_subagent_transcript_usage.
    Returns None on any error.
    """
    if not agent_transcript_path:
        return None
    path = Path(agent_transcript_path)
    if not path.is_file():
        return None
    last_report = None
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # Capture last assistant message
            if entry.get("role") == "assistant":
                content = entry.get("content")
                if content:
                    last_report = _text_from_content(content)
    except OSError:
        return None
    return last_report


def _check_continuation_loop_guard(manifest: dict[str, Any] | None) -> tuple[bool, str]:
    """DGC-206: Check depth and no_progress loop guards.

    Returns (should_allow: bool, reason: str).
    - (False, reason) if depth >= max_depth or no_progress detected.
    - (True, reason) otherwise.
    """
    if not manifest or not isinstance(manifest, dict):
        return (True, "")

    depth = int(manifest.get("depth", 0))
    max_depth = config_int("continuation.max_depth", 3)

    if depth >= max_depth:
        return (False, f"Continuation depth {depth} >= max_depth {max_depth}")

    # Check no_progress: compare current manifest with last dispatch for this lineage.
    # This would require tracking the previous manifest, which is handled at
    # dispatch time. Here we just flag if no_progress config is enabled.
    no_progress_halts = config_get("continuation.no_progress_halts", True)
    if no_progress_halts and continuation_manifest.no_progress(None, manifest):
        return (False, "No progress detected in manifest remaining items")

    return (True, "")


def _build_partial_stop_reminder(manifest: dict[str, Any] | None) -> str:
    """DGC-206: Build a compact reminder for PARTIAL stop event.

    When event_subagent_stop detects result_status=PARTIAL, inject this reminder
    into the main-session context to guide the orchestrator's redispatch decision.

    Returns a compact context string (markdown).
    """
    if not manifest or not isinstance(manifest, dict):
        return ""

    compact_json = continuation_manifest.compact_continuation(manifest)
    if not compact_json:
        return ""

    depth = int(manifest.get("depth", 0))
    remaining_ids = continuation_manifest.get_remaining_ids(manifest)

    reminder = f"**Subagent PARTIAL:** depth={depth}, {len(remaining_ids)} remaining units."
    if compact_json:
        reminder += f"\nContinuation: {compact_json[:200]}"
    return reminder


def _validate_task_manifest_lineage(
    manifest: dict[str, Any] | None, task_payload: dict[str, Any] | None
) -> tuple[bool, str]:
    """DGC-206: Validate that task_payload references a remaining manifest item.

    Hook validates (step 2) that the next Task matches a remaining item from the manifest.
    This prevents accidental dispatch of unrelated work while continuation is pending.

    Returns (is_valid: bool, reason: str).
    """
    if not task_payload:
        return (False, "No task payload to validate")

    task_text = task_payload.get("prompt") or task_payload.get("task_text") or ""
    is_valid, reason = continuation_manifest.validate_manifest_lineage(manifest, task_text)
    return (is_valid, reason)


def event_subagent_stop(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    state = _load_state()
    conversation = _conversation_key(payload)
    subagents = state["subagents"]
    existing = subagents.get(conversation)
    if isinstance(existing, dict):
        remaining = max(0, int(existing.get("count", 0)) - 1)
        if remaining:
            subagents[conversation] = {"count": remaining, "ts": time.time()}
        else:
            del subagents[conversation]
    if not is_claude:
        global_existing = subagents.get(_GLOBAL_SUBAGENT_KEY)
        if isinstance(global_existing, dict):
            global_remaining = max(0, int(global_existing.get("count", 0)) - 1)
            if global_remaining:
                subagents[_GLOBAL_SUBAGENT_KEY] = {"count": global_remaining, "ts": time.time()}
            else:
                del subagents[_GLOBAL_SUBAGENT_KEY]

    # DGC-006: build before the stall ledger entry is dropped below.
    try:
        telemetry_row = _build_subagent_stop_telemetry_row(payload, conversation, state)
    except Exception:
        telemetry_row = None

    subagent_id = _subagent_id(payload)
    if subagent_id:
        entry = _get_session(state["sessions"], session_id_of(payload))
        last_est = entry.get("last_dispatch_context_estimate")
        est_tokens = None
        if isinstance(last_est, dict):
            raw_total = last_est.get("total_tokens")
            if isinstance(raw_total, int):
                est_tokens = raw_total

        # DGC-106: Parse manifest and extract result_status before progress pop
        report_text = None
        manifest_source = "none"
        manifest = None
        depth = 0
        result_status = "UNKNOWN"

        # Try agent_transcript_path first, fall back to payload
        try:
            transcript_path = payload.get("agent_transcript_path")
            if transcript_path:
                report_text = _extract_report_from_transcript(transcript_path)
                if report_text:
                    manifest_source = "agent_transcript_path"
        except Exception:
            pass

        if not report_text:
            report_text = payload.get("last_assistant_message") or payload.get("result")
            if report_text:
                manifest_source = "payload"

        # Parse manifest (never raises)
        if report_text:
            try:
                manifest = continuation_manifest.parse_manifest(report_text)
            except Exception:
                manifest = None

        # Extract depth from manifest (default 0)
        if isinstance(manifest, dict):
            depth = int(manifest.get("depth", 0))
            manifest_status = manifest.get("status", "UNKNOWN")
        else:
            manifest_status = "UNKNOWN"

        # Read stall_killed BEFORE progress pop
        progress_entry = state.get("subagent_progress", {}).get(subagent_id, {})
        stall_killed = bool(progress_entry.get("stall_killed", False))

        # Calculate result_status
        if manifest_status in ("COMPLETE", "PARTIAL"):
            result_status = manifest_status
            if stall_killed:
                result_status = "PARTIAL"
        elif stall_killed:
            result_status = "PARTIAL" if manifest_status in ("COMPLETE", "PARTIAL") else "UNKNOWN"
        else:
            result_status = "UNKNOWN"

        try:
            mark_registry_idle(
                conversation,
                subagent_id,
                est_context_tokens=est_tokens,
                result_status=result_status,
                depth=depth,
                manifest_source=manifest_source,
                stall_killed=stall_killed,
            )
        except OSError as e:
            _warn("routing_state_io_failed", {"file": "check_routing.py", "error": str(e)})
        # BWS-T2-003: drop the finished worker's stall ledger entry.
        state.get("subagent_progress", {}).pop(subagent_id, None)

    _save_state(state)
    try:
        append_event(_subagent_usage_record(payload, conversation))
    except OSError as e:
        _warn("routing_state_io_failed", {"file": "check_routing.py", "error": str(e)})
    if telemetry_row is not None:
        # DGC-106: Emit result_status in telemetry row
        if subagent_id and "result_status" in locals():
            telemetry_row["result_status"] = result_status
        try:
            append_event(telemetry_row)
        except Exception:
            # Never block SubagentStop on telemetry failure
            pass

    # DGC-206: Inject PARTIAL reminder with manifest context
    output = {
        "continue": True,
        "hookSpecificOutput": {
            "additionalContext": "Subagent done. Reply in 3 sentences or Key findings: list. Do not re-render subagent output.",
        },
    } if is_claude else {}

    if is_claude and "result_status" in locals() and result_status == "PARTIAL" and manifest:
        try:
            partial_reminder = _build_partial_stop_reminder(manifest)
            if partial_reminder:
                existing_context = output.get("hookSpecificOutput", {}).get("additionalContext", "")
                output["hookSpecificOutput"]["additionalContext"] = f"{existing_context}\n{partial_reminder}"
        except Exception:
            pass

    return output


def event_read(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Accounting-only: increment mechanical_since_dispatch on reads when on a reasoning tier.

    Never denies — this is purely a counter so COST-1 nudge/deny can see read volume.
    """
    state = _load_state()
    session_id = session_id_of(payload)
    entry = _get_session(state["sessions"], session_id)
    # RD-3: Track read count for work unit intent classification
    entry["read_count"] = int(entry.get("read_count", 0)) + 1
    if entry.get("tier") == "reasoning" and not entry.get("fast_path_exempt"):
        entry["mechanical_since_dispatch"] = int(entry.get("mechanical_since_dispatch", 0)) + 1
        _save_state(state)
    else:
        _save_state(state)
    if is_claude:
        return {"continue": True}
    return {"permission": "allow"}
































def _pending_batch_reminder_text(file_path: str) -> str:
    name = Path(file_path).name if file_path else "(unknown file)"
    return (
        f"[cost-router] Reminder: `{name}` already hit the fast-path same-file limit this turn — "
        "batch all remaining edits to it into ONE Task dispatch rather than another direct edit."
    )


def event_gate(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Thin wrapper around `_event_gate_core` that layers the sticky
    same-file batch reminder on top of every PreToolUse response for the
    rest of the turn, once `pending_batch_reminder` has been set (see the
    fast-path same-file limit check inside `_event_gate_core`)."""
    result = _event_gate_core(payload, is_claude)

    session_id = session_id_of(payload)
    state = _load_state()
    entry = _get_session(state["sessions"], session_id)
    pending_file = entry.get("pending_batch_reminder")
    if not pending_file:
        return result

    tool = _tool_name(payload)
    if tool in ("Task", "Agent"):
        task_text = _dispatch_task_text(payload)
        if task_text and Path(pending_file).name in task_text:
            entry["pending_batch_reminder"] = None
            _save_state(state)
            return result

    reminder = _pending_batch_reminder_text(pending_file)
    if is_claude:
        hook_output = result.get("hookSpecificOutput")
        if isinstance(hook_output, dict):
            existing = hook_output.get("additionalContext")
            hook_output["additionalContext"] = f"{existing}\n{reminder}" if existing else reminder
        else:
            result.setdefault("hookSpecificOutput", {})
            result["hookSpecificOutput"]["additionalContext"] = reminder
    else:
        existing_msg = result.get("agent_message")
        result["agent_message"] = f"{existing_msg}\n{reminder}" if existing_msg else reminder
    return result


def _event_gate_core(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    tool = _tool_name(payload)

    # PROD-19/21: Shell/Bash verification paths — bulk gate runs before fast-path
    # exemption and before PROD-19 dispatch-threshold counting.
    if tool in _SHELL_TOOLS:
        command = _shell_command(payload)
        is_explicit_worker = _is_subagent_context(payload)
        if command:
            state = _load_state()
            entry = _get_session(state["sessions"], session_id_of(payload))

            # TICKET-015: this branch always returns before the later
            # total_gate_calls_session increment, so Bash/Shell must count here.
            entry["total_gate_calls_session"] = int(entry.get("total_gate_calls_session", 0)) + 1

            # RD-5: fix-loop detection for test commands - check FIRST before any other gates
            fix_loop_result = _check_fix_loop(state, entry, payload, tool, is_claude)
            if fix_loop_result is not None:
                return fix_loop_result

            if _is_bulk_verification_command(command):
                # TICKET-040 (fix-7): the fix just dispatched classified as
                # LIGHT tier (single file, no design ambiguity) — redirect a
                # bulk/full-suite verification command to a scoped, targeted
                # one instead of falling through to the mandatory full
                # independent re-run flow below. Scoping the command to the
                # touched file makes it no longer classify as bulk at all
                # (see _is_bulk_verification_command's single-.py-file
                # exemption), so a corrected retry proceeds via the cheaper
                # per-command path a few lines down, not this branch.
                _tier = entry.get("last_dispatch_tier")
                _tier_file = entry.get("last_dispatch_tier_file")
                if _tier == "light" and _tier_file and not light_tier_command_ok(command, _tier_file):
                    _bump(state, "gate_bulk_shell_light_tier_redirect")
                    _save_state(state)
                    return _deny(is_claude, _light_tier_redirect_message(command, _tier_file))

                cmd_hash = hash_target(command)
                pending_hash = entry.get("bulk_shell_pending_hash")
                choice = entry.get("bulk_shell_choice")
                if pending_hash and cmd_hash and cmd_hash != pending_hash:
                    choice = None
                    entry["bulk_shell_choice"] = None
                if cmd_hash:
                    entry["bulk_shell_pending_hash"] = cmd_hash

                # PROD-21 fix: on Cursor, a dispatched worker's OWN tool-call
                # payload never carries agent_id/subagent_id/etc. (those only
                # appear on the subagentStart/subagentStop lifecycle events —
                # see host-payloads.md's PROD-6 live-capture finding), so
                # _is_subagent_context alone returns False for every worker
                # call and would permanently deadlock the `subagent` choice
                # (parent denied, worker also denied). Fall back to the same
                # TTL-backed active-subagent counter the rest of this gate
                # already trusts for that host.
                is_bulk_worker = is_explicit_worker or _in_active_subagent(
                    payload, state, is_claude
                )

                if is_bulk_worker:
                    if choice == "user":
                        _bump(state, "gate_bulk_shell_deny_worker_user_path")
                        _save_state(state)
                        return _deny(is_claude, _bulk_shell_user_runs_message(command))
                    _bump(state, "gate_bulk_shell_allow_worker")
                    _save_state(state)
                else:
                    if choice is None:
                        _bump(state, "gate_bulk_shell_ask")
                        _save_state(state)
                        return _deny(
                            is_claude,
                            _bulk_shell_ask_message(command, normalize_payload(payload).model),
                        )
                    if choice == "user":
                        _bump(state, "gate_bulk_shell_user")
                        _save_state(state)
                        return _deny(is_claude, _bulk_shell_user_runs_message(command))
                    if _safe_dispatch_debt_active(session_id_of(payload)):
                        _bump(state, "gate_bulk_shell_allow_parent_rt_exhausted")
                        _save_state(state)
                    else:
                        _bump(state, "gate_bulk_shell_deny_parent")
                        _save_state(state)
                        return _deny(is_claude, _bulk_shell_dispatch_message())

            if not is_explicit_worker and _is_verification_command(command):
                if not _in_active_subagent(payload, state, is_claude):
                    if not entry.get("fast_path_exempt"):
                        entry["mechanical_since_dispatch"] = (
                            int(entry.get("mechanical_since_dispatch", 0)) + 1
                        )
                        result = _check_dispatch_threshold(state, entry, is_claude, session_id=session_id_of(payload))
                        if result is not None:
                            return result
                        _save_state(state)

            # SHELL-CLASS-1: deploy/resync scripts target external paths — exempt from
            # shell volume gate (GATE-3). They are not working-tree mutations.
            if not is_explicit_worker and _is_deploy_command(command):
                _save_state(state)
                return _allow(is_claude, None)

            # Shell volume gate (GATE-3): count inline shell calls and nudge/deny
            # once the threshold is reached. Subagent workers are exempt.
            if not is_explicit_worker:
                entry["shell_calls_since_dispatch"] = (
                    int(entry.get("shell_calls_since_dispatch", 0)) + 1
                )
                shell_result = _check_shell_gate(state, entry, is_claude, session_id=session_id_of(payload))
                if shell_result is not None:
                    return shell_result

            # Non-bulk, non-verification shell commands (ls, git status, grep
            # on non-graphified files, etc.) are permitted freely — they do not
            # mutate state and must not fall through to the verdict gate.
            _save_state(state)
            return _allow(is_claude, None)

    # PROD-20 (break-018): an unrecognized tool name that plainly LOOKS like
    # it mutates state (matches a mutation keyword) is gated the same as a
    # known MUTATING_TOOLS member — fail closed on the unknown case instead
    # of defaulting to allow just because it's absent from the fixed list.
    # F3: Agent is the Claude Code spelling of Task; it must reach the Task/Agent
    # branch below (grouping, concurrency cap, session budget cap) instead of
    # being allowed here as a non-mutating tool.
    if tool not in MUTATING_TOOLS and tool != "Agent" and not _looks_mutating(tool):
        return _allow(is_claude, None)

    state = _load_state()
    sessions = state["sessions"]
    session_id = session_id_of(payload)
    entry = _get_session(sessions, session_id)
    turn_id = _turn_id(payload)

    # Dispatch volume gate: check if this call would exceed the inline mutation threshold.
    # This fires BEFORE the verdict gate, only when no Task has been dispatched in session yet.
    # Only applies to DISPATCH_MECHANICAL_TOOLS (expensive mutations). Resets after Task dispatch.
    if tool in DISPATCH_MECHANICAL_TOOLS and not _is_subagent_context(payload):
        # Only apply volume gate if no Task has been dispatched this session yet
        if int(entry.get("task_dispatched_count_session", 0)) == 0:
            entry["inline_since_dispatch"] = int(entry.get("inline_since_dispatch", 0)) + 1  
            if entry["inline_since_dispatch"] >= DISPATCH_GATE_THRESHOLD:
                  if _safe_dispatch_debt_active(session_id_of(payload)):
                      _bump(state, "gate_dispatch_vol_allow_rt_exhausted")
                      _save_state(state)
                  else:
                      _bump(state, "gate_deny_dispatch_volume")
                      _save_state(state)
                      return _deny(is_claude,
  _inline_dispatch_volume_deny_message(entry["inline_since_dispatch"], is_claude))    
    elif tool in ("Task", "Agent"):
        # Reset the inline dispatch counter when a Task or Agent is dispatched
        entry["inline_since_dispatch"] = 0
        # Resume gate: clear forced_fo state when Task is dispatched
        _clear_resume_forced(session_id)
        _bump(state, "resume_gate_cleared_by_task")

    # Resume gate: deny Edit/Write before Task when forced_fo state is active.
    # Fires after dispatch volume gate, before subagent exemption, so subagents
    # can always proceed downstream of an already-dispatched Task.
    if (
        tool in DISPATCH_MECHANICAL_TOOLS
        and not _is_subagent_context(payload)
        and int(entry.get("task_dispatched_count_session", 0)) == 0
    ):
        _resume_entry = _check_resume_forced(session_id, turn_id)
        if _resume_entry is not None:
            denial_count = int(_resume_entry.get("denial_count", 0)) + 1
            if denial_count >= RESUME_GATE_MAX_DENIALS:
                _clear_resume_forced(session_id)
                _bump(state, "resume_gate_auto_lapsed")
                try:
                    append_event({"type": "resume_gate_auto_lapsed",
                                  "session_id": session_id,
                                  "turn_id": turn_id,
                                  "denials": denial_count})
                except OSError:
                    pass
                _save_state(state)
            else:
                _increment_resume_denial(session_id, denial_count)
                _bump(state, "gate_deny_resume_forced_fo")
                _save_state(state)
                return _deny(is_claude, _resume_forced_fo_deny_message(
                    int(_resume_entry.get("work_units", 0)), is_claude
                ))

    # Dispatched workers are downstream of an already-routed parent decision.
    # SGF-101: harness-injected agent_id/agent_type is standalone proof of
    # subagent status — no session-local state required, eliminates the
    # transcript-flush race that blocked subagent writes.
    if _is_subagent_context(payload):
        # RC-3: Task/Agent dispatches from subagents must check wave barrier.
        if tool in ("Task", "Agent"):
            barrier_result = _wave_barrier_check(payload, state, is_claude)
            if barrier_result is not None:
                _save_state(state)
                return barrier_result
        stall_result = _subagent_progress_gate(payload, state, is_claude)
        if stall_result is not None:
            _bump(state, "gate_deny_stall_partial_report")
            _save_state(state)
            return stall_result
        _bump(state, "gate_allow_subagent_harness")
        _save_state(state)
        return _allow(is_claude, None)
    # TTL fallback for hosts (Cursor) whose worker payloads lack explicit markers.
    # GATE-BUG-004: excluded for Task/Agent so a fresh dispatch always
    # reaches the cap + serialize_verify block below.
    if tool not in ("Task", "Agent") and _in_active_subagent(payload, state, is_claude):
        stall_result = _subagent_progress_gate(payload, state, is_claude)
        if stall_result is not None:
            _bump(state, "gate_deny_stall_partial_report")
            _save_state(state)
            return stall_result
        _bump(state, "gate_allow_subagent_ttl")
        _save_state(state)
        return _allow(is_claude, None)

    if tool in ("Task", "Agent"):
        entry["mechanical_since_dispatch"] = 0
        entry["task_dispatched_count_session"] = int(entry.get("task_dispatched_count_session", 0)) + 1

        # Grouping enforcement: check before any early return so it always fires.
        tool_input = _dispatch_task_input(payload)
        task_text = _dispatch_task_text(payload)
        routing_snippet = str(entry.get("last_assistant_snippet", ""))
        grouping = check_grouping_enforcement(
            tool_input,
            routing_text=routing_snippet,
            session_entry=entry,
            turn_id=turn_id,
            transcript_path=_transcript_path(payload),
        )
        if grouping.get("should_deny"):
            _bump(state, "gate_deny_grouping")
            try:
                append_event({
                    "type": "dispatch_grouping_violation",
                    "action": "blocked",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "resume_suggested": grouping.get("resume"),
                    "reason": grouping.get("reason"),
                    "target_hash": hash_target(task_text[:TASK_TARGET_HASH_CHARS] if task_text else ""),
                })
            except OSError as e:
                _warn("routing_state_io_failed", {"file": "check_routing.py", "error": str(e)})
            _save_state(state)
            return _deny(is_claude, grouping.get("message") or _GATE_AGENT_MESSAGE)

        # RC-5: block FO dispatches whose task complexity is 'low' — not worth a subagent.
        spawn_check = _spawn_worthiness_check(task_text or "", routing_snippet)
        if spawn_check.get("should_deny"):
            _bump(state, "gate_deny_spawn_worthiness")
            _save_state(state)
            return _deny(is_claude, spawn_check.get("message") or _GATE_AGENT_MESSAGE)

        conversation_key = _conversation_key(payload)
        subagents_state = state.get("subagents", {})
        active_count = _effective_subagent_count(subagents_state.get(conversation_key))
        if not is_claude:
            active_count = max(active_count, _effective_subagent_count(subagents_state.get(_GLOBAL_SUBAGENT_KEY)))
        cap = config_int("concurrency.max_inflight_subagents", 3)
        if active_count >= cap:
            _bump(state, "gate_deny_concurrency_cap")
            _save_state(state)
            return _deny(is_claude, _concurrency_cap_deny_message(active_count, cap))

        # BWS-T2-004: session-level cumulative fan-out cap — deny new Task/Agent
        # dispatch once actual cumulative subagent cost this session crosses
        # concurrency.session_budget_pct of the session budget. This is
        # additive-blind protection: per-dispatch gates above (grouping,
        # spawn-worthiness, concurrency cap) never see the running total, so a
        # swarm of individually-compliant dispatches can still sum to
        # monolith-scale cost (unboundedOrchestrator obs-4).
        if _safe_session_dispatch_cap_active(session_id):
            # Opt-in bounded override (unbound-01 G2): off unless the limit is > 0.
            # The override waives ONLY this cap: execution falls through so the
            # context-pressure and serialize-verify gates and the dispatch
            # bookkeeping below still run for an overridden dispatch.
            _sbo_granted = False
            try:
                _sbo_limit = config_int("concurrency.session_budget_override_limit", 0)
                _sbo_used = int(entry.get("session_budget_override_count", 0) or 0)
                _sbo_text = f"{task_text or ''} {routing_snippet}"
                if (
                    _sbo_limit > 0
                    and SESSION_BUDGET_OVERRIDE_RE.search(_sbo_text)
                    and _sbo_used < _sbo_limit
                ):
                    entry["session_budget_override_count"] = _sbo_used + 1
                    _bump(state, "gate_allow_session_budget_override")
                    _save_state(state)
                    _sbo_granted = True
                    try:
                        append_event({
                            "type": "session_budget_override",
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "used": _sbo_used + 1,
                            "limit": _sbo_limit,
                            "note": "cost figures are estimates, not exact spend",
                        })
                    except OSError as e:
                        _warn("routing_state_io_failed", {"file": "check_routing.py", "error": str(e)})
            except Exception:  # noqa: BLE001
                pass  # a failure before the grant leaves _sbo_granted False -> normal deny
            if not _sbo_granted:
                _bump(state, "gate_deny_session_budget_cap")
                _save_state(state)
                # DGC-205: compute token-aware reservation for the dispatch being denied
                requested_model = _dispatch_requested_model(payload)
                return _deny(is_claude, _session_budget_cap_deny_message(session_id, task_text, requested_model))

        # RC-7: block new spawns when orchestrator context is saturated (>=120% output budget).
        raw_path = _transcript_path(payload)
        pressure_check = _context_pressure_check(session_id, raw_path)
        if pressure_check.get("should_deny"):
            _bump(state, "gate_deny_context_pressure")
            _save_state(state)
            return _deny(is_claude, pressure_check.get("message") or _GATE_AGENT_MESSAGE)
        if pressure_check.get("would_deny"):
            _bump(state, "would_deny:context_pressure")
            _save_state(state)

        # BWS-T2-002: verification must not run while sibling wave units are
        # still in flight — serialize it after the wave drains.
        if config_get("concurrency.serialize_verify", False) and active_count > 0:
            handoff = classify_handoff(task_text or "", "auto", extract_paths_from_prompt(task_text or ""))
            if handoff == "zero":
                _bump(state, "gate_deny_serialize_verify")
                _save_state(state)
                return _deny(is_claude, _serialize_verify_deny_message(active_count))

        entry["task_dispatched_turn_id"] = turn_id
        entry["inline_mutations_since_dispatch"] = 0
        entry["shell_calls_since_dispatch"] = 0
        entry["shell_same_type_counts"] = {"schema_version": 1}
        # T7: dj nudge_heeded — if we were nudging for dispatch justification and
        # the agent now dispatched a Task, the nudge was heeded; emit telemetry and reset.
        if int(entry.get("dj_consecutive_turns", 0)) > 0:
            _bump(state, "gate_nudge_dj_heeded")
            entry["dj_consecutive_turns"] = 0
            entry.pop("dj_last_turn_id", None)
        # RD-7: Reset cumulative bulk-write counter on Task dispatch.
        entry["bulk_write_file_types"] = {}
        # RD-5: Reset fix-loop tracking on Task dispatch
        entry["fix_loop_file_set"] = []
        entry["fix_loop_fail_count"] = 0
        entry["fix_loop_test_ran"] = False
        # RD-2: Reset structural similarity tracking on Task dispatch.
        entry["last_write_fingerprints"] = []
        entry["mechanical_similarity_flag"] = False
        # RD-3: Reset work unit intent tracking on Task dispatch.
        entry["read_count"] = 0
        entry["work_unit_intent"] = None
        # TICKET-040: reset tiered-verification tracking on every fresh Task
        # dispatch — a tier classified for a prior dispatch must never leak
        # into this one's verification decisions.
        entry["last_dispatch_tier"] = None
        entry["last_dispatch_tier_file"] = None
        if task_text:
            entry["last_task_dispatch_turn_id"] = turn_id
            entry["last_task_dispatch_files"] = extract_paths_from_prompt(task_text)
            # TICKET-040 (fix-7): classify this dispatch's verification tier
            # from its own dispatch-schema JSON (files/pattern/why/constraints/
            # worker) so the bulk-verify gate below can size the mandatory
            # post-fix verification instead of always requiring a full
            # independent re-run. See scripts/verification_tier.py.
            _dispatch_tier, _dispatch_tier_files = classify_dispatch_json(task_text)
            entry["last_dispatch_tier"] = _dispatch_tier
            entry["last_dispatch_tier_file"] = touched_file_from_files(_dispatch_tier_files)
        resume_param = tool_input.get("resume")
        # Confirmed from this same code path: task_text/tool_input above are
        # computed unconditionally before this resume check, so a resume
        # dispatch DOES re-fire PreToolUse with its own fresh tool_input.prompt
        # — it is not a bare pass-through to SubagentStart. That means the
        # resume_agent_id match below always has a real worker/intent to pair
        # with, not a stale or absent one.
        if isinstance(resume_param, str) and resume_param.strip():
            entry["last_task_dispatch_agent_id"] = resume_param.strip()
            # BWS-T2-003: a fresh dispatch/resume resets the stall counter,
            # mirroring mechanical_since_dispatch's reset semantics.
            state.setdefault("subagent_progress", {})[resume_param.strip()] = {"stall_count": 0}

        # Parse worker/intent here — this is the only hook that ever has the
        # real dispatch JSON (SubagentStart's payload never does; see
        # event_subagent_start) — and queue it for that later event to consume.
        _dw, _di = _parse_dispatch_worker_intent(task_text)
        requested_model = _dispatch_requested_model(payload)
        _push_pending_dispatch_worker_intent(
            entry,
            worker=_dw,
            intent=_di,
            resume_agent_id=resume_param.strip() if isinstance(resume_param, str) and resume_param.strip() else None,
            now=time.time(),
            task_text=task_text,
            requested_model=requested_model,
        )

        # PROD-19: same tier-vs-work nudge as subagentStart, via PreToolUse Task path.
        if task_text and requested_model:
            label = classify_dispatch_work(task_text)
            dispatch_tier = _tier_of_model_with_event(requested_model)
            if label == "mechanical" and dispatch_tier == "reasoning":
                _bump(state, "dispatch_nudge_mechanical_on_reasoning")
                nudge = _dispatch_mechanical_on_reasoning_nudge_message(requested_model)
                _save_state(state)
                return _deny(is_claude, nudge)
            elif label == "judgment" and dispatch_tier == "fast":
                # Advisory only — matches event_subagent_start's treatment of
                # the identical condition (lines ~2022-2024): judgment work on
                # a fast-tier model is a quality nudge, not a cost-protection
                # hard block, so it must not stop the dispatch.
                #
                # _bump() increments the long-term, global observability
                # counter ("dispatch_nudge_judgment_on_fast" in
                # state["counters"]) used to tune this threshold later — it
                # must keep incrementing every time, unconditionally.
                _bump(state, "dispatch_nudge_judgment_on_fast")
                # Separately, track a session-scoped count so repeated
                # ignored nudges within the same session escalate to a hard
                # block instead of nudging forever.
                session_count = int(entry.get("judgment_on_fast_nudge_count_session", 0)) + 1
                entry["judgment_on_fast_nudge_count_session"] = session_count
                if session_count >= 5:
                    escalation = _dispatch_judgment_on_fast_escalation_message(requested_model)
                    _bump(state, "gate_deny_judgment_on_fast_escalation")
                    _save_state(state)
                    return _deny(is_claude, escalation)
                nudge = _dispatch_judgment_on_fast_nudge_message(requested_model)
                _save_state(state)
                return _allow(is_claude, nudge)

        # Role-vs-tier cross-check: the declared routing role (FO/RB) is
        # self-reported and never checked against the tier of the model
        # actually dispatched, so role analytics can't be trusted. Record
        # every mismatch; nudge at most once per session (advisory only,
        # never denies). Skipped when no role was declared, on a resume
        # (same worker/tier as the original dispatch), and for explore/plan
        # subagents (short-lived, fixed-tier by convention).
        declared_role = _current_role(payload, entry)
        resume_active = isinstance(resume_param, str) and bool(resume_param.strip())
        subagent_type_for_dispatch = str(tool_input.get("subagent_type") or "").strip().lower()
        if (
            task_text
            and requested_model
            and declared_role in ("FO", "RB")
            and not resume_active
            and subagent_type_for_dispatch not in ("explore", "plan")
        ):
            dispatched_tier = _tier_of_model_with_event(requested_model)
            expected_role: str | None = None
            if declared_role == "FO" and dispatched_tier == "reasoning":
                expected_role = "RB"
            elif declared_role == "RB" and dispatched_tier == "fast":
                expected_role = "FO"
            if expected_role:
                try:
                    append_event({
                        "type": "role_tier_mismatch",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "declared_role": declared_role,
                        "dispatched_tier": dispatched_tier,
                        "expected_role": expected_role,
                    })
                except OSError as e:
                    _warn("routing_state_io_failed", {"file": "check_routing.py", "error": str(e)})
                if not entry.get("role_tier_mismatch_nudged"):
                    entry["role_tier_mismatch_nudged"] = True
                    _bump(state, "dispatch_nudge_role_tier_mismatch")
                    _save_state(state)
                    return _allow(
                        is_claude,
                        _role_tier_mismatch_nudge_message(declared_role, expected_role),
                    )
    elif tool in DISPATCH_MECHANICAL_TOOLS and not entry.get("fast_path_exempt"):
        entry["mechanical_since_dispatch"] = int(entry.get("mechanical_since_dispatch", 0)) + 1

    # Session-level tier-agnostic gate: if this session has never dispatched a
    # Task and has accumulated threshold mutating gate calls, deny regardless of
    # tier, verdict, or fast-path status. RC-4: subagent-initiated chains use
    # FAST_PATH_CALL_LIMIT instead of SESSION_DISPATCH_DENY_THRESHOLD to catch
    # stalls earlier before they accumulate across orchestrator turns.
    if tool != "Task":
        entry["total_gate_calls_session"] = int(entry.get("total_gate_calls_session", 0)) + 1
        is_subagent = _is_subagent_context(payload) or _in_active_subagent(payload, state, is_claude)
        stall_threshold = FAST_PATH_CALL_LIMIT if is_subagent else SESSION_DISPATCH_DENY_THRESHOLD
        if (
              entry["total_gate_calls_session"] >= stall_threshold
              and int(entry.get("task_dispatched_count_session", 0)) == 0
          ):
              if _safe_dispatch_debt_active(session_id_of(payload)):
                  _bump(state, "gate_session_allow_rt_exhausted")
                  _save_state(state)
              else:
                  _bump(state, "gate_deny_session_no_dispatch")
                  _save_state(state)
                  return _deny(
                      is_claude,
                      _session_no_dispatch_deny_message(entry["total_gate_calls_session"]),
                  )

    # PROD-20 (break-005): fast-path exemption is capped at
    # FAST_PATH_CALL_LIMIT mutating calls per turn (see its docstring).
    # Calls beyond the cap fall through to normal verdict gating below,
    # exactly as if the session had never been fast-path exempt.
    fast_path_exempt = bool(entry.get("fast_path_exempt"))
    over_fast_path_limit = False
    if fast_path_exempt and tool in DISPATCH_MECHANICAL_TOOLS:
        entry["fast_path_calls"] = int(entry.get("fast_path_calls", 0)) + 1
        if entry["fast_path_calls"] > FAST_PATH_CALL_LIMIT:
            fast_path_exempt = False
            over_fast_path_limit = True

        # Same-file fast-path hardening: unconditional deny once a single file has
        # been hit more than `routing.fast_path_same_file_limit` times this turn via
        # a fast-path-exempt mutating call. This fires regardless of verdict status
        # (unlike the aggregate FAST_PATH_CALL_LIMIT check below) — repeated direct
        # edits to the same file should always be batched into one Task dispatch.
        target_file = _mutation_target_file(payload)
        if target_file:
            by_file = entry.setdefault("fast_path_calls_by_file", {})
            by_file[target_file] = int(by_file.get(target_file, 0)) + 1
            same_file_limit = config_int("routing.fast_path_same_file_limit", 2)
            if by_file[target_file] > same_file_limit:
                entry["pending_batch_reminder"] = target_file
                _bump(state, "gate_deny_fast_path_same_file")
                _save_state(state)
                return _deny(
                    is_claude,
                    _fast_path_same_file_limit_deny_message(by_file[target_file], target_file),
                )

    verdict = _verdict_is_current(entry, turn_id)
    # Fast-path turns are allowed regardless of verdict, so never pay for
    # transcript I/O on them — this is the highest-frequency, lowest-value case.
    if not verdict and not fast_path_exempt:
        raw_path_for_detection = _transcript_path(payload)
        detected = _routing_in_current_turn(payload)
        if detected is True:
            entry["verdict"] = True
            entry["verdict_turn"] = int(entry.get("turn", 0))
            entry["verdict_turn_id"] = turn_id
            entry["verdict_created_at"] = time.time()  # TICKET-042: record TTL for eviction
            verdict = True
            _bump(state, "transcript_hit")
        elif detected is False:
            _bump(state, "transcript_absent")
        elif (
            detected is None
            and not (raw_path_for_detection and Path(raw_path_for_detection).is_file())
            and entry.get("scan_seen_turn_id") != turn_id
            and tool in MUTATING_TOOLS
        ):
            # PROD-3/20: no transcript and no scan — one benefit-of-doubt nudge; unknown mutators fail closed.
            _bump(state, "gate_no_transcript")
            if entry.get("no_transcript_nudged_turn") != turn_id:
                # First occurrence this turn: nudge the agent
                entry["no_transcript_nudged_turn"] = turn_id
                _bump(state, "gate_nudge_no_transcript")
                _save_state(state)
                return _allow(is_claude, _no_transcript_nudge_message())
            else:
                # PROD-20 (break-005): repeat mutating call with no transcript/verdict — deny.
                _bump(state, "gate_deny_no_transcript_repeat")
                _save_state(state)
                return _deny(is_claude, _GATE_AGENT_MESSAGE)
        else:
            _bump(state, "transcript_fallback")

    # Exactly one _save_state below: state changes are persisted once per call.
    # PROD-11: transcript may lag PreToolUse; false-deny self-heals on retry.
    if not fast_path_exempt and not verdict:
        if over_fast_path_limit:
            _bump(state, "gate_deny_no_verdict")
            _save_state(state)
            return _deny(is_claude, _fast_path_limit_deny_message(int(entry.get("fast_path_calls", 0))))
        # Guard: if turn_id is None, dedup would always match (both stored and current
        # are None), so skip dedup and always nudge on the first call.
        dedup_matches = turn_id is not None and entry.get("no_verdict_nudged_turn_id") == turn_id
        if not dedup_matches:
            # First no-verdict hit this turn (or turn_id is None): nudge the agent
            entry["no_verdict_nudged_turn_id"] = turn_id
            _bump(state, "gate_nudge_no_verdict")
            _save_state(state)
            return _allow(is_claude, _no_verdict_nudge_message())
        else:
            # Repeat no-verdict hit this turn: deny
            _bump(state, "gate_deny_no_verdict")
            _save_state(state)
            return _deny(is_claude, _GATE_AGENT_MESSAGE)

    # Resolve role before fast-path exit: P12-8 bulk-write guard must fire even
    # on fast-path-exempt turns (casual prompts don't excuse DIRECT + bulk writes).
    role = _current_role(payload, entry)

    # P12-8: DIRECT + bulk file targets — redirect to FO + Task dispatch.
    # Use current-turn transcript text so the file count and role come from
    # the same source; fall back to persisted snippet when transcript is absent.
    if role == "DIRECT" and tool in DISPATCH_MECHANICAL_TOOLS and not _is_subagent_context(payload):
        _bulk_limit = config_int("routing.direct_bulk_write_limit", 3)
        _snippet_bulk = _current_turn_assistant_text(payload) or str(entry.get("last_assistant_snippet", ""))
        if _direct_write_file_count(_snippet_bulk) >= _bulk_limit:
            _bump(state, "gate_deny_direct_bulk_write")
            _save_state(state)
            return _deny(is_claude, _direct_bulk_write_deny_message(_bulk_limit))

    # RD-7: Cumulative same-type write counter — survives re-declarations of same role.
    # RD-1: Tracks distinct files by name pattern (e.g., test_*.py); editing the same
    # file multiple times doesn't increment the counter (prevents false positives on fix-loop scenarios).
    if role == "DIRECT" and tool in ("Write", "Edit", "StrReplace") and not _is_subagent_context(payload):
        target = _mutation_target_file(payload)
        if target:
            pattern = _file_name_pattern(target)
            _bulk_limit = config_int("routing.direct_bulk_write_limit", 3)
            # RD-1: Track distinct files per name pattern (e.g., test_*.py)
            file_patterns = entry.setdefault("bulk_write_file_types", {})
            if pattern not in file_patterns:
                file_patterns[pattern] = []
            # Only add to list if not already present (distinct files only)
            if target not in file_patterns[pattern]:
                file_patterns[pattern].append(target)
            # Check if we've hit the limit of distinct files for this pattern
            if len(file_patterns[pattern]) >= _bulk_limit:
                _bump(state, "gate_deny_direct_bulk_write_cumulative")
                _save_state(state)
                return _deny(is_claude, _direct_bulk_write_deny_message(_bulk_limit))
            # Track the last declared role when in DIRECT
            entry["bulk_write_last_role"] = role
    elif role != "DIRECT":
        # When changing away from DIRECT to another role, reset the cumulative counter
        last_role = entry.get("bulk_write_last_role")
        if last_role == "DIRECT":
            entry["bulk_write_file_types"] = {}
            entry["bulk_write_last_role"] = role

    # RD-2 compliance: check if model heeded or overrode prior RD-2 nudge.
    _pending_rd2 = entry.get("pending_rd2_nudge")
    if _pending_rd2:
        _latency = entry.get("turn", 0) - _pending_rd2.get("turn", 0)
        if tool in ("Write", "Edit", "StrReplace", "Task"):
            if tool == "Task":
                _rd2_outcome = "complied"
            else:
                # Compare target_hash to distinguish override from pivot
                _ti = payload.get("tool_input") or payload.get("toolInput") or {}
                _new_content = _ti.get("content", "") if isinstance(_ti, dict) else ""
                _new_hash = hash_target(_new_content) if _new_content else ""
                _prev_hash = _pending_rd2.get("target_hash", "")
                _rd2_outcome = "overrode" if _new_hash == _prev_hash else "pivoted"
            append_event({
                "type": "gate_rd2_compliance",
                "session_id": session_id_of(payload),
                "turn_id": turn_id,
                "nudge_id": _pending_rd2["nudge_id"],
                "complied": _rd2_outcome == "complied",
                "outcome": _rd2_outcome,
                "next_tool": tool,
                "latency_turns": _latency,
            })
            del entry["pending_rd2_nudge"]
        elif _latency > 3:
            # TTL expiry: 3+ turns without a mutation — model pivoted
            append_event({
                "type": "gate_rd2_compliance",
                "session_id": session_id_of(payload),
                "turn_id": turn_id,
                "nudge_id": _pending_rd2["nudge_id"],
                "complied": False,
                "outcome": "expired",
                "next_tool": tool,
                "latency_turns": _latency,
            })
            del entry["pending_rd2_nudge"]

    # RD-2: Structural similarity detection for consecutive writes.
    # If 2+ consecutive writes are >70% similar in structure, flag as mechanical.
    if role == "DIRECT" and tool == "Write" and not _is_subagent_context(payload):
        tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
        content = tool_input.get("content", "") if isinstance(tool_input, dict) else ""
        if isinstance(content, str) and len(content) > 50:  # Skip trivial writes
            fp = _structural_fingerprint(content)
            if fp:  # Only track if fingerprint has substance
                # Load recent fingerprints from state
                recent_fps = entry.get("last_write_fingerprints", [])
                # Compare against each recent fingerprint
                for _rd2_i, prev_fp_list in enumerate(recent_fps):
                    prev_fp = set(prev_fp_list)
                    sim = _jaccard_similarity(fp, prev_fp)
                    if sim >= 0.7:
                        entry["mechanical_similarity_flag"] = True
                        _bulk_limit = config_int("routing.direct_bulk_write_limit", 3)
                        _nudge_id = f"{session_id_of(payload)}_{entry.get('turn', 0)}_rd2_{_rd2_i}"
                        append_event({
                            "type": "gate_rd2_structural_similarity",
                            "session_id": session_id_of(payload),
                            "turn_id": turn_id,
                            "similarity": round(sim, 3),
                            "decision": "deny",
                            "target_hash": hash_target(content),
                            "fingerprint_size": len(fp),
                            "prior_write_count": len(recent_fps),
                            "nudge_id": _nudge_id,
                        })
                        entry["pending_rd2_nudge"] = {
                            "nudge_id": _nudge_id,
                            "target_hash": hash_target(content),
                            "turn": entry.get("turn", 0),
                        }
                        _bump(state, "gate_deny_structural_similarity_promoted")
                        _save_state(state)
                        return _deny(is_claude, _structural_similarity_nudge_message(sim))

                # Store this fingerprint (keep last 5)
                recent_fps.append(sorted(fp))
                if len(recent_fps) > 5:
                    recent_fps = recent_fps[-5:]
                entry["last_write_fingerprints"] = recent_fps

    # RD-3: Real-time mechanical intent classification.
    # If work unit is classified as mechanical AND we're on DIRECT,
    # lower the bulk-write deny threshold by 1 (fires sooner).
    if role == "DIRECT" and tool in ("Write", "Edit", "StrReplace") and not _is_subagent_context(payload):
        work_intent = classify_work_unit_intent(entry)
        entry["work_unit_intent"] = work_intent
        if work_intent == "mechanical":
            # Check if we're approaching the bulk-write limit
            _bulk_limit = config_int("routing.direct_bulk_write_limit", 3)
            file_patterns = entry.get("bulk_write_file_types", {})
            max_pattern_count = max(
                (len(v) if isinstance(v, list) else 0 for v in file_patterns.values()),
                default=0
            )
            # Mechanical intent lowers threshold by 1
            effective_limit = max(_bulk_limit - 1, 2)
            # Only fire if we're at the effective limit (not yet at the absolute limit)
            if max_pattern_count == effective_limit:
                _bump(state, "gate_deny_mechanical_intent")
                _save_state(state)
                return _deny(is_claude, _mechanical_intent_deny_message(work_intent, max_pattern_count))

        # OC-1: Nudge when orchestrator tier doesn't match the routing_matrix
        # recommendation for this intent. GATE-BUG-004: a one-shot nudge was
        # silently ignorable forever after the first mismatch -- this now
        # escalates to a deny once the mismatch recurs
        # routing.orchestrator_nudge_deny_after times for this session.
        session_model = model_of(payload)
        if session_model:
            matrix_entry = get_routing_for_intent(work_intent)
            expected_orchestrator = matrix_entry.get("orchestrator", "sonnet")
            session_tier = _tier_of_model_with_event(session_model)
            expected_tier = _tier_of_model_with_event(expected_orchestrator)
            if session_tier != expected_tier and session_tier != "unknown":
                _oc1_constraints = payload.get("constraints") or []
                _oc1_override = any(
                    isinstance(c, str) and "override-model-routing" in c.lower()
                    for c in _oc1_constraints
                )
                if _oc1_override:
                    _bump(state, "gate_allow_orchestrator_ceiling_override")
                    _save_state(state)
                    # TICKET-021: explicit opt-in override constraint bypasses the
                    # nudge_count escalation / hard-deny entirely -- logged as an
                    # advisory allow instead. Default recurrence threshold below
                    # (routing.orchestrator_nudge_deny_after) is unchanged for
                    # calls that don't set this constraint.
                    result = _allow(
                        is_claude,
                        f"[cost-router] {work_intent.capitalize()} work detected — "
                        f"{session_model} does not match routing_matrix.orchestrator "
                        f"({expected_orchestrator}) for this intent, but an explicit "
                        f"override-model-routing constraint is present. Allowing this "
                        f"dispatch through as an advisory only.",
                    )
                    result["_gate_name_override"] = "orchestrator_ceiling"
                    return result
                nudge_count = entry.get("orchestrator_ceiling_nudge_count", 0) + 1
                entry["orchestrator_ceiling_nudge_count"] = nudge_count
                _deny_after = config_int("routing.orchestrator_nudge_deny_after", 3)
                if nudge_count >= _deny_after:
                    _bump(state, "gate_deny_orchestrator_ceiling")
                    _save_state(state)
                    # GATE-BUG-005: distinguish OC-1's deny from RD-7's bulk_write_denied
                    # in session_log.jsonl -- both ran under gate_name="routing_gate".
                    result = _deny(
                        is_claude,
                        f"[cost-router] {work_intent.capitalize()} work detected — "
                        f"{session_model} does not match routing_matrix.orchestrator "
                        f"({expected_orchestrator}) for this intent, and this mismatch has "
                        f"now recurred {nudge_count} times this session. Switch the "
                        f"orchestrator to {expected_orchestrator.capitalize()} to continue.",
                    )
                    result["_gate_name_override"] = "orchestrator_ceiling"
                    return result
                _bump(state, "gate_nudge_orchestrator_ceiling")
                _save_state(state)
                # GATE-BUG-005: distinguish OC-1's nudge from RD-5's fix_loop_nudge
                # in session_log.jsonl -- both ran under gate_name="routing_gate".
                result = _allow(
                    is_claude,
                    f"[cost-router] {work_intent.capitalize()} work detected — consider using "
                    f"{expected_orchestrator.capitalize()} (matrix recommendation) "
                    f"instead of {session_model} to reduce orchestration cost. "
                    f"Reserve higher-tier models for tasks genuinely requiring deep reasoning. "
                    f"({nudge_count}/{_deny_after} before this becomes a hard deny.)",
                )
                result["_gate_name_override"] = "orchestrator_ceiling"
                return result
            else:
                entry["orchestrator_ceiling_nudge_count"] = 0

    # RD-6: Track role for FW scope reset when changing away from FW.
    prev_role = entry.get("_last_gate_role")
    entry["_last_gate_role"] = role
    if prev_role == "FW" and role != "FW":
        entry.pop("fw_scope_files", None)

    # RD-6: FW scope guard — deny when edits span >N files under single FW declaration.
    if role == "FW" and tool in DISPATCH_MECHANICAL_TOOLS and not _is_subagent_context(payload):
        target = _mutation_target_file(payload)
        if target:
            fw_files = set(entry.get("fw_scope_files", []))
            fw_files.add(target)
            entry["fw_scope_files"] = sorted(fw_files)
            _fw_limit = config_int("routing.fw_scope_file_limit", 5)
            if len(fw_files) > _fw_limit:
                _bump(state, "gate_deny_fw_scope")
                _save_state(state)
                return _deny(is_claude, _fw_scope_deny_message(len(fw_files), _fw_limit))

    if fast_path_exempt:
        _bump(state, "gate_allow_fastpath")
        _save_state(state)
        return _allow(is_claude, None)

    # RD-5: fix-loop detection — check FIRST, before the dispatch-threshold and
    # inline-mutations gates, so a repeated edit-test-fail loop on the same
    # file is never masked by the generic "N mechanical calls" nudge/deny
    # (mirrors the shell-command path above, which also checks this first).
    fix_loop_result = _check_fix_loop(state, entry, payload, tool, is_claude)
    if fix_loop_result is not None:
        return fix_loop_result

    threshold_result = _check_dispatch_threshold(state, entry, is_claude, session_id=session_id)
    if threshold_result is not None:
        return threshold_result

    # PROD-20 (break-014): FO/RB claimed while mutating directly (not Task) — deny.
    if role in ("FO", "RB") and tool != "Task":
        _bump(state, "gate_deny_role_mismatch")
        _save_state(state)
        return _deny(is_claude, _role_tool_mismatch_deny_message(role, tool))

    # P12-11: DIRECT without a reason is indistinguishable from lazy routing.
    # Use current-turn text first (same pattern as the bulk-write check above);
    # fall back to persisted snippet when no inline text is available.
    if role == "DIRECT" and tool in DISPATCH_MECHANICAL_TOOLS and not _is_subagent_context(payload):
        _snippet_reason = _current_turn_assistant_text(payload) or str(entry.get("last_assistant_snippet", ""))
        if _snippet_reason and not _direct_has_reason(_snippet_reason):
            try:
                append_event({
                    "type": "routing_no_reason",
                    "session_id": session_id,
                    "turn_id": turn_id or "unknown",
                    "role": "DIRECT",
                })
            except OSError as e:
                _warn("routing_state_io_failed", {"file": "check_routing.py", "error": str(e)})
            _bump(state, "gate_deny_direct_no_reason")
            _save_state(state)
            return _deny(is_claude, _direct_no_reason_deny_message())

    # Tier-agnostic inline mutation gate (GATE-2): increment counter then check thresholds.
    # Only fires for DISPATCH_MECHANICAL_TOOLS (not Task) and not subagent calls.
    # Fires here — after verdict passes and FO/RB check — so unrouted/subagent calls never count.
    if tool in DISPATCH_MECHANICAL_TOOLS and not _is_subagent_context(payload):
        entry["inline_mutations_since_dispatch"] = (
            int(entry.get("inline_mutations_since_dispatch", 0)) + 1
        )
        threshold_inline = _check_inline_mutation_threshold(state, entry, is_claude, session_id=session_id)
        if threshold_inline is not None:
            return threshold_inline

    # PROD-20 (break-020): deny mutations to files not in scope.
    # Scope = prompt_files ∪ test-derived names ∪ current-turn assistant text ∪ stem matches.
    target = _mutation_target_file(payload)
    prompt_files = entry.get("prompt_files") or []
    if target and prompt_files and not _target_is_in_scope(target, prompt_files, payload):
        _bump(state, "gate_deny_out_of_scope")
        _save_state(state)
        return _deny(is_claude, _out_of_scope_deny_message(target, prompt_files))

    # PROD-18: advisory nudge when large Write is claimed DIRECT.
    if tool == "Write":
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = payload.get("toolInput")
        if not isinstance(tool_input, dict):
            tool_input = {}
        content = tool_input.get("content", "")
        if isinstance(content, str) and content:
            line_count = content.count("\n") + 1
            byte_count = len(content.encode("utf-8"))
            if line_count > LARGE_WRITE_LINE_THRESHOLD or byte_count > LARGE_WRITE_BYTE_THRESHOLD:
                if _routing_role_in_current_turn(payload) == "DIRECT":
                    _bump(state, "gate_nudge_direct_large_write")
                    _save_state(state)
                    return _allow(is_claude, _direct_on_large_write_nudge_message(line_count, byte_count))

    _bump(state, "gate_allow_routed")
    _save_state(state)
    return _allow(is_claude, None)








def inline_narration_gate(payload: dict[str, Any], is_claude: bool) -> dict[str, Any] | None:
    """Gate: nudge when large inline narration is detected in the current assistant turn."""
    turn_id = _turn_id(payload)
    text = _current_turn_assistant_text(payload)
    if not text:
        return None
    intent = classify_intent(text[-500:] if len(text) > 500 else text)
    if intent != "inline_narration":
        return None
    min_hits = config_int("inline_narration_gate.inline_narration_min_hits", 1)
    state = _load_state()
    entry = _get_session(state["sessions"], session_id_of(payload))
    stored_turn_id = entry.get("inline_narration_hits_turn_id")
    if stored_turn_id != turn_id:
        entry["inline_narration_hits_turn_id"] = turn_id
        entry["inline_narration_hits_count"] = 1
    else:
        entry["inline_narration_hits_count"] = int(entry.get("inline_narration_hits_count", 0)) + 1
    hit_count = int(entry["inline_narration_hits_count"])
    # Track last tool call turn so dispatch_justification_gate can compute gaps
    entry["last_tool_call_turn"] = int(entry.get("turn", 0))
    if hit_count < min_hits:
        _save_state(state)
        return None
    _bump(state, "gate_nudge_inline_narration")
    _save_state(state)
    return _allow(is_claude, _inline_narration_nudge_message())


def dispatch_justification_gate(payload: dict[str, Any], is_claude: bool) -> dict[str, Any] | None:
    """Gate: nudge after 3+ consecutive turns of dispatch justification without execution."""
    turn_id = _turn_id(payload)
    text = _current_turn_assistant_text(payload)
    if not text:
        return None
    intent = classify_intent(text)
    state = _load_state()
    entry = _get_session(state["sessions"], session_id_of(payload))
    current_turn_count = int(entry.get("turn", 0))
    # TTL: reset counter if dj_last_turn_id was set more than 5 turns ago
    if entry.get("dj_last_turn_id") is not None:
        dj_last_turn_count = int(entry.get("dj_last_turn_count", 0))
        if current_turn_count - dj_last_turn_count > 5:
            entry["dj_consecutive_turns"] = 0
            entry["dj_last_turn_id"] = None
    # Turn-gap check: detect no-tool-call over-explanation
    prev_last_tool_call_turn = int(entry.get("last_tool_call_turn", current_turn_count))
    turn_gap = current_turn_count - prev_last_tool_call_turn
    # Update last_tool_call_turn to current turn (inline_narration_gate sets this on tool turns;
    # here we advance it so the gap resets when dispatch_justification is not the intent)
    if intent != "dispatch_justification":
        entry["last_tool_call_turn"] = current_turn_count
    changed = False
    if intent == "dispatch_justification":
        if entry.get("dj_last_turn_id") != turn_id:
            entry["dj_consecutive_turns"] = int(entry.get("dj_consecutive_turns", 0)) + 1
            entry["dj_last_turn_id"] = turn_id
            entry["dj_last_turn_count"] = current_turn_count
            changed = True
    else:
        if entry.get("dj_consecutive_turns", 0) != 0:
            entry["dj_consecutive_turns"] = 0
            entry.pop("dj_last_turn_id", None)
            changed = True
    count = int(entry.get("dj_consecutive_turns", 0))
    if count >= 2:
        _bump(state, "gate_nudge_dispatch_justification")
        _save_state(state)
        return _allow(is_claude, _dispatch_justification_nudge_message(count))
    # Gap nudge: dispatch_justification text with no tool calls for 2+ turns
    if intent == "dispatch_justification" and turn_gap >= 2:
        _bump(state, "gate_nudge_dj_gap")
        _save_state(state)
        return _allow(
            is_claude,
            f"No tool calls detected for {turn_gap} turn(s) — dispatch justification without "
            f"execution. Stop explaining and dispatch now: emit Routing + Task/Agent call.",
        )
    if changed:
        _save_state(state)
    return None


def shell_same_type_gate(payload: dict[str, Any], is_claude: bool) -> dict[str, Any] | None:
    """Gate: deny when the same shell command type (first token) is called 2+ times inline.

    Tracks per-session counts in ``shell_same_type_counts`` on the session entry.
    Resets on Task dispatch (see reset block near shell_calls_since_dispatch).
    Skips subagent contexts and unknown command types (fail-closed).
    """
    tool = _tool_name(payload)
    if tool not in _SHELL_TOOLS:
        return None
    if _is_subagent_context(payload):
        return None
    command = _shell_command(payload)
    if not command:
        return None
    cmd_type = _extract_command_type(command)
    if cmd_type == "unknown":
        return None
    state = _load_state()
    session_id = session_id_of(payload)
    entry = _get_session(state["sessions"], session_id)
    # Total inline shell cap
    entry["shell_calls_total_inline"] = int(entry.get("shell_calls_total_inline", 0)) + 1
    total_inline_cap = config_int("shell_batch_cap.total_inline_cap", 4)
    # Relief: once dispatch debt is active the budget cap denies Task dispatches, which are the
    # only thing that reset these counters. Denying shell too would deadlock the session.
    debt_relief = _safe_dispatch_debt_active(session_id)
    if entry["shell_calls_total_inline"] >= total_inline_cap:
        if debt_relief:
            _bump(state, "gate_shell_total_cap_allow_rt_exhausted")
            _save_state(state)
            return None
        _bump(state, "gate_deny_shell_total_cap")
        _save_state(state)
        return _deny(is_claude, "Too many inline shell calls - batch into a Task dispatch.")
    raw_counts = entry.get("shell_same_type_counts") or {}
    if not isinstance(raw_counts, dict) or raw_counts.get("schema_version") != 1:
        raw_counts = {"schema_version": 1}
        entry["shell_same_type_counts"] = raw_counts
    counts: dict[str, int] = dict(raw_counts)
    counts[cmd_type] = int(counts.get(cmd_type, 0)) + 1
    entry["shell_same_type_counts"] = counts
    count = counts[cmd_type]
    if count >= 2:
        if debt_relief:
            _bump(state, "gate_shell_same_type_allow_rt_exhausted")
            _save_state(state)
            return None
        _bump(state, "gate_deny_shell_same_type")
        _save_state(state)
        return _deny(is_claude, _shell_same_type_deny_message(cmd_type, count))
    _save_state(state)
    return None


def analytical_qa_gate(payload: dict[str, Any], is_claude: bool) -> dict[str, Any] | None:
    """Gate: nudge when analytical Q&A is detected inline on a reasoning-tier model."""
    turn_id = _turn_id(payload)
    state = _load_state()
    entry = _get_session(state["sessions"], session_id_of(payload))
    text = _current_turn_assistant_text(payload)
    if not text or len(text) < 50:
        return None
    if _is_subagent_context(payload):
        return None
    intent = classify_intent(text[-500:] if len(text) > 500 else text)
    if intent != "analytical_qa":
        return None
    _bump(state, "gate_nudge_analytical_qa")
    _save_state(state)
    return _allow(is_claude, _analytical_qa_nudge_message())


def verdict_pre_nudge(payload: dict[str, Any], is_claude: bool) -> dict[str, Any] | None:
    """Gate: nudge when no Routing: verdict is present before a write/edit tool call."""
    if not isinstance(payload, dict):
        return None
    role = payload.get("routing_role", "")
    text = _current_turn_assistant_text(payload) or ""
    if role or "Routing:" in text:
        return None
    return _allow(
        is_claude,
        "No routing verdict found - emit Routing: ROLE reason before this tool call, then retry.",
    )


def structured_output_gate(payload: dict[str, Any], is_claude: bool) -> dict[str, Any] | None:
    """Gate: nudge when 4+ markdown table rows are detected inline — dispatch to Haiku instead."""
    import re
    text = _current_turn_assistant_text(payload) or ""
    rows = len([line for line in text.splitlines() if re.match(r"\s*\|.+\|", line)])
    if rows >= 4:
        return _allow(
            is_claude,
            "Large inline table (" + str(rows) + " rows) - dispatch as Agent(model=haiku,subagent_type=Explore) instead.",
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--event",
        choices=("reset", "scan", "gate", "subagent_start", "subagent_stop", "read"),
        required=True,
    )
    args = parser.parse_args()

    try:
        _raw = read_stdin_json()
        payload = normalize_payload(_raw)
        is_claude = _is_claude_payload(payload.raw)
        if args.event == "reset":
            output = event_reset(payload.raw, is_claude)
        elif args.event == "scan":
            output = event_scan(payload.raw, is_claude)
        elif args.event == "subagent_start":
            output = event_subagent_start(payload.raw, is_claude)
        elif args.event == "subagent_stop":
            output = event_subagent_stop(payload.raw, is_claude)
        elif args.event == "read":
            output = event_read(payload.raw, is_claude)
        else:
            output = event_gate(payload.raw, is_claude)
        print(json.dumps(output))
        if not is_claude and output.get("permission") == "deny":
            return 2
    except Exception as exc:  # noqa: BLE001 — fail-closed for critical gates
        try:
            state = _load_state()
            _bump(state, "fail_closed")
            _save_state(state)
        except Exception:  # noqa: BLE001
            pass
        print(json.dumps({"permission": "deny", "message": f"Gate error (fail-closed): {type(exc).__name__}: {exc}"}))
    return 0


# ---------------------------------------------------------------------------
# Re-export shims — symbols extracted to dedicated modules.
# WARNING (TICKET-022): these imports REBIND any same-named def/assignment
# above in this file — Python module-level imports execute at import time
# and overwrite the global name unconditionally. A same-named local def
# above this block is DEAD CODE, not "authoritative": any caller inside this
# file resolves the name to whatever this block imported, never to the
# earlier def. Before editing a function in this file, grep this block for
# its name first — TICKET-020's fix silently no-opped this way, because the
# real implementation lived in routing_resolver.py, not the local def here.
# `from check_routing import X` still resolves after the move, which is this
# block's actual purpose — it is not a comment on which body wins locally.
# ---------------------------------------------------------------------------
from transcript_scanner import (  # noqa: E402,F811
    _extract_role,
    _is_genuine_user_prompt,
    _read_session_output_tokens,
    _read_subagent_transcript_usage,
    _record_content,
    _record_is_meta,
    _record_role,
    _scan_role_backward,
    _text_from_content,
    _transcript_path,
    _read_transcript_tail,
)
from session_state import (  # noqa: E402,F811
    _bump,
    _default_session_entry,
    _get_session,
    _load_state,
    _save_state,
)
from command_detector import (  # noqa: E402,F811
    BULK_VERIFY_RE,
    _bulk_choice_from_prompt,
    _is_bulk_verification_command,
    _is_deploy_command,
    _is_verification_command,
    _parse_bulk_verify_choice,
    _shell_command,
)
from verification_tier import (  # noqa: E402,F811
    classify_dispatch_json,
    light_tier_command_ok,
    touched_file_from_files,
)
from routing_resolver import (  # noqa: E402,F811
    _check_dispatch_threshold,
    _check_inline_mutation_threshold,
    _check_resume_forced,
    _check_shell_gate,
    _clear_resume_forced,
    _context_pressure_check,
    _current_role,
    _current_turn_assistant_text,
    _dispatch_budget_nudge_and_log,
    _dispatch_requested_model,
    _dispatch_task_input,
    _dispatch_task_text,
    _get_declared_routing,
    _has_dispatch_budget_override,
    _has_dispatch_override,
    _increment_resume_denial,
    _routing_in_current_turn,
    _routing_role_in_current_turn,
    _scan_lines_backward,
    _spawn_worthiness_check,
    _verdict_is_current,
    classify_dispatch_work,
    get_routing_for_intent,
    has_routing_verdict,
    is_fast_path_exempt,
    score_complexity,
)
from subagent_tracker import (  # noqa: E402,F811
    _GLOBAL_SUBAGENT_KEY,
    _MODEL_COST_RANK,
    _dispatch_bulk_edit_on_sonnet_nudge_message,
    _dispatch_judgment_on_fast_escalation_message,
    _dispatch_judgment_on_fast_nudge_message,
    _dispatch_mechanical_on_reasoning_nudge_message,
    _dispatch_tier_mismatch_deny_message,
    _effective_subagent_count,
    _in_active_subagent,
    _is_subagent_context,
    _model_cost_rank,
    _role_tier_mismatch_nudge_message,
    _tier_transitions,
)
from routing_state_manager import (  # noqa: E402,F811
    _DISPATCH_OVERRIDE_MARKERS,
    _GATE_AGENT_MESSAGE,
    _PROACTIVE_HINT,
    _SIMPLE_MARKERS,
    _bulk_shell_ask_message,
    _bulk_shell_dispatch_message,
    _bulk_shell_user_runs_message,
    _light_tier_redirect_message,
    _conversation_key,
    _dispatch_debt_active,
    _session_budget_cap_deny_message,
    _session_dispatch_cap_active,
_is_claude_payload,
    _text_for_assistant,
    _text_for_prompt,
    _tool_name,
    _turn_id,
)
from dispatch_enforcer import (  # noqa: E402,F811
    _MUTATION_KEYWORD_RE,
    _PROGRESS_MARKER_RE,
    _PROMPT_FILE_RE,
    _ROUTING_REASON_RE,
    _allow,
    _candidate_role,
    _check_fix_loop,
    _churn_nudge_message,
    _concurrency_cap_deny_message,
    _deny,
    _direct_bulk_write_deny_message,
    _direct_has_reason,
    _direct_no_reason_deny_message,
    _direct_on_large_write_nudge_message,
    _direct_write_file_count,
    _dispatch_deny_message,
    _dispatch_nudge_message,
    _expand_scope_names,
    _expire_dead_subagents,
    _fast_path_limit_deny_message,
    _fast_path_same_file_limit_deny_message,
    _file_name_pattern,
    _files_in_current_turn_assistant_text,
    _files_mentioned_in_prompt,
    _fix_loop_deny_message,
    _fix_loop_nudge_message,
    _fw_scope_deny_message,
    _inline_dispatch_volume_deny_message,
    _inline_mutation_deny_message,
    _inline_mutation_nudge_message,
    _jaccard_similarity,
    _load_candidate_table,
    _looks_mutating,
    _mechanical_intent_deny_message,
    _mutation_target_file,
    _no_transcript_nudge_message,
    _out_of_scope_deny_message,
    _resume_forced_fo_deny_message,
    _role_tool_mismatch_deny_message,
    _serialize_verify_deny_message,
    _session_no_dispatch_deny_message,
    _shell_gate_deny_message,
    _shell_gate_nudge_message,
    _stall_partial_report_message,
    _structural_fingerprint,
    _structural_similarity_nudge_message,
    _subagent_progress_gate,
    _subagent_recent_assistant_text,
    _target_is_in_scope,
    _wave_barrier_check,
    event_explore_model_gate,
)


if __name__ == "__main__":
    raise SystemExit(main())
