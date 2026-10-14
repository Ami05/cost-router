#!/usr/bin/env python3
"""Gate: block Edit/Write/Shell when routing role is FO or RB.

HG-102: FO/RB roles mean "I will dispatch via Task" — calling mutation
tools directly is a contradiction. Small reads (limit <= 50 lines) are
allowed under FO/RB to compose Task prompts; larger reads are denied
with guidance to use a Haiku Explore agent.

Reads the most recent "Routing: <ROLE>" line from the conversation
context (transcript tail).  No Routing line → ALLOW (don't break
sessions without cost-router).

SEM-202: After the regex gate passes (ALLOW), invoke semantic alignment
check — advisory only, never changes gate outcome.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import GateResult, _debug_log, _warn, gate_allow, gate_deny, subagent_id_of  # noqa: E402
from _config import get_thresholds  # noqa: E402
from _model_utils import tool_input_of  # noqa: E402

_ROLE_RE = re.compile(r"Routing:\s*(DIRECT|FO|FW|RB|FV|YIELD)\b", re.IGNORECASE)
_DISPATCH_ROLES = {"FO", "RB"}
_READONLY_ROLES = {"FV"}  # may Read/Shell (lint/check); must not Edit/Write
_YIELD_ROLES = {"YIELD"}
_MUTATION_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "StrReplace"}
_READ_LIMIT_THRESHOLD = get_thresholds()["role_read_max_lines"]
_TRANSCRIPT_TAIL_BYTES = 30_000

# Dispatch-protocol worker types (validate_dispatch_schema.py validates the
# JSON shape at Task-dispatch time; this is the post-dispatch half — once
# the worker subagent is running, its declared `worker` field caps which
# tools it may call directly).
_VALID_WORKER_TYPES = {"read", "shell", "mutation", "create"}
_WORKER_TOOL_ALLOWLIST: dict[str, set[str]] = {
    "read": {"Read", "Grep", "Glob", "Bash"},
    "shell": {"Bash"},
    "mutation": {"Read", "Edit", "Write", "Bash"},
    "create": {"Write", "Read", "Edit"},
}
# Only these tools are ever routed through the pre_write/pre_read chains
# that call this gate — scope enforcement to exactly them so a worker's
# other allowed tools (e.g. a "read" worker's Grep/Glob, which this plugin
# never gates) are never accidentally caught up.
_WORKER_GATED_TOOLS = {"Read", "Write", "Edit", "Bash"}


def _subagent_transcript_path(transcript_path: str, agent_id: str) -> Path | None:
    """Resolve the on-disk file that actually holds *agent_id*'s own turns.

    GATE-BUG-001 (confirmed 2026-09-09 via live PreToolUse payload capture):
    on Claude Code CLI, the hook payload's `transcript_path` ALWAYS points to
    the parent session's top-level `<session_id>.jsonl` file — even for tool
    calls made by a dispatched subagent (Task worker) whose own `agent_id` is
    correctly present in the same payload. A subagent's own turns (including
    the dispatch-prompt "user" turn this module parses for its declared
    `worker` type / routing role) are written to a SEPARATE file at
    `<session_dir>/subagents/agent-<agentId>.jsonl`, which `transcript_path`
    never references. Scanning `transcript_path` alone therefore always
    fails to find the subagent's own dispatch turn, so both
    extract_declared_worker() and extract_role_from_context() fell through
    to their "not found" case for every real dispatch — the worker-tool
    allowlist and (for FO/RB) the role gate silently failed open for every
    subagent on this host.

    Returns the per-agent file Path if it exists, else None — callers must
    still fall back to scanning transcript_path directly, which remains
    correct for any host/format where subagent turns really are interleaved
    inline (e.g. older transcripts, or hosts without this split-file layout).
    """
    try:
        base = Path(transcript_path)
        candidate = base.parent / base.stem / "subagents" / f"agent-{agent_id}.jsonl"
        if candidate.exists():
            return candidate
    except (OSError, ValueError):
        pass
    return None


def extract_role_from_context(payload: dict[str, Any]) -> str | None:
    """Extract the most recent routing role from available context.

    Checks (in order):
      1. payload["routing_role"] — if check_routing.py already parsed it
      2. payload["text"] — inline assistant text
      3. transcript tail scan
    Returns uppercase role string or None if no Routing line found.
    """
    # Direct role field (set by check_routing.py in some flows)
    role = payload.get("routing_role")
    if role and role.upper() in ("DIRECT", "FO", "FW", "RB", "FV"):
        return role.upper()

    # Scan text fields for Routing: line
    for field in ("text", "assistant_text", "last_assistant_message"):
        text = payload.get(field, "")
        if text:
            matches = _ROLE_RE.findall(str(text))
            if matches:
                return matches[-1].upper()

    # Transcript tail (expensive, last resort)
    transcript_path = payload.get("transcript_path", "")
    if transcript_path:
        try:
            current_agent_id = subagent_id_of(payload)
            path = None
            if current_agent_id:
                # GATE-BUG-001: prefer the subagent's own transcript file —
                # see _subagent_transcript_path() docstring.
                path = _subagent_transcript_path(transcript_path, current_agent_id)
            if path is None:
                path = Path(transcript_path)
            if path.exists():
                tail = path.read_bytes()[-50000:]
                text = tail.decode("utf-8", errors="replace")

                # Only scan the transcript when we can key by agentId.
                # Without an agentId, the tail scan is unreliable: sidechain
                # records interleave with parent records, non-JSON lines bypass
                # sidechain filtering, and the 50KB boundary can place a stale
                # FO after a recent DIRECT — causing false role inheritance.
                # The payload text fields above already capture the parent's own
                # role in the common case, so failing open here is correct.
                if not current_agent_id:
                    return None

                import json as _json
                matches = []
                for line in reversed(text.splitlines()):
                    try:
                        rec = _json.loads(line)
                        if rec.get("agentId") == current_agent_id:
                            content = rec.get("message", {}).get("content", "")
                            if isinstance(content, list):
                                content = " ".join(
                                    b.get("text", "") for b in content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                            role_hits = _ROLE_RE.findall(str(content))
                            if role_hits:
                                matches = [role_hits[-1]]
                                break
                        elif rec.get("type") == "user" and not rec.get("isSidechain"):
                            break  # hit a real user turn — stop
                    except Exception:
                        continue

                if matches:
                    return matches[-1].upper()
        except (OSError, UnicodeDecodeError) as e:
            _warn("transcript_read_failed", {"file": "enforce_role_gates.py", "error": str(e)})

    return None


def check_role_gate(payload: dict[str, Any]) -> GateResult:
    """Check that FO/RB roles don't call mutation tools directly."""
    tool_name = payload.get("tool_name", "")
    role = extract_role_from_context(payload)

    # No Routing line → ALLOW (don't break sessions without cost-router)
    if role is None:
        return gate_allow()

    # FV (readonly) + mutation tool → DENY
    if role in _READONLY_ROLES:
        if tool_name in _MUTATION_TOOLS:
            return gate_deny(
                f"Routing: {role} declared — read-only role. Edit/Write mutations are blocked. "
                f"Use Task(...) to dispatch mutations, or re-emit Routing: FW/DIRECT to work inline."
            )
        return gate_allow()

    # DIRECT/FW → ALLOW any tool
    if role not in _DISPATCH_ROLES and role not in _YIELD_ROLES:
        return gate_allow()

    # YIELD → DENY all mutations (step-boundary handoff)
    if role in _YIELD_ROLES:
        if tool_name in _MUTATION_TOOLS or tool_name in ("Bash", "Shell"):
            return gate_deny(
                f"Routing: {role} declared — step-boundary handoff. All mutations blocked for rest of turn. Return findings to orchestrator and resume via new dispatch."
            )
        return gate_allow()

    # FO/RB + mutation tool → DENY
    if tool_name in _MUTATION_TOOLS:
        return gate_deny(
            f"Routing: {role} declared — direct mutations are blocked. Use Task(...) to dispatch, or re-emit Routing: DIRECT to work inline."
        )

    # FO/RB + Read: check limit
    if tool_name in ("Read", "read_file"):
        tool_input = payload.get("tool_input", {})
        if isinstance(tool_input, dict):
            limit = tool_input.get("limit")
            if limit is None or (isinstance(limit, (int, float)) and limit > _READ_LIMIT_THRESHOLD):
                return gate_deny(
                    f"Use Haiku Explore agent for reads >{_READ_LIMIT_THRESHOLD} lines. "
                    f"{role} role should dispatch reads via Task()."
                )
        return gate_allow()

    # FO/RB + Bash/Shell → DENY
    if tool_name in ("Bash", "Shell"):
        return gate_deny(
            f"Routing: {role} declared — direct mutations are blocked. Use Task(...) to dispatch, or re-emit Routing: DIRECT to work inline."
        )

    return gate_allow()


