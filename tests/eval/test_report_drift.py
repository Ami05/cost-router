"""T6 (confusion matrix + drift check) and T8 (intent x difficulty matrix)."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import eval_option_d  # noqa: E402
import heldout  # noqa: E402
import report  # noqa: E402


@pytest.fixture(scope="module")
def baseline():
    return report.load_baseline()


@pytest.fixture(scope="module")
def live():
    held = report.score_heldout_results()
    perturb = eval_option_d.run_population(eval_option_d.PERTURBATIONS, "text", "label")
    return {
        "held": held,
        "perturb": perturb,
        "per_class": eval_option_d.score_population(held)["per_class"],
    }


# ---- T6 ---------------------------------------------------------------------

def test_confusion_matrix_covers_8x8_and_sums_to_support(live):
    cm = report.confusion_matrix(live["held"])
    assert set(cm) == set(heldout.INTENTS)
    for a, row in cm.items():
        assert set(row) == set(heldout.INTENTS) | {"other"}
        assert sum(row.values()) == live["per_class"][a]["support"]


def test_live_matches_committed_baseline_no_drift(live, baseline):
    drift = report.drift_check(live["per_class"], baseline["heldout"]["score"]["per_class"])
    assert drift == [], f"drift > 5 points; review then run report.py --update-baseline: {drift}"


def test_baseline_confusion_matrix_matches_live(live, baseline):
    assert baseline["confusion_matrix"] == report.confusion_matrix(live["held"])


def test_baseline_records_source_hashes_and_is_current(baseline):
    meta = baseline["baseline_meta"]
    assert "git_commit" in meta and meta["source_hashes"]
    assert report.stale_baseline_files(baseline) == [], (
        "baseline recorded against different source files; review and run "
        "python3 tests/eval/report.py --update-baseline"
    )


@pytest.mark.parametrize("metric", ["precision", "recall"])
@pytest.mark.parametrize("cls", heldout.INTENTS)
def test_drift_check_fails_on_move_over_5_points(baseline, cls, metric):
    base = baseline["heldout"]["score"]["per_class"]
    moved = copy.deepcopy(base)
    moved[cls][metric] = round(base[cls][metric] - 0.06, 3)
    drift = report.drift_check(moved, base)
    assert [(d["class"], d["metric"]) for d in drift] == [(cls, metric)]


def test_drift_check_tolerates_move_within_5_points(baseline):
    base = baseline["heldout"]["score"]["per_class"]
    moved = copy.deepcopy(base)
    moved["qa_simple"]["recall"] = round(base["qa_simple"]["recall"] - 0.04, 3)
    assert report.drift_check(moved, base) == []


def test_drift_check_flags_improvements_too(baseline):
    base = baseline["heldout"]["score"]["per_class"]
    moved = copy.deepcopy(base)
    moved["qa_simple"]["precision"] = round(min(1.0, base["qa_simple"]["precision"] + 0.2), 3)
    assert report.drift_check(moved, base)


def test_drift_check_ignored_below_min_support_but_reported(baseline):
    base = baseline["heldout"]["score"]["per_class"]
    thin = copy.deepcopy(base)
    thin["analytical_qa"]["support"] = eval_option_d.MIN_CLASS_SUPPORT - 1
    thin["analytical_qa"]["recall"] = 0.0
    assert report.drift_check(thin, base) == []
    assert report.skipped_low_support(thin, base) == ["analytical_qa"]


def test_baseline_is_not_rewritten_without_explicit_flag(tmp_path, monkeypatch):
    """The plain eval and the drift CLI must never touch the committed baseline."""
    before = eval_option_d.BASELINE_PATH.read_bytes()
    eval_option_d.main([])
    report.main([])
    assert eval_option_d.BASELINE_PATH.read_bytes() == before


def test_update_baseline_only_via_flag(monkeypatch):
    called = []
    monkeypatch.setattr(report, "update_baseline", lambda: called.append(1) or Path("x"))
    report.main([])
    assert not called
    report.main(["--update-baseline"])
    assert called == [1]


# ---- T8 ---------------------------------------------------------------------

def test_tier_matrix_is_8_intents_by_4_tiers_without_empty_cell(live):
    tm = report.tier_matrix(live["held"], live["perturb"])
    assert set(tm) == set(heldout.INTENTS)
    assert report.TIERS == ("canonical", "exemplar", "adversarial", "perturbation")
    for row in tm.values():
        assert set(row) == set(report.TIERS)
    assert report.empty_cells(tm) == [], f"empty cells: {report.empty_cells(tm)}"


def test_empty_cell_detector_fires():
    tm = report.tier_matrix([], [])
    assert len(report.empty_cells(tm)) == 8 * 4


def test_perturbation_tier_covers_all_8_intents():
    labels = {p["label"] for p in eval_option_d.PERTURBATIONS}
    assert labels == set(heldout.INTENTS)


def test_new_perturbations_do_not_overlap_exemplars():
    ex = [(f"exemplar[{i}]", e["text"]) for i, e in enumerate(heldout.load_exemplars())]
    new = [(p["source_id"], p["text"]) for p in eval_option_d.PERTURBATIONS
           if p["source_id"].startswith("T8")]
    assert new and heldout.find_overlaps(new, ex) == []


def test_adversarial_qa_simple_recall_is_tracked_and_stable(live, baseline):
    cell = report.tier_matrix(live["held"], live["perturb"])["qa_simple"]["adversarial"]
    base_cell = baseline["tier_matrix"]["qa_simple"]["adversarial"]
    assert base_cell["support"] >= eval_option_d.MIN_CLASS_SUPPORT
    assert cell["recall"] is not None
    assert cell["recall"] >= base_cell["recall"] - report.DRIFT_TOLERANCE, (
        f"adversarial qa_simple recall {cell['recall']} fell vs baseline {base_cell['recall']}"
    )


def test_baseline_tier_matrix_matches_live(live, baseline):
    assert baseline["tier_matrix"] == report.tier_matrix(live["held"], live["perturb"])
