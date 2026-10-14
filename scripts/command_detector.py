#!/usr/bin/env python3
"""Command classification utilities — shell, bulk-verify, deploy detection.

Extracted from check_routing.py. All callers in check_routing.py use
re-export shims; this module can also be imported directly.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

# ---------------------------------------------------------------------------
# Verification command pattern
# ---------------------------------------------------------------------------

# PROD-19: matches the decision table's "run test suite / lint / verification
# command after an edit -> Task(fast model); never: run inline" row.
_VERIFICATION_COMMAND_RE = re.compile(
    r"\b(pytest|py\.test|python3?\s+-m\s+(?:pytest|unittest)|unittest\b|"
    r"npm\s+(?:run\s+)?test|yarn\s+test|pnpm\s+test|go\s+test|cargo\s+test|"
    r"shellcheck|eslint|ruff\b|flake8|mypy\b|tsc\b)",
    re.IGNORECASE,
)


def _is_verification_command(command: str) -> bool:
    return bool(_VERIFICATION_COMMAND_RE.search(command))


# ---------------------------------------------------------------------------
# Shell command extraction
# ---------------------------------------------------------------------------

def _shell_command(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = payload.get("toolInput")
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command", "")
    return command.strip() if isinstance(command, str) else ""


# ---------------------------------------------------------------------------
# Deploy command detection
# ---------------------------------------------------------------------------

def _is_deploy_command(command: str) -> bool:
    """True for deploy/resync scripts that target external paths, not working-tree mutations.

    Conservative pattern: only matches `resync` and `deploy` script names — both have clear
    external-target semantics. Excludes `install` (ambiguous: pip/npm install modifies working
    tree) and `sync` (ambiguous: could sync local state). SHELL-CLASS-1.

    Matches: bash resync/resync.sh, bash scripts/deploy.sh, sh deploy.sh
    Does NOT match: bash scripts/install_deps.sh, bash sync_state.sh
    """
    if not command:
        return False
    lower = command.lower()
    if re.match(r"(?:bash|sh)\s+", lower):
        m = re.search(r"(?:bash|sh)\s+(\S+)", command, re.IGNORECASE)
        if m and re.search(r"(?:resync|deploy)", m.group(1).lower()):
            return True
    return False


# ---------------------------------------------------------------------------
# Bulk verification detection
# ---------------------------------------------------------------------------

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

BULK_VERIFY_RE = re.compile(r"bulk-verify:\s*(user-local|subagent)", re.IGNORECASE)


def _is_bulk_verification_command(command: str) -> bool:
    """Heuristic bulk-suite detector. Fail-open (returns False) on parse errors."""
    if not command:
        return False
    try:
        lower = command.lower()

        if "run_scenarios" in lower:
            return not bool(re.search(r"--scenario\b", lower))

        if not _is_verification_command(command):
            return False

        if re.search(r"::\w", command):
            return False
        if re.search(r"(?:^|\s)-k\s+\S", command):
            return False

        # GATE-5a: multi-file pytest detection — replaces the old single-target
        # early-return so we can distinguish one .py file (targeted) from two+
        # .py files (bulk). Handles: pytest, python -m pytest, python3 -m pytest,
        # python3.11 -m pytest, etc.
        if re.search(r"\b(?:pytest|py\.test)\b", lower):
            try:
                tokens = shlex.split(command)
            except ValueError:
                return True
            # Collect positional targets by stripping prefix tokens and flags.
            positional = []
            i = 0
            while i < len(tokens):
                tok = tokens[i]
                # Skip interpreter prefix: python, python3, python3.11, etc.
                if re.match(r"python\w*$", tok, re.IGNORECASE):
                    i += 1
                    continue
                # Skip -m marker
                if tok == "-m":
                    i += 1
                    continue
                # Skip the pytest/py.test binary name itself
                if re.match(r"(?:pytest|py\.test)$", tok, re.IGNORECASE):
                    i += 1
                    continue
                # Skip flags (anything starting with -)
                if tok.startswith("-"):
                    i += 1
                    continue
                # What remains are positional targets
                positional.append(tok)
                i += 1

            if len(positional) == 0:
                # Bare pytest — bulk
                return True
            if len(positional) == 1:
                target = positional[0]
                # Single .py file — targeted, not bulk
                if target.endswith(".py"):
                    return False
                # Single directory (e.g. tests/, ./tests) — bulk
                return True
            # Two or more positional args: bulk if they are .py files or tests/* paths
            py_targets = [
                t for t in positional if t.endswith(".py") or re.match(r"\.?/?tests", t)
            ]
            if len(py_targets) >= 2:
                return True
            # Mixed or unrecognised multi-arg — treat as bulk (safe default)
            return True

        if re.search(r"python3?\s+-m\s+pytest\s*$", command.strip(), re.IGNORECASE):
            return True
        if re.search(r"python3?\s+-m\s+pytest\s+tests\b", command, re.IGNORECASE):
            return True

        if re.search(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b", lower):
            after = re.split(
                r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b", lower, maxsplit=1
            )
            remainder = after[1].strip() if len(after) > 1 else ""
            if remainder and not remainder.startswith(("-", "--")):
                return False
            return True

        if "unittest" in lower and "discover" in lower:
            return True

        if re.search(r"\b(?:bash|sh)\s+\S+", lower):
            if re.search(r"(?:pytest|run_scenarios|npm\s+test)", lower):
                return True

        for linter in ("eslint", "ruff", "flake8", "mypy", "tsc", "shellcheck"):
            if re.search(rf"\b{linter}\b", lower):
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    return True
                has_file = any(
                    not t.startswith("-") and ("." in t or "/" in t)
                    for t in tokens[1:]
                )
                return not has_file

        if re.search(r"\b(?:go\s+test|cargo\s+test)\b", lower):
            return True

        return False
    except Exception:  # noqa: BLE001 — advisory gate must fail open
        return False


def _bulk_choice_from_prompt(prompt: str) -> str | None:
    lower = prompt.lower()
    user_hit = any(signal in lower for signal in _BULK_USER_LOCAL_PROMPT_SIGNALS)
    agent_hit = any(signal in lower for signal in _BULK_AGENT_PROMPT_SIGNALS)
    if user_hit and agent_hit:
        return None
    if user_hit:
        return "user"
    if agent_hit:
        return "agent"
    return None


def _parse_bulk_verify_choice(text: str) -> str | None:
    match = BULK_VERIFY_RE.search(text)
    if not match:
        return None
    token = match.group(1).lower()
    return "user" if token == "user-local" else "agent"
