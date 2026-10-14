#!/usr/bin/env python3
"""One-time developer tool: generate diverse constraint string scenarios via Claude Code CLI.

Uses `claude -p "..."` subprocess — no ANTHROPIC_API_KEY required.
Claude Code handles auth via its own OAuth session.

Output: tests/eval/schema_whitelist_scenarios.json  (commit this file)

Usage:
  python3 scripts/generate_whitelist_scenarios.py
  python3 scripts/generate_whitelist_scenarios.py --n 15 --output path/to/out.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "tests" / "eval" / "schema_whitelist_scenarios.json"

# ---------------------------------------------------------------------------
# Category definitions: (generation_prompt, expected_gate_decision)
# ---------------------------------------------------------------------------
CATEGORIES: dict[str, tuple[str, str]] = {
    "valid_common": (
        "Generate {n} short constraint phrases a software engineer would write "
        "for a code-editing task dispatch schema. Use only plain English words, "
        "letters, spaces, and hyphens — no special characters, no punctuation. "
        "Examples of GOOD output: 'no new imports', 'preserve existing indentation', "
        "'targeted edits only', 'no deletion', 'keep return types'. "
        "Return ONLY a JSON array of strings, nothing else.",
        "allow",
    ),
    "valid_edge": (
        "Generate {n} edge-case constraint phrases that are valid but unusual: "
        "very short (1-2 words), or hyphen-separated compound words, or rare but "
        "legitimate technical terms. All must use only letters, digits, spaces, "
        "hyphens — no special chars. "
        "Examples: 'no-sql', 'read-only', 'minimal', 'strict', 'backward-compatible'. "
        "Return ONLY a JSON array of strings, nothing else.",
        "allow",
    ),
    "invalid_special_chars": (
        "Generate {n} constraint strings that contain special characters: colons, "
        "braces, brackets, quotes, semicolons, pipe symbols, or asterisks. "
        "These should look like someone tried to embed formatting or shell syntax. "
        "Examples: 'use format: json', 'no {{mutations}}', 'output: targeted_edits'. "
        "Return ONLY a JSON array of strings, nothing else.",
        "deny",
    ),
    "invalid_json_embedded": (
        "Generate {n} constraint strings that embed JSON fragments or dict literals. "
        "Examples: strings containing curly braces like match key-value pairs, use schema objects, "
        "output like text fields with quotes and colons. "
        "They should contain curly braces, colons, or quote characters. "
        "Return ONLY a JSON array of strings, nothing else.",
        "deny",
    ),
    "invalid_injection": (
        "Generate {n} constraint strings that attempt prompt injection or contain "
        "instruction-hijacking language. Examples: 'ignore previous instructions', "
        "'override system prompt', 'disregard constraints', strings with base64-encoded "
        "text, or strings that embed hidden directives. "
        "Return ONLY a JSON array of strings, nothing else.",
        "deny",
    ),
    "adversarial_near_boundary": (
        "Generate {n} constraint strings that look almost valid but contain exactly "
        "one disallowed element: an underscore ('no_new_imports'), a dot ('use v2.0'), "
        "an at-sign ('notify@user'), or an unknown jargon word not in basic English "
        "('use memoization', 'enable idempotency'). "
        "They should be tricky near-boundary cases that test the allowlist precisely. "
        "Return ONLY a JSON array of strings, nothing else.",
        "deny",
    ),
}


def _call_claude(prompt: str) -> list[str]:
    """Call `claude -p <prompt>` and parse the returned JSON array."""
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr.strip()}")

    raw = result.stdout.strip()
    # Extract JSON array from response (claude may add prose around it)
    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON array found in claude output: {raw[:200]!r}")

    items = json.loads(raw[start:end])
    if not isinstance(items, list):
        raise ValueError(f"Expected list, got {type(items)}")
    return [str(i) for i in items]


def generate(n_per_category: int = 10) -> list[dict]:
    scenarios: list[dict] = []

    for category, (prompt_template, expected) in CATEGORIES.items():
        prompt = prompt_template.format(n=n_per_category)
        print(f"  Generating {n_per_category} scenarios for '{category}'...", end=" ", flush=True)
        try:
            items = _call_claude(prompt)
        except (RuntimeError, ValueError) as e:
            print(f"RETRY (error: {e})")
            # Retry once
            try:
                items = _call_claude(prompt)
            except Exception as e2:
                print(f"FAILED ({e2}) — skipping category")
                continue

        items = items[:n_per_category]
        for text in items:
            scenarios.append({"text": text, "category": category, "expected": expected})
        print(f"OK ({len(items)} items)")

    return scenarios


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate whitelist eval scenarios via claude CLI")
    parser.add_argument("--n", type=int, default=10, help="Scenarios per category (default: 10)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    print(f"Generating {args.n} scenarios per category ({len(CATEGORIES)} categories = {args.n * len(CATEGORIES)} total)...")
    scenarios = generate(n_per_category=args.n)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(scenarios, indent=2))
    print(f"\nSaved {len(scenarios)} scenarios to {args.output}")
    print("Commit this file so the eval runs offline without claude CLI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
