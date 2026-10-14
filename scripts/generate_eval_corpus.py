"""generate_eval_corpus.py — documents the signal patterns used to author real_user_corpus.json.

This script is a read-only reference tool. It reads classify_intent.py signal lists,
prints them grouped by intent class, and describes the 5 prompt types used in the corpus.

It does NOT call the Anthropic API. No ANTHROPIC_API_KEY is required.
The corpus (tests/data/real_user_corpus.json) is pre-generated and committed.

Usage:
    python3 scripts/generate_eval_corpus.py
    python3 scripts/generate_eval_corpus.py --validate
    python3 scripts/generate_eval_corpus.py --stats
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

CORPUS_PATH = PROJECT_ROOT / "tests" / "data" / "real_user_corpus.json"

# ---------------------------------------------------------------------------
# Prompt type descriptions
# ---------------------------------------------------------------------------

PROMPT_TYPES = {
    "canonical": (
        "Canonical prompts match the primary signal patterns verbatim. "
        "They use the exact bigrams, trigger words, or phrase patterns that the "
        "keyword classifier looks for. These establish the accuracy floor."
    ),
    "informal_typos": (
        "Informal prompts simulate real user input with common typos, "
        "abbreviations (pls, bc, rn, 4, 2gether), and casual language. "
        "The normalization pipeline (_normalize_canonical) should handle most fillers; "
        "typos test whether partial signal matching still fires."
    ),
    "mid_task_shorthand": (
        "Mid-task shorthand prompts are ultra-compact notations that appear "
        "in the middle of a session (e.g., 'fix: normalize_canonical off-by-one', "
        "'dispatch — haiku — tests'). They lack grammatical structure but carry "
        "clear intent via the leading keyword."
    ),
    "implicit_context": (
        "Implicit context prompts embed the intent in surrounding context rather "
        "than stating it directly (e.g., 'I'm new to this codebase — explain how "
        "the pieces fit together'). The primary clause extraction (T020) should "
        "isolate the core intent signal."
    ),
    "boundary": (
        "Boundary prompts sit at the edge between two classes or contain "
        "a primary clause of one class and a trailing clause of another "
        "(e.g., 'Fix the bug, also tell me what caused it.'). They test whether "
        "the _extract_primary_clause function correctly isolates the leading intent "
        "and whether exclusion/suppression rules prevent cross-class leakage."
    ),
}

# ---------------------------------------------------------------------------
# Signal catalog — mirrors classify_intent.py signal sets
# ---------------------------------------------------------------------------

SIGNAL_CATALOG: dict[str, dict[str, list[str]]] = {
    "dispatch_justification": {
        "bigrams": [
            "spin up", "kick off", "send to worker", "route to",
            "route this", "dispatch the", "delegate the", "pass to a",
            "subagent for", "worker for", "task for the",
            "spin up a task", "kick off a task",
        ],
        "unigrams": [
            "dispatch to", "dispatch this", "dispatching",
            "subagent", "worker", "hand off", "farm out",
            "delegate", "offload", "spinning up a task", "Task()",
            "dispatch_justification",
        ],
        "context_boosters": [
            "parallel", "background", "separate", "agent", "orchestrate", "batch",
        ],
    },
    "inline_narration": {
        "bigrams": [
            "doing this", "working on it", "editing the", "fixing the",
            "looking at the", "patching the", "updating the file",
            "correcting the", "adjusting the", "fixing it in place",
            "handling the", "routing: direct",
            "i'll handle", "doing it myself", "tackling the",
        ],
        "unigrams": [
            "inline", "doing this myself", "no dispatch", "handling this directly",
            "editing the file now", "reading the", "working on it here",
            "no need to dispatch", "tackling this myself",
            "i'll handle", "i'll do this", "i'll fix", "doing it now",
            "i'm reading", "i'm editing", "i am handling", "handling this now",
            "fixing this now", "editing this now",
        ],
        "context_boosters": [
            "now", "here", "directly", "myself", "inline", "currently",
            "right", "immediately", "already",
        ],
    },
    "content_generation": {
        "bigrams": [
            "write the", "draft the", "generate the", "create the",
            "write up", "design doc", "spec document", "changelog",
            "write docs", "write documentation",
        ],
        "unigrams": [
            "generate", "create a", "write a", "build a", "make a",
            "draft a", "produce a", "compose a",
            "html file", "html page", "markdown file", "readme",
            "documentation", "boilerplate", "template",
            "scaffold", "skeleton",
        ],
    },
    "agentic_action": {
        "word_boundary_verbs": [
            "add", "fix", "create", "delete", "refactor", "update", "edit",
            "write", "run", "build", "implement", "change", "remove", "rename",
            "migrate", "deploy",
        ],
        "exclusion_context": [
            "worker", "subagent", "dispatch", "delegate", "task()",
            "inline", "directly", "myself", "no dispatch", "here in",
        ],
    },
    "qa_architecture": {
        "bigrams": [
            "how the system", "how does the", "how it works", "explain the flow",
            "explain the architecture", "high level", "system design",
            "how everything fits", "overall flow", "whole flow",
            "high-level overview", "how things work", "how does it all",
        ],
        "unigrams": [
            "how does the whole", "all files", "architecture", "flow", "overview",
            "structure of", "how are", "how do these",
        ],
        "whole_pipeline": [
            "whole pipeline", "whole system", "entire system", "overall architecture",
            "big picture", "end to end", "end-to-end", "from start to finish",
            "complete flow", "full flow",
        ],
    },
    "qa_with_reads": {
        "unigrams": [
            "show me", "where is", "find the", "what does", "look at",
            "which file", "show the code",
        ],
        "suppressors": [
            "now", "editing", "fixing", "directly", "myself",
            "right now", "in place", "handling",
        ],
    },
    "qa_simple": {
        "unigrams": [
            "what is", "what are", "who is", "when did", "where did",
            "explain what", "tell me what", "what does it mean",
            "how many", "is it true", "are there", "what version",
            "what type", "what kind",
        ],
    },
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

VALID_INTENTS = frozenset(SIGNAL_CATALOG.keys())
VALID_TYPES = frozenset(PROMPT_TYPES.keys())


def validate_corpus(corpus_path: Path) -> list[str]:
    """Validate the corpus JSON structure. Returns list of error messages."""
    errors: list[str] = []

    with open(corpus_path) as f:
        data = json.load(f)

    prompts = data.get("prompts", [])
    if not prompts:
        errors.append("No 'prompts' key or empty list in corpus.")
        return errors

    for i, item in enumerate(prompts):
        loc = f"prompts[{i}]"
        if "intent" not in item:
            errors.append(f"{loc}: missing 'intent' field")
        elif item["intent"] not in VALID_INTENTS:
            errors.append(f"{loc}: unknown intent '{item['intent']}'")

        if "prompt" not in item or not item["prompt"].strip():
            errors.append(f"{loc}: missing or empty 'prompt' field")

        if "type" not in item:
            errors.append(f"{loc}: missing 'type' field")
        elif item["type"] not in VALID_TYPES:
            errors.append(f"{loc}: unknown type '{item['type']}'")

        if "notes" not in item:
            errors.append(f"{loc}: missing 'notes' field")

    return errors


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def corpus_stats(corpus_path: Path) -> None:
    """Print distribution stats for the corpus."""
    with open(corpus_path) as f:
        data = json.load(f)

    prompts = data.get("prompts", [])
    total = len(prompts)

    by_intent: dict[str, list] = defaultdict(list)
    by_type: Counter = Counter()
    by_intent_type: dict[str, Counter] = defaultdict(Counter)

    for item in prompts:
        intent = item.get("intent", "unknown")
        ptype = item.get("type", "unknown")
        by_intent[intent].append(item)
        by_type[ptype] += 1
        by_intent_type[intent][ptype] += 1

    print(f"\n=== Corpus Stats: {corpus_path.name} ===")
    print(f"Total prompts: {total}")

    print("\n--- By intent class ---")
    for intent in sorted(VALID_INTENTS, key=lambda c: -len(by_intent.get(c, []))):
        count = len(by_intent.get(intent, []))
        type_dist = ", ".join(
            f"{t}:{n}" for t, n in sorted(by_intent_type[intent].items())
        )
        print(f"  {intent:<30} {count:>4}   [{type_dist}]")

    print("\n--- By prompt type ---")
    for ptype in sorted(VALID_TYPES, key=lambda t: -by_type.get(t, 0)):
        print(f"  {ptype:<25} {by_type.get(ptype, 0):>4}")


# ---------------------------------------------------------------------------
# Signal report
# ---------------------------------------------------------------------------

def print_signals() -> None:
    """Print signal catalog grouped by intent class."""
    print("\n=== Signal Catalog (from classify_intent.py) ===\n")
    for intent, signals in SIGNAL_CATALOG.items():
        print(f"--- {intent} ---")
        for signal_type, items in signals.items():
            print(f"  [{signal_type}]")
            for item in items:
                print(f"    {item!r}")
        print()

    print("\n=== Prompt Type Descriptions ===\n")
    for ptype, desc in PROMPT_TYPES.items():
        print(f"--- {ptype} ---")
        print(f"  {desc}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate eval corpus reference — prints signal patterns and validates corpus JSON."
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate tests/data/real_user_corpus.json structure and exit.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print per-class and per-type distribution stats.",
    )
    args = parser.parse_args()

    if args.validate:
        errors = validate_corpus(CORPUS_PATH)
        if errors:
            print(f"VALIDATION FAILED ({len(errors)} errors):")
            for err in errors:
                print(f"  - {err}")
            return 1
        else:
            total = len(json.loads(CORPUS_PATH.read_text()).get("prompts", []))
            print(f"OK: corpus valid ({total} prompts, no errors).")
            return 0

    if args.stats:
        corpus_stats(CORPUS_PATH)
        return 0

    # Default: print signal catalog + type descriptions
    print_signals()
    print(f"\nCorpus path: {CORPUS_PATH}")
    print("Run with --validate to check the JSON structure.")
    print("Run with --stats to see per-class and per-type distribution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
