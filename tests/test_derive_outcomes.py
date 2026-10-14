"""Tests for scripts/derive_outcomes.py (DGC-004 S2).

Runs against tests/fixtures/derived_outcome_sample.jsonl, an anonymised slice
of real routing_events.jsonl. Assertions are invariants computed independently
of the pairing logic, so they hold for any real slice.
"""

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import derive_outcomes as do  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "derived_outcome_sample.jsonl"


def _events():
    return do.load_events(FIXTURE)


def _declared_dispatches(events):
    declared = {(e["session_id"], e["subagent_id"]) for e in events if e["type"] == "dispatch_worker_declared"}
    return [
        e for e in events
        if e["type"] == "subagent_dispatch" and (e["session_id"], e["subagent_id"]) in declared
    ]


def test_one_row_per_declared_dispatch():
    events = _events()
    rows = do.derive(events)
    assert len(rows) == len(_declared_dispatches(events))


def test_excluded_undeclared_count_matches_filtered_dispatches():
    events = _events()
    all_dispatches = sum(1 for e in events if e["type"] == "subagent_dispatch")
    assert do.count_undeclared(events) == all_dispatches - len(_declared_dispatches(events))
    assert do.count_undeclared(events) + len(do.derive(events)) == all_dispatches


def test_derived_outcome_in_allowed_set():
    assert {r["derived_outcome"] for r in do.derive(_events())} <= set(do.OUTCOMES)


def test_no_stop_event_equals_dispatches_without_a_stop():
    events = _events()
    d = Counter(e["subagent_id"] for e in _declared_dispatches(events))
    s = Counter(e["agent_id"] for e in events if e["type"] == "subagent_stop_telemetry")
    expected = sum(max(0, n - s.get(sid, 0)) for sid, n in d.items())
    rows = do.derive(_events())
    assert sum(1 for r in rows if r["derived_outcome"] == "no_stop_event") == expected


def test_raw_outcome_preserved_not_overwritten():
    for r in do.derive(_events()):
        if r["derived_outcome"] != "no_stop_event":
            assert r["raw_outcome"] is not None


def test_typed_workers_come_from_declared_events():
    events = _events()
    declared = {e["worker"] for e in events if e["type"] == "dispatch_worker_declared"}
    workers = {r["worker"] for r in do.derive(events)}
    assert workers <= declared | {"untyped"}


def test_zero_window_disables_retry():
    rows = do.derive(_events(), retry_window_sec=-1)
    assert not [r for r in rows if r["derived_outcome"] == "retried"]


def test_summary_reports_insufficient_data_on_small_sample():
    s = do.summarize(do.derive(_events()))
    assert s["status"] == "insufficient_data"
    assert s["total_n"] == sum(v["n"] for v in s["per_worker_type"].values())
    assert s["date_range"][0] <= s["date_range"][1]


def test_wilson_interval_bounds():
    assert do.wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = do.wilson_interval(0, 30)
    assert lo == 0.0 and 0.0 < hi < 0.15
    lo, hi = do.wilson_interval(15, 30)
    assert lo < 0.5 < hi


def test_cli_does_not_modify_events_and_refuses_same_path(tmp_path, capsys):
    before = FIXTURE.read_bytes()
    out = tmp_path / "derived.jsonl"
    assert do.main(["--events", str(FIXTURE), "--out", str(out)]) == 0
    assert FIXTURE.read_bytes() == before
    assert len(out.read_text().splitlines()) == len(do.derive(_events()))
    json.loads(capsys.readouterr().out)
    assert do.main(["--events", str(FIXTURE), "--out", str(FIXTURE)]) == 2


def _split_fixture(tmp_path, dup_archive=False):
    """Split the real fixture: older half to usage_archive.jsonl, newer half to the live file."""
    lines = [ln for ln in FIXTURE.read_text().splitlines() if ln.strip()]
    mid = len(lines) // 2
    live = tmp_path / "routing_events.jsonl"
    archive = tmp_path / "usage_archive.jsonl"
    live.write_text("\n".join(lines[mid:]) + "\n")
    archive_lines = lines[:mid] + (lines[:mid] if dup_archive else [])
    archive.write_text("\n".join(archive_lines) + "\n")
    return live, archive


