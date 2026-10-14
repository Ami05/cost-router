"""Regenerate tests/report.html from a real pytest run.

Usage: python3 tests/generate_report.py   (run from cost-router-v4/)

Runs the full suite via pytest-json-report, pulls the first line of each test
function's docstring (falling back to a humanized test name when absent), and
renders the same dark-themed report layout grouped by module.
"""

from __future__ import annotations

import ast
import html
import importlib.metadata
import json
import platform as platform_mod
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO / "tests"

# Display order: related-functionality files grouped together.
MODULE_ORDER = [
    "test_common_core_utils.py",
    "test_event_log_persistence.py",
    "test_routing_gate.py",
    "test_read_repeat_gate.py",
    "test_estimate_tokens_hook.py",
    "test_inject_rule_hook.py",
    "test_session_summary_report.py",
    "test_analyze_routing_feedback.py",
    "test_policy_manager_autoapply.py",
    "test_policy_gc.py",
    "test_reward_scoring.py",
    "test_install_export_scripts.py",
    "test_config_manifest_integrity.py",
    "test_e2e_integration.py",
    "test_human_persona_agents.py",
    "test_adversarial_robustness.py",
    "test_compact_log_scenarios.py",
]


def run_pytest() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "report.json"
        subprocess.run(
            [
                sys.executable, "-m", "pytest", "tests/", "-q",
                "--json-report", f"--json-report-file={report_path}",
            ],
            cwd=REPO, capture_output=True, text=True, check=False,
        )
        return json.loads(report_path.read_text())


def _docstrings_for_module(path: Path) -> dict[str, str]:
    """Map function/method name -> first docstring line (or '') via ast."""
    doc_by_name: dict[str, str] = {}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            doc = ast.get_docstring(node)
            doc_by_name[node.name] = (doc.strip().splitlines()[0].strip() if doc else "")
    return doc_by_name


def _humanize(name: str) -> str:
    name = re.sub(r"^test_", "", name)
    name = name.replace("_", " ")
    return name[:1].upper() + name[1:] if name else name


def _base_name_and_short_id(nodeid: str) -> tuple[str, str]:
    """'tests/test_x.py::TestFoo::test_bar[param]' -> ('test_bar', 'TestFoo.test_bar[param]')."""
    after_file = nodeid.split("::", 1)[1]
    parts = after_file.split("::")
    short = ".".join(parts)
    func_part = parts[-1]
    base = func_part.split("[", 1)[0]
    return base, short


def build_modules(report: dict) -> list[dict]:
    by_file: dict[str, list[dict]] = {}
    path_by_file: dict[str, Path] = {}  # tests live in subdirs (e2e/, eval/), not just tests/
    for t in report["tests"]:
        file_rel = t["nodeid"].split("::", 1)[0]
        file_name = Path(file_rel).name
        by_file.setdefault(file_name, []).append(t)
        path_by_file.setdefault(file_name, REPO / file_rel)

    doc_cache: dict[str, dict[str, str]] = {}
    modules = []
    order = [m for m in MODULE_ORDER if m in by_file] + sorted(set(by_file) - set(MODULE_ORDER))
    for file_name in order:
        tests = by_file[file_name]
        path = path_by_file[file_name]
        if file_name not in doc_cache:
            doc_cache[file_name] = _docstrings_for_module(path)
        docs = doc_cache[file_name]

        rows = []
        for t in tests:
            base, short_id = _base_name_and_short_id(t["nodeid"])
            desc = docs.get(base) or _humanize(base)
            rows.append({
                "name": short_id,
                "desc": desc,
                "outcome": _normalize_outcome(t["outcome"]),
            })
        passed = sum(1 for r in rows if r["outcome"] == "passed")
        failed = sum(1 for r in rows if r["outcome"] == "failed")
        skipped = sum(1 for r in rows if r["outcome"] == "skipped")
        modules.append({
            "file": file_name,
            "path": path_by_file[file_name].relative_to(REPO).as_posix(),
            "rows": rows,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "total": len(rows),
        })
    return modules


def _normalize_outcome(outcome: str) -> str:
    """Remove 'subtests ' prefix from outcome strings (e.g. 'subtests passed' -> 'passed')."""
    if outcome.startswith("subtests "):
        return outcome[9:]  # Remove "subtests " (9 chars)
    return outcome


