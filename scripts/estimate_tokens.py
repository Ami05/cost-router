#!/usr/bin/env python3
"""cost-router hook: log prompt/tool/response events with estimated token counts.

Observational only — always allows the action. Host field names differ
(Cursor vs Claude Code); `hook_event_name` + `cursor_version` pick the shape.
Claude tier may be unknown when `model` is absent. Privacy: byte length,
est. tokens, tier, tool name, and truncated target hash only — no raw text.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import append_event, hash_target, read_stdin_json, session_id_of, DATA_DIR  # noqa: E402
from _model_utils import estimate_tokens, model_of, tier_of_model, _MODEL_VERSION_PATTERNS  # noqa: E402


def _is_claude_payload(payload: dict) -> bool:
    """Claude when hook_event_name is set and cursor_version is absent."""
    return bool(payload.get("hook_event_name")) and not payload.get("cursor_version")


def _text_for_prompt(payload: dict) -> str:
    for key in ("prompt", "text", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


_ERROR_SUBSTRINGS = ("error", "exception", "not found", "failed")


def _text_for_tool(payload: dict) -> tuple[str, str | None, str | None, bool]:
    """Returns (text_to_estimate, tool_name, target_hash, has_error).

    has_error is True when the raw tool_output string (before json.dumps)
    contains any of the substrings in _ERROR_SUBSTRINGS (case-insensitive).
    Only string tool_output values are checked; dict/other types never set
    has_error.
    """
    tool_name = payload.get("tool_name") or payload.get("tool") or payload.get("toolName")

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = payload.get("toolInput") if isinstance(payload.get("toolInput"), dict) else {}

    # Cursor uses `tool_output`/`result`/`output`; Claude Code's PostToolUse
    # uses `tool_response` for the same concept.
    tool_output = (
        payload.get("tool_output")
        or payload.get("result")
        or payload.get("output")
        or payload.get("tool_response")
    )

    # Detect error signal from the raw string value before any encoding.
    has_error = False
    if isinstance(tool_output, str):
        lower = tool_output.lower()
        has_error = any(sub in lower for sub in _ERROR_SUBSTRINGS)

    target = None
    for key in ("file_path", "path", "target_file", "command"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            target = value
            break

    if isinstance(tool_output, str):
        output_text = tool_output
    elif tool_output:
        output_text = json.dumps(tool_output)
    else:
        output_text = ""

    input_text = json.dumps(tool_input) if tool_input else ""
    combined = input_text + output_text
    return combined, tool_name, hash_target(target), has_error


def _text_for_response(payload: dict) -> tuple[str, dict | None]:
    """Return (response_text, usage_dict_or_None).

    Cursor's afterAgentResponse supplies text directly via the `text` key.
    Claude Code's Stop hook omits both `last_assistant_message` and `usage`
    from the payload — fall back to reading the last assistant turn from the
    transcript file at `transcript_path`.
    """
    for key in ("text", "last_assistant_message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value, None
    # Claude Code fallback: read last assistant entry from transcript JSONL.
    if _is_claude_payload(payload):
        transcript_path = payload.get("transcript_path")
        if isinstance(transcript_path, str) and transcript_path:
            try:
                from pathlib import Path as _Path
                path = _Path(transcript_path)
                if path.is_file():
                    for raw in reversed(path.read_bytes().splitlines()):
                        if not raw.strip():
                            continue
                        try:
                            entry = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if entry.get("role") != "assistant":
                            continue
                        content = entry.get("content", "")
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            text = " ".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        else:
                            text = ""
                        usage = entry.get("usage")
                        return text, usage if isinstance(usage, dict) else None
            except OSError:
                pass
    return "", None


def _text_for_read_file(payload: dict) -> tuple[str, str | None]:
    """Returns (content, target_hash) for a beforeReadFile event.

    Unlike postToolUse (whose `tool_output` is not reliably populated with
    real file content for Read on current Cursor builds — verified: every
    Read/read_file event logged via postToolUse showed a near-constant
    ~200-250 bytes regardless of the file's actual size, i.e. only the
    JSON-encoded tool_input/path was ever being measured), beforeReadFile
    hands over the real absolute file_path and full content directly. This
    is a strictly more accurate source for both token estimation and
    repeat-read detection on Read events specifically.
    """
    content = payload.get("content")
    text = content if isinstance(content, str) else ""
    file_path = payload.get("file_path")
    target_hash = hash_target(file_path) if isinstance(file_path, str) else None
    return text, target_hash


def _normalize_model_key(model: str | None) -> str:
    """Normalize model name to registry key (sonnet_45/46/5) or 'unknown'."""
    if not model:
        return "unknown"
    lower = model.lower()
    for key, pattern in _MODEL_VERSION_PATTERNS.items():
        if pattern.search(lower):
            return key
    return "unknown"


def _write_calibration_record(
    session_id: str,
    model_id: str,
    content_kind: str,
    estimated_tokens: int,
    actual_tokens: int,
) -> None:
    """Append one calibration observation to data/calibration.jsonl."""
    import datetime
    record = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "session_id": session_id,
        "model_id": model_id,
        "content_kind": content_kind,
        "estimated_tokens": estimated_tokens,
        "actual_tokens": actual_tokens,
        "ratio": round(actual_tokens / estimated_tokens, 4) if estimated_tokens else None,
    }
    # Reuse DATA_DIR from _common — same directory as usage.jsonl
    cal_log = DATA_DIR / "calibration.jsonl"
    try:
        with cal_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass  # unwritable — continue without logging


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", choices=("prompt", "tool", "response", "read_file"), required=True)
    args = parser.parse_args()

    payload = read_stdin_json()
    session_id = session_id_of(payload)
    model = model_of(payload)

    record: dict = {
        "type": "tool" if args.event == "read_file" else args.event,
        "session_id": session_id,
        "tier": tier_of_model(model),
        "model": model,
    }
    output: dict = {}
    skip_log = False

    if args.event == "prompt":
        text = _text_for_prompt(payload)
        record["direction"] = "input"
        record["bytes"] = len(text)
        record["est_tokens"] = estimate_tokens(text, model, "prose")
        output = {"continue": True}
        # One-tap explicit feedback: thumbs-up/down emoji anywhere in the prompt.
        thumbs_up = "\U0001f44d" in text
        thumbs_down = "\U0001f44e" in text
        if thumbs_up or thumbs_down:
            append_event({
                "type": "explicit_feedback",
                "session_id": session_id,
                "value": "positive" if thumbs_up else "negative",
                "tier": tier_of_model(model),
                "model": model,
            })
    elif args.event == "tool":
        text, tool_name, target_hash, has_error = _text_for_tool(payload)
        # On Cursor, Read/read_file events are logged more accurately by the
        # beforeReadFile hook (real content + real path, see
        # _text_for_read_file) — skip here to avoid double-counting the same
        # physical read under two different (and differently-sized) records.
        # Claude Code has no beforeReadFile equivalent, so its PostToolUse
        # Read events are the only source and must not be skipped.
        if tool_name in ("Read", "read_file") and not _is_claude_payload(payload):
            skip_log = True
        record["direction"] = "context"
        record["tool_name"] = tool_name
        record["target_hash"] = target_hash
        record["bytes"] = len(text)
        record["est_tokens"] = estimate_tokens(text, model, "code")
        if has_error:
            record["error"] = True
        output = {}
    elif args.event == "read_file":
        text, target_hash = _text_for_read_file(payload)
        record["direction"] = "context"
        record["tool_name"] = "Read"
        record["target_hash"] = target_hash
        record["bytes"] = len(text)
        record["est_tokens"] = estimate_tokens(text, model, "code")
        # Log-only: always allow. This hook exists to fix cost/re-read
        # measurement accuracy, not to block or gate reads.
        output = {"permission": "allow"}
    else:  # response
        if os.environ.get("COST_ROUTER_DEBUG"):
            import datetime as _dt
            _debug_line = json.dumps({
                "ts": _dt.datetime.utcnow().isoformat() + "Z",
                "event": "stop_payload_keys",
                "keys": sorted(payload.keys()),
                "has_text": bool(payload.get("text")),
                "has_last_assistant": bool(payload.get("last_assistant_message")),
                "has_transcript_path": bool(payload.get("transcript_path")),
                "is_claude": _is_claude_payload(payload),
            })
            try:
                with (DATA_DIR / "debug.log").open("a", encoding="utf-8") as _dbg:
                    _dbg.write(_debug_line + "\n")
            except OSError:
                pass
        text, transcript_usage = _text_for_response(payload)
        record["direction"] = "output"
        record["bytes"] = len(text)
        estimated_tokens_heuristic = estimate_tokens(text, model, "prose")
        record["est_tokens"] = estimated_tokens_heuristic
        usage = payload.get("usage") or transcript_usage
        if isinstance(usage, dict) and "input_tokens" in usage:
            record["est_tokens"] = usage.get("output_tokens", 0)
            record["est_input_tokens_real"] = usage.get("input_tokens", 0)
            record["est_cache_read_tokens"] = usage.get("cache_read_input_tokens", 0)
            record["token_source"] = "real"
        else:
            record["token_source"] = "heuristic"

        # Write calibration record when we have real token counts
        if record.get("token_source") == "real":
            _write_calibration_record(
                session_id=session_id,
                model_id=_normalize_model_key(model),
                content_kind="prose",
                estimated_tokens=estimated_tokens_heuristic,
                actual_tokens=record["est_tokens"],  # actual output_tokens
            )
        if _is_claude_payload(payload):
            # Claude's Stop hook has no `additional_context` output field
            # (it uses a top-level "decision" key for blocking, which this
            # hook never sets) — an empty object is the correct no-op.
            output = {}
        else:
            # afterAgentResponse rejects fully empty stdout in some Cursor
            # versions — emit a harmless no-op context field.
            output = {"additional_context": ""}

    should_log = record.get("bytes", 0) > 0 or args.event == "response"
    if not skip_log and should_log:
        try:
            append_event(record)
        except OSError:
            pass  # DATA_DIR unwritable — continue without logging

    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