def _key(e):
    return json.dumps(e, sort_keys=True)


def test_live_plus_archive_equals_unsplit_fixture(tmp_path):
    live, archive = _split_fixture(tmp_path)
    merged = do.load_events(live, [archive])
    assert Counter(map(_key, merged)) == Counter(map(_key, _events()))
    assert len(do.load_events(live)) < len(merged)


def test_archive_internal_duplicates_are_dropped(tmp_path):
    live, archive = _split_fixture(tmp_path, dup_archive=True)
    merged = do.load_events(live, [archive])
    assert Counter(map(_key, merged)) == Counter(map(_key, _events()))


def test_missing_archive_is_ignored_but_missing_live_raises(tmp_path):
    live, _ = _split_fixture(tmp_path)
    assert do.load_events(live, [tmp_path / "usage_archive.1.jsonl"]) == do.load_events(live)
    try:
        do.load_events(tmp_path / "nope.jsonl")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError for a missing live file")


def _without_early_stops(events):
    """Simulate the pre-stop-logging era: drop stop rows older than the median stop ts."""
    stops = sorted(e["ts"] for e in events if e["type"] == "subagent_stop_telemetry")
    cut = stops[len(stops) // 2]
    return [e for e in events if not (e["type"] == "subagent_stop_telemetry" and e["ts"] < cut)], cut


def test_stop_epoch_is_earliest_stop_ts_or_none():
    events = _events()
    stops = [e["ts"] for e in events if e["type"] == "subagent_stop_telemetry"]
    assert do.stop_epoch(events) == min(stops)
    assert do.stop_epoch([e for e in events if e["type"] != "subagent_stop_telemetry"]) is None


def test_since_excludes_dispatches_before_stop_logging():
    events, cut = _without_early_stops(_events())
    epoch = do.stop_epoch(events)
    assert epoch >= cut
    everything = do.derive(events)
    kept = do.derive(events, since=epoch)
    assert kept and len(kept) <= len(everything)
    assert all(r["ts"] >= epoch for r in kept)
    assert len(everything) - len(kept) == sum(1 for r in everything if r["ts"] < epoch)
    assert not [r for r in kept if r["ts"] < epoch and r["derived_outcome"] == "no_stop_event"]


def test_default_since_none_keeps_every_declared_dispatch():
    events = _events()
    assert do.derive(events, since=None) == do.derive(events)


def test_cli_reports_pre_stop_exclusion_and_flag_disables_it(tmp_path, capsys):
    events, _ = _without_early_stops(_events())
    p = tmp_path / "routing_events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    assert do.main(["--events", str(p), "--no-archive"]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["stop_epoch"] == do.stop_epoch(events)
    assert s["excluded_pre_stop_logging"] == len(do.derive(events)) - s["total_n"]
    assert do.main(["--events", str(p), "--no-archive", "--include-pre-stop-logging"]) == 0
    s2 = json.loads(capsys.readouterr().out)
    assert s2["excluded_pre_stop_logging"] == 0
    assert s2["total_n"] == len(do.derive(events))


def test_cli_merges_sibling_archive_by_default(tmp_path, capsys):
    live, _ = _split_fixture(tmp_path)
    assert do.main(["--events", str(live)]) == 0
    with_archive = json.loads(capsys.readouterr().out)
    assert do.main(["--events", str(live), "--no-archive"]) == 0
    live_only = json.loads(capsys.readouterr().out)
    assert with_archive["total_n"] == do.summarize(do.derive(_events()))["total_n"]
    assert live_only["total_n"] <= with_archive["total_n"]
    assert do.default_archive_paths(live) == [
        tmp_path / "usage_archive.1.jsonl",
        tmp_path / "usage_archive.2.jsonl",
        tmp_path / "usage_archive.3.jsonl",
        tmp_path / "usage_archive.jsonl",
    ]
