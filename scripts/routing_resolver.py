#!/usr/bin/env python3
"""Routing resolution — role detection, verdict checking, dispatch classification.

Extracted from check_routing.py. All callers in check_routing.py use
re-export shims; this module can also be imported directly.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    FAST_PATH_MAX_PROMPT_CHARS,
    ASSISTANT_SNIPPET_CHARS,
    SESSION_DISPATCH_DENY_THRESHOLD,
    TASK_TARGET_HASH_CHARS,
    append_event,
    config_get,
    config_int,
    hash_target,
    load_json_state,
    read_events,
    save_json_state,
    session_id_of,
    subagent_id_of,
)
from _model_utils import is_valid_subagent_model, model_context_window_resolved, tier_of_model  # noqa: E402
from classify_intent import (  # noqa: E402
    _AGENTIC_VERBS,
    _ARCHITECTURE_SIGNALS,
    _TARGETED_READ_SIGNALS,
    classify_intent,
)
from estimate_dispatch_context import (  # noqa: E402
    budget_nudge_message,
    estimate_dispatch_context,
    extract_paths_from_prompt,
)
from transcript_scanner import (  # noqa: E402
    ROUTING_VERDICT_RE,
    _extract_role,
    _is_genuine_user_prompt,
    _read_live_context_tokens,
    _read_session_output_tokens,
    _read_transcript_tail,
    _scan_role_backward,
)
from session_state import ROUTING_STATE_FILE, RESUME_STATE_FILE  # noqa: E402

# ---------------------------------------------------------------------------
# Routing matrix lookup
# ---------------------------------------------------------------------------

_REQUIRED_ROUTING_KEYS = ("orchestrator", "worker", "explorer", "test_runner")
_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _validate_routing_matrix() -> None:
    """Fail fast (module load time) if config.json's routing_matrix is malformed.

    Without this, a missing/bad row silently falls back to `_default` inside
    get_routing_for_intent() and the misconfiguration goes unnoticed. `_doc` is
    metadata (a string), not a routing row, and is skipped.
    """
    matrix = config_get("routing_matrix")
    if not isinstance(matrix, dict):
        return
    for name, row in matrix.items():
        if name == "_doc":
            continue
        if not isinstance(row, dict):
            raise ValueError(
                f"config.json routing_matrix.{name} is malformed: "
                f"expected an object, got {type(row).__name__}"
            )
        missing = [k for k in _REQUIRED_ROUTING_KEYS if k not in row]
        if missing:
            raise ValueError(
                f"config.json routing_matrix.{name} is missing required keys: {missing}"
            )
        for key in _REQUIRED_ROUTING_KEYS:
            value = row[key]
            if not is_valid_subagent_model(value):
                raise ValueError(
                    f"config.json routing_matrix.{name}.{key} has an invalid "
                    f"tier value: {value!r}"
                )
        if row.get("effort") not in _VALID_EFFORTS:
            raise ValueError(
                f"config.json routing_matrix.{name}.effort must be one of "
                f"{_VALID_EFFORTS}, got {row.get('effort')!r}"
            )


_validate_routing_matrix()


def get_routing_for_intent(intent: str) -> dict:
    """Return model assignments from routing_matrix for the given work_complexity intent.

    Falls back to routing_matrix._default if intent is not found, but emits a
    routing_intent_fallback event first so the fallback is never silent (see
    _validate_routing_matrix's docstring — this closes the gap it flags).
    Returns dict with keys: orchestrator, worker, explorer, test_runner.
    """
    _default_fallback = {
        "orchestrator": "sonnet",
        "worker": "sonnet",
        "explorer": "haiku",
        "test_runner": "haiku",
    }
    if intent.startswith("_"):
        return _default_fallback
    default = config_get("routing_matrix._default") or _default_fallback
    row = config_get(f"routing_matrix.{intent}")
    if not row:
        try:
            append_event({"type": "routing_intent_fallback", "intent": intent})
        except Exception:  # noqa: BLE001 — visibility must fail open to the fallback
            pass
        return default
    return row


def get_dispatch_output_config(intent: str) -> dict:
    """Return the `output_config` fragment (effort) for a dispatch of this intent."""
    return {"effort": get_routing_for_intent(intent).get("effort", "high")}


# ---------------------------------------------------------------------------
# Dispatch debt
# ---------------------------------------------------------------------------

def _dispatch_debt_active(session_id: str) -> bool:
    """RT-02: Check if the session has accumulated dispatch debt.

    Returns True if either cost or all-inline trigger fires; fail-open False on error.

    Trigger 1 (cost): cumulative_cost_usd >= budget_usd * min(max(warning_pcts) / 100,
        concurrency.session_budget_pct) — aligned with the BWS-T2-004 dispatch cap so
        relief engages no later than the cap denies new fan-out.
    Trigger 2 (all-inline): no subagents dispatched yet AND gate call count has
        reached SESSION_DISPATCH_DENY_THRESHOLD — prevents deadlock when the
        session is running entirely inline without ever dispatching a Task.
    """
    try:
        budget_usd: float = float(config_get("session_cost_budget.budget_usd") or 5.0)
        warning_pcts: list = list(config_get("session_cost_budget.warning_pcts") or [50, 80])
        session_budget_pct: float = float(config_get("concurrency.session_budget_pct") or 0.6)
        relief_threshold_pct: float = min(max(warning_pcts) / 100.0 if warning_pcts else 0.8, session_budget_pct)

        # Trigger 1: cumulative cost has reached the relief threshold (aligned with deny cap).
        stats = load_json_state(DATA_DIR / "agent_call_stats.json")
        this_session = stats.get("sessions", {}).get(session_id, {})
        cumulative_cost: float = float(this_session.get("cumulative_cost_usd", 0.0))
        if cumulative_cost >= budget_usd * relief_threshold_pct:
            return True

        # Trigger 2: all-inline deadlock guard — no dispatches yet but gate calls
        # have hit the session deny threshold.
        rstate = load_json_state(ROUTING_STATE_FILE)
        entry = (rstate.get("sessions", {}).get(session_id, {}) or {})
        task_dispatched = int(entry.get("task_dispatched_count_session", 0))
        total_gate_calls = int(entry.get("total_gate_calls_session", 0))
        if task_dispatched == 0 and total_gate_calls >= SESSION_DISPATCH_DENY_THRESHOLD:
            return True

        return False
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Complexity scoring
# ---------------------------------------------------------------------------

_DISPATCH_JUDGMENT_SIGNALS = frozenset(_ARCHITECTURE_SIGNALS) | frozenset({
    "design", "decide", "trade-off", "tradeoff", "brainstorm", "evaluate",
    "strategy", "ambiguous", "which approach", "recommend", "pros and cons",
    "judgment", "explore options",
})

_DISPATCH_MECHANICAL_SIGNALS = frozenset(_AGENTIC_VERBS) | frozenset(_TARGETED_READ_SIGNALS)

_FILE_PATH_PATTERN = re.compile(
    r"(?:[a-zA-Z0-9_\-]+/)+[a-zA-Z0-9_\-]+\.[a-zA-Z]{1,6}"  # paths with slashes
    r"|"
    r"\b[a-zA-Z0-9_\-]+\.[a-zA-Z]{2,6}\b"  # bare filenames like file.py
)

_SIMPLE_MARKERS = (
    "append ",
    "add a line",
    "rename ",
    "tweak ",
    "update the field",
    "update a field",
    "fix typo",
    "one line",
    "single field",
    "config field",
)


def score_complexity(task_text: str) -> str:
    """RC-5: Classify task complexity as 'low', 'medium', or 'high'.

    'low'    — short prompt with no file references and only mechanical signals.
    'high'   — judgment signals present OR 3+ file references.
    'medium' — everything else.

    Used by _spawn_worthiness_check to downgrade FO dispatches that are too
    trivial to justify subagent overhead.
    """
    if not task_text:
        return "medium"  # unknown intent — fail-open, do not block
    lower = task_text.lower()
    words = frozenset(re.split(r"\W+", lower))
    has_judgment = any(signal in lower for signal in _DISPATCH_JUDGMENT_SIGNALS)
    file_refs = _FILE_PATH_PATTERN.findall(task_text)
    if has_judgment or len(set(file_refs)) >= 3:
        return "high"
    has_mechanical = bool(words & _AGENTIC_VERBS) or any(
        signal in lower for signal in _TARGETED_READ_SIGNALS
    )
    if has_mechanical and len(task_text) <= 400 and len(set(file_refs)) <= 1:
        return "low"
    return "medium"


# ---------------------------------------------------------------------------
# Spawn worthiness check
# ---------------------------------------------------------------------------

def _spawn_worthiness_check(
    task_text: str,
    routing_snippet: str,
) -> dict[str, Any]:
    """RC-5: Block FO dispatches whose tasks are too trivial to justify a subagent.

    Returns {"should_deny": True, "message": ...} when:
    - declared routing role is FO, AND
    - task complexity is 'low'.

    Caller should downgrade to DIRECT and handle inline instead.
    Fail-open: any exception returns should_deny=False.
    """
    from command_detector import _is_verification_command  # noqa: PLC0415
    try:
        # RC-5 exemption: Verification commands (Run:, etc.) bypass spawn-worthiness checks.
        # BWS serialize_verify must not be blocked by low-complexity scoring.
        if _is_verification_command(task_text):
            return {"should_deny": False}

        role = _extract_role(routing_snippet)
        if role != "FO":
            return {"should_deny": False}
        complexity = score_complexity(task_text)
        if complexity != "low":
            return {"should_deny": False}
        return {
            "should_deny": True,
            "message": (
                "RC-5: Task complexity is 'low' — subagent spawn not justified.\n"
                "Re-emit `Routing: DIRECT — <reason>` and handle this inline instead of dispatching a Task."
            ),
        }
    except Exception:  # noqa: BLE001
        return {"should_deny": False}


# ---------------------------------------------------------------------------
# Context pressure check
# ---------------------------------------------------------------------------

def _context_pressure_check(
    session_id: str,
    transcript_path: str | None,
) -> dict[str, Any]:
    """RC-7: gate Task dispatch on LIVE main-session context size.

    ratio = (input + cache_read + cache_creation tokens of the last main-session
    assistant record) / model context window. Modes (config context_pressure.mode):
    off = no-op; warn (default, also for missing/invalid) = never deny, return
    would_deny at ratio >= threshold; enforce = deny at ratio >= threshold.
    Unknown model window (default fallback) fails open and bumps
    context_pressure_default_window. Fail-open on any error.
    """
    allow: dict[str, Any] = {"should_deny": False, "would_deny": False}
    try:
        mode = config_get("context_pressure.mode")
        if mode not in ("off", "warn", "enforce"):
            mode = "warn"
        if mode == "off":
            return allow
        live = _read_live_context_tokens(transcript_path or "")
        if not live or live["tokens"] <= 0:
            return allow
        window, is_default = model_context_window_resolved(live.get("model"))
        if is_default or window <= 0:
            try:
                from session_state import _bump, _load_state, _save_state  # noqa: PLC0415
                st = _load_state()
                _bump(st, "context_pressure_default_window")
                _save_state(st)
            except Exception:  # noqa: BLE001
                pass
            return allow
        ratio = live["tokens"] / window
        threshold = float(config_get("context_pressure.threshold") or 0.85)
        if ratio < threshold:
            return {**allow, "context_pct": ratio}
        message = (
            f"RC-7: Orchestrator context at {ratio:.0%} of the model window (live main-session context). "
            "Run `/compact` to reduce context before dispatching new subagents."
        )
        if mode == "warn":
            return {"should_deny": False, "would_deny": True, "context_pct": ratio, "message": message}
        return {"should_deny": True, "would_deny": True, "context_pct": ratio, "message": message}
    except Exception:  # noqa: BLE001
        return allow


# ---------------------------------------------------------------------------
# Dispatch work classification
# ---------------------------------------------------------------------------

_BULK_EDIT_FILE_THRESHOLD = 5
_SCATTERED_SITE_THRESHOLD = 8

_BULK_EDIT_PHRASE_SIGNALS = frozenset({
    "all files", "every file", "all test files", "across all",
})


def mixed_action_bundle_signal(task_text: str) -> int | None:
    """Return the file-reference count when task_text mixes dispatch-judgment
    signals with mechanical signals and crosses _BULK_EDIT_FILE_THRESHOLD file
    refs — the mixed-action-bundle pattern classify_dispatch_work treats as
    'ambiguous' (allow-silently) by design. None when the pattern isn't
    present. Shared by classify_dispatch_work's telemetry and the
    mixed-action nudge gate (enforce_worker_model.check_mixed_action_bundle)."""
    if not task_text:
        return None
    lower = task_text.lower()
    words = frozenset(re.split(r"\W+", lower))
    has_judgment = any(signal in lower for signal in _DISPATCH_JUDGMENT_SIGNALS)
    has_mechanical = bool(words & _AGENTIC_VERBS) or any(
        signal in lower for signal in _TARGETED_READ_SIGNALS
    )
    if not (has_judgment and has_mechanical):
        return None
    file_refs = _FILE_PATH_PATTERN.findall(task_text)
    count = len(set(file_refs))
    return count if count >= _BULK_EDIT_FILE_THRESHOLD else None


def disambiguate(label: str, text: str, kind: str) -> str | None:
    """Load routing_disambiguation.json and return prefer value when:
    - kind matches the entry's kind, AND
    - if_intent equals label, AND
    - any when_any phrase matches in lowercased text.

    Returns None if no match found or file missing (fail-open).
    """
    try:
        from pathlib import Path as PathlibPath
        config_path = PathlibPath(__file__).resolve().parent.parent / "config" / "routing_disambiguation.json"
        if not config_path.is_file():
            return None
        import json as json_module
        with open(config_path) as f:
            data = json_module.load(f)
        entries = data.get("entries", [])
        lower_text = text.lower()
        for entry in entries:
            if (entry.get("kind") == kind and
                entry.get("if_intent") == label):
                when_any = entry.get("when_any", [])
                if any(phrase in lower_text for phrase in when_any):
                    return entry.get("prefer")
        return None
    except Exception:  # noqa: BLE001 — fail open
        return None


def resolve_role_ambiguity(text: str) -> str:
    """Return RB if any role-kind disambiguation entry matches in text, else FO."""
    try:
        from pathlib import Path as PathlibPath
        config_path = PathlibPath(__file__).resolve().parent.parent / "config" / "routing_disambiguation.json"
        if not config_path.is_file():
            return "FO"
        import json as json_module
        with open(config_path) as f:
            data = json_module.load(f)
        entries = data.get("entries", [])
        lower_text = text.lower()
        for entry in entries:
            if entry.get("kind") == "role":
                when_any = entry.get("when_any", [])
                if any(phrase in lower_text for phrase in when_any):
                    prefer = entry.get("prefer")
                    if prefer == "RB":
                        return "RB"
        return "FO"
    except Exception:  # noqa: BLE001 — fail open to FO
        return "FO"


def classify_dispatch_work(task_text: str) -> str:
    """Classify a Task dispatch's own prompt as 'mechanical', 'judgment',
    'ambiguous', or 'bulk-edit' (PROD-19). Ambiguous/unclassifiable prompts
    are treated as allow-silently by the caller — a false positive here
    (wrongly nudging a genuinely ambiguous dispatch) costs more in agent trust
    than a missed nudge saves in cost.

    'bulk-edit' is returned when the prompt is mechanical AND either:
    - 5+ distinct file path references are detected, OR
    - a bulk-scope phrase is present ("all files", "across all", etc.)
    """
    if not task_text:
        return "ambiguous"
    lower = task_text.lower()
    words = frozenset(re.split(r"\W+", lower))
    has_judgment = any(signal in lower for signal in _DISPATCH_JUDGMENT_SIGNALS)
    has_mechanical = bool(words & _AGENTIC_VERBS) or any(
        signal in lower for signal in _TARGETED_READ_SIGNALS
    )
    if has_judgment and not has_mechanical:
        return "judgment"
    if has_mechanical and not has_judgment:
        # Check for bulk-edit signals before returning "mechanical".
        has_bulk_phrase = any(phrase in lower for phrase in _BULK_EDIT_PHRASE_SIGNALS)
        file_refs = _FILE_PATH_PATTERN.findall(task_text)
        has_many_files = len(set(file_refs)) >= _BULK_EDIT_FILE_THRESHOLD
        max_same_path = max(Counter(file_refs).values()) if file_refs else 0
        has_scattered_sites = max_same_path >= _SCATTERED_SITE_THRESHOLD
        if has_bulk_phrase or has_many_files or has_scattered_sites:
            return "bulk-edit"
        # Check disambiguation layer before returning "mechanical".
        disambiguated = disambiguate("mechanical", task_text, "intent")
        if disambiguated:
            return disambiguated
        return "mechanical"
    if has_judgment and has_mechanical:
        # Mixed-action bundle: falls through to allow-silently below by
        # design (see docstring), but that means an oversized bundle with
        # both bulk mechanical scope and a judgment signal never gets
        # enforced or nudged to split — it just rides whatever model the
        # dispatcher declared. Make that silent pass-through visible.
        file_count = mixed_action_bundle_signal(task_text)
        if file_count:
            try:
                append_event({
                    "type": "mixed_action_bundle_unenforced",
                    "file_count": file_count,
                })
            except Exception:  # noqa: BLE001 — visibility must fail open
                pass
    return "ambiguous"


# ---------------------------------------------------------------------------
# Task dispatch input helpers
# ---------------------------------------------------------------------------

def _dispatch_task_input(payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = payload.get("toolInput")
    return tool_input if isinstance(tool_input, dict) else {}


def _dispatch_task_text(payload: dict[str, Any]) -> str:
    """The dispatched Task's own prompt text.

    Confirmed live (2026-08-03, see knowledge/host-payloads.md): Cursor's
    `subagentStart` payload carries this as a top-level `task` field. Claude
    Code's equivalent — presumably `tool_input.prompt` on its `PreToolUse`
    `Task` call — is NOT yet confirmed (no live capture exists showing
    Claude Code's `Task` `tool_input` shape); checked defensively as a
    fallback so this degrades to 'ambiguous' (silent allow) rather than
    guessing, if that assumption turns out wrong.
    """
    for key in ("task", "prompt", "description"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    tool_input = _dispatch_task_input(payload)
    for key in ("prompt", "task", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _dispatch_requested_model(payload: dict[str, Any]) -> str | None:
    """The model actually requested/assigned for a Task dispatch, as opposed
    to `model_of(payload)` which (on the payload shapes this project has
    seen so far) already happens to read the *dispatched* model's name on
    Cursor's `subagentStart`/`subagentStop` — confirmed live 2026-08-03 (see
    host-payloads.md): both carry a top-level `model` field that already
    reflects the subagent's own model, not the parent session's. `model_of`
    is tried first for that reason; `subagent_model` and a defensive
    `tool_input.model` (Claude Code, unconfirmed) are fallbacks/aliases.
    """
    for key in ("subagent_model", "model", "model_name", "modelName"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    tool_input = _dispatch_task_input(payload)
    value = tool_input.get("model")
    if isinstance(value, str) and value:
        return value
    return None


# ---------------------------------------------------------------------------
# Verdict detection
# ---------------------------------------------------------------------------

def has_routing_verdict(text: str) -> bool:
    return bool(ROUTING_VERDICT_RE.search(text))


def _verdict_is_current(entry: dict[str, Any], turn_id: str | None) -> bool:
    """Honor a persisted verdict only for the turn it was set in."""
    if not entry.get("verdict"):
        return False
    verdict_turn_id = entry.get("verdict_turn_id")
    if turn_id is not None or verdict_turn_id is not None:
        return verdict_turn_id == turn_id
    # Neither host turn id available (e.g. tests): fall back to the internal
    # per-turn counter maintained by reset.
    return entry.get("verdict_turn") == int(entry.get("turn", 0))


def _get_declared_routing(session_id: str | None, turn_id: str | None) -> str | None:
    """O(1)-ish event-log lookup: return the role from a routing_declared event
    written by event_scan (PostToolUse) for the current turn, or None if not
    found.  When present, callers skip transcript scan entirely (P12-3)."""
    if not session_id or not turn_id:
        return None
    try:
        for ev in read_events():
            if (ev.get("type") == "routing_declared"
                    and ev.get("session_id") == session_id
                    and ev.get("turn_id") == turn_id):
                role = str(ev.get("role", ""))
                return role if role else None
    except Exception:  # noqa: BLE001 — advisory; fail open
        pass
    return None


def _scan_lines_backward(lines: list[str]) -> bool | None:
    """Scan the transcript tail from the end toward the start, parsing only as
    far back as the current turn's boundary.

    Returns True as soon as an assistant message in the current turn contains a
    routing line; False when the current turn's start (the last genuine user
    prompt) is reached without one; None when no boundary is found in the tail
    (turn larger than the read window) so the caller can fall back safely.

    Parsing back-to-front means a routed turn typically parses only a handful of
    lines, instead of the entire read window."""
    from transcript_scanner import _record_content, _record_role, _text_from_content  # noqa: PLC0415
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            import json  # noqa: PLC0415
            record = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(record, dict):
            continue
        if _is_genuine_user_prompt(record):
            return False  # reached the current turn's start; no routing above it
        if _record_role(record) == "assistant":
            text = _text_from_content(_record_content(record))
            if text and has_routing_verdict(text):
                return True
    return None


def _routing_in_current_turn(payload: dict[str, Any]) -> bool | None:
    """True/False when the current turn's routing status can be determined,
    None when it can't (missing transcript, unparseable, no turn boundary) — in
    which case the caller falls back to the turn-tagged persisted verdict.

    PROD-20 (break-016): inline fields (`agent_message`/`text`/
    `last_assistant_message`) are trusted as a cheap shortcut ONLY when no
    `transcript_path` is present at all — i.e. when there is no stronger
    verification available. When a `transcript_path` IS present, it is the
    sole source of truth: an inline claim is no longer accepted on its own,
    because a caller that supplies both is expected to have them agree, and
    trusting whichever one is more permissive when they disagree is exactly
    the hole this closes. An unreadable/missing transcript in that case is
    treated as "can't verify" (None / turn-tagged fallback below), not as
    license to fall back to the unverified inline claim.

    P12-3: checks routing_declared event-log entry first (written by
    event_scan PostToolUse) — O(1) lookup skips transcript scan when found."""
    from transcript_scanner import _transcript_path  # noqa: PLC0415
    if _get_declared_routing(session_id_of(payload), _turn_id(payload)):
        return True
    raw_path = _transcript_path(payload)
    if not raw_path:
        # No stronger verification available — inline fields are the only
        # source, so trust them (this is the pre-existing Cursor fast path).
        for key in ("agent_message", "text", "last_assistant_message"):
            value = payload.get(key)
            if isinstance(value, str) and has_routing_verdict(value):
                return True
        return None
    try:
        path = Path(raw_path)
        if not path.is_file():
            return None
        return _scan_lines_backward(_read_transcript_tail(path))
    except Exception:  # noqa: BLE001 — detection must fail open to the fallback
        return None


def _routing_role_in_current_turn(payload: dict[str, Any]) -> str | None:
    """PROD-18: which role was claimed in the current turn's `Routing:` line,
    if any. Mirrors `_routing_in_current_turn`'s precedence (see PROD-20/
    break-016 note there): inline fields are only trusted when no
    `transcript_path` is present to verify against; otherwise the
    transcript is the sole source, so the gate can check whether the
    claimed role fits the mutation — not just that some role was claimed.

    P12-3: checks routing_declared event-log entry first (written by
    event_scan PostToolUse) — O(1) lookup skips transcript scan when found."""
    from transcript_scanner import _transcript_path  # noqa: PLC0415
    declared = _get_declared_routing(session_id_of(payload), _turn_id(payload))
    if declared:
        return declared
    raw_path = _transcript_path(payload)
    if not raw_path:
        for key in ("agent_message", "text", "last_assistant_message"):
            value = payload.get(key)
            if isinstance(value, str):
                role = _extract_role(value)
                if role:
                    return role
        return None
    try:
        path = Path(raw_path)
        if not path.is_file():
            return None
        return _scan_role_backward(_read_transcript_tail(path), _turn_id(payload))
    except Exception:  # noqa: BLE001 — advisory check must fail open
        return None


def _current_turn_assistant_text(payload: dict[str, Any]) -> str:
    """Return the assistant's full text from the current turn — used by P12-8
    to count file targets in the routing reason.  Mirrors the transcript-first
    precedence of `_routing_role_in_current_turn` so the file count and the
    role come from the exact same source."""
    from transcript_scanner import _transcript_path, _record_content, _record_role, _text_from_content  # noqa: PLC0415
    import json  # noqa: PLC0415
    raw_path = _transcript_path(payload)
    if not raw_path:
        for key in ("agent_message", "text", "last_assistant_message"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    try:
        path = Path(raw_path)
        if not path.is_file():
            return ""
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
                text = _text_from_content(_record_content(record))
                if text and has_routing_verdict(text):
                    return text
    except Exception:
        pass
    return ""


def _current_role(payload: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """Best-effort resolution of the role governing the CURRENT verdict: a
    freshly-detectable inline/transcript match takes precedence (same
    precedence `_routing_role_in_current_turn` already implements), falling
    back to the persisted verdict's own snippet (`last_assistant_snippet`,
    set by the same `scan` call that set `entry['verdict']`) when nothing
    fresh is available. Used by the advisory large-write nudge and the
    PROD-20 FO/RB role/tool-mismatch deny; `None` (can't determine) is
    always a safe no-op for both callers."""
    role = _routing_role_in_current_turn(payload)
    if role:
        return role
    if entry.get("verdict"):
        return _extract_role(str(entry.get("last_assistant_snippet", "")))
    return None


# ---------------------------------------------------------------------------
# Fast-path exemption
# ---------------------------------------------------------------------------

def is_fast_path_exempt(prompt: str) -> bool:
    """True when the ≤3-call fast path applies — gate should not fire."""
    text = prompt.strip()
    if not text:
        return True
    intent = classify_intent(text)
    if intent != "agentic_action":
        return True
    if len(text) > FAST_PATH_MAX_PROMPT_CHARS:
        return False
    lower = text.lower()
    return any(marker in lower for marker in _SIMPLE_MARKERS)


# ---------------------------------------------------------------------------
# Override flags
# ---------------------------------------------------------------------------

def _has_dispatch_override(entry: dict[str, Any]) -> bool:
    # Self-service overrides disabled (P2 audit). Agent text cannot bypass gates.
    # Use !override in user prompt for manual bypass.
    return False


def _has_dispatch_budget_override(*texts: str) -> bool:
    # Self-service overrides disabled (P2 audit). Agent text cannot bypass gates.
    # Use !override in user prompt for manual bypass.
    return False


# ---------------------------------------------------------------------------
# Budget nudge
# ---------------------------------------------------------------------------

def _dispatch_budget_nudge_and_log(
    payload: dict[str, Any],
    task_text: str,
    requested_model: str | None,
    entry: dict[str, Any],
) -> str | None:
    """Run context estimator; persist + log; return advisory nudge if warranted."""
    est = estimate_dispatch_context(task_text, model=requested_model)
    entry["last_dispatch_context_estimate"] = est
    # Count paths extracted from dispatch (safe import with fallback)
    paths_count = 0
    try:
        from split_planner import count_dispatch_paths  # noqa: E402
        paths_count = count_dispatch_paths(task_text)
    except Exception:
        paths_count = 0
    try:
        subagent_id = subagent_id_of(payload)
        if subagent_id is None:
            subagent_id = "n/a"
            # Visibility maintained via log_level/type in the dispatch_context_estimate event (JSONL sink).
            # Removed dual-sink _warn() call to prevent silent divergence between warnings.jsonl and routing_events.jsonl.
        append_event({
            "type": "dispatch_context_estimate",
            "session_id": session_id_of(payload),
            "turn_id": _turn_id(payload),
            "subagent_id": subagent_id,
            "model": requested_model,
            "tier": tier_of_model(requested_model) if requested_model else None,
            "est_tokens": est.get("total_tokens", 0),
            "infra_tokens": est.get("infra_tokens", 0),
            "prompt_tokens": est.get("prompt_tokens", 0),
            "file_tokens": est.get("file_tokens", 0),
            "pct_of_budget": est.get("pct_of_window", 0),
            "over_budget": bool(est.get("over_budget")),
            "estimate_failed": bool(est.get("estimate_failed")),
            "target_hash": hash_target(task_text[:TASK_TARGET_HASH_CHARS]),
            "paths_extracted_count": paths_count,
        })
    except OSError:
        pass
    return budget_nudge_message(est, advisory_only=False, prompt=task_text)


# ---------------------------------------------------------------------------
# Gate threshold checks
# ---------------------------------------------------------------------------

def _check_dispatch_threshold(
    state: dict[str, Any], entry: dict[str, Any], is_claude: bool,
    session_id: str = "",
) -> dict[str, Any] | None:
    """Shared by the MUTATING_TOOLS path and the PROD-19 verification-command
    path below: same thresholds, same nudge/deny messages, same counters —
    only what increments `mechanical_since_dispatch` differs by caller."""
    from _common import DISPATCH_DENY_THRESHOLD, DISPATCH_NUDGE_THRESHOLD  # noqa: PLC0415
    count = int(entry.get("mechanical_since_dispatch", 0))
    expensive = entry.get("tier") == "reasoning"
    if expensive and count >= DISPATCH_DENY_THRESHOLD and not _has_dispatch_override(entry):
        from session_state import _bump, _save_state  # noqa: PLC0415
        from dispatch_enforcer import _deny, _dispatch_deny_message  # noqa: PLC0415
        _bump(state, "gate_deny_dispatch")
        _save_state(state)
        return _deny(is_claude, _dispatch_deny_message(count))
    if expensive and count >= DISPATCH_NUDGE_THRESHOLD:
        if session_id and _dispatch_debt_active(session_id):
            from session_state import _bump, _save_state  # noqa: PLC0415
            _bump(state, "gate_allow_dispatch_nudge_rt_exhausted")
            _save_state(state)
            return None
        from session_state import _bump, _save_state  # noqa: PLC0415
        from dispatch_enforcer import _allow, _dispatch_nudge_message  # noqa: PLC0415
        _bump(state, "gate_nudge_dispatch")
        _save_state(state)
        return _allow(is_claude, _dispatch_nudge_message(count))
    return None


def _check_inline_mutation_threshold(
    state: dict[str, Any], entry: dict[str, Any], is_claude: bool,
    session_id: str = "",
) -> dict[str, Any] | None:
    """Tier-agnostic inline mutation gate — fires on Claude Code (tier=unknown) and Cursor alike.

    Reads the already-incremented inline_mutations_since_dispatch counter.
    Deny at INLINE_MUTATION_DENY_THRESHOLD; nudge at INLINE_MUTATION_NUDGE_THRESHOLD.
    Only called after the verdict check passes, so it never fires on unrouted calls.

    If the round-trip budget is exhausted (no more Task dispatches allowed),
    allow inline edits to avoid a deadlock where both gates deny simultaneously.
    """
    from _common import INLINE_MUTATION_DENY_THRESHOLD, INLINE_MUTATION_NUDGE_THRESHOLD  # noqa: PLC0415
    count = int(entry.get("inline_mutations_since_dispatch", 0))
    if count >= INLINE_MUTATION_DENY_THRESHOLD:
        if session_id and _dispatch_debt_active(session_id):
            from session_state import _bump, _save_state  # noqa: PLC0415
            _bump(state, "gate_allow_inline_mutation_rt_exhausted")
            _save_state(state)
            return None
        from session_state import _bump, _save_state  # noqa: PLC0415
        from dispatch_enforcer import _deny, _inline_mutation_deny_message  # noqa: PLC0415
        _bump(state, "gate_deny_inline_mutation_limit")
        _save_state(state)
        return _deny(is_claude, _inline_mutation_deny_message(count))
    if count >= INLINE_MUTATION_NUDGE_THRESHOLD:
        if session_id and _dispatch_debt_active(session_id):
            from session_state import _bump, _save_state  # noqa: PLC0415
            _bump(state, "gate_allow_inline_mutation_nudge_rt_exhausted")
            _save_state(state)
            return None
        from session_state import _bump, _save_state  # noqa: PLC0415
        from dispatch_enforcer import _deny, _inline_mutation_nudge_message  # noqa: PLC0415
        _bump(state, "gate_deny_inline_mutation_nudge_promoted")
        _save_state(state)
        return _deny(is_claude, _inline_mutation_nudge_message(count, INLINE_MUTATION_DENY_THRESHOLD))
    return None


def _check_shell_gate(
    state: dict[str, Any], entry: dict[str, Any], is_claude: bool,
    session_id: str = "",
) -> dict[str, Any] | None:
    """GATE-3: Shell volume gate — fires on inline Bash/Shell calls without Task dispatch.

    Reads the already-incremented shell_calls_since_dispatch counter.
    Deny at SHELL_DENY_THRESHOLD; nudge at SHELL_NUDGE_THRESHOLD.
    Only called for _SHELL_TOOLS and not subagent context.
    """
    from _common import SHELL_DENY_THRESHOLD, SHELL_NUDGE_THRESHOLD  # noqa: PLC0415
    count = int(entry.get("shell_calls_since_dispatch", 0))
    if count >= SHELL_DENY_THRESHOLD:
        if session_id and _dispatch_debt_active(session_id):
            from session_state import _bump, _save_state  # noqa: PLC0415
            _bump(state, "gate_shell_allow_rt_exhausted")
            _save_state(state)
        else:
            from session_state import _bump, _save_state  # noqa: PLC0415
            from dispatch_enforcer import _deny, _shell_gate_deny_message  # noqa: PLC0415
            _bump(state, "gate_deny_shell_volume")
            _save_state(state)
            return _deny(is_claude, _shell_gate_deny_message(count))
    if count >= SHELL_NUDGE_THRESHOLD:
        from session_state import _bump, _save_state  # noqa: PLC0415
        from dispatch_enforcer import _allow, _shell_gate_nudge_message  # noqa: PLC0415
        _bump(state, "gate_nudge_shell_volume")
        _save_state(state)
        return _allow(is_claude, _shell_gate_nudge_message(count))
    return None


# ---------------------------------------------------------------------------
# Resume gate helpers
# ---------------------------------------------------------------------------

def _check_resume_forced(session_id: str, turn_id: str | None) -> dict | None:
    """Return resume state entry if forced_fo is active for this session+turn.

    Returns None if: no state file, session absent, already cleared, or
    turn_id does not match (auto-lapse — new turn supersedes old forced state).
    """
    try:
        state = load_json_state(RESUME_STATE_FILE)
        sessions = state.get("sessions", {})
        entry = sessions.get(session_id)
        if not isinstance(entry, dict):
            return None
        if entry.get("cleared"):
            return None
        stored_turn = entry.get("forced_fo_turn_id")
        if stored_turn and turn_id and stored_turn != turn_id:
            return None  # auto-lapse: different turn
        return entry
    except Exception:  # noqa: BLE001
        return None


def _clear_resume_forced(session_id: str) -> None:
    """Mark the resume forced_fo state as cleared for this session."""
    try:
        state = load_json_state(RESUME_STATE_FILE)
        sessions = state.get("sessions", {})
        if isinstance(sessions.get(session_id), dict):
            sessions[session_id]["cleared"] = True
            save_json_state(RESUME_STATE_FILE, {"sessions": sessions})
    except Exception:  # noqa: BLE001
        pass


def _increment_resume_denial(session_id: str, count: int) -> None:
    """Bump the denial counter so auto-lapse can trigger after N denials."""
    try:
        state = load_json_state(RESUME_STATE_FILE)
        sessions = state.get("sessions", {})
        if isinstance(sessions.get(session_id), dict):
            sessions[session_id]["denial_count"] = count
            save_json_state(RESUME_STATE_FILE, {"sessions": sessions})
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Internal helper (mirrors check_routing._turn_id without importing it)
# ---------------------------------------------------------------------------

def _turn_id(payload: dict[str, Any]) -> str | None:
    for key in ("generation_id", "generationId", "prompt_id", "promptId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None
