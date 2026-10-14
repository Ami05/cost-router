"""T9: production-weighted coverage report. Uses ONLY tests/data/prod_events_fixture.jsonl
(hand-written); the real production file is never read by tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import heldout  # noqa: E402
import report  # noqa: E402

FIXTURE = heldout.ROOT / "tests" / "data" / "prod_events_fixture.jsonl"

PER_CLASS = {c: {"precision": 0.9, "recall": 0.9, "support": 20} for c in heldout.INTENTS}


def test_fixture_is_not_the_prod_file():
    assert FIXTURE.resolve() != report.DEFAULT_PROD_EVENTS.resolve()


def test_denominator_is_intent_events_only():
    ev = report.load_prod_intent_events(FIXTURE)
    assert ev and all(e["type"] == "intent" for e in ev)
    assert len(ev) == 10  # 13 lines in the fixture minus 3 non-intent events


def test_dedupe_rule_keeps_classifier_event_and_unpaired_twin():
    kept = report.dedupe_intent_events(report.load_prod_intent_events(FIXTURE))
    # s1 pair->1, s2 pair->1 (classifier version wins), s3->1, s4 twin only->1, s5 two pairs->2
    assert len(kept) == 6
    s2 = [e for e in kept if e["session_id"] == "s2"]
    assert [e["intent"] for e in s2] == ["qa_architecture"] and "source" in s2[0]
    assert [e["intent"] for e in kept if e["session_id"] == "s4"] == ["dispatch_justification"]
    assert sum(1 for e in kept if e["session_id"] == "s5") == 2


def test_weighted_accuracy_matches_hand_computation():
    rep = report.production_weighted_report(report.load_prod_intent_events(FIXTURE), PER_CLASS)
    assert rep["deduped_intent_events"] == 6 and rep["raw_intent_events"] == 10
    counts = {c: v["prod_count"] for c, v in rep["per_class"].items()}
    assert counts == {"agentic_action": 1, "qa_architecture": 1, "qa_simple": 1,
                      "dispatch_justification": 1, "inline_narration": 2}
    assert rep["weighted_accuracy"] == pytest.approx(0.9)
    assert rep["uncovered"] == []
    assert "content_generation" in rep["heldout_classes_without_prod_events"]


def test_weighting_uses_production_counts():
    per_class = copy_with_recall({"inline_narration": 1.0, "agentic_action": 0.0,
                                  "qa_architecture": 0.0, "qa_simple": 0.0,
                                  "dispatch_justification": 0.0})
    rep = report.production_weighted_report(report.load_prod_intent_events(FIXTURE), per_class)
    assert rep["weighted_accuracy"] == pytest.approx(2 / 6, abs=1e-4)


def copy_with_recall(overrides):
    out = {c: dict(v) for c, v in PER_CLASS.items()}
    for c, r in overrides.items():
        out[c]["recall"] = r
    return out


def test_class_without_heldout_support_is_reported_uncovered():
    per_class = copy_with_recall({})
    per_class["inline_narration"]["support"] = 0
    rep = report.production_weighted_report(report.load_prod_intent_events(FIXTURE), per_class)
    assert rep["uncovered"] == ["inline_narration"]
    assert rep["uncovered_weight"] == pytest.approx(2 / 6, abs=1e-4)


def test_dedupe_rule_is_documented_in_output_and_docstring():
    rep = report.production_weighted_report(report.load_prod_intent_events(FIXTURE), PER_CLASS)
    assert "source" in rep["dedupe_rule"] and "type=='intent'" in rep["dedupe_rule"]
    assert "T12" in rep["dedupe_rule"]
    assert "check_routing" in report.dedupe_intent_events.__doc__ or "DEDUPE_RULE" in report.dedupe_intent_events.__doc__


def test_missing_prod_file_fails_loudly_not_skips(tmp_path):
    with pytest.raises(report.ProdEventsMissing, match="--prod-file"):
        report.load_prod_intent_events(tmp_path / "nope.jsonl")


def test_cli_missing_prod_file_exit_code_2(tmp_path, capsys):
    assert report.main(["--prod-weighted", "--prod-file", str(tmp_path / "nope.jsonl")]) == 2
    assert "not found" in capsys.readouterr().err


def test_cli_with_fixture_override_prints_weighted_accuracy(capsys):
    assert report.main(["--prod-weighted", "--prod-file", str(FIXTURE)]) == 0
    assert "weighted_accuracy" in capsys.readouterr().out


def test_empty_intent_set_fails(tmp_path):
    f = tmp_path / "e.jsonl"
    f.write_text('{"type":"tool_invocation","session_id":"x","ts":"t"}\n')
    with pytest.raises(report.ProdEventsMissing):
        report.production_weighted_report(report.load_prod_intent_events(f), PER_CLASS)