BADGE = {"passed": "pass", "failed": "fail", "skipped": "skip"}
LABEL = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}

CSS = """
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0f1117; color: #e2e8f0; min-height: 100vh; }

  header { background: #1a1d2e; border-bottom: 1px solid #2d3148; padding: 24px 40px; }
  header h1 { font-size: 1.4rem; font-weight: 700; color: #fff; letter-spacing: -0.02em; }
  header p  { font-size: 0.82rem; color: #64748b; margin-top: 4px; }

  .summary-bar {
    display: flex; gap: 16px; padding: 20px 40px;
    background: #13151f; border-bottom: 1px solid #1e2235; flex-wrap: wrap;
  }
  .stat { background: #1a1d2e; border: 1px solid #2d3148; border-radius: 10px;
          padding: 14px 22px; min-width: 110px; text-align: center; }
  .stat .num { font-size: 1.9rem; font-weight: 800; line-height: 1; }
  .stat .lbl { font-size: 0.72rem; color: #64748b; margin-top: 4px; text-transform: uppercase; letter-spacing: .06em; }
  .num.green { color: #4ade80; }
  .num.red   { color: #f87171; }
  .num.blue  { color: #60a5fa; }
  .num.gray  { color: #94a3b8; }
  .num.amber { color: #fbbf24; }

  .content { padding: 32px 40px; max-width: 1200px; }

  .module { margin-bottom: 28px; border: 1px solid #2d3148; border-radius: 12px; overflow: hidden; }
  .module-header {
    background: #1a1d2e; padding: 14px 20px;
    display: flex; align-items: center; justify-content: space-between;
    cursor: pointer; user-select: none;
  }
  .module-header:hover { background: #1e2235; }
  .module-name { font-size: 0.88rem; font-weight: 700; color: #93c5fd; font-family: "SF Mono", "Fira Code", monospace; }
  .module-meta { font-size: 0.75rem; color: #64748b; margin-top: 2px; }
  .module-badge {
    background: #0f4c2a; color: #4ade80; font-size: 0.72rem; font-weight: 700;
    padding: 3px 10px; border-radius: 20px; letter-spacing: .05em; white-space: nowrap;
  }
  .module-badge.fail { background: #4c1f1f; color: #f87171; }

  .test-table { width: 100%; border-collapse: collapse; }
  .test-table th {
    background: #13151f; color: #475569; font-size: 0.72rem; text-transform: uppercase;
    letter-spacing: .06em; padding: 10px 20px; text-align: left; border-bottom: 1px solid #2d3148;
  }
  .test-table td { padding: 11px 20px; border-bottom: 1px solid #1e2235;
                   font-size: 0.82rem; vertical-align: middle; }
  .test-table tr:last-child td { border-bottom: none; }
  .test-table tr:hover td { background: #13151f; }

  .test-name { font-family: "SF Mono", "Fira Code", monospace; color: #cbd5e1; }
  .test-desc { color: #64748b; font-size: 0.78rem; margin-top: 3px; }

  .badge {
    display: inline-block; padding: 3px 10px; border-radius: 6px;
    font-size: 0.70rem; font-weight: 700; letter-spacing: .06em;
  }
  .badge.pass  { background: #0f4c2a; color: #4ade80; }
  .badge.fail  { background: #4c1f1f; color: #f87171; }
  .badge.skip  { background: #1e293b; color: #94a3b8; }
  .badge.xfail { background: #1c1917; color: #a8a29e; border: 1px solid #44403c; }

  .num-col { color: #475569; font-size: 0.78rem; text-align: center; }

  .legend {
    display: flex; gap: 12px; padding: 14px 40px;
    background: #13151f; border-bottom: 1px solid #1e2235;
    font-size: 0.76rem; color: #64748b; flex-wrap: wrap;
  }
  .legend span { display: flex; align-items: center; gap: 6px; }

  footer { padding: 24px 40px; font-size: 0.75rem; color: #374151;
           border-top: 1px solid #1e2235; margin-top: 8px; }
"""


