"""routing_report — HTML impact card generator (IMP-302).

Exposes:
    generate(report, since: str, until: str) -> Path
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from impact_calculator import GATE_DISPLAY_NAMES  # noqa: E402

DATA_DIR = _ROOT / "data"


def generate(report: object, since: str, until: str) -> Path:
    """Generate a self-contained HTML impact card at data/impact-card.html.

    Args:
        report: WindowReport from impact_calculator.compute_window
        since:  ISO date string for window start
        until:  ISO date string for window end

    Returns:
        Path to the written file.
    """
    gate_count = sum(v[0] for v in report.gate_breakdown.values())

    if report.prev_week_routing_rate is not None:
        delta = report.prev_week_routing_rate
        arrow = "&#8593;" if delta >= 0 else "&#8595;"
        trend_text = f"{arrow} {abs(delta):.0%} vs. last week"
    else:
        trend_text = "gathering data"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>cost-router impact</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@400;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #F2F3EF;
    --paper: #FFFFFF;
    --ink: #1B241F;
    --ink-dim: #53605A;
    --enforced: #1F7A54;
    --line: #DCE0D8;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--ink);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem;
  }}
  .card {{
    background: var(--paper);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 2.5rem 2rem;
    max-width: 720px;
    width: 100%;
  }}
  .header {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    color: var(--ink-dim);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 1.5rem;
    padding-bottom: 1rem;
    border-bottom: 1px solid var(--line);
  }}
  .hero {{
    text-align: center;
    padding: 1.5rem 0;
  }}
  .hero-number {{
    font-family: 'Source Serif 4', serif;
    font-size: clamp(2.5rem, 5vw, 4rem);
    font-weight: 700;
    color: var(--ink);
    line-height: 1;
  }}
  .hero-label {{
    font-size: 0.9rem;
    color: var(--ink-dim);
    margin-top: 0.5rem;
  }}
  .metrics {{
    display: flex;
    gap: 1rem;
    margin: 1.5rem 0;
  }}
  .metric-card {{
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 1rem;
  }}
  .metric-label {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.65rem;
    color: var(--ink-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 0.5rem;
  }}
  .metric-value {{
    font-family: 'Source Serif 4', serif;
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--enforced);
  }}
  .metric-sub {{
    font-size: 0.75rem;
    color: var(--ink-dim);
    margin-top: 0.25rem;
  }}
  .footer {{
    border-top: 1px solid var(--line);
    padding-top: 1rem;
    margin-top: 0.5rem;
    font-size: 0.75rem;
    color: var(--ink-dim);
  }}
  .footer-stats {{
    font-family: 'JetBrains Mono', monospace;
    margin-bottom: 0.75rem;
  }}
  .disclosure {{
    font-size: 0.7rem;
    line-height: 1.5;
  }}
  .disclosure strong {{ color: var(--ink); }}
  @media (max-width: 600px) {{
    .metrics {{ flex-direction: column; }}
  }}
</style>
</head>
<body>
<div class="card">
  <div class="header">cost-router impact &middot; Week of {since} &ndash; {until}</div>

  <div class="hero">
    <div class="hero-number">${report.total_est_saved:.0f}</div>
    <div class="hero-label">est. saved this week</div>
  </div>

  <div class="metrics">
    <div class="metric-card">
      <div class="metric-label">Routing</div>
      <div class="metric-value">${report.routing_savings:.0f}</div>
      <div class="metric-sub">{report.routing_rate:.0%} of output on fast tier</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Gates</div>
      <div class="metric-value">${report.waste_prevented:.0f}</div>
      <div class="metric-sub">{gate_count} denial(s) prevented</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Trend</div>
      <div class="metric-value" style="font-size:1.2rem">{trend_text}</div>
      <div class="metric-sub">week-over-week fast routing rate</div>
    </div>
  </div>

  <div class="footer">
    <div class="footer-stats">
      {report.total_sessions} sessions &middot; {report.total_turns} turns &middot;
      ${report.total_spent:.0f} est. spent
    </div>
    <div class="disclosure">
      <strong>Baseline:</strong> all-{report.baseline_model} &middot;
      <strong>All numbers estimated.</strong>
      <em>Why estimated?</em> Cost is derived from local token counting (~4 chars/token heuristic)
      and <code>scripts/pricing.json</code> list prices. Excludes prompt-cache discounts.
      Not real billing data.
    </div>
  </div>
</div>
</body>
</html>"""

    out_path = DATA_DIR / "impact-card.html"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
