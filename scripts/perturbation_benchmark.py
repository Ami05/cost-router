#!/usr/bin/env python3
"""Perturbation benchmark — tests classifier robustness across 9 perturbation types.

Applies each perturbation to the exemplar/synthetic dataset, runs classification,
and reports per-type accuracy with a >=30% floor enforcement.

Perturbation types:
  clean, typo, filler_words, casing_variation, truncated,
  verbose, domain_jargon, cross_class_contamination, negation

Usage:
  python3 scripts/perturbation_benchmark.py [--json] [--exemplars-path PATH]
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPTS_DIR.parent

sys.path.insert(0, str(_SCRIPTS_DIR))
from _common import DATA_DIR, load_json_state, now_iso  # noqa: E402

_DEFAULT_EXEMPLARS = _PLUGIN_ROOT / "config" / "intent_exemplars.json"
_BENCHMARK_STATE = DATA_DIR / "perturbation_benchmark.json"

ACCURACY_FLOOR = 0.30
PERTURBATION_TYPES = [
    "clean", "typo", "filler_words", "casing_variation", "truncated",
    "verbose", "domain_jargon", "cross_class_contamination", "negation",
]

# Seed for reproducibility
_RNG = random.Random(42)

# ---------------------------------------------------------------------------
# Perturbation functions
# ---------------------------------------------------------------------------

_TYPO_SWAPS = {"a": "s", "e": "r", "i": "o", "o": "p", "t": "y", "n": "m", "s": "d"}

def _apply_typo(text: str) -> str:
    """Introduce 1-2 character swaps in the text."""
    chars = list(text)
    positions = [i for i, c in enumerate(chars) if c.lower() in _TYPO_SWAPS and c.isalpha()]
    if not positions:
        return text
    n_swaps = min(2, len(positions))
    for pos in _RNG.sample(positions, n_swaps):
        original = chars[pos]
        replacement = _TYPO_SWAPS.get(original.lower(), original)
        chars[pos] = replacement.upper() if original.isupper() else replacement
    return "".join(chars)


_FILLERS = ["um", "like", "you know", "basically", "so", "well", "actually", "I mean"]

def _apply_filler_words(text: str) -> str:
    """Insert 1-2 filler words/phrases into the text."""
    words = text.split()
    if len(words) < 3:
        return text
    n_inserts = min(2, len(words) // 3)
    for _ in range(n_inserts):
        pos = _RNG.randint(1, len(words) - 1)
        filler = _RNG.choice(_FILLERS)
        words.insert(pos, filler)
    return " ".join(words)


def _apply_casing_variation(text: str) -> str:
    """Randomly change casing — ALL CAPS, all lower, or rAnDoM."""
    choice = _RNG.randint(0, 2)
    if choice == 0:
        return text.upper()
    elif choice == 1:
        return text.lower()
    else:
        return "".join(c.upper() if _RNG.random() > 0.5 else c.lower() for c in text)


def _apply_truncated(text: str) -> str:
    """Truncate to first 40-60% of the text."""
    cut = int(len(text) * (_RNG.uniform(0.4, 0.6)))
    return text[:max(cut, 10)]


_VERBOSE_PREFIXES = [
    "I was wondering if you could help me with something — ",
    "So I've been thinking about this for a while and I think what I need is to ",
    "Hey, quick question if you don't mind — could you please ",
    "I'm not sure exactly how to phrase this but basically I need to ",
]

def _apply_verbose(text: str) -> str:
    """Add verbose padding around the core prompt."""
    prefix = _RNG.choice(_VERBOSE_PREFIXES)
    return prefix + text.lower()


_JARGON_MAP = {
    "fix": "hotfix the regression in",
    "change": "refactor the implementation of",
    "read": "do a code review of",
    "write": "scaffold out",
    "create": "bootstrap",
    "update": "patch",
    "add": "wire up",
    "remove": "deprecate",
    "test": "run the CI suite against",
    "check": "validate the contract of",
}

def _apply_domain_jargon(text: str) -> str:
    """Replace common verbs with domain-specific jargon."""
    result = text
    for plain, jargon in _JARGON_MAP.items():
        pattern = re.compile(r"\b" + re.escape(plain) + r"\b", re.IGNORECASE)
        result = pattern.sub(jargon, result, count=1)
    return result


_CROSS_CLASS_CONTAMINANTS = {
    "qa_simple": "also refactor the implementation while you're at it",
    "qa_with_reads": "and generate a full report document",
    "qa_architecture": "then write the implementation code",
    "agentic_action": "but first explain the architecture",
    "content_generation": "and also fix any bugs you find",
    "dispatch_justification": "while narrating your thought process",
    "inline_narration": "then dispatch tasks to handle it",
}

def _apply_cross_class_contamination(text: str, intent_class: str) -> str:
    """Append a phrase from a conflicting intent class."""
    contaminant = _CROSS_CLASS_CONTAMINANTS.get(intent_class, "and do other things too")
    return f"{text}, {contaminant}"


_NEGATION_PREFIXES = [
    "Do NOT ",
    "Don't ",
    "I don't want you to ",
    "Please avoid ",
    "Skip the part where you ",
]

def _apply_negation(text: str) -> str:
    """Add negation that shouldn't change the core intent classification."""
    # Negate a secondary action, not the primary intent
    prefix = _RNG.choice(_NEGATION_PREFIXES)
    return f"{text}. {prefix}explain your reasoning."


