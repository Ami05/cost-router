#!/usr/bin/env python3
"""Gate: SPO-007 — enforce JSON Structural Dispatch Protocol on Agent/Task prompts.

Registered in gates.py under the pre_task chain (runs after enforce_explicit_model).

Checks 1-5 bail on first failure (each depends on the previous having passed —
e.g. content checks are meaningless until the prompt is confirmed valid JSON
with a known worker and its required fields). Checks 6-8 (content/token checks) 
are independent, so they are all evaluated and reported together in a single 
denial — a prompt with 3 separate content problems gets 1 denial listing all 3, 
not 3 sequential denials discovered one round-trip at a time. Check 6 
(thin dispatch exemption) happens after content validation so small invalid 
JSON payloads are still rejected. Check 8 (token ceiling) only applies to 
larger dispatches.
  1. Token count < 200 (non-JSON only) → pass (thin dispatch exemption)
  2. Triple backtick present → hard block (code_block), checked before JSON parse
  3. json.loads fails   → hard block (not_json)
  4. data["worker"] not in ["mutation","read","create","shell"] → hard block (unknown_worker)
  5. Required fields missing for worker type → hard block (missing_fields)
  6. Content checks (constraints, examples, where/why) → hard block (schema_stuffing, invalid_examples, invalid_field_value)
     Orchestrator-only: `step` implying oversized scope (20+ files, 3+ artifact
     types, or combined-redesign signal) → hard block (orchestrator_oversized_scope,
     TICKET-036)
  7. Token count < 200 (JSON payloads) → pass (thin dispatch exemption)
  8. Token count exceeds per-worker ceiling → hard block (token_limit)
     - mutation with examples field: max 650 tokens
     - mutation without examples field: max 560 tokens
     - read: max 650 tokens
     - create/shell: max 560 tokens
  8. constraints present: >5 items OR any item >60 chars OR item contains chars outside [a-zA-Z0-9 -,.]
     OR any word not in the closed vocabulary at config/constraint_allowlist.json (case-insensitive)
     → hard block (schema_stuffing), collected with 9 below
  9. examples field (mutation only): must be list or None, max 3 {before, after} dicts,
     each string ≤400 chars, backticks auto-stripped before validation → hard block (invalid_examples) only for other violations, collected with 8 below
     where/why (mutation only): where ≤170 chars, why ≤150 chars → hard block (invalid_field_value), collected with 8/9

Token count approximation: len(prompt) // 4

Every block embeds the gate_template from dispatch-schemas.json.
Every block logs {subtype, session_id, ts} to data/dispatch_denials.jsonl
(no prompt content, tokens, or rejected values — category only). Mutation-worker
denials additionally log a bounded diagnosis-language signal (matched keyword
only, e.g. "figure out" — never the pattern/where/why text itself).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import GateResult, DATA_DIR, gate_allow, gate_deny  # noqa: E402

# Shared pure-check helpers — extracted to _validate_shared so schema_validators
# package can import them without depending on this gate module.
from _validate_shared import check_constraints as _check_constraints  # noqa: E402
from _validate_shared import _ALLOWLIST_PATH  # noqa: E402, F401
from _validate_shared import _CONSTRAINT_ALLOWLIST  # noqa: E402, F401
from _validate_shared import check_examples as _check_examples  # noqa: E402
from _validate_shared import check_field_values as _check_field_values  # noqa: E402
from _validate_shared import suggest_allowlist_matches as _suggest_allowlist_matches  # noqa: E402

# Per-worker validator registry — each module exposes validate(schema)->ValidationResult.
from schema_validators import SCHEMA_VALIDATORS  # noqa: E402

# ── gate templates — loaded from scripts/docs/dispatch-schemas.json ───────────
# The JSON file is the single source of truth. Fallback strings are used only
# when the file is missing or malformed so the gate never fails closed.

_SCHEMA_FILE = Path(__file__).parent / "docs" / "dispatch-schemas.json"

_MUTATION_TEMPLATE_FALLBACK = (
    '{"worker":"mutation","intent":"<one-phrase task>",'
    '"files":["path/to/file.py:line_or_range"],'
    '"pattern":"<1-2 sentence transformation description>",'
    '"constraints":["<hard constraint>"],'
    '"output":"targeted_edits"}'
)
_READ_TEMPLATE_FALLBACK = (
    '{"worker":"read","goal":"<what to find or understand>",'
    '"scope":["path/to/search/"],'
    '"return":["function signatures","file:line references"],'
    '"max_files":10}'
)
_CREATE_TEMPLATE_FALLBACK = (
    '{"worker":"create","intent":"<what to create>",'
    '"file_path":"<path/to/new/file>",'
    '"template_ref":"<optional: path to format reference>",'
    '"fields":{"<key>":"<value>"}}'
)
_SHELL_TEMPLATE_FALLBACK = (
    '{"worker":"shell","intent":"<what the command does>",'
    '"command":"<shell command to run>",'
    '"expect":"exit_code|stdout|stderr|all"}'
)
_ORCHESTRATOR_TEMPLATE_FALLBACK = (
    '{"worker":"orchestrator","intent":"<one-phrase overall goal>",'
    '"step":"<single-phase description: what this dispatch does — one phase only, max_phases_per_dispatch: 1>",'
    '"model":"sonnet"}'
)

try:
    _schema_data = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))
    _MUTATION_TEMPLATE: str = _schema_data["gate_templates"]["mutation"]
    _READ_TEMPLATE: str = _schema_data["gate_templates"]["read"]
    _CREATE_TEMPLATE: str = _schema_data["gate_templates"]["create"]
    _SHELL_TEMPLATE: str = _schema_data["gate_templates"]["shell"]
    _ORCHESTRATOR_TEMPLATE: str = _schema_data["gate_templates"].get(
        "orchestrator", _ORCHESTRATOR_TEMPLATE_FALLBACK
    )
except Exception:  # noqa: BLE001
    # Fail open: a missing or malformed schema file must not block all dispatches.
    _MUTATION_TEMPLATE = _MUTATION_TEMPLATE_FALLBACK
    _READ_TEMPLATE = _READ_TEMPLATE_FALLBACK
    _CREATE_TEMPLATE = _CREATE_TEMPLATE_FALLBACK
    _SHELL_TEMPLATE = _SHELL_TEMPLATE_FALLBACK
    _ORCHESTRATOR_TEMPLATE = _ORCHESTRATOR_TEMPLATE_FALLBACK

_MUTATION_REQUIRED = {"intent", "files", "pattern", "output"}
_READ_REQUIRED = {"goal", "scope", "return"}
_CREATE_REQUIRED = {"intent", "file_path"}
_SHELL_REQUIRED = {"intent", "command", "expect"}
_ORCHESTRATOR_REQUIRED = {"intent", "step"}
_VALID_WORKERS = ("mutation", "read", "create", "shell", "orchestrator")
_WORKER_CONFIG = {
    "mutation": (_MUTATION_REQUIRED, _MUTATION_TEMPLATE),
    "read": (_READ_REQUIRED, _READ_TEMPLATE),
    "create": (_CREATE_REQUIRED, _CREATE_TEMPLATE),
    "shell": (_SHELL_REQUIRED, _SHELL_TEMPLATE),
    "orchestrator": (_ORCHESTRATOR_REQUIRED, _ORCHESTRATOR_TEMPLATE),
}


# ── helpers ───────────────────────────────────────────────────────────────────


def _token_count(text: str) -> int:
    return len(text) // 4


_DIAGNOSIS_PHRASE_RE = re.compile(
    r"\b(figure out|diagnose|investigate why|find (?:the )?(?:root )?cause|"
    r"root cause|not sure (?:what|why)|unclear why|determine why)\b",
    re.IGNORECASE,
)


def _detect_diagnosis_phrase(data: dict) -> str | None:
    """Bounded signal only: returns the matched phrase, never the field text
    it was found in. Used to measure how often mutation dispatches read as
    diagnosis-shaped, without persisting the pattern/where/why content itself.
    """
    if data.get("worker") != "mutation":
        return None
    haystack = " ".join(
        str(data.get(field, "")) for field in ("pattern", "where", "why")
    )
    match = _DIAGNOSIS_PHRASE_RE.search(haystack)
    return match.group(0).lower() if match else None


def _log_violation(
    subtype: str,
    session_id: str,
    diagnosis_phrase: str | None = None,
) -> None:
    """Append a denial record to data/dispatch_denials.jsonl. Never raises.

    Deliberately excludes prompt content, tokens, and any rejected-value
    snippet — only the denial category, session id, and timestamp are
    persisted, so this file is safe to aggregate/report on without exposing
    what anyone actually typed. The one exception is `diagnosis_phrase`: a
    matched keyword (e.g. "figure out"), never the pattern/where/why text it
    was found in — enough to measure frequency without logging content.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "subtype": subtype,
            "session_id": session_id,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if diagnosis_phrase:
            record["diagnosis_matched"] = True
            record["diagnosis_phrase"] = diagnosis_phrase
        denials_log = DATA_DIR / "dispatch_denials.jsonl"
        with denials_log.open("ab") as fh:
            fh.write((json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001
        pass  # logging must never crash the gate



def _block(
    subtype: str,
    reason: str,
    template: str,
    session_id: str,
    data: dict | None = None,
) -> GateResult:
    _log_violation(subtype, session_id, _detect_diagnosis_phrase(data) if data else None)
    msg = (
        f"[dispatch-schema] BLOCKED ({subtype}): {reason}\n"
        f"Use the correct JSON dispatch schema:\n{template}"
    )
    return gate_deny(msg)


# ── public API ────────────────────────────────────────────────────────────────

def validate_dispatch_schema(prompt: str, session_id: str = "") -> GateResult:
    """Run SPO-007 checks. Returns GateResult(allow) or GateResult(deny)."""
    tokens = _token_count(prompt)

    # 1. Thin dispatch exemption
    if tokens < 200:
        return gate_allow()

    # 2. Code-block detection (before JSON parse)
    if "```" in prompt:
        return _block("code_block",
                      "prompt contains triple backticks (code block). Use JSON dispatch schema.",
                      _MUTATION_TEMPLATE, session_id)

    # 3. Must be valid JSON (check before token limit so we can apply per-worker ceiling)
    try:
        data = json.loads(prompt)
    except (json.JSONDecodeError, ValueError):
        prose_snippet = prompt[:80].replace("\n", " ")
        return _block("not_json",
                      f"prompt is not valid JSON. Detected prose: {prose_snippet!r}",
                      _MUTATION_TEMPLATE, session_id)

    if not isinstance(data, dict):
        prose_snippet = prompt[:80].replace("\n", " ")
        return _block("not_json",
                      f"prompt JSON must be an object. Detected prose: {prose_snippet!r}",
                      _MUTATION_TEMPLATE, session_id)

    # 4. worker field
    worker = data.get("worker", "")
    if worker not in _VALID_WORKERS:
        template = _WORKER_CONFIG.get(worker, (_READ_REQUIRED, _MUTATION_TEMPLATE))[1]
        return _block("unknown_worker",
                      f"worker must be one of {_VALID_WORKERS!r}, got {worker!r}",
                      template, session_id)

    # 5. Required fields
    required, template = _WORKER_CONFIG[worker]
    missing = required - set(data.keys())
    if missing:
        missing_str = ", ".join(sorted(missing))
        return _block("missing_fields",
                      f"missing required fields for worker={worker!r}: {missing_str}",
                      template, session_id)

    # 5a. Optional field: depth (must be integer if present)
    if "depth" in data:
        depth = data.get("depth")
        if isinstance(depth, bool) or not isinstance(depth, int):
            return _block("invalid_field_value",
                          f"depth must be an integer if present, got {type(depth).__name__}",
                          template, session_id)

    # 6. Per-worker token ceiling (mutation: 650 if examples present, 560 otherwise;
    #    read: 650; create/shell: 560; orchestrator: 1200)
    token_limit = 560
    if worker == "mutation" and "examples" in data:
        token_limit = 650
    elif worker == "read":
        token_limit = 650
    elif worker == "orchestrator":
        token_limit = 1200
    if tokens > token_limit:
        # Auto-extract shell commands to a temp script so the retry prompt fits within limits.
        if worker == "shell":
            command = data.get("command", "")
            if command:
                import hashlib
                script_hash = hashlib.md5(command.encode()).hexdigest()[:8]
                script_path = Path(f"/tmp/cr_task_{script_hash}.sh")
                try:
                    script_path.write_text(command, encoding="utf-8")
                    script_path.chmod(0o755)
                    intent = data.get("intent", "extracted shell command")
                    expect = data.get("expect", "exit_code")
                    # Cleanup is baked into the command: exit code preserved, script always removed.
                    run_cmd = f"bash {script_path}; _s=$?; rm -f {script_path}; exit $_s"
                    short_prompt = json.dumps(
                        {"worker": "shell", "intent": intent, "command": run_cmd, "expect": expect},
                        separators=(",", ":"),
                    )
                    _log_violation("token_limit_auto_extracted", session_id)
                    return gate_deny(
                        f"[dispatch-schema] shell command too large ({tokens} tokens > {token_limit}). "
                        f"Extracted to {script_path}. Retry with:\n{short_prompt}"
                    )
                except OSError:
                    pass  # fall through to standard block if write fails
        return _block("token_limit",
                      f"prompt is {tokens} tokens (max {token_limit} for {worker})",
                      template, session_id, data)

    # 7-9. Content checks — delegated to per-worker SCHEMA_VALIDATORS.
    # Each validator returns a ValidationResult with ok + violations list.
    # Violations are collected across all checks and reported in ONE denial
    # so a prompt with multiple problems doesn't cost one round-trip per problem.
    content_violations: list[tuple[str, str, str | None, str | None]] = []

    validator = SCHEMA_VALIDATORS.get(worker)
    if validator is not None:
        result = validator.validate(data)
        if not result.ok:
            # Orchestrator violations are behavioral (plan/multi-phase) — map them
            # to early hard-blocks matching pre-existing gate behaviour.
            if worker == "orchestrator":
                for reason, sub_reason, _rejected in result.violations:
                    if sub_reason in (
                        "orchestrator_plan_field",
                        "orchestrator_multi_phase",
                        "orchestrator_oversized_scope",
                    ):
                        return _block(sub_reason, reason, template, session_id)
            # All other workers: accumulate with legacy subtype labels.
            for reason, sub_reason, rejected_item in result.violations:
                if sub_reason in (
                    "orchestrator_plan_field",
                    "orchestrator_multi_phase",
                    "orchestrator_oversized_scope",
                ):
                    subtype = sub_reason
                elif sub_reason in ("item_count", "item_length", "bad_chars", "allowlist"):
                    subtype = "schema_stuffing"
                elif sub_reason is None and "examples" in reason:
                    subtype = "invalid_examples"
                elif sub_reason is None and ("where" in reason or "why" in reason):
                    subtype = "invalid_field_value"
                else:
                    subtype = "schema_stuffing"
                content_violations.append((subtype, reason, sub_reason, rejected_item))

    if content_violations:
        diagnosis_phrase = _detect_diagnosis_phrase(data)
        for subtype, _reason, sub_reason, rejected_item in content_violations:
            _log_violation(subtype, session_id, diagnosis_phrase)
        numbered = "\n".join(
            f"{i + 1}) [{subtype}] {reason}"
            for i, (subtype, reason, _sub_reason, _rejected_item) in enumerate(content_violations)
        )
        msg = (
            f"[dispatch-schema] BLOCKED ({len(content_violations)} problem"
            f"{'s' if len(content_violations) != 1 else ''} found):\n{numbered}\n"
            f"Use the correct JSON dispatch schema:\n{template}"
        )
        return gate_deny(msg)

    # Advisory: create worker missing optional template_ref
    if worker == "create" and "template_ref" not in data:
        return gate_allow(
            "[dispatch-schema] ADVISORY: create worker has no template_ref. "
            "Add template_ref (e.g. 'python_module', 'json_config') so the worker "
            "can probe an existing file as a structural reference."
        )

    return gate_allow()


def event_pre_task(payload: dict[str, Any], is_claude: bool) -> dict[str, Any]:
    """Gate function for pre_task chain. Returns host-specific JSON."""
    from _common import emit_gate_result  # noqa: E402

    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Task", "Agent"):
        from _common import gate_allow as _ga
        return emit_gate_result(_ga(), is_claude)

    tool_input = payload.get("tool_input", {})
    if isinstance(tool_input, dict):
        prompt = tool_input.get("prompt", "") or ""
    elif isinstance(tool_input, str):
        prompt = tool_input
    else:
        prompt = str(tool_input)

    session_id = payload.get("session_id", "")
    result = validate_dispatch_schema(prompt, session_id)
    return emit_gate_result(result, is_claude)


def main() -> None:
    import sys
    prompt = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read().strip()
    result = validate_dispatch_schema(prompt)
    if result.decision == "deny":
        print("FAIL: " + (result.message or "schema error"))
        sys.exit(1)
    if result.message:
        print("WARN: " + result.message)
    print("PASS")


if __name__ == "__main__":
    main()