def check_direct_new_file_gate(payload: dict[str, Any]) -> GateResult:
    """Deny Write/Edit on a non-existent file when role is DIRECT.

    New file creation is codegen work — it requires Routing: FW, not DIRECT.
    Fail-open: if file_path cannot be resolved from the payload, allow.
    """
    role = extract_role_from_context(payload)
    if role != "DIRECT":
        return gate_allow()

    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Write", "Edit"):
        return gate_allow()

    tool_input = tool_input_of(payload)
    file_path = tool_input.get("path") or tool_input.get("file_path") or tool_input.get("target_file")
    if not file_path:
        return gate_allow()  # fail-open

    if Path(file_path).exists():
        return gate_allow()

    return gate_deny("New file creation is codegen — re-emit Routing: FW before Write.")


# ---------------------------------------------------------------------------
# Post-dispatch worker tool allowlist
# ---------------------------------------------------------------------------


def _log_debug_worker_allowlist(
    payload: dict[str, Any],
    current_agent_id: str | None,
    transcript_path: str,
    resolved_worker: str | None,
    reason: str,
) -> None:
    """GATE-BUG-001 diagnostic logging for extract_declared_worker().

    Opt-in only: writes one line to data/debug.log via _common._debug_log,
    which no-ops unless COST_ROUTER_DEBUG is set — matching the rest of the
    plugin's debug-logging convention. (Previously wrote an always-on,
    unconditional JSONL file used to root-cause GATE-BUG-001; now that the
    root cause is fixed, that file would just be noise in production.)

    Fail-open by design: never raises, never affects gate behavior.
    """
    try:
        path_obj = Path(transcript_path) if transcript_path else None
        exists = bool(path_obj and path_obj.exists())
        _debug_log(
            f"worker_allowlist tool={payload.get('tool_name', '')} "
            f"agent_id={current_agent_id} transcript_path={transcript_path} "
            f"transcript_exists={exists} resolved_worker={resolved_worker} reason={reason}"
        )
    except Exception:
        pass  # logging must never break the gate