def apply_perturbation(text: str, perturbation_type: str, intent_class: str = "") -> str:
    """Apply a single perturbation type to the text."""
    if perturbation_type == "clean":
        return text
    elif perturbation_type == "typo":
        return _apply_typo(text)
    elif perturbation_type == "filler_words":
        return _apply_filler_words(text)
    elif perturbation_type == "casing_variation":
        return _apply_casing_variation(text)
    elif perturbation_type == "truncated":
        return _apply_truncated(text)
    elif perturbation_type == "verbose":
        return _apply_verbose(text)
    elif perturbation_type == "domain_jargon":
        return _apply_domain_jargon(text)
    elif perturbation_type == "cross_class_contamination":
        return _apply_cross_class_contamination(text, intent_class)
    elif perturbation_type == "negation":
        return _apply_negation(text)
    else:
        return text


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def _load_exemplars(path: Path | None = None) -> list[dict]:
    """Load exemplars from intent_exemplars.json."""
    p = path or _DEFAULT_EXEMPLARS
    if not p.exists():
        return []
    data = load_json_state(p)
    return data.get("exemplars", []) if isinstance(data, dict) else data


def run_benchmark(exemplars_path: Path | None = None, output_path: Path | None = None) -> dict:
    """Run full perturbation benchmark and return results."""
    from classify_intent import classify_intent  # noqa: E402 — lazy import for testability

    exemplars = _load_exemplars(exemplars_path)
    if not exemplars:
        return {
            "verdict": "NO_DATA",
            "reason": "No exemplars found",
            "timestamp": now_iso(),
        }

    results_by_type: dict[str, dict] = {}

    for ptype in PERTURBATION_TYPES:
        correct = 0
        total = 0
        failures: list[dict] = []

        for ex in exemplars:
            text = ex.get("text", "")
            expected = ex.get("intent_class", "")
            if not text or not expected:
                continue

            perturbed = apply_perturbation(text, ptype, expected)
            predicted = classify_intent(perturbed)
            total += 1

            if predicted == expected:
                correct += 1
            else:
                if len(failures) < 5:  # keep up to 5 failure examples per type
                    failures.append({
                        "original": text[:80],
                        "perturbed": perturbed[:80],
                        "expected": expected,
                        "predicted": predicted,
                    })

        accuracy = correct / total if total > 0 else 0.0
        passes_floor = accuracy >= ACCURACY_FLOOR

        results_by_type[ptype] = {
            "accuracy": round(accuracy, 4),
            "correct": correct,
            "total": total,
            "passes_floor": passes_floor,
            "floor": ACCURACY_FLOOR,
            "sample_failures": failures,
        }

    # Overall verdict
    all_pass = all(r["passes_floor"] for r in results_by_type.values())
    failing_types = [t for t, r in results_by_type.items() if not r["passes_floor"]]

    # Accuracy gap: clean vs worst perturbation
    clean_acc = results_by_type.get("clean", {}).get("accuracy", 0.0)
    worst_type = min(results_by_type, key=lambda t: results_by_type[t]["accuracy"])
    worst_acc = results_by_type[worst_type]["accuracy"]
    max_gap = round(clean_acc - worst_acc, 4)

    result = {
        "verdict": "PASS" if all_pass else "FAIL",
        "all_types_above_floor": all_pass,
        "failing_types": failing_types,
        "clean_accuracy": clean_acc,
        "worst_type": worst_type,
        "worst_accuracy": worst_acc,
        "max_accuracy_gap": max_gap,
        "per_type": results_by_type,
        "total_exemplars": len(exemplars),
        "accuracy_floor": ACCURACY_FLOOR,
        "timestamp": now_iso(),
    }

    # Persist
    try:
        target_path = output_path or _BENCHMARK_STATE
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(result, indent=2) + "\n")
    except OSError:
        pass

    return result


