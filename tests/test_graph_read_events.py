"""record_graph_read logging + graph_read_report adoption math."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import graph_read_report  # noqa: E402
import graph_store  # noqa: E402


def _ev(session, lines, offset=None, limit=None, hint=True):
    return {"session_id": session, "path_hash": "h", "file_lines": lines,
            "offset": offset, "limit": limit, "hint_shown": hint}


def test_record_graph_read_writes_expected_fields(monkeypatch, tmp_path):
    log = tmp_path / "graph_read_events.jsonl"
    monkeypatch.setattr(graph_store, "READ_EVENTS_LOG", log)
    src = tmp_path / "f.py"
    src.write_text("a\nb\nc\n")

    graph_store.record_graph_read("s1", "hh", src, {"offset": 2, "limit": 1}, {"symbols": [1, 2]}, "hit", True)

    e = json.loads(log.read_text().splitlines()[0])
    assert (e["session_id"], e["path_hash"], e["file_lines"], e["symbol_count"]) == ("s1", "hh", 3, 2)
    assert (e["offset"], e["limit"], e["state"], e["hint_shown"]) == (2, 1, "hit", True)


def test_record_graph_read_missing_file_records_error_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(graph_store, "READ_EVENTS_LOG", tmp_path / "ev.jsonl")
    monkeypatch.setattr(graph_store, "ERRORS_LOG", tmp_path / "graph_errors.jsonl")

    graph_store.record_graph_read("s", "h", tmp_path / "missing.py", {}, None, "build_failed", False)

    comps = [json.loads(x)["component"] for x in (tmp_path / "graph_errors.jsonl").read_text().splitlines()]
    assert "graph_store.record_graph_read.file_lines" in comps
    assert json.loads((tmp_path / "ev.jsonl").read_text().splitlines()[0])["file_lines"] == 0


def test_record_graph_read_truncates_to_5000(monkeypatch, tmp_path):
    log = tmp_path / "ev.jsonl"
    monkeypatch.setattr(graph_store, "READ_EVENTS_LOG", log)
    log.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(5000)))
    src = tmp_path / "f.py"
    src.write_text("x\n")

    graph_store.record_graph_read("s", "h", src, {}, None, "hit", True)

    assert len(log.read_text().splitlines()) == 5000


def test_report_attributes_savings_only_to_adoption_lift():
    # Session A: first large read full, later large read targeted (lift). Session B: first targeted.
    events = [
        _ev("A", 1000), _ev("A", 1000, offset=10, limit=50),
        _ev("B", 600, offset=1, limit=100), _ev("B", 600),
    ]
    r = graph_read_report.compute(events)
    assert r["targeted_rate_first_large_read"] == 0.5
    assert r["targeted_rate_later_large_reads"] == 0.5
    assert r["adoption_lift"] == 0.0
    assert r["est_lines_avoided_attributable"] == 0.0
    assert r["est_lines_avoided_upper"] == (1000 - 50) + (600 - 100)


def test_report_positive_lift_and_overhead():
    events = [_ev("A", 1000), _ev("A", 1000, limit=100), _ev("B", 1000), _ev("B", 1000, limit=100)]
    r = graph_read_report.compute(events)
    assert r["adoption_lift"] == 1.0
    assert r["est_lines_avoided_attributable"] == 1800  # 2 later targeted reads x 900 lines, lift/rate_later = 1
    assert r["net_tokens_attributable"] == 1800 * 10 - 4 * 120


def test_report_counts_bad_lines_and_missing_session(tmp_path):
    p = tmp_path / "ev.jsonl"
    p.write_text('not json\n[1,2]\n' + json.dumps({"file_lines": 400, "hint_shown": True}) + "\n")

    events, parse_errors, io_error = graph_read_report.load_events(p)
    r = graph_read_report.compute(events)

    assert (len(events), parse_errors, io_error) == (1, 2, None)
    assert r["missing_session_id"] == 1


def test_report_missing_file_is_reported_not_silent(tmp_path):
    _, _, io_error = graph_read_report.load_events(tmp_path / "nope.jsonl")
    assert io_error == "events file not found"
