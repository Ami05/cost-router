"""Journey: qa_simple prompt -> classify_intent -> get_routing_for_intent.

Regression coverage for a production incident where a simple-QA turn was
classified as qa_simple but the routing_matrix row still resolved to
sonnet, defeating the point of the qa_simple fast path. Chains through the
REAL entrypoints (classify_intent.classify_intent, routing_resolver.
get_routing_for_intent), no mocks of the system under test.

Also covers the fail-fast guard (_validate_routing_matrix) added so a
malformed routing_matrix row raises immediately at load time instead of
silently falling through to a bad default deep in a session.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import classify_intent as ci  # noqa: E402
import routing_resolver as rr  # noqa: E402


def test_qa_simple_user_turn_resolves_to_all_haiku():
    # Step 1: a plain simple-QA user turn, classified via the real keyword
    # classifier -- no reads/writes signal, no agentic verbs.
    prompt = "What is a race condition?"
    intent = ci.classify_intent(prompt)

    # This journey is specifically about the qa_simple fast path; if the
    # classifier's keyword heuristics ever change and this prompt stops
    # landing on qa_simple, that's a separate classifier regression --
    # fail loudly here rather than silently testing the wrong intent.
    assert intent == "qa_simple", (
        f"expected classify_intent to route this simple-QA prompt to "
        f"qa_simple, got {intent!r}"
    )

    # Step 2: the real get_routing_for_intent entrypoint on that intent.
    routing = rr.get_routing_for_intent(intent)

    # Step 3: this is the actual production fix -- qa_simple must resolve
    # every role to haiku, never sonnet.
    for role in ("orchestrator", "worker", "explorer", "test_runner"):
        assert routing[role] == "haiku", (
            f"qa_simple.{role} resolved to {routing[role]!r}, expected "
            f"haiku (regression of the qa_simple->sonnet incident)"
        )


def test_broken_routing_matrix_row_raises_immediately():
    # Step 1: simulate a routing_matrix row missing a required key
    # ("test_runner") the way it might land after a bad manual config.json
    # edit -- this must be caught at load time, not silently substituted
    # with the _default fallback deep inside a live session.
    broken_matrix = {
        "_default": {
            "effort": "high",
            "orchestrator": "sonnet",
            "worker": "sonnet",
            "explorer": "haiku",
            "test_runner": "haiku",
        },
        "qa_simple": {
            "effort": "low",
            "orchestrator": "haiku",
            "worker": "haiku",
            "explorer": "haiku",
            # "test_runner" missing -- malformed row.
        },
    }

    def _fake_config_get(path, default=None, flat_key=None):
        if path == "routing_matrix":
            return broken_matrix
        return default

    # Step 2: re-run the real fail-fast validator (the exact function that
    # already ran once at routing_resolver import time) against the broken
    # matrix -- must raise immediately, never fall through.
    with patch("routing_resolver.config_get", side_effect=_fake_config_get):
        with pytest.raises(ValueError, match="missing required keys"):
            rr._validate_routing_matrix()
