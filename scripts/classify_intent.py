"""cost-router hook: classify prompt intent and inject a read-budget hint.

Keyword heuristics across seven classes (qa_simple, qa_with_reads,
qa_architecture, agentic_action, content_generation,
dispatch_justification, inline_narration). Observational — always allows.
Host shape via hook_event_name.
Logs byte count, intent, tier, and model — never raw prompt text.

SEM-201: After keyword classification, if result is qa_simple the semantic
scorer is invoked as a second-pass to upgrade to a richer class when
sufficient confidence exists.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    QA_ARCHITECTURE_MAX_FILES,
    QA_WITH_READS_MAX_FILES,
    QA_WITH_READS_MAX_LINES,
    append_event,
    read_stdin_json,
    session_id_of,
)
from _model_utils import model_of, tier_of_model  # noqa: E402
from intent_rule import build_registry  # noqa: E402

_INTENT_REGISTRY = build_registry()

# ---------------------------------------------------------------------------
# Intent signal lists
# ---------------------------------------------------------------------------

_AGENTIC_VERBS = frozenset({
    "add", "fix", "create", "delete", "refactor", "update", "edit",
    "write", "run", "build", "implement", "change", "remove", "rename",
    "migrate", "deploy", "scaffold",
    "replace", "tidy", "clean", "polish", "organize", "sort", "lint", "format", "patch",
})

_ARCHITECTURE_SIGNALS = frozenset({
    "how does the whole", "all files", "flow", "overview",
    "structure of", "how are", "how do these", "overall design", "lifecycle",
    "integrate with", "interact with", "how does", "how do they",
    "explain how the", "new to this codebase", "understand how",
    "architecture:", "routing matrix",
})

# Whole-pipeline and end-to-end architecture indicators (C4-FIX-001)
# These are higher-fidelity than generic "what does" targeted reads.
_ARCHITECTURE_WHOLE_PIPELINE_SIGNALS = frozenset({
    "whole pipeline", "whole system", "entire system", "overall architecture",
    "big picture", "end to end", "end-to-end", "from start to finish",
    "complete flow", "full flow",
})

_TARGETED_READ_SIGNALS = frozenset({
    "show me", "where is", "find the", "look at",
    "which file", "show the code", "find where", "where does",
    "check the", "examine the", "inspect the", "peek at",
    "let me see", "let me open", "bring up",
    "point me to", "open up", "what's in", "where are",
    "take me to", "pull up", "show where", "find me", "locate the",
    "check if there", "is there a test", "check if", "find:",
    "need to see", "i need to see", "what does the", "what does this", "what does it", "what does:",
    "does this file", "does the file",
})

_CONTENT_GEN_SIGNALS = frozenset({
    "generate", "create a ", "write a ", "build a ", "make a ",
    "draft a ", "produce a ", "compose a ",
    "html file", "html page", "markdown file", "readme",
    "documentation", "boilerplate", "scaffold", "scafflod", "template",
    "skeleton",
    "docs", "guidelines", "style guide", "spec doc", "runbook",
    "onboarding",
})

_DISPATCH_SIGNALS = frozenset({
    "dispatch to", "dispatch this", "dispatching",
    "subagent", "worker", "hand off", "farm out",
    "delegate", "offload", "spinning up a task", "task()",
    "fo situation", "dispatch_justification",
})

_ANALYTICAL_QA_SIGNALS = frozenset({
    "evaluate", "justify", "impact", "workaround", "tradeoff", "assess",
    "efficiency", "viable", "compare", "operational cost",
    "cost benefit", "should we", "how efficient",
    "data pipeline", "what will happen", "justify the",
})

_ANALYTICAL_QA_BIGRAMS = frozenset({
    "cost diff", "cost impact", "design solution", "what cost", "how much",
    "architect evaluate", "product owner", "data architect",
    "risk register", "go no go",
})

_INLINE_NARRATION_SIGNALS = frozenset({
    "doing this myself", "no dispatch", "handling this directly",
    "editing the file now", "reading the", "working on it here",
    "no need to dispatch", "tackling this myself",
    "i'll handle", "i'll do this", "i'll fix", "doing it now",
    "i'm reading", "i'm editing", "i am handling", "handling this now",
    "fixing this now", "editing this now", "fix this now", "fix it now",
    "fix in place", "fix it in place",
    "i am handle", "handle this directly", "handle this now",
    "work on it here", "do this myself", "do it now", "tackle this myself",
    "i'm read", "i'm edit", "edit the file now", "edit this now",
    "editing directly", "editing in place", "editing this",
    "do it here", "doing it here", "im doing it here", "it here now", "doing this here",
    "reading this", "let me open", "going to read", "about to read", "about to edit",
    "fixn ", "fixng ",
    "currently read", "i see the issue",
})

_QA_SIMPLE_SIGNALS = frozenset({
    "what is", "what are", "who is", "when did", "where did",
    "what does", "explain what", "tell me what", "what does it mean",
    "how many", "how much", "is it true", "are there", "what version",
    "what type", "what kind", "is there", "is this",
    "unit test", "are ther", "how is", "by default", "enabled by",
    "concurrency cap",
})

# ---------------------------------------------------------------------------
# Bigram / trigram signals (T014) — higher-fidelity multi-word phrases
# ---------------------------------------------------------------------------

_DISPATCH_BIGRAMS = frozenset({
    "spin up", "kick off", "send to worker", "route to",
    "route this", "dispatch the", "delegate the", "pass to a",
    "subagent for", "worker for", "task for the",
    "spin up a task", "kick off a task", "separate agent",
    "fo dispatch", "going to dispatch", "will dispatch",
    "lets dispatch", "let's dispatch", "going to delegate", "rather than edit",
})

_ARCHITECTURE_BIGRAMS = frozenset({
    "how the system", "how does the system", "explain the flow",
    "explain the architecture", "high level", "system design",
    "how everything fits", "overall flow", "whole flow",
    "high-level overview", "how things work", "how does it all",
    "mental model", "from scratch", "compare the", "what constraints",
    "what would break", "pipeline look", "what would happen",
    "tradeoffs between", "trade-offs between", "design tradeoff",
    "would break if", "if i were to", "if we removed",
    "walk me through", "architecture overview", "architecture of", "routing matrix",
})

_NARRATION_BIGRAMS = frozenset({
    "doing this", "working on it", "editing the", "fixing the",
    "patching the", "update the file",
    "correcting the", "adjusting the", "fixing it in place",
    "handle this", "routing: direct", "this inline",
    "i'll handle", "doing it myself", "tackling the",
    "do it myself",
    "reading the", "reading through", "let me read", "let me look", "let me check",
    "going through", "walking through", "now i'll", "now i'm",
    "i'll edit", "i'll open", "i'll fix", "i'll write", "i'll make",
    "scanning the", "writing the", "working on the", "making the",
    "looking at",
    "working on this", "working on a", "implementing the", "implementing this",
    "applying the", "updating the", "modifying the", "changing the",
    "inline edit", "inline fix", "inline now", "do it inline", "fix inline", "looking at",
})

_CONTENT_GEN_BIGRAMS = frozenset({
    "write the doc", "write the spec", "write the readme", "write the docs", "draft the", "generate the",
    "write up", "design doc", "spec document", "changelog",
    "write docs", "write documentation",
    "create the doc", "create the guide", "create the readme", "create the spec",
    "make the doc", "make the guide", "make the readme",
})

# QA/inquiry bigrams — weighted 1.5x for simple question detection.
_QA_SIMPLE_BIGRAMS = frozenset({
    "what is the", "what are the", "how many",
})

# Typo canonicalization — fuzzy matching for common misspellings.
_TYPO_CANONICAL = {
    "clasess": "classes",
    "scring": "scoring",
    "defalt": "default",
    "ther": "there",
    "addd": "add",
    "feild": "field",
    "shuld": "should",
    "benfit": "benefit",
}

# Informal prompts often omit the trailing "?". Yes/no and "what is" openers still mark a simple
# question. how/where/why/which/who/when are deliberately excluded: those open questions are
# often architecture or file-read prompts, so they need keyword corroboration (see
# classify_intent_detailed) rather than being treated as simple questions.
_INTERROGATIVE_OPENERS = frozenset({
    "is", "are", "does", "do", "did", "can", "could", "should", "would", "will",
    "what", "whats", "what's",
})


def _is_question(text: str) -> bool:
    stripped = text.rstrip()
    if stripped.endswith("?"):
        return True
    first = re.split(r"\W+", _normalize_typos(stripped.lower()).lstrip(), maxsplit=1)[0]
    return first in _INTERROGATIVE_OPENERS

# File-type tokens that indicate agentic code/config creation rather than content gen.
_CODE_FILE_TOKENS = frozenset({
    ".py", ".yaml", ".yml", ".json", ".ts", ".js", ".sh", ".toml", ".ini", ".cfg",
    "test script", "fixture", "stub", "stubs", "spec file",
})

# ---------------------------------------------------------------------------
# Negative exclusion / tiebreaking (T015)
# ---------------------------------------------------------------------------

# When agentic verbs match but these context words also appear, suppress agentic
# and let the prompt fall through to semantic scoring.
_AGENTIC_EXCLUSION_CONTEXT = frozenset({
    "worker", "dispatch to", "dispatch this", "delegate to", "task()",
    "doing inline", "working inline", "directly", "myself", "here in",
    "don't change", "not asking", "not asking you to", "did anyone", "tell me", "just tell",
    "who ",
    "yesterday", "last week", "already", "was that", "was it", "was this",
    "any thoughts", "looks right", "does it look", "did that", "was done",
    "thoughts on", "approach", "thoughts about",
})

# When qa_with_reads signals match but these narration context words also appear,
# suppress qa_with_reads so it falls through.
_QA_READS_NARRATION_SUPPRESSORS = frozenset({
    "now", "editing", "fixing", "directly", "myself",
    "right now", "in place", "handling",
})

# ---------------------------------------------------------------------------
# Context window disambiguation (T017)
# ---------------------------------------------------------------------------

_DISPATCH_BOOSTERS = frozenset({
    "subagent", "task", "parallel", "background", "separate",
    "agent", "orchestrate", "batch",
})

_NARRATION_BOOSTERS = frozenset({
    "now", "here", "directly", "myself", "currently", "just",
    "right", "immediately", "already",
})

_AGENTIC_SUPPRESSORS_NEAR_DISPATCH = frozenset({
    "worker", "subagent", "delegate", "dispatch", "agent", "task(",
})

_NEGATION_DISPATCH_PHRASES = frozenset({
    "no worker", "no subagent", "no agent", "no dispatch",
    "no reason to", "rather than agent", "rather than dispatch",
    "without worker", "without subagent",
    "without a worker", "without a subagent", "without an agent", "without dispatching",
    "don't dispatch", "do not dispatch", "don't spin up",
    "rather than spin up", "rather than spinning up",
    "instead of spinning", "instead of dispatching",
})

_QUESTION_STARTERS = frozenset({
    "what is", "what are", "what does", "what do", "what should",
    "how do", "how does", "how should", "how would", "how can",
    "why does", "why do", "why is", "why are", "why would",
    "when does", "when do", "when should", "when would",
    "where does", "where do", "where is", "where are",
    "which is", "which are", "which should", "which would",
    "can you", "could you", "would you", "should i", "should we",
    "is it", "is this", "are there", "is there", "does it",
})


# ---------------------------------------------------------------------------
# Canonical normalization (T019) — collapse paraphrases to similar forms
# ---------------------------------------------------------------------------

_FILLER_WORDS = frozenset({
    "um", "uh", "like", "you know", "basically", "so", "well", "actually",
    "i mean", "just", "really", "pretty much", "kind of", "sort of",
    "honestly", "literally", "obviously", "clearly", "essentially",
    "hey", "hi", "hello", "please", "could you", "can you", "would you",
    "i was wondering", "quick question", "if you don't mind",
})

_FILLER_PHRASES = sorted(
    (f for f in _FILLER_WORDS if " " in f), key=len, reverse=True
)

_VERB_LEMMA = {
    "shows": "show", "showed": "show", "showing": "show", "shown": "show",
    "finds": "find", "found": "find", "finding": "find",
    "looks": "look", "looked": "look", "looking": "look",
    "tells": "tell", "told": "tell", "telling": "tell",
    "gives": "give", "gave": "give", "giving": "give", "given": "give",
    "makes": "make", "made": "make", "making": "make",
    "gets": "get", "got": "get", "getting": "get",
    "puts": "put", "putting": "put",
    "takes": "take", "took": "take", "taking": "take", "taken": "take",
    "writes": "write", "wrote": "write", "writing": "write", "written": "write",
    "reads": "read", "reading": "read",
    "runs": "run", "ran": "run", "running": "run",
    "builds": "build", "built": "build", "building": "build",
    "creates": "create", "created": "create", "creating": "create",
    "adds": "add", "added": "add", "adding": "add",
    "fixes": "fix", "fixed": "fix", "fixing": "fix",
    "changes": "change", "changed": "change", "changing": "change",
    "removes": "remove", "removed": "remove", "removing": "remove",
    "updates": "update", "updated": "update", "updating": "update",
    "deletes": "delete", "deleted": "delete", "deleting": "delete",
    "implements": "implement", "implemented": "implement", "implementing": "implement",
    "refactors": "refactor", "refactored": "refactor", "refactoring": "refactor",
    "generates": "generate", "generated": "generate", "generating": "generate",
    "deploys": "deploy", "deployed": "deploy", "deploying": "deploy",
    "migrates": "migrate", "migrated": "migrate", "migrating": "migrate",
    "renames": "rename", "renamed": "rename", "renaming": "rename",
    "explains": "explain", "explained": "explain", "explaining": "explain",
    "describes": "describe", "described": "describe", "describing": "describe",
    "pulls": "pull", "pulled": "pull", "pulling": "pull",
    "scafflod": "scaffold", "explan": "explain", "wher": "where", "architecure": "architecture",
    "refactr": "refactor", "updat": "update", "remov": "remove", "fixe": "fix",
    "handlin": "handle", "wat": "what", "directy": "directly",
    "points": "point", "pointed": "point", "pointing": "point",
    "handles": "handle", "handled": "handle", "handling": "handle", "handlin": "handle",
    "handing": "hand", "handed": "hand", "hands": "hand",
    "kicking": "kick", "kicked": "kick", "kicks": "kick",
    "dispatches": "dispatch", "dispatched": "dispatch", "dispatching": "dispatch",
    "delegates": "delegate", "delegated": "delegate", "delegating": "delegate",
}


def _normalize_canonical(text: str) -> str:
    """Strip fillers, lemmatize verbs, normalize whitespace."""
    result = text.lower().strip()
    for phrase in _FILLER_PHRASES:
        result = result.replace(phrase, " ")
    words = result.split()
    words = [w for w in words if w not in _FILLER_WORDS]
    words = [_VERB_LEMMA.get(w.rstrip('.,!?;'), w.rstrip('.,!?;')) for w in words]
    return " ".join(words)


def _normalize_typos(text: str) -> str:
    """Fix common typos using word-boundary regex to prevent partial matches."""
    result = text
    for typo, canonical in _TYPO_CANONICAL.items():
        result = re.sub(r'\b' + re.escape(typo) + r'\b', canonical, result)
    return result


# ---------------------------------------------------------------------------
# Primary clause extraction (T020) — resist cross-class contamination
# ---------------------------------------------------------------------------

_CLAUSE_SPLITTERS = re.compile(
    r",\s*(?:and\s+)?(?:also|then|while|but\s+also|but\s+first)\s+(?!(?:evaluate|assess|justify)\b)",
    re.IGNORECASE,
)


def _extract_primary_clause(text: str) -> str:
    """Extract the primary intent clause, stripping appended contaminants."""
    parts = _CLAUSE_SPLITTERS.split(text)
    if len(parts) > 1:
        return parts[0].strip()
    negation_split = re.split(
        r"\.\s*(?:Do\s+NOT|Don't|I\s+don't\s+want\s+you\s+to|Please\s+avoid|Skip\s+the)\s+",
        text,
        flags=re.IGNORECASE,
    )
    if len(negation_split) > 1:
        return negation_split[0].strip()
    return text


def _context_boost(text_words: list[str], match_word: str, boosters: frozenset) -> float:
    """Return +0.5 if any booster appears within 5 words of match_word."""
    try:
        idx = text_words.index(match_word)
    except ValueError:
        return 0.0
    window = set(text_words[max(0, idx - 5):idx + 6])
    if window & boosters:
        return 0.5
    return 0.0


# Minimum semantic confidence to upgrade from qa_simple to a richer class.
_SEMANTIC_UPGRADE_THRESHOLD = 0.40


def _hints() -> dict[str, str]:
    """Hint text injected into additional_context per intent."""
    return {
        "qa_simple": (
            "[cost-router] qa_simple detected — answer directly, no file reads needed."
        ),
        "qa_with_reads": (
            "[cost-router] qa_with_reads detected — use targeted/section reads only. "
            f"Max {QA_WITH_READS_MAX_FILES} file reads. "
            f"Never load full file >{QA_WITH_READS_MAX_LINES}L."
        ),
        "qa_architecture": (
            f"[cost-router] qa_architecture detected — use section reads, max {QA_ARCHITECTURE_MAX_FILES} files. Never full-file load."
        ),
        "analytical_qa": (
            "[cost-router] analytical_qa detected — Dispatch to haiku subagent — "
            "analytical synthesis question requiring judgment not code reads."
        ),
        "agentic_action": "",
        "content_generation": (
            "[cost-router] content_generation detected — bulk text output. "
            "Use /fast mode or dispatch to a Fast worker to reduce output token cost."
        ),
        "dispatch_justification": (
            "[cost-router] dispatch_justification detected — task delegation decision."
        ),
        "inline_narration": (
            "[cost-router] inline_narration detected — inline work narration."
        ),
    }


def _hints_with_source(source: str) -> dict[str, str]:
    """Return hints dict with advisory appended for semantic/fallback sources."""
    base = _hints()
    if source in ("semantic", "fallback"):
        advisory = " [source: semantic scorer]"
        return {k: (v + advisory if v else v) for k, v in base.items()}
    return base


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


# Tie resolution order: when two classes have equal scores, prefer the
# class that appears earlier in this list.
_TIE_PRIORITY = [
    "dispatch_justification", "inline_narration", "content_generation",
    "agentic_action", "qa_architecture", "analytical_qa", "qa_with_reads", "qa_simple",
]


def _score_keywords(text: str) -> dict[str, float]:
    """Score *text* against all intent classes simultaneously.

    Returns a dict mapping each class name to a float score.
    Scoring weights:
      - Bigram/trigram match  = 1.5
      - Single-word signal    = 1.0
      - Word-boundary verb    = 0.8
      - Weak/partial match    = 0.3
    T015 exclusion penalties are applied as negative weights.
    """
    text = _extract_primary_clause(text)
    text = _normalize_canonical(text)

    lower = text.lower()
    lower = _normalize_typos(lower)
    words = frozenset(re.split(r"\W+", lower))

    scores: dict[str, float] = {cls: 0.0 for cls in _TIE_PRIORITY}

    # --- Registry-driven scoring (bands 10-149) ---
    # Rules are sorted by ascending priority; high-specificity rules (low numbers)
    # accumulate before broad single-word signals (higher numbers).
    for rule in _INTENT_REGISTRY:
        matched = (
            rule.pattern.search(lower)
            if isinstance(rule.pattern, re.Pattern)
            else rule.pattern in lower
        )
        if matched:
            if rule.is_suppressor:
                scores[rule.intent] = 0.0
            else:
                scores[rule.intent] += rule.weight

    # --- Word-boundary verb matching — weight 0.8 ---
    agentic_matches = words & _AGENTIC_VERBS
    scores["agentic_action"] += len(agentic_matches) * 0.8

    # --- T015: exclusion suppressions ---
    if scores["agentic_action"] > 0:
        if any(ctx in lower for ctx in _AGENTIC_EXCLUSION_CONTEXT):
            scores["agentic_action"] = 0.0

    if scores["qa_with_reads"] > 0:
        if any(ctx in lower for ctx in _QA_READS_NARRATION_SUPPRESSORS):
            scores["qa_with_reads"] = 0.0

    if "look at" in lower and scores["qa_with_reads"] == 0 and scores["inline_narration"] == 0:
        if any(ctx in lower for ctx in _QA_READS_NARRATION_SUPPRESSORS):
            scores["inline_narration"] = 1.5

    # --- Negated read intent: "without any need to look at files" is NOT a file-read request ---
    if scores["qa_with_reads"] > 0 and re.search(
        r"\b(?:without|no need to|not need to|don'?t need to|do not need to|never need to)\b[^.;]{0,30}?\b(?:look|read|open|check|inspect)",
        lower,
    ):
        scores["qa_with_reads"] = 0.0

    # --- Conditional "is there" suppressor: suppress qa_with_reads only when agentic co-occurs ---
    if scores["qa_with_reads"] > 0 and scores["agentic_action"] > 0 and "is there" in lower:
        scores["qa_with_reads"] = 0.0

    # --- Definitional suppressor: "what does X mean" questions are definitional, not file-read ---
    if "what does" in lower and " mean" in lower:
        scores["qa_with_reads"] = 0.0

    # --- analytical_qa scoring ---
    signal_hits = sum(1 for sig in _ANALYTICAL_QA_SIGNALS if sig in lower)
    bigram_hits = sum(1 for bg in _ANALYTICAL_QA_BIGRAMS if bg in lower)
    analytical_score = signal_hits * 1.5 + bigram_hits * 2.0
    # Suppress if agentic_action score > 2.0 (user asking model to DO something)
    if analytical_score > 0 and scores["agentic_action"] <= 2.0:
        # Boost if interrogative and long enough to be a substantive question
        if "?" in text and len(text) > 100:
            analytical_score += 1.0
        scores["analytical_qa"] = analytical_score

    # --- Negation-of-dispatch suppressor: clear dispatch, seed narration ---
    if scores["dispatch_justification"] > 0:
        if any(phrase in lower for phrase in _NEGATION_DISPATCH_PHRASES):
            scores["dispatch_justification"] = 0.0
            scores["inline_narration"] = max(scores["inline_narration"], 0.5)

    # --- Question boost: interrogative form reinforces qa_simple (post-agentic) ---
    if lower.rstrip().endswith("?") and scores["agentic_action"] == 0:
        already_signaled = any(v >= 1.0 for k, v in scores.items() if k != "qa_simple")
        if not already_signaled:
            scores["qa_simple"] += 1.0

    # --- "What does X do?" tiebreaker: general capability question → qa_simple ---
    # Normalized text strips trailing "?" so match " do" at end (normalized from "do?").
    # Only fire when qa_with_reads is sole scorer (no other active signals).
    # Skip when "what does the" is present — that pattern indicates a code-symbol question (qa_with_reads).
    if re.search(r"\bdo\??$", lower.rstrip()) and "what does" in lower and " file" not in lower and "what does the" not in lower:
        other_active = any(v > 0 for k, v in scores.items() if k not in ("qa_with_reads", "qa_simple"))
        if not other_active:
            scores["qa_simple"] = max(scores["qa_simple"], scores["qa_with_reads"] + 0.1)

    # --- Code-symbol boost: "what does <identifier>" → qa_with_reads over qa_simple ---
    # Only fire when "what does" is the trigger (not "tell me what", "explain what", etc.)
    # and the text contains a code symbol (underscore identifier or camelCase).
    if scores["qa_simple"] >= 1.0 and scores["qa_with_reads"] == 0.0:
        if "what does" in lower and not any(p in lower for p in ("tell me what", "explain what", " mean")) and not lower.rstrip("? ").endswith(" do"):
            if re.search(r"\b\w+_\w+\b", lower) or re.search(r"\b[a-z]+[A-Z]\w+\b", text):
                scores["qa_with_reads"] = scores["qa_simple"] + 0.1

    # --- Return suppressor: "what does X return" is architecture, not a read ---
    if "what does" in lower and re.search(r"\breturn\b", lower):
        scores["qa_architecture"] = 0.0
        if any(sig in lower for sig in _ARCHITECTURE_BIGRAMS):
            scores["qa_with_reads"] = 0.0
    # --- qa_simple dominance: suppress dispatch only when dispatch score is weak/ambiguous ---
    # Don't suppress if dispatch has a dedicated signal (score >= 1.0) — those are explicit.
    if scores["qa_simple"] >= 1.5 and scores["dispatch_justification"] <= 1.0:
        if not any(sig in lower for sig in _DISPATCH_BIGRAMS):
            scores["dispatch_justification"] = 0.0

    if scores["agentic_action"] <= 1.0 and re.search(r"\bbefore\s+i\b", lower) and bool(words & {"what", "how", "why", "which", "where", "when"}):
        scores["agentic_action"] = 0.0
        scores["qa_simple"] = max(scores["qa_simple"], 1.5)

    # Suppress content_generation when inline narration context is present
    if scores["content_generation"] > 0:
        narration_ctx = [ctx for ctx in _NARRATION_BOOSTERS if ctx in lower and ctx not in ("now", "just", "right")]
        if narration_ctx:
            scores["content_generation"] = 0.0
            # Also give a weak narration score so narration can win over agentic
            if scores["agentic_action"] > 0:
                scores["inline_narration"] = max(scores["inline_narration"], scores["agentic_action"] + 0.1)
                scores["agentic_action"] = 0.0

    # Leading code-mutation imperative ("Fix the bug ... and write a changelog line"): the primary
    # task is the code change; the trailing text-production step must not outvote it.
    if scores["content_generation"] > 0 and scores["agentic_action"] > 0:
        _lead = re.match(r"\s*(fix|patch|refactor|rename|migrate|debug)\b", lower)
        if _lead and re.search(r"\band\b", lower):
            scores["agentic_action"] = max(scores["agentic_action"], scores["content_generation"] + 0.1)

    # Suppress content_generation when target is a code/config file (agentic creation).
    # "create a test file", "generate yaml stubs" etc. are agentic, not content gen.
    if scores["content_generation"] > 0:
        if any(tok in lower for tok in _CODE_FILE_TOKENS):
            scores["content_generation"] = 0.0
            scores["agentic_action"] = max(scores["agentic_action"], 0.8)

    # Narration cues after "while I keep/continue/work ..." describe the user's own parallel work
    # while a worker runs; they are a dispatch hallmark, not inline narration.
    if scores["dispatch_justification"] > 0 and scores["inline_narration"] > 0:
        if re.search(r"\bwhile\s+i\s+(?:keep|continue|am|work|carry)", lower):
            scores["inline_narration"] = 0.0

    # --- T017: context window boosters/suppressors ---
    text_words = lower.split()

    # Boost dispatch when dispatch boosters appear near dispatch signals
    if scores["dispatch_justification"] > 0:
        for signal in _DISPATCH_BIGRAMS | _DISPATCH_SIGNALS:
            first_word = signal.split()[0]
            boost = _context_boost(text_words, first_word, _DISPATCH_BOOSTERS)
            if boost > 0:
                scores["dispatch_justification"] += boost
                break

    # Boost narration when narration boosters appear near narration signals
    if scores["inline_narration"] > 0:
        for signal in _NARRATION_BIGRAMS | _INLINE_NARRATION_SIGNALS:
            first_word = signal.split()[0]
            boost = _context_boost(text_words, first_word, _NARRATION_BOOSTERS)
            if boost > 0:
                scores["inline_narration"] += boost
                break

    # Suppress agentic when dispatch context appears near agentic verbs
    if scores["agentic_action"] > 0 and scores["dispatch_justification"] > 0:
        for verb in words & _AGENTIC_VERBS:
            suppress = _context_boost(text_words, verb, _AGENTIC_SUPPRESSORS_NEAR_DISPATCH)
            if suppress > 0:
                scores["agentic_action"] = 0.0
                break

    return scores


def classify_intent(text: str) -> str:
    """Return one of seven intent category strings via hybrid keyword scoring.

    Uses _score_keywords() to score all categories simultaneously. If exactly
    one class scored > 0, returns it directly. If multiple classes scored > 0,
    picks the highest scorer with _TIE_PRIORITY tiebreaking — no ambiguity gap.
    If no class scored, returns 'qa_simple'.
    """
    scores = _score_keywords(text)

    # Filter to classes with non-zero scores
    active = {cls: sc for cls, sc in scores.items() if sc > 0}

    if not active:
        return "qa_simple"

    # Pick highest scorer; ties broken by priority order
    return max(
        active,
        key=lambda cls: (active[cls], -_TIE_PRIORITY.index(cls)),
    )


def classify_work_unit_intent(entry: dict) -> str:
    """Classify the current work unit as 'mechanical' or 'reasoning' based on tool history.

    Signals for 'mechanical':
    - agentic_action prompt intent (already resolved what to do)
    - 2+ Read calls completed before first Write (spec is loaded)
    - mechanical_similarity_flag is True (RD-2 detected structural similarity)
    - bulk_write_file_types has entries (RD-7 is tracking same-type writes)

    Signals for 'reasoning':
    - qa_* prompt intent
    - 0 Read calls before first Write (agent is reasoning without reference)
    - No similarity flag

    Returns 'mechanical' or 'reasoning'.
    """
    signals_mechanical = 0
    signals_reasoning = 0

    # Signal 1: Prompt-level intent (single signal is weak; needs support)
    prompt_intent = entry.get("prompt_intent", "")
    if prompt_intent == "agentic_action":
        signals_mechanical += 0.5  # Weak signal alone; needs supporting evidence
    elif prompt_intent in ("qa_simple", "qa_with_reads", "qa_architecture", "content_generation"):
        signals_reasoning += 1

    # Signal 2: Reads before writes suggest spec is loaded (strong signal)
    reads_done = int(entry.get("read_count", 0))
    writes_done = int(entry.get("inline_mutations_since_dispatch", 0))
    if reads_done >= 2 and writes_done >= 1:
        signals_mechanical += 2  # Strong signal
    elif reads_done == 0 and writes_done >= 1:
        signals_reasoning += 1

    # Signal 3: RD-2 structural similarity detected (very strong signal)
    if entry.get("mechanical_similarity_flag"):
        signals_mechanical += 3  # Very strong signal

    # Signal 4: RD-7 bulk-write tracking has 3+ entries (strong signal)
    file_types = entry.get("bulk_write_file_types", {})
    total_files = sum(len(v) if isinstance(v, list) else 0 for v in file_types.values())
    if total_files >= 3:
        signals_mechanical += 2  # Strong signal only if we have many files

    return "mechanical" if signals_mechanical > signals_reasoning else "reasoning"


# ---------------------------------------------------------------------------
# Semantic fallback (SEM-201)
# ---------------------------------------------------------------------------

_semantic_scorer = None


def _get_semantic_scorer():
    global _semantic_scorer
    if _semantic_scorer is None:
        from semantic_scorer import SemanticScorer
        _semantic_scorer = SemanticScorer()
    return _semantic_scorer


def classify_intent_detailed(text: str) -> dict:
    """Return ``{intent, confidence, source}`` with semantic fallback.

    *source* is one of ``"keyword"``, ``"semantic"``, or ``"fallback"``.
    Keyword hits for non-qa_simple classes skip the semantic path entirely.
    """
    kw_result = classify_intent(text)

    # Keyword hit -> return immediately, no semantic overhead
    if kw_result != "qa_simple":
        return {"intent": kw_result, "confidence": 1.0, "source": "keyword"}

    # qa_simple fallback -> try semantic scorer
    scorer = _get_semantic_scorer()
    normalized = _normalize_typos(_normalize_canonical(_extract_primary_clause(text)))
    intent, confidence, was_fallback = scorer.resolve_with_fallback(normalized)

    # Block semantic upgrades that lack keyword corroboration
    if intent != "qa_simple":
        _ks = _score_keywords(text)
        _qs = _ks["qa_simple"]
        _eq = _is_question(text)
        _az = all(v == 0.0 for v in _ks.values())
        _has_cls = _ks.get(intent, 0.0) > 0.0
        if intent == "analytical_qa":
            # _score_keywords has no analytical_qa score; analytical keyword support is checked
            # separately, and classify_intent would already have returned analytical_qa if any
            # signal fired. So a semantic-only analytical_qa upgrade has no keyword support
            # (T19: prevented a false positive on "patchng the import manualy ...").
            _lower = _normalize_typos(text.lower())
            if not any(sig in _lower for sig in _ANALYTICAL_QA_SIGNALS | _ANALYTICAL_QA_BIGRAMS):
                intent = "qa_simple"
        elif not _has_cls:
            if (_qs >= 1.0 and _eq) or (_az and _eq):
                intent = "qa_simple"
            elif intent == "agentic_action" and _qs >= 1.0:
                _ni = not re.search(r"\b(fix|add|update|create|remove|delete|refactor|implement|run|write)\b", text, re.I)
                if _ni:
                    intent = "qa_simple"

    # Don't upgrade from qa_simple when confidence is below threshold
    if intent != "qa_simple" and confidence < _SEMANTIC_UPGRADE_THRESHOLD:
        return {"intent": "qa_simple", "confidence": confidence, "source": "fallback" if was_fallback else "semantic"}

    if was_fallback:
        return {"intent": intent, "confidence": confidence, "source": "fallback"}
    return {"intent": intent, "confidence": confidence, "source": "semantic"}


# ---------------------------------------------------------------------------
# Platform helpers (mirrors estimate_tokens.py)
# ---------------------------------------------------------------------------

def _is_claude_payload(payload: dict) -> bool:
    """Claude when hook_event_name is set and cursor_version is absent."""
    return bool(payload.get("hook_event_name")) and not payload.get("cursor_version")


def _text_for_prompt(payload: dict) -> str:
    for key in ("prompt", "text", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        payload = read_stdin_json()
        session_id = session_id_of(payload)
        model = model_of(payload)

        text = _text_for_prompt(payload)
        detail = classify_intent_detailed(text)
        intent = detail["intent"]
        source = detail["source"]

        hints = _hints_with_source(source)
        hint = hints[intent]

        if len(text) > 0 and session_id != "unknown-session":
            try:
                append_event({
                    "type": "intent",
                    "session_id": session_id,
                    "intent": intent,
                    "tier": tier_of_model(model),
                    "model": model,
                    "bytes": len(text),
                    "source": source,
                })
            except OSError:
                pass  # DATA_DIR unwritable — continue without logging

        if _is_claude_payload(payload):
            # Claude Code has no additional_context output field.
            print(json.dumps({}))
        else:
            print(json.dumps({"continue": True, "additional_context": hint}))

        return 0
    except Exception:  # noqa: BLE001 — hook must fail open
        print(json.dumps({}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
