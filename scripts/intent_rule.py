"""IntentRule registry — open/closed extension point for classify_intent.py.

## Adding a new rule (OCP extension path)

1. Create an IntentRule entry in build_registry() with:
   - pattern: a str substring or compiled re.Pattern
   - intent: one of the seven class names
   - priority: lower number = higher precedence (fires first)
   - domain: human-readable label for grouping/debugging
   - weight: score contribution (default 1.0; bigrams should use 1.5)
   - is_suppressor: if True, sets the named intent score to 0.0 instead of adding

2. Sort order is ascending by priority — rule 10 fires before rule 100.

3. Do NOT edit _score_keywords() inline for new rules — add an IntentRule instead.
   Only edit _score_keywords() when you need a new suppression *interaction* between
   two classes (e.g. "if A > 0 and B > 0, zero out A").

## Priority bands (by convention)

  10-49   Whole-pipeline / end-to-end architecture (highest specificity)
  50-99   Bigram signals (high-fidelity multi-word)
 100-149  Single-word / phrase signals
 150-199  Verb-boundary agentic signals
 200-249  Weak / partial signals
 250+     Fallback / qa_simple boosts
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union


@dataclass
class IntentRule:
    """A single classification rule.

    Attributes:
        pattern:      Substring (str) or compiled regex (re.Pattern) to match
                      against the lowercased, normalized prompt text.
        intent:       Target intent class name (e.g. "agentic_action").
        priority:     Lower = higher precedence. Rules are evaluated in ascending
                      priority order. First match within a priority band wins for
                      suppressor rules; scoring rules accumulate.
        domain:       Human-readable grouping label for debugging/reporting.
        weight:       Score delta applied when pattern matches (default 1.0).
                      Ignored when is_suppressor=True.
        is_suppressor: When True, sets scores[intent] = 0.0 on match instead of
                       adding weight. Use for exclusion/negation rules.
    """
    pattern: Union[str, re.Pattern]
    intent: str
    priority: int
    domain: str
    weight: float = 1.0
    is_suppressor: bool = False


def build_registry() -> list[IntentRule]:
    """Return all IntentRules sorted by ascending priority.

    High-specificity rules have *lower* priority numbers so they are evaluated
    (and accumulated) before low-specificity rules. This prevents broad single-word
    signals from overwhelming narrow bigram/pipeline signals in the scorer.

    Callers: _score_keywords() in classify_intent.py iterates this list to compute
    per-class scores before applying cross-class suppression interactions.
    """
    rules: list[IntentRule] = [

        # -----------------------------------------------------------------------
        # Band 10-49: Whole-pipeline / end-to-end architecture (highest specificity)
        # These must fire before generic "what does" qa_with_reads signals.
        # -----------------------------------------------------------------------
        IntentRule("whole pipeline",         "qa_architecture", 10, "whole_pipeline", 1.0),
        IntentRule("whole system",           "qa_architecture", 10, "whole_pipeline", 1.0),
        IntentRule("entire system",          "qa_architecture", 10, "whole_pipeline", 1.0),
        IntentRule("overall architecture",   "qa_architecture", 10, "whole_pipeline", 1.0),
        IntentRule("big picture",            "qa_architecture", 10, "whole_pipeline", 1.0),
        IntentRule("end to end",             "qa_architecture", 10, "whole_pipeline", 1.0),
        IntentRule("end-to-end",             "qa_architecture", 10, "whole_pipeline", 1.0),
        IntentRule("from start to finish",   "qa_architecture", 10, "whole_pipeline", 1.0),
        IntentRule("complete flow",          "qa_architecture", 10, "whole_pipeline", 1.0),
        IntentRule("full flow",              "qa_architecture", 10, "whole_pipeline", 1.0),

        # -----------------------------------------------------------------------
        # Band 50-99: Bigram signals (weight 1.5 — high-fidelity multi-word)
        # -----------------------------------------------------------------------

        # content_generation bigrams
        IntentRule("write the doc",          "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("write the spec",         "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("write the readme",       "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("write the docs",         "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("draft the",              "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("generate the",           "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("write up",               "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("design doc",             "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("spec document",          "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("changelog",              "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("write docs",             "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("write documentation",    "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("create the doc",         "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("create the guide",       "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("create the readme",      "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("create the spec",        "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("make the doc",           "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("make the guide",         "content_generation", 50, "content_gen_bigram", 1.5),
        IntentRule("make the readme",        "content_generation", 50, "content_gen_bigram", 1.5),

        # dispatch_justification bigrams
        IntentRule("spin up",                "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("kick off",               "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("send to worker",         "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("route to",               "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("route this",             "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("dispatch the",           "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("delegate the",           "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("pass to a",              "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("subagent for",           "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("worker for",             "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("task for the",           "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("spin up a task",         "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("kick off a task",        "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("separate agent",         "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("fo dispatch",            "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("going to dispatch",      "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("will dispatch",          "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("lets dispatch",          "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("let's dispatch",         "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("going to delegate",      "dispatch_justification", 51, "dispatch_bigram", 1.5),
        IntentRule("rather than edit",       "dispatch_justification", 51, "dispatch_bigram", 1.5),

        # inline_narration bigrams
        IntentRule("doing this",             "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("working on it",          "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("editing the",            "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("fixing the",             "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("patching the",           "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("update the file",        "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("correcting the",         "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("adjusting the",          "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("fixing it in place",     "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("handle this",            "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("routing: direct",        "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("this inline",            "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("i'll handle",            "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("doing it myself",        "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("tackling the",           "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("do it myself",           "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("reading the",            "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("reading through",        "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("let me read",            "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("let me look",            "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("let me check",           "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("going through",          "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("walking through",        "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("now i'll",               "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("now i'm",                "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("i'll edit",              "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("i'll open",              "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("i'll fix",               "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("i'll write",             "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("i'll make",              "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("scanning the",           "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("writing the",            "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("working on the",         "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("making the",             "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("looking at",             "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("working on this",        "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("working on a",           "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("implementing the",       "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("implementing this",      "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("applying the",           "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("updating the",           "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("modifying the",          "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("changing the",           "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("inline edit",            "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("inline fix",             "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("inline now",             "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("do it inline",           "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("fix inline",             "inline_narration", 52, "narration_bigram", 1.5),

        # qa_architecture bigrams
        IntentRule("how the system",         "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("how does the system",    "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("explain the flow",       "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("explain the architecture", "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("high level",             "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("system design",          "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("how everything fits",    "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("overall flow",           "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("whole flow",             "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("high-level overview",    "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("how things work",        "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("how does it all",        "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("mental model",           "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("from scratch",           "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("compare the",           "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("what constraints",       "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("what would break",       "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("pipeline look",          "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("what would happen",      "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("tradeoffs between",      "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("trade-offs between",     "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("design tradeoff",        "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("would break if",         "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("if i were to",           "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("if we removed",          "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("walk me through",        "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("architecture overview",  "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("architecture of",        "qa_architecture", 53, "arch_bigram", 1.5),
        IntentRule("routing matrix",         "qa_architecture", 53, "arch_bigram", 1.5),

        # qa_simple bigrams
        IntentRule("what is the",            "qa_simple", 54, "qa_simple_bigram", 1.5),
        IntentRule("what are the",           "qa_simple", 54, "qa_simple_bigram", 1.5),
        IntentRule("how many",               "qa_simple", 54, "qa_simple_bigram", 1.5),

        # -----------------------------------------------------------------------
        # Band 100-149: Single-word / phrase signals (weight 1.0)
        # -----------------------------------------------------------------------

        # content_generation phrase signals
        IntentRule("generate",               "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("create a ",              "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("write a ",               "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("build a ",               "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("make a ",                "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("draft a ",               "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("produce a ",             "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("compose a ",             "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("html file",              "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("html page",              "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("markdown file",          "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("readme",                 "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("documentation",          "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("boilerplate",            "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("scaffold",               "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("scafflod",               "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("template",               "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("skeleton",               "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("docs",                   "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("guidelines",             "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("style guide",            "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("spec doc",               "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("runbook",                "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("onboarding",             "content_generation", 100, "content_gen_signal", 1.0),

        # dispatch_justification phrase signals
        IntentRule("dispatch to",            "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("dispatch this",          "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("dispatching",            "dispatch_justification", 101, "dispatch_signal", 1.0),
        # Shorthand notation such as "dispatch - haiku - tests": a prompt that opens with the bare word.
        IntentRule(re.compile(r"^\s*dispatch\b"), "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("subagent",               "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("worker",                 "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("hand off",               "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("farm out",               "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("delegate",               "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("offload",                "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("spinning up a task",     "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("task()",                 "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("fo situation",           "dispatch_justification", 101, "dispatch_signal", 1.0),
        IntentRule("dispatch_justification", "dispatch_justification", 101, "dispatch_signal", 1.0),

        # inline_narration phrase signals
        IntentRule("doing this myself",      "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("no dispatch",            "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("handling this directly", "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("editing the file now",   "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("working on it here",     "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("no need to dispatch",    "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("tackling this myself",   "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("i'll handle",            "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("i'll do this",           "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("i'll fix",               "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("doing it now",           "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("i'm reading",            "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("i'm editing",            "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("i am handling",          "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("handling this now",      "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("fixing this now",        "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("editing this now",       "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("fix this now",           "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("fix it now",             "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("fix in place",           "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("fix it in place",        "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("i am handle",            "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("handle this directly",   "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("handle this now",        "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("work on it here",        "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("do this myself",         "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("do it now",              "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("tackle this myself",     "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("i'm read",               "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("i'm edit",               "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("edit the file now",      "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("edit this now",          "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("editing directly",       "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("editing in place",       "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("editing this",           "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("do it here",             "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("doing it here",          "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("im doing it here",       "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("it here now",            "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("doing this here",        "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("reading this",           "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("let me open",            "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("going to read",          "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("about to read",          "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("about to edit",          "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("fixn ",                  "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("fixng ",                 "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("currently read",         "inline_narration", 102, "narration_signal", 1.0),
        IntentRule("i see the issue",        "inline_narration", 102, "narration_signal", 1.0),

        # qa_architecture phrase signals
        IntentRule("how does the whole",     "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("all files",              "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("flow",                   "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("overview",               "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("structure of",           "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("how are",                "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("how do these",           "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("overall design",         "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("lifecycle",              "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("integrate with",         "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("interact with",          "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("how does",               "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("how do they",            "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("explain how the",        "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("new to this codebase",   "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("understand how",         "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("architecture:",          "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("routing matrix",         "qa_architecture", 103, "arch_signal", 1.0),

        # qa_with_reads targeted read signals
        IntentRule("show me",                "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("where is",               "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("find the",               "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("look at",                "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("which file",             "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("show the code",          "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("find where",             "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("where does",             "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("check the",              "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("examine the",            "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("inspect the",            "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("peek at",                "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("let me see",             "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("let me open",            "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("bring up",               "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("point me to",            "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("open up",                "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("what's in",              "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("where are",              "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("take me to",             "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("pull up",                "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("show where",             "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("find me",                "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("locate the",             "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("check if there",         "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("is there a test",        "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("check if",               "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("find:",                  "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("need to see",            "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("i need to see",          "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("what does the",          "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("what does this",         "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("what does it",           "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("what does:",             "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("does this file",         "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("does the file",          "qa_with_reads", 104, "targeted_read", 1.0),

        # qa_simple signals
        IntentRule("what is",                "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("what are",               "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("who is",                 "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("when did",               "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("where did",              "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("what does",              "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("explain what",           "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("tell me what",           "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("what does it mean",      "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("how many",               "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("how much",               "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("is it true",             "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("are there",              "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("what version",           "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("what type",              "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("what kind",              "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("is there",               "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("is this",                "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("unit test",              "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("are ther",               "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("how is",                 "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("by default",             "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("enabled by",             "qa_simple", 105, "qa_simple_signal", 1.0),
        IntentRule("concurrency cap",        "qa_simple", 105, "qa_simple_signal", 1.0),
    ]

    # T18 targeted additions (confusable-pair gaps); each is a phrase with one clear intent.
    rules += [
        IntentRule("in this session",        "inline_narration", 52, "narration_bigram", 1.5),
        IntentRule("open the",               "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("where in the code",      "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("defined in",             "qa_with_reads", 104, "targeted_read", 1.0),
        IntentRule("layering",               "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("plugs into",             "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("plug into",              "qa_architecture", 103, "arch_signal", 1.0),
        IntentRule("draft an ",              "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("write an ",              "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("compose an ",            "content_generation", 100, "content_gen_signal", 1.0),
        IntentRule("release notes",          "content_generation", 50, "content_gen_bigram", 1.5),
        # Bare imperative "summarize" (no object): a text-production request, not a question.
        IntentRule(re.compile(r"^\s*summari[sz]e\s*$"), "content_generation", 50, "content_gen_bigram", 1.5),
    ]
    return sorted(rules, key=lambda r: r.priority)
