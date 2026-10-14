#!/usr/bin/env python3
"""PreToolUse hook: proactively validate Task/Agent dispatch schema.

Reads the incoming payload from stdin. For Task and Agent tool calls whose
prompt is a JSON dispatch object, checks field-level constraints and emits an
advisory (permissionDecision=allow + additionalContext) when violations exist.
Non-JSON prompts and non-Task/Agent tools pass through silently.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_ALLOWLIST_PATH = REPO_ROOT / "config" / "constraint_allowlist.json"

# Token ceilings per worker type (len(prompt) // 4)
_CEILINGS: dict[str, int] = {
    "mutation": 560,
    "read": 650,
    "shell": 560,
    "create": 560,
    "orchestrator": 1200,
}
_MUTATION_EXAMPLES_CEILING = 650

_FIELD_LIMITS: dict[str, int] = {
    "pattern": 200,
    "where": 170,
    "why": 150,
}

_CONSTRAINT_CHAR_RE = re.compile(r"^[a-zA-Z0-9 \-,.]+$")


def _tokenize_constraint(s: str) -> list[str]:
    """Split on spaces and hyphens, lowercase."""
    if not s:
        return []
    return [tok.lower() for tok in re.split(r"[ \-]", s) if tok]


def _load_allowlist() -> frozenset[str]:
    """Load constraint allowlist words. Returns empty frozenset if file missing."""
    try:
        data = json.loads(_ALLOWLIST_PATH.read_text())
        if isinstance(data, list):
            return frozenset(w.lower() for w in data if isinstance(w, str))
        if isinstance(data, dict):
            words = data.get("words", data.get("allowlist", []))
            if isinstance(words, list):
                return frozenset(w.lower() for w in words if isinstance(w, str))
    except Exception:
        pass
    return frozenset()


def _check(prompt: str) -> list[str]:
    """Return list of violation strings for a dispatch prompt string.

    Returns [] for non-JSON, JSON arrays, or dispatch objects with no violations.
    """
    if not prompt or not prompt.strip().startswith("{"):
        return []
    try:
        data = json.loads(prompt)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    violations: list[str] = []
    worker = data.get("worker", "")

    # Field character limits
    for field, limit in _FIELD_LIMITS.items():
        value = data.get(field)
        if isinstance(value, str) and len(value) > limit:
            violations.append(
                f"field '{field}' length {len(value)} exceeds limit {limit}"
            )

    # Constraints validation
    constraints = data.get("constraints")
    if isinstance(constraints, list):
        if len(constraints) > 5:
            violations.append(
                f"constraints list length {len(constraints)} exceeds max 5 items"
            )
        allowlist = _load_allowlist()
        for i, item in enumerate(constraints):
            if not isinstance(item, str):
                continue
            if len(item) > 60:
                violations.append(
                    f"constraints[{i}] length {len(item)} exceeds limit 60"
                )
            if not _CONSTRAINT_CHAR_RE.match(item):
                violations.append(
                    f"constraints[{i}] contains disallowed characters: {item!r}"
                )
            elif allowlist:
                for word in _tokenize_constraint(item):
                    if word not in allowlist:
                        violations.append(
                            f"constraints[{i}] word '{word}' not in allowlist"
                        )

    # Examples validation
    examples = data.get("examples")
    if isinstance(examples, list):
        for i, ex in enumerate(examples):
            if not isinstance(ex, dict):
                continue
            for side in ("before", "after"):
                val = ex.get(side, "")
                if not isinstance(val, str):
                    continue
                if len(val) > 400:
                    violations.append(
                        f"examples[{i}].{side} length {len(val)} exceeds limit 400"
                    )
                if "`" in val:
                    violations.append(
                        f"examples[{i}].{side} contains backtick character"
                    )

    # Token ceiling
    if isinstance(worker, str) and worker in _CEILINGS:
        tokens = len(prompt) // 4
        ceiling = (
            _MUTATION_EXAMPLES_CEILING
            if worker == "mutation" and isinstance(examples, list) and examples
            else _CEILINGS[worker]
        )
        if tokens > ceiling:
            violations.append(
                f"dispatch token count {tokens} exceeds ceiling {ceiling} for worker '{worker}'"
            )

    # File existence
    files = data.get("files")
    if isinstance(files, list):
        for entry in files:
            if not isinstance(entry, str):
                continue
            # Strip :line or :start,end suffix
            path_part = entry.split(":")[0]
            resolved = REPO_ROOT / path_part
            if not resolved.exists():
                violations.append(
                    f"file does not exist: {path_part}"
                )

    return violations


def main() -> None:
    raw = sys.stdin.read().strip()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        print("{}")
        return

    if not isinstance(payload, dict):
        print("{}")
        return

    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Task", "Agent"):
        print("{}")
        return

    tool_input = payload.get("tool_input", {})
    if isinstance(tool_input, str):
        prompt = tool_input
    elif isinstance(tool_input, dict):
        prompt = tool_input.get("prompt", "")
        if not isinstance(prompt, str):
            prompt = ""
    else:
        prompt = ""

    violations = _check(prompt)
    advisory = ""
    if violations:
        advisory = "Dispatch schema advisory:\n" + "\n".join(f"  - {v}" for v in violations)

    out = {
        "hookSpecificOutput": {
            "permissionDecision": "allow",
            "additionalContext": advisory,
        }
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
