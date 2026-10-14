#!/usr/bin/env python3
"""Lint: CLAUDE.md intent mentions vs config.json routing_matrix keys.

Parses CLAUDE.md for known intent tokens (mechanical, bulk-edit, additive,
design, ambiguous, debug, default) and compares against the keys present in
config.json's `routing_matrix`. Prints any intent mentioned in CLAUDE.md that
is missing from routing_matrix, and any routing_matrix key (other than
internal `_doc`) not mentioned anywhere in CLAUDE.md.

Exit 0 if clean, 1 on mismatch. Intended for pre-commit lint use. Does not
modify config.json.
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
EXTRA_DOCS = [
    REPO_ROOT / "scripts" / "docs" / "dispatch-protocol.md",
    REPO_ROOT / "scripts" / "docs" / "bws.md",
]
CONFIG_JSON = REPO_ROOT / "config.json"

KNOWN_INTENTS = ["mechanical", "bulk-edit", "additive", "design", "ambiguous", "debug", "default"]


def load_routing_matrix_keys():
    with open(CONFIG_JSON) as f:
        config = json.load(f)
    matrix = config.get("routing_matrix", {})
    keys = set()
    for key in matrix.keys():
        if key == "_doc":
            continue
        keys.add(key.lstrip("_"))
    return keys


def find_mentioned_intents(text):
    mentioned = set()
    for intent in KNOWN_INTENTS:
        pattern = r"\b" + re.escape(intent) + r"\b"
        if re.search(pattern, text):
            mentioned.add(intent)
    return mentioned


def main():
    claude_text = CLAUDE_MD.read_text()
    for extra in EXTRA_DOCS:
        if extra.exists():
            claude_text += "\n" + extra.read_text()
    matrix_keys = load_routing_matrix_keys()
    mentioned_intents = find_mentioned_intents(claude_text)

    known_intents_set = set(KNOWN_INTENTS)

    missing_from_matrix = sorted(mentioned_intents & known_intents_set - matrix_keys)
    missing_from_claude = sorted((matrix_keys & known_intents_set) - mentioned_intents)

    clean = True

    if missing_from_matrix:
        clean = False
        print("Intents mentioned in CLAUDE.md (+ moved docs) but missing from config.json routing_matrix:")
        for intent in missing_from_matrix:
            print(f"  - {intent}")

    if missing_from_claude:
        clean = False
        print("routing_matrix keys not mentioned anywhere in CLAUDE.md (+ moved docs):")
        for intent in missing_from_claude:
            print(f"  - {intent}")

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from _config import validate_gate_config

    config_errors = validate_gate_config()
    if config_errors:
        clean = False
        print("Config validation errors:")
        for err in config_errors:
            print(f"  - {err}")

    if clean:
        print("OK: CLAUDE.md intent mentions and config.json routing_matrix are consistent.")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