def _scan_transcript_for_worker(path: Path, current_agent_id: str) -> tuple[str | None, str]:
    """Scan one transcript file for *current_agent_id*'s dispatch turn.

    Returns (resolved_worker_or_None, reason). reason == "no_dispatch_turn_found"
    means the caller should try another candidate file (if any) before giving up.
    """
    import json as _json

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = _json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict) or rec.get("agentId") != current_agent_id:
            continue  # another agent's record, or a sidechain line with no agentId

        message = rec.get("message")
        message = message if isinstance(message, dict) else {}
        role = rec.get("type") or message.get("role")
        if role != "user":
            continue  # keep scanning for this agent's own dispatch turn

        content = message.get("content", rec.get("content", ""))
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        content = str(content).strip()

        try:
            data = _json.loads(content)
        except Exception:
            # Thin "Run: ..." dispatches (the <200-token exemption) are
            # valid but not worker-schema JSON. Resolve them to the shell
            # worker instead of falling through to fail-open None, which
            # previously let such a dispatch call Edit/Write unrestricted.
            if content.strip().lower().startswith("run:"):
                return "shell", "implicit_shell_thin_dispatch"
            return None, "first_dispatch_turn_not_json"

        worker = data.get("worker") if isinstance(data, dict) else None
        resolved = worker if worker in _VALID_WORKER_TYPES else None
        return resolved, ("worker_resolved" if resolved else "worker_field_missing_or_invalid")

    return None, "no_dispatch_turn_found"


