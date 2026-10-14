#!/usr/bin/env python3
"""cost-router: Gate evaluation logic — pure pass/fail computation and evidence collection.

No output formatting or CLI logic. Exposes evaluate_gates() returning
structured results that callers can render however they need.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# ---------------------------------------------------------------------------
# Gate signal sets
# ---------------------------------------------------------------------------

_TARGETED_READ_SIGNALS = frozenset({
    "show me", "where is", "find the", "what does", "look at",
    "which file", "show the code",
})

_ARCHITECTURE_SIGNALS = frozenset({
    "how does the whole", "all files", "architecture", "flow", "overview",
    "structure of", "how are", "how do these",
})

_DISPATCH_SIGNALS = frozenset({
    "dispatch to", "dispatch this", "dispatching",
    "subagent", "worker", "hand off", "farm out",
    "delegate", "offload", "spinning up a task", "Task()",
    "dispatch_justification",
})

_AGENTIC_VERBS = frozenset({
    "add", "fix", "create", "delete", "refactor", "update", "edit",
    "write", "run", "build", "implement", "change", "remove", "rename",
    "migrate", "deploy",
})

_ACTION_VERBS = frozenset({
    "add", "fix", "create", "delete", "refactor", "update", "edit",
    "write", "run", "build", "implement", "change", "remove", "rename",
    "migrate", "deploy", "move", "install", "configure", "set up",
})

# Gate → (implied_intent, routed_model)
GATE_SPEC = {
    "haiku_explore":        ("qa_with_reads", "haiku"),
    "haiku_read":           ("qa_with_reads", "haiku"),
    "sonnet_architecture":  ("qa_architecture", "sonnet"),
    "sonnet_dispatch":      ("dispatch_justification", "sonnet"),
    "sonnet_simple_action": ("agentic_action", "sonnet"),
}

# Model cost constants (also used by callers for cost projection)
COST_PER_PROMPT = {"haiku": 0.001, "sonnet": 0.01, "opus": 0.05}
CLASSIFIER_COST = 0.002  # estimated per-prompt classifier overhead

# Intent → model mapping (no opus subagents)
INTENT_TO_MODEL = {
    "qa_simple": "haiku",
    "qa_with_reads": "haiku",
    "qa_architecture": "sonnet",
    "agentic_action": "sonnet",
    "content_generation": "sonnet",
    "dispatch_justification": "sonnet",
    "inline_narration": "sonnet",
}


# ---------------------------------------------------------------------------
# Gate signal helpers
# ---------------------------------------------------------------------------


def _has_signal(text_lower: str, signals: frozenset) -> bool:
    return any(s in text_lower for s in signals)


def _has_action_verb(text_lower: str) -> bool:
    """Check if text starts with or contains a standalone agentic verb."""
    words = text_lower.split()
    if not words:
        return False
    # first word is an agentic verb (imperative)
    if words[0] in _AGENTIC_VERBS:
        return True
    # any word is an agentic verb (but require word boundary)
    for w in words:
        if w in _AGENTIC_VERBS:
            return True
    return False


def simulate_gate(text: str) -> str | None:
    """Apply gates in priority order. Returns gate name or None."""
    t = text.lower().strip()

    # (a) Explore agent detection
    if "explore" in t and ("agent" in t or "codebase" in t or "search" in t):
        return "haiku_explore"

    # (b) Read-only intent — targeted read signals WITHOUT action verbs
    if _has_signal(t, _TARGETED_READ_SIGNALS) and not _has_action_verb(t):
        return "haiku_read"

    # (c) Architecture signals — sonnet subagent (no opus subagents)
    if _has_signal(t, _ARCHITECTURE_SIGNALS):
        return "sonnet_architecture"

    # (d) Dispatch signals
    if _has_signal(t, _DISPATCH_SIGNALS):
        return "sonnet_dispatch"

    # (e) Short agentic action (< 60 chars + agentic verb)
    if len(t) < 60 and _has_action_verb(t):
        return "sonnet_simple_action"

    # (f) No gate matched
    return None


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def run_simulation(samples: list[dict], classify_fn: Any) -> list[dict]:
    """Run gates-first vs classifier-first simulation on *samples*.

    *classify_fn* is a callable matching the signature of
    ``classify_intent_detailed(text) -> dict`` with keys
    ``intent``, ``confidence``, ``source``.

    Returns a list of per-sample result dicts.
    """
    results = []
    for s in samples:
        text = s["text"]
        expected = s["expected_intent"]

        # Gate result
        gate = simulate_gate(text)
        if gate:
            gate_intent, gate_model = GATE_SPEC[gate]
            gate_correct = (gate_intent == expected)
        else:
            gate_intent = None
            gate_model = None
            gate_correct = None

        # Classifier result
        cls_result = classify_fn(text)
        cls_intent = cls_result["intent"]
        cls_confidence = cls_result["confidence"]
        cls_source = cls_result["source"]
        cls_correct = (cls_intent == expected)

        results.append({
            "text": text,
            "expected_intent": expected,
            "data_source": s["source"],
            "gate_name": gate,
            "gate_intent": gate_intent,
            "gate_model": gate_model,
            "gate_correct": gate_correct,
            "classifier_intent": cls_intent,
            "classifier_confidence": cls_confidence,
            "classifier_source": cls_source,
            "classifier_correct": cls_correct,
        })
    return results


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------


def compute_metrics(results: list[dict]) -> dict:
    """Compute all pass/fail metrics and evidence from simulation results.

    Returns a structured dict — no formatted strings, no print calls.
    """
    total = len(results)

    # --- Coverage ---
    gate_caught = [r for r in results if r["gate_name"] is not None]
    gate_missed = [r for r in results if r["gate_name"] is None]
    coverage = len(gate_caught) / total * 100

    per_gate_counts = Counter(r["gate_name"] for r in gate_caught)

    # --- Gate accuracy ---
    gate_correct = sum(1 for r in gate_caught if r["gate_correct"])
    gate_accuracy = gate_correct / len(gate_caught) * 100 if gate_caught else 0

    # --- Classifier accuracy (overall) ---
    cls_correct_all = sum(1 for r in results if r["classifier_correct"])
    cls_accuracy_all = cls_correct_all / total * 100

    # --- Classifier accuracy on gate-missed (residual) ---
    cls_correct_residual = sum(1 for r in gate_missed if r["classifier_correct"])
    cls_accuracy_residual = cls_correct_residual / len(gate_missed) * 100 if gate_missed else 0

    # --- Classifier accuracy on gate-caught subset ---
    cls_correct_on_caught = sum(1 for r in gate_caught if r["classifier_correct"])
    cls_accuracy_on_caught = cls_correct_on_caught / len(gate_caught) * 100 if gate_caught else 0

    # --- Uncertainty reduction ---
    uncertain_current = sum(1 for r in results if r["classifier_source"] in ("semantic", "fallback"))
    uncertain_rate_current = uncertain_current / total * 100

    # Under gates-first, gate-caught prompts skip the classifier entirely
    uncertain_residual = sum(
        1 for r in gate_missed if r["classifier_source"] in ("semantic", "fallback")
    )
    uncertain_rate_projected = uncertain_residual / total * 100

    # --- Model distribution: classifier-first ---
    model_dist_classifier = Counter()
    for r in results:
        m = INTENT_TO_MODEL.get(r["classifier_intent"], "sonnet")
        model_dist_classifier[m] += 1

    # --- Model distribution: gates-first ---
    model_dist_gates = Counter()
    for r in gate_caught:
        model_dist_gates[r["gate_model"]] += 1
    for r in gate_missed:
        m = INTENT_TO_MODEL.get(r["classifier_intent"], "sonnet")
        model_dist_gates[m] += 1

    # --- Cost projection per 1000 prompts ---
    scale = 1000 / total

    cost_classifier_first = sum(
        CLASSIFIER_COST + COST_PER_PROMPT.get(INTENT_TO_MODEL.get(r["classifier_intent"], "sonnet"), 0.01)
        for r in results
    ) * scale

    cost_gates_first = 0.0
    for r in gate_caught:
        cost_gates_first += COST_PER_PROMPT.get(r["gate_model"], 0.01)  # no classifier cost
    for r in gate_missed:
        m = INTENT_TO_MODEL.get(r["classifier_intent"], "sonnet")
        cost_gates_first += CLASSIFIER_COST + COST_PER_PROMPT.get(m, 0.01)
    cost_gates_first *= scale

    cost_savings_pct = (1 - cost_gates_first / cost_classifier_first) * 100 if cost_classifier_first else 0

    # --- Error analysis ---
    gate_errors = [r for r in gate_caught if not r["gate_correct"]]
    false_confidence_rate = len(gate_errors) / len(gate_caught) * 100 if gate_caught else 0

    # Severity: wrong-tier vs same-tier
    severe_errors = []
    mild_errors = []
    for r in gate_errors:
        expected_model = INTENT_TO_MODEL.get(r["expected_intent"], "sonnet")
        routed_model = r["gate_model"]
        if expected_model != routed_model:
            tier_diff = abs(
                ["haiku", "sonnet", "opus"].index(routed_model)
                - ["haiku", "sonnet", "opus"].index(expected_model)
            )
            if tier_diff >= 2:
                severe_errors.append(r)
            else:
                mild_errors.append(r)
        else:
            mild_errors.append(r)  # same model but wrong intent class

    # --- Per-class accuracy comparison ---
    classes = sorted(set(r["expected_intent"] for r in results))
    per_class = {}
    for cls in classes:
        cls_samples = [r for r in results if r["expected_intent"] == cls]
        n = len(cls_samples)
        cc = sum(1 for r in cls_samples if r["classifier_correct"])
        gc = [r for r in cls_samples if r["gate_name"] is not None]
        gc_correct = sum(1 for r in gc if r["gate_correct"])
        per_class[cls] = {
            "total": n,
            "classifier_correct": cc,
            "classifier_accuracy": cc / n * 100 if n else 0,
            "gate_caught": len(gc),
            "gate_correct": gc_correct,
            "gate_accuracy": gc_correct / len(gc) * 100 if gc else 0,
            "gate_coverage": len(gc) / n * 100 if n else 0,
        }

    return {
        "total_samples": total,
        "coverage": coverage,
        "per_gate_counts": dict(per_gate_counts),
        "gate_accuracy": gate_accuracy,
        "gate_correct_count": gate_correct,
        "gate_caught_count": len(gate_caught),
        "classifier_accuracy_overall": cls_accuracy_all,
        "classifier_accuracy_on_caught": cls_accuracy_on_caught,
        "classifier_accuracy_residual": cls_accuracy_residual,
        "uncertainty_rate_current": uncertain_rate_current,
        "uncertainty_rate_projected": uncertain_rate_projected,
        "model_dist_classifier": dict(model_dist_classifier),
        "model_dist_gates": dict(model_dist_gates),
        "cost_classifier_first": round(cost_classifier_first, 2),
        "cost_gates_first": round(cost_gates_first, 2),
        "cost_savings_pct": round(cost_savings_pct, 1),
        "false_confidence_rate": round(false_confidence_rate, 1),
        "gate_errors": gate_errors,
        "severe_errors": severe_errors,
        "mild_errors": mild_errors,
        "per_class": per_class,
        "gate_missed_count": len(gate_missed),
    }


def evaluate_gates(samples: list[dict], classify_fn: Any) -> dict[str, Any]:
    """Top-level entry point: simulate + compute metrics.

    Returns ``{"results": [...], "metrics": {...}}`` — structured, not formatted.
    """
    results = run_simulation(samples, classify_fn)
    metrics = compute_metrics(results)
    return {"results": results, "metrics": metrics}
