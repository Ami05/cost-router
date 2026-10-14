"""Real hook scripts end to end: build -> repeat -> edit+mark stale -> rebuild -> gc; one graph per path throughout."""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"


def _run(script, data_dir, payload):
    env = {**os.environ, "COST_ROUTER_DATA_DIR": str(data_dir)}
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script)],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60,
    )


def _graphs(data_dir):
    d = data_dir / "file-graphs"
    return sorted(p for p in d.glob("*.json") if p.name not in ("index.json", "stale_refresh.json")) if d.is_dir() else []


def _index(data_dir):
    return json.loads((data_dir / "file-graphs" / "index.json").read_text())


def _source(tmp_path):
    src = tmp_path / "big_module.py"
    src.write_text("\n".join(f"def fn_{i}():\n    return {i}\n" for i in range(200)))
    return src


def test_hook_chain_keeps_one_graph_per_path(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    src = _source(tmp_path)
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(src)}}

    first = _run("build_file_graph.py", data_dir, payload)
    assert first.returncode == 0, first.stderr
    assert len(_graphs(data_dir)) == 1
    assert len(_index(data_dir)) == 1

    again = _run("build_file_graph.py", data_dir, payload)
    assert again.returncode == 0, again.stderr
    assert len(_graphs(data_dir)) == 1
    assert len(_index(data_dir)) == 1

    src.write_text(src.read_text() + "\ndef extra():\n    return 1\n")
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(src)}}
    marked = _run("mark_graph_stale.py", data_dir, edit)
    assert marked.returncode == 0, marked.stderr
    graph = json.loads(_graphs(data_dir)[0].read_text())
    assert graph.get("stale") is True

    rebuilt = _run("build_file_graph.py", data_dir, payload)
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert len(_graphs(data_dir)) == 1
    assert len(_index(data_dir)) == 1
    graph = json.loads(_graphs(data_dir)[0].read_text())
    assert not graph.get("stale")
    assert any(s.get("name") == "extra" for s in graph["symbols"])

    gc = _run("graph_gc.py", data_dir, {})
    assert gc.returncode == 0, gc.stderr
    assert len(_graphs(data_dir)) == 1  # fresh graph survives GC
    stats = json.loads((data_dir / "graph_gc_stats.json").read_text())
    assert stats["runs"] == 1 and stats["deleted"] == 0


def test_hook_chain_records_no_errors_on_happy_path(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    src = _source(tmp_path)
    _run("build_file_graph.py", data_dir, {"tool_name": "Read", "tool_input": {"file_path": str(src)}})
    errors = data_dir / "graph_errors.jsonl"
    assert not errors.exists() or errors.read_text().strip() == ""