def extract_declared_worker(payload: dict[str, Any]) -> str | None:
    """Find the `worker` type this agent was dispatched with.

    Scans this agent's own transcript lines — filtered by its own
    agentId, exactly like extract_role_from_context — for the earliest
    "user" turn belonging to this agent, i.e. the dispatch prompt the
    Task() caller sent it. That turn's text is parsed as the JSON dispatch
    payload (scripts/docs/dispatch-schemas.json). Returns the worker string
    ("read"|"shell"|"mutation"|"create"), or None.

    GATE-BUG-001 (fixed 2026-09-09): on Claude Code CLI, `payload["transcript_path"]`
    always points to the PARENT session's top-level .jsonl file, never to the
    dispatched subagent's own turns — those live in a separate
    `<session_dir>/subagents/agent-<agentId>.jsonl` file (see
    _subagent_transcript_path() for the full finding). This function tries
    that per-agent file FIRST (where it exists) and only falls back to
    scanning transcript_path directly for hosts/formats where subagent
    turns really are interleaved inline.

    Fail-open, by design, in every one of these cases:
      - no agentId on the payload (this is the parent session's own tool
        call, not a dispatched worker — never gated here)
      - no transcript_path, or neither candidate file can be read
      - the agent's first user turn isn't valid worker-schema JSON (e.g.
        a thin dispatch under the 200-token exemption, or a worker
        dispatched before this schema existed)
    """
    current_agent_id = subagent_id_of(payload)
    if not current_agent_id:
        _log_debug_worker_allowlist(payload, current_agent_id, payload.get("transcript_path", ""), None, "no_agent_id")
        return None

    transcript_path = payload.get("transcript_path", "")
    if not transcript_path:
        _log_debug_worker_allowlist(payload, current_agent_id, transcript_path, None, "no_transcript_path")
        return None

    candidates: list[Path] = []
    per_agent_path = _subagent_transcript_path(transcript_path, current_agent_id)
    if per_agent_path is not None:
        candidates.append(per_agent_path)
    main_path = Path(transcript_path)
    if main_path.exists():
        candidates.append(main_path)

    if not candidates:
        _log_debug_worker_allowlist(payload, current_agent_id, transcript_path, None, "transcript_not_found")
        return None

    last_reason = "transcript_not_found"
    for path in candidates:
        try:
            resolved, reason = _scan_transcript_for_worker(path, current_agent_id)
        except (OSError, UnicodeDecodeError) as e:
            _warn("transcript_read_failed", {"file": "enforce_role_gates.py", "error": str(e)})
            last_reason = f"transcript_read_error:{e}"
            continue
        if reason != "no_dispatch_turn_found":
            _log_debug_worker_allowlist(payload, current_agent_id, str(path), resolved, reason)
            return resolved
        last_reason = "no_dispatch_turn_found_for_agent"

    _log_debug_worker_allowlist(payload, current_agent_id, transcript_path, None, last_reason)
    return None


def check_worker_tool_allowlist(payload: dict[str, Any]) -> GateResult:
    """Deny tool calls outside the declared worker type's allowlist.

    validate_dispatch_schema.py validates dispatch JSON shape before
    Task() fires; this is the other half — once the worker is running,
    calls that fall outside its declared worker type's tool set are
    denied. Skips entirely (allow) when no worker field is found, which
    covers the parent session's own tool calls (no agentId → no worker).
    """
    worker = extract_declared_worker(payload)
    current_agent_id = subagent_id_of(payload)
    transcript_path = payload.get("transcript_path", "")
    tool_name = payload.get("tool_name", "")

    if worker is None:
        _log_debug_worker_allowlist(payload, current_agent_id, transcript_path, worker, "allow_no_worker_declared")
        return gate_allow()

    if tool_name not in _WORKER_GATED_TOOLS:
        _log_debug_worker_allowlist(payload, current_agent_id, transcript_path, worker, "allow_tool_not_gated")
        return gate_allow()

    allowed = _WORKER_TOOL_ALLOWLIST.get(worker, set())
    if tool_name not in allowed:
        _log_debug_worker_allowlist(payload, current_agent_id, transcript_path, worker, "deny_tool_outside_allowlist")
        return gate_deny(
            f"Declared worker={worker!r} may only use {sorted(allowed)} — "
            f"{tool_name} is out of scope for this dispatch."
        )
    _log_debug_worker_allowlist(payload, current_agent_id, transcript_path, worker, "allow_tool_in_allowlist")
    return gate_allow()


# ---------------------------------------------------------------------------
# SEM-202: Semantic role alignment advisory
# ---------------------------------------------------------------------------


