"""_validate_shared — pure helper functions shared by schema_validators/*.

These functions contain zero I/O and zero serialization — they operate only
on in-memory dicts/strings.  Extracted so each per-worker validator module
can import from a single source of truth instead of duplicating logic.

All functions are re-exported by validate_dispatch_schema.py as well, so
existing callers are unaffected.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

# ── Constraint allowlist ──────────────────────────────────────────────────────
# Deliberate fail-open: a missing/broken file sets to None and skips the
# allowlist check rather than hard-blocking every dispatch.
_ALLOWLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "constraint_allowlist.json"
try:
    _CONSTRAINT_ALLOWLIST: set[str] | None = set(
        json.loads(_ALLOWLIST_PATH.read_text())["allowed_tokens"]
    )
except Exception:  # noqa: BLE001
    _CONSTRAINT_ALLOWLIST = None


def suggest_allowlist_matches(unknown_words: list[str]) -> str:
    """Return a human-readable nearest-match suggestion for each unknown word."""
    if _CONSTRAINT_ALLOWLIST is None:
        return ""
    suggestions: list[str] = []
    vocab = sorted(_CONSTRAINT_ALLOWLIST)
    for word in unknown_words:
        close = difflib.get_close_matches(word, vocab, n=1, cutoff=0.6)
        if not close:
            close = [w for w in vocab if word in w or w in word][:1]
        if close:
            suggestions.append(f"{word!r} -> {close[0]!r}")
        else:
            suggestions.append(f"{word!r} -> no close match")
    return "; ".join(suggestions)


def check_constraints(data: dict[str, Any]) -> list[tuple[str, str | None, str | None]]:
    """Return every constraints violation as (reason, sub_reason, rejected_item)."""
    violations: list[tuple[str, str | None, str | None]] = []
    constraints = data.get("constraints")
    if constraints is None:
        return violations
    if len(constraints) > 5:
        violations.append((f"constraints has {len(constraints)} items (max 5)", "item_count", None))
    _CONSTRAINT_RE = re.compile(r"^[a-zA-Z0-9\s\-,.]+$")
    for item in constraints:
        if not isinstance(item, str):
            continue
        if len(item) > 60:
            violations.append((f"constraint item exceeds 60 chars: {item[:40]!r}...", "item_length", item))
            continue
        if not _CONSTRAINT_RE.match(item):
            violations.append((f"constraint item contains disallowed chars: {item[:40]!r}", "bad_chars", item))
            continue
        if _CONSTRAINT_ALLOWLIST is not None:
            words = [w for w in re.split(r"[\s\-,.]+", item.lower()) if w]
            unknown = [w for w in words if w not in _CONSTRAINT_ALLOWLIST]
            if unknown:
                suggestion = suggest_allowlist_matches(unknown[:3])
                reason = (
                    f"constraint item has unknown tokens: {unknown[:3]} "
                    f"(not in config/constraint_allowlist.json)."
                )
                if suggestion:
                    reason += f" Nearest allowed match(es): {suggestion}."
                reason += " Run: cat config/constraint_allowlist.json | python3 -c \"import json,sys; print('\\n'.join(sorted(json.load(sys.stdin)['allowed_tokens'])))\" to browse the full vocabulary."
                violations.append((reason, "allowlist", item))
    return violations


def check_examples(worker: str, data: dict[str, Any]) -> list[tuple[str, str | None, str | None]]:
    """Return every examples violation (mutation worker only) as (reason, sub_reason, rejected_item)."""
    violations: list[tuple[str, str | None, str | None]] = []
    if worker != "mutation":
        return violations
    examples = data.get("examples")
    if examples is None:
        return violations
    if not isinstance(examples, list):
        violations.append((f"examples must be a list or None, got {type(examples).__name__}", None, None))
        return violations
    if len(examples) > 3:
        violations.append((f"examples has {len(examples)} items (max 3)", None, None))
    for idx, item in enumerate(examples):
        if not isinstance(item, dict):
            violations.append((f"examples[{idx}] must be dict, got {type(item).__name__}", None, None))
            continue
        if set(item.keys()) != {"before", "after"}:
            violations.append((f"examples[{idx}] must have exactly 'before' and 'after' keys", None, None))
            continue
        before_val = item.get("before")
        after_val = item.get("after")
        if not isinstance(before_val, str):
            violations.append((f"examples[{idx}]['before'] must be string, got {type(before_val).__name__}", None, None))
            continue
        if not isinstance(after_val, str):
            violations.append((f"examples[{idx}]['after'] must be string, got {type(after_val).__name__}", None, None))
            continue
        if len(before_val) > 400:
            violations.append((f"examples[{idx}]['before'] exceeds 400 chars (got {len(before_val)})", None, None))
        if len(after_val) > 400:
            violations.append((f"examples[{idx}]['after'] exceeds 400 chars (got {len(after_val)})", None, None))
        if "`" in before_val:
            violations.append((f"examples[{idx}]['before'] contains backticks (not allowed)", None, None))
        if "`" in after_val:
            violations.append((f"examples[{idx}]['after'] contains backticks (not allowed)", None, None))
    return violations


def check_field_values(worker: str, data: dict[str, Any]) -> list[tuple[str, str | None, str | None]]:
    """Return where/why violations (mutation worker only) as (reason, sub_reason, rejected_item)."""
    violations: list[tuple[str, str | None, str | None]] = []
    if worker != "mutation":
        return violations
    where = data.get("where")
    if where is not None and (not isinstance(where, str) or len(where) > 170):
        violations.append(("where must be a string <= 170 chars", None, None))
    why = data.get("why")
    if why is not None and (not isinstance(why, str) or len(why) > 150):
        violations.append(("why must be a string <= 150 chars", None, None))
    return violations