def _format_text(result: dict) -> str:
    """Human-readable output."""
    lines = ["=" * 65, "PERTURBATION ROBUSTNESS BENCHMARK", "=" * 65, ""]

    if result.get("verdict") == "NO_DATA":
        lines.append(f"Verdict: {result['verdict']} — {result.get('reason', '')}")
        return "\n".join(lines)

    lines.append(f"Verdict:          {result['verdict']}")
    lines.append(f"Exemplars tested: {result['total_exemplars']}")
    lines.append(f"Accuracy floor:   {result['accuracy_floor']:.0%}")
    lines.append(f"Clean accuracy:   {result['clean_accuracy']:.1%}")
    lines.append(f"Worst type:       {result['worst_type']} ({result['worst_accuracy']:.1%})")
    lines.append(f"Max gap (clean→worst): {result['max_accuracy_gap']:.1%}")
    lines.append("")

    lines.append(f"{'Type':<30} {'Accuracy':>10} {'Pass':>6}")
    lines.append("-" * 50)
    for ptype in PERTURBATION_TYPES:
        info = result["per_type"].get(ptype, {})
        acc = info.get("accuracy", 0.0)
        passes = info.get("passes_floor", False)
        marker = "YES" if passes else "FAIL"
        lines.append(f"{ptype:<30} {acc:>9.1%} {marker:>6}")

    if result.get("failing_types"):
        lines.append("")
        lines.append("FAILING TYPES:")
        for ft in result["failing_types"]:
            info = result["per_type"][ft]
            lines.append(f"  {ft}: {info['accuracy']:.1%} (need >= {ACCURACY_FLOOR:.0%})")
            for fail in info.get("sample_failures", [])[:2]:
                lines.append(f"    Expected: {fail['expected']}, Got: {fail['predicted']}")
                lines.append(f"    Input:    {fail['perturbed']}")

    lines.append("")
    lines.append(f"Saved to: {_BENCHMARK_STATE}")
    return "\n".join(lines)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run perturbation robustness benchmark")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    parser.add_argument("--exemplars-path", type=Path, help="Path to intent_exemplars.json")
    parser.add_argument("--output", type=str, default=str(_BENCHMARK_STATE), help="Output file path for JSON benchmark results")
    args = parser.parse_args()

    output_path = Path(args.output)
    result = run_benchmark(args.exemplars_path, output_path)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_format_text(result))

    if result.get("verdict") == "FAIL":
        sys.exit(1)


if __name__ == "__main__":
    main()