def _last_assistant_text(payload: dict[str, Any]) -> str:
    """Extract last assistant text from payload fields or transcript tail.

    Tries inline fields first (cheap), falls back to reading the last
    assistant turn from the transcript JSONL (same file extract_role_from_context
    already reads successfully for role detection).
    """
    for field in ("text", "assistant_text", "last_assistant_message"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value

    transcript_path = payload.get("transcript_path", "")
    if not transcript_path:
        return ""

    try:
        import json as _json
        path = Path(transcript_path)
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _TRANSCRIPT_TAIL_BYTES:
                fh.seek(-_TRANSCRIPT_TAIL_BYTES, 2)
                data = fh.read()
                nl = data.find(b"\n")
                if nl != -1:
                    data = data[nl + 1:]
            else:
                data = fh.read()
        for raw_line in reversed(data.decode("utf-8", "replace").splitlines()):
            if not raw_line.strip():
                continue
            try:
                rec = _json.loads(raw_line)
            except _json.JSONDecodeError:
                continue
            role = rec.get("role") or (rec.get("message", {}) or {}).get("role", "")
            if role != "assistant":
                continue
            content = rec.get("content") or (rec.get("message", {}) or {}).get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            return ""
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def _check_semantic_alignment(
    role: str,
    text: str,
    log_path: "Path | None" = None,
) -> None:
    """Run semantic alignment check and log misalignment — advisory only.

    Never raises. Never changes gate decisions.
    """
    if not text or not text.strip():
        return
    from semantic_scorer import SemanticScorer  # noqa: PLC0415
    try:
        scorer = SemanticScorer.get_instance()
        result = scorer.score_role_alignment(role, text)

        if not result.get("aligned", True):
            _log_alignment_mismatch(role=role, text=text, result=result, log_path=log_path)
    except (OSError, ValueError, KeyError) as e:  # advisory must never break the gate
        _warn("semantic_role_check_failed", {"file": "enforce_role_gates.py", "error": str(e)})


def _log_alignment_mismatch(
    role: str,
    text: str,
    result: dict,
    log_path: "Path | None" = None,
) -> None:
    """Write a JSONL entry for role/text semantic misalignment."""
    import hashlib
    import json
    import time
    from pathlib import Path as _Path

    _DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
    _ALIGNMENT_LOG = _DATA_DIR / "semantic_role_alignment.jsonl"
    path = log_path or _ALIGNMENT_LOG

    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "role_alignment_mismatch",
        "role": role,
        "input_text_hash": hashlib.sha256(text.encode()).hexdigest()[:16],
        "aligned": result.get("aligned", False),
        "confidence": round(float(result.get("confidence", 0.0)), 4),
        "reason": result.get("reason", ""),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def event_pre_write(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Gate function for pre_write chain."""
    from _common import emit_gate_result  # noqa: E402

    gate_result = check_role_gate(payload)
    direct_new_file_denied = False
    if gate_result.decision == "allow":
        gate_result = check_direct_new_file_gate(payload)
        if gate_result.decision == "deny":
            direct_new_file_denied = True

    worker_denied = False
    if gate_result.decision == "allow":
        gate_result = check_worker_tool_allowlist(payload)
        if gate_result.decision == "deny":
            worker_denied = True

    # SEM-202: advisory alignment check on ALLOW only — never on DENY
    if gate_result.decision == "allow":
        role = extract_role_from_context(payload)
        if role:
            text = _last_assistant_text(payload)
            _check_semantic_alignment(role, text)

    result = emit_gate_result(gate_result, is_claude)
    if direct_new_file_denied:
        # Signal to run_gate which internal sub-gate fired so the correct
        # event type ("direct_codegen_misroute") is logged to session_log.jsonl.
        # run_gate pops this key before returning to the caller.
        result["_gate_name_override"] = "direct_new_file_gate"
    elif worker_denied:
        result["_gate_name_override"] = "worker_tool_allowlist_gate"
    return result


def event_pre_read(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Gate function for pre_read chain."""
    from _common import emit_gate_result  # noqa: E402

    gate_result = check_role_gate(payload)

    worker_denied = False
    if gate_result.decision == "allow":
        gate_result = check_worker_tool_allowlist(payload)
        if gate_result.decision == "deny":
            worker_denied = True

    # SEM-202: advisory alignment check on ALLOW only — never on DENY
    if gate_result.decision == "allow":
        role = extract_role_from_context(payload)
        if role:
            text = _last_assistant_text(payload)
            _check_semantic_alignment(role, text)

    result = emit_gate_result(gate_result, is_claude)
    if worker_denied:
        result["_gate_name_override"] = "worker_tool_allowlist_gate"
    return result
