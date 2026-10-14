#!/usr/bin/env python3
"""PreToolUse:Read gate — limits inline reads per turn.

GATE-1 (verdict-based thresholds):
  - DIRECT verdict: nudge at 5, deny at 7
  - All other verdicts (FO, FW, RB, etc.): nudge at 3, deny at 4

GATE-2 (post-mutation read pressure):
  - When inline_mutations_since_dispatch >= 1: override thresholds to nudge at 1, deny at 3

Resets on new UserPromptSubmit or any Task dispatch.

Exemptions (do not count the read):
  - subagent context (agent_id / subagent_id present in payload)
  - image or PDF file extensions
  - file < 100 lines (fail open if unreadable)
  - path already read this turn (path dedup, normalized)

State: data/read_gate_state.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_DIR,
    emit_gate_result,
    gate_allow,
    gate_deny,
    load_json_state,
    read_stdin_json,
    save_json_state,
    session_id_of,
)
from _model_utils import is_claude_payload, tool_input_of  # noqa: E402

READ_GATE_STATE_FILE = DATA_DIR / "read_gate_state.json"


def _dispatch_debt_active(session_id: str) -> bool:
    """Check if dispatch debt is active (cost-based escape hatch) for this session."""
    import check_routing  # late import to avoid circular
    try:
        return check_routing._dispatch_debt_active(session_id)
    except Exception:  # noqa: BLE001 — fail open
        return False


def _bump_debt_check_counter() -> None:
    """Increment read_gate_debt_check_count in routing_state.json so that
    whether these escape-hatch sites ever fire is observable in telemetry."""
    routing_state_file = DATA_DIR / "routing_state.json"
    try:
        state = load_json_state(routing_state_file)
        counters = state.setdefault("counters", {})
        counters["read_gate_debt_check_count"] = int(counters.get("read_gate_debt_check_count", 0)) + 1
        save_json_state(routing_state_file, state)
    except Exception:  # noqa: BLE001 — observability only, never block the gate
        pass


NUDGE_THRESHOLD = 3
DENY_THRESHOLD = 4

# GATE-1: DIRECT verdict — tightened to match FO thresholds; re-classify to FO for 3+ file reads
DIRECT_NUDGE_THRESHOLD = 3
DIRECT_DENY_THRESHOLD = 5

# GATE-2: post-mutation read pressure (tighter thresholds when mutations have occurred)
POST_MUTATION_NUDGE_THRESHOLD = 1
POST_MUTATION_DENY_THRESHOLD = 3

MAX_READ_PATHS = 500  # cap session path dedup set to avoid unbounded growth

ADJACENT_THRESHOLD = 5  # lines — ranges within this distance are treated as overlapping

IMAGE_PDF_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf",
    ".svg", ".bmp", ".ico", ".tiff",
}

_DENY_MSG = (
    "Inline read limit reached (4 reads this turn without Task dispatch).\n"
    "Dispatch a Haiku Explore worker instead:\n"
    "  Agent(model=\"haiku\", subagent_type=\"Explore\", prompt=\"Read <files>...\")\n"
    "Pass file excerpts in CONTEXT: block to FW workers."
)

_NUDGE_MSG = (
    "[read-gate] 3rd inline read this turn. "
    "Next read will be denied — dispatch a Haiku Explore worker."
)

_DIRECT_NUDGE_MSG = (
    "DIRECT task has read {n} files — re-classify as FO and dispatch remaining reads "
    "to a Haiku Explore worker"
)

_POST_MUTATION_NUDGE_MSG = (
    'Read after mutation detected — dispatch a Haiku worker to investigate: '
    'Agent(model="haiku", subagent_type="Explore", ...)'
)


def _is_subagent_context(payload: dict[str, Any]) -> bool:
    """True when the read originates from a dispatched Task worker."""
    for key in (
        "agent_id", "agent_type", "subagent_id",
        "subagent_type", "parent_conversation_id",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return True
    return payload.get("is_parallel_worker") is True


def _file_path_of(payload: dict[str, Any]) -> str | None:
    """Extract the file path being read from the hook payload."""
    tool_input = tool_input_of(payload)
    for key in ("path", "file_path", "target_file"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_image_or_pdf(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in IMAGE_PDF_EXTS


def _is_small_file(file_path: str) -> bool:
    """True if file has fewer than 100 lines. Fail open (return True) on error."""
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        return text.count("\n") < 100
    except OSError:
        return True  # fail open: treat as small, do not count


def _load_mutation_count() -> int:
    """Return inline_mutations_since_dispatch for the current session.

    Reads routing_state.json and finds the session with the highest turn count,
    or the session identified by CLAUDE_SESSION_ID / COST_ROUTER_SESSION_ID env vars.
    Returns 0 on any exception (fail-open).
    """
    import os
    try:
        routing_state_path = DATA_DIR / "routing_state.json"
        text = routing_state_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(text)
        sessions = data.get("sessions", {})
        if not sessions:
            return 0

        # Check env vars first for explicit session identification
        session_id = (
            os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("COST_ROUTER_SESSION_ID")
        )
        if session_id and session_id in sessions:
            sess = sessions[session_id]
            return int(sess.get("inline_mutations_since_dispatch", 0))

        # No matching session → no mutations tracked for this session
        return 0
    except Exception:  # noqa: BLE001 — fail open
        return 0


def _load_state() -> dict[str, Any]:
    state = load_json_state(READ_GATE_STATE_FILE)
    # Normalise missing fields
    if "read_count" not in state:
        state["read_count"] = 0
    if "read_paths" not in state:
        state["read_paths"] = []
    if "last_routing_verdict" not in state:
        state["last_routing_verdict"] = "DIRECT"
    if "task_dispatched_in_turn" not in state:
        state["task_dispatched_in_turn"] = False
    if "read_ranges" not in state:
        state["read_ranges"] = {}  # resolved_path -> list of [start, end] pairs
    return state


def _reset(payload: dict[str, Any]) -> None:  # noqa: ARG001
    state = _load_state()
    state["read_count"] = 0
    state["read_paths"] = []
    state["task_dispatched_in_turn"] = False
    state["read_ranges"] = {}
    save_json_state(READ_GATE_STATE_FILE, state)
    # No output needed — reset hooks just allow silently


def _gate(payload: dict[str, Any]) -> None:
    is_claude = is_claude_payload(payload)

    try:
        result = _evaluate(payload)
    except Exception:  # noqa: BLE001 — hook must fail open
        result = gate_allow(None)

    print(json.dumps(emit_gate_result(result, is_claude)))
    if not is_claude and result.decision == "deny":
        sys.exit(2)


def _evaluate(payload: dict[str, Any]):
    # Exemption 1: subagent context
    if _is_subagent_context(payload):
        return gate_allow(None)

    file_path = _file_path_of(payload)

    # Exemption 2: image/PDF
    if file_path and _is_image_or_pdf(file_path):
        return gate_allow(None)

    # Exemption 3: small file (<100 lines)
    if file_path and _is_small_file(file_path):
        return gate_allow(None)

    state = _load_state()

    # Exemption 4: path already seen this turn (dedup by resolved path)
    resolved = None
    if file_path:
        try:
            resolved = str(Path(file_path).resolve())
        except (OSError, ValueError):
            resolved = file_path

    read_paths: list[str] = state["read_paths"]
    if resolved and resolved in read_paths:
        return gate_allow(None)

    # Exemption 5 / overlap check: deny if new range overlaps or is adjacent to a stored range
    tool_input = tool_input_of(payload)
    raw_offset = tool_input.get("offset")
    raw_limit = tool_input.get("limit")
    if resolved and raw_offset is not None and raw_limit is not None:
        try:
            new_start = int(raw_offset)
            new_end = int(raw_offset) + int(raw_limit)
            read_ranges: dict[str, list[list[int]]] = state.get("read_ranges", {})
            for stored_start, stored_end in read_ranges.get(resolved, []):
                # Overlap or within ADJACENT_THRESHOLD lines
                if new_start <= stored_end + ADJACENT_THRESHOLD and new_end >= stored_start - ADJACENT_THRESHOLD:
                    short_name = Path(resolved).name
                    return gate_deny(
                        f"[read-gate] Adjacent/overlapping re-read blocked: "
                        f"{short_name} lines {new_start}-{new_end} overlaps already-read "
                        f"range {stored_start}-{stored_end}. Use existing context or dispatch "
                        f"a Haiku Explore worker for broader reads."
                    )
        except (TypeError, ValueError):
            pass  # fail open if values are not coercible to int

    # Count this read
    count: int = state["read_count"] + 1
    state["read_count"] = count

    if resolved:
        read_paths.append(resolved)
        # Cap session path set
        if len(read_paths) > MAX_READ_PATHS:
            read_paths = read_paths[-MAX_READ_PATHS:]
        state["read_paths"] = read_paths

        # Track line range if offset+limit were provided
        try:
            r_offset = tool_input.get("offset")
            r_limit = tool_input.get("limit")
            if r_offset is not None and r_limit is not None:
                range_start = int(r_offset)
                range_end = int(r_offset) + int(r_limit)
                read_ranges = state.get("read_ranges", {})
                ranges_for_path: list[list[int]] = read_ranges.get(resolved, [])
                ranges_for_path.append([range_start, range_end])
                read_ranges[resolved] = ranges_for_path
                state["read_ranges"] = read_ranges
        except (TypeError, ValueError):
            pass  # fail open

    save_json_state(READ_GATE_STATE_FILE, state)

    # Resolve session_id for RT budget check
    sid = session_id_of(payload) or ""

    # GATE-2: post-mutation pressure overrides everything
    mutation_count = _load_mutation_count()
    if mutation_count >= 1:
        if count >= POST_MUTATION_DENY_THRESHOLD:
            if sid and _dispatch_debt_active(sid):
                _bump_debt_check_counter()
                return gate_allow(None)
            return gate_deny(_POST_MUTATION_NUDGE_MSG)
        if count >= POST_MUTATION_NUDGE_THRESHOLD:
            if sid and _dispatch_debt_active(sid):
                _bump_debt_check_counter()
                return gate_allow(None)
            return gate_allow(_POST_MUTATION_NUDGE_MSG)
        return gate_allow(None)

    # GATE-1: verdict-based thresholds
    verdict = state.get("last_routing_verdict", "")
    if verdict == "DIRECT":
        nudge_threshold = DIRECT_NUDGE_THRESHOLD
        deny_threshold = DIRECT_DENY_THRESHOLD
        nudge_msg = _DIRECT_NUDGE_MSG.format(n=count)
    else:
        nudge_threshold = NUDGE_THRESHOLD
        deny_threshold = DENY_THRESHOLD
        nudge_msg = _NUDGE_MSG

    if count >= deny_threshold:
        if sid and _dispatch_debt_active(sid):
            _bump_debt_check_counter()
            return gate_allow(None)
        return gate_deny(_DENY_MSG)
    if count >= nudge_threshold:
        return gate_allow(nudge_msg)
    return gate_allow(None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default="gate")
    args = parser.parse_args()

    payload = read_stdin_json()

    if args.event == "reset":
        _reset(payload)
        return 0

    _gate(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