def render(report: dict, modules: list[dict]) -> str:
    passed = sum(m['passed'] for m in modules)
    failed = sum(m['failed'] for m in modules)
    skipped = sum(m['skipped'] for m in modules)
    total = sum(m['total'] for m in modules)
    pass_rate = (passed / total * 100) if total else 0.0
    duration = report["duration"]
    env = report.get("environment", {})
    py_ver = env.get("Python", "") or sys.version.split()[0]
    platform = env.get("Platform", "") or platform_mod.system()
    try:
        pytest_ver = env.get("Packages", {}).get("pytest", "") or importlib.metadata.version("pytest")
    except importlib.metadata.PackageNotFoundError:
        pytest_ver = ""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counter = 0

    module_html = []
    for m in modules:
        badge_class = "" if m["failed"] == 0 else " fail"
        rows_html = []
        for r in m["rows"]:
            counter += 1
            badge = BADGE[r["outcome"]]
            label = LABEL[r["outcome"]]
            rows_html.append(
                f'      <tr><td class="num-col">{counter}</td>'
                f'<td><div class="test-name">{html.escape(r["name"])}</div></td>'
                f'<td class="test-desc">{html.escape(r["desc"])}</td>'
                f'<td><span class="badge {badge}">{label}</span></td></tr>'
            )
        module_html.append(f"""<!-- \u2500\u2500 {m['file']} \u2500\u2500 -->
<div class="module">
  <div class="module-header" onclick="this.nextElementSibling.hidden=!this.nextElementSibling.hidden">
    <div>
      <div class="module-name">{m['path']}</div>
      <div class="module-meta">{m['total']} tests</div>
    </div>
    <span class="module-badge{badge_class}">{m['passed']} / {m['total']} PASSED{'' if m['failed'] == 0 else f", {m['failed']} FAILED"}</span>
  </div>
  <div>
  <table class="test-table">
    <thead><tr><th>#</th><th>Test case</th><th>What it verifies</th><th>Status</th></tr></thead>
    <tbody>
{chr(10).join(rows_html)}
    </tbody>
  </table>
  </div>
</div>""")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>cost-router &mdash; Test Report</title>
<style>
{CSS}</style>
</head>
<body>

<header>
  <h1>cost-router &mdash; Test Report</h1>
  <p>pytest {pytest_ver} &nbsp;&bull;&nbsp; Python {py_ver} &nbsp;&bull;&nbsp; {platform.split('-')[0]} &nbsp;&bull;&nbsp; {today} &nbsp;&bull;&nbsp; {len(modules)} files</p>
</header>

<div class="summary-bar">
  <div class="stat"><div class="num green">{passed}</div><div class="lbl">Passed</div></div>
  <div class="stat"><div class="num red">{failed}</div><div class="lbl">Failed</div></div>
  <div class="stat"><div class="num gray">{skipped}</div><div class="lbl">Skipped</div></div>
  <div class="stat"><div class="num blue">{total}</div><div class="lbl">Total</div></div>
  <div class="stat"><div class="num {'green' if failed == 0 else 'red'}">{pass_rate:.1f}%</div><div class="lbl">Pass rate</div></div>
  <div class="stat"><div class="num gray">{duration:.2f}s</div><div class="lbl">Duration</div></div>
</div>

<div class="legend">
  <span><span class="badge pass">PASS</span> test passed</span>
  <span><span class="badge fail">FAIL</span> test failed</span>
  <span><span class="badge skip">SKIP</span> skipped</span>
</div>

<div class="content">

{chr(10).join(module_html)}

</div><!-- /content -->

<footer>
  Generated from a live pytest run via tests/generate_report.py &nbsp;&bull;&nbsp; cost-router plugin &nbsp;&bull;&nbsp; {today} &nbsp;&bull;&nbsp;
  {total} tests across {len(modules)} files &nbsp;&bull;&nbsp; {duration:.2f}s &nbsp;&bull;&nbsp; <strong style="color:{'#4ade80' if failed == 0 else '#f87171'}">{pass_rate:.1f}% pass rate</strong>
  &nbsp;&bull;&nbsp; {skipped} skipped
</footer>

</body>
</html>
"""


def main() -> int:
    report = run_pytest()
    modules = build_modules(report)
    html_out = render(report, modules)
    (TESTS_DIR / "report.html").write_text(html_out, encoding="utf-8")
    print(f"Wrote {TESTS_DIR / 'report.html'} — {report['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
