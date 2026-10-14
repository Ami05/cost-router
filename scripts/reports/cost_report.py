"""cost_report — Markdown weekly digest generator (IMP-303).

Exposes:
    generate(report, since: str, until: str) -> Path

Helper sections (shadow eval, routing candidates) are private to this module.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from impact_calculator import GATE_DISPLAY_NAMES  # noqa: E402

DATA_DIR = _ROOT / "data"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_shadow_eval_section() -> str:
    """Build a Shadow Evaluation markdown section for the weekly digest.

    Reads data/shadow_evaluation.json. Skips section with a note when the file
    is missing. Flags staleness if the file is older than exactly 48 hours.
    Also reads data/quality_signal.json for per-intent revert_rate; skips with
    a note when that file is missing.
    """
    lines: list[str] = ["## Shadow Evaluation", ""]

    shadow_path = DATA_DIR / "shadow_evaluation.json"
    if not shadow_path.exists():
        lines.append("_No shadow evaluation data — run `python3 scripts/shadow_evaluator.py` to generate._")
        lines.append("")
        return "\n".join(lines)

    try:
        raw = json.loads(shadow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        lines.append("_shadow_evaluation.json could not be read._")
        lines.append("")
        return "\n".join(lines)

    # Staleness: flag if older than exactly 48 hours
    try:
        mtime = shadow_path.stat().st_mtime
        age_hours = (datetime.now(timezone.utc).timestamp() - mtime) / 3600
        if age_hours > 48:
            lines.append(
                f"> **Stale:** shadow_evaluation.json is {age_hours:.0f}h old "
                "(threshold: 48h). Re-run `python3 scripts/shadow_evaluator.py`."
            )
            lines.append("")
    except OSError:
        pass

    timestamp = raw.get("timestamp", "")
    if timestamp:
        lines.append(f"_Evaluated: {timestamp}_")
        lines.append("")

    rules: dict = raw.get("rules", {})
    if not rules:
        lines.append("_No per-rule entries in shadow_evaluation.json._")
        lines.append("")
    else:
        lines.append("| Rule | Intent | Actual Role | Candidate Role | Delta | N |")
        lines.append("|------|--------|-------------|----------------|-------|---|")
        for rule_id, entry in rules.items():
            intent = entry.get("intent", "unknown")
            actual_role = entry.get("actual_role", "?")
            candidate_role = entry.get("candidate_role", "?")
            delta = entry.get("improvement_delta", 0.0)
            n = entry.get("sample_count", 0)
            delta_sign = "+" if delta >= 0 else ""
            bias_flag = " ⚠" if entry.get("selection_bias_warning") else ""
            lines.append(
                f"| {rule_id} | {intent} | {actual_role} | {candidate_role} | "
                f"{delta_sign}{delta:.4f}{bias_flag} | {n} |"
            )
        lines.append("")
        n_matched = len(rules)
        lines.append(f"_n_matched = {n_matched} rule(s). ⚠ = selection_bias_warning (candidate history < 10 samples)._")
        lines.append("")

    # Quality Signal subsection
    lines.append("### Quality Signal")
    lines.append("")
    quality_path = DATA_DIR / "quality_signal.json"
    if not quality_path.exists():
        lines.append("_No quality signal data — data/quality_signal.json not found._")
        lines.append("")
    else:
        try:
            q_raw = json.loads(quality_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            q_raw = {}
            lines.append("_quality_signal.json could not be read._")
            lines.append("")

        per_intent: dict = q_raw.get("per_intent", {}) if isinstance(q_raw, dict) else {}
        if not per_intent:
            lines.append("_No per-intent revert data in quality_signal.json._")
            lines.append("")
        else:
            lines.append("| Intent | Revert Rate |")
            lines.append("|--------|-------------|")
            for intent_key, intent_data in per_intent.items():
                revert_rate = intent_data.get("revert_rate", 0.0) if isinstance(intent_data, dict) else intent_data
                lines.append(f"| {intent_key} | {revert_rate:.1%} |")
            lines.append("")

    return "\n".join(lines)


def _build_routing_candidates_section() -> str:
    """Build a Routing Candidates markdown section for the weekly digest.

    Data sources:
      - data/confidence_scores.json  → entries with ready=True
      - data/shadow_evaluation.json  → improvement_delta per rule/intent
      - data/quality_signal.json     → per_intent[intent].revert_rate

    Composite score:
      composite_score = confidence * max(0, min(1, delta)) * (1 - revert_rate)

    Action thresholds:
      composite > 0.7 and blast_radius == "low"  → Promote
      composite > 0.4                             → Review
      otherwise                                   → Monitor
    """
    lines: list[str] = ["## Routing Candidates", ""]

    conf_path = DATA_DIR / "confidence_scores.json"
    try:
        conf_raw = json.loads(conf_path.read_text(encoding="utf-8"))
        conf_entries = [
            e for e in conf_raw.get("entries", [])
            if isinstance(e, dict) and e.get("ready")
        ]
    except (OSError, json.JSONDecodeError):
        conf_entries = []

    if not conf_entries:
        lines.append("_No routing candidates — no ready entries in data/confidence_scores.json._")
        lines.append("")
        return "\n".join(lines)

    shadow_path = DATA_DIR / "shadow_evaluation.json"
    shadow_timestamp = ""
    delta_lookup: dict[str, float] = {}
    try:
        shadow_raw = json.loads(shadow_path.read_text(encoding="utf-8"))
        shadow_timestamp = shadow_raw.get("timestamp", "")
        for rule_id, entry in shadow_raw.get("rules", {}).items():
            if isinstance(entry, dict):
                intent_key = entry.get("intent", rule_id)
                delta_val = entry.get("improvement_delta", 0.0)
                try:
                    delta_val = float(delta_val)
                except (TypeError, ValueError):
                    delta_val = 0.0
                delta_lookup[intent_key] = delta_val
                delta_lookup[rule_id] = delta_val
    except (OSError, json.JSONDecodeError):
        pass

    quality_path = DATA_DIR / "quality_signal.json"
    quality_timestamp = ""
    revert_lookup: dict[str, float] = {}
    try:
        q_raw = json.loads(quality_path.read_text(encoding="utf-8"))
        quality_timestamp = q_raw.get("timestamp", "")
        for intent_key, intent_data in q_raw.get("per_intent", {}).items():
            if isinstance(intent_data, dict):
                rr = intent_data.get("revert_rate", 0.0)
            else:
                rr = intent_data
            try:
                rr = float(rr)
            except (TypeError, ValueError):
                rr = 0.0
            revert_lookup[intent_key] = rr
    except (OSError, json.JSONDecodeError):
        pass

    rows: list[dict] = []
    for entry in conf_entries:
        intent = entry.get("intent", "")
        confidence = float(entry.get("confidence", 0.0))
        n = entry.get("n", 0)
        current_role = entry.get("verdict_role", "")
        suggested_role = entry.get("suggested_role") or "—"
        blast_radius = entry.get("blast_radius", "")
        rule_id = entry.get("rule_id", "")

        delta = delta_lookup.get(intent, delta_lookup.get(rule_id, 0.0))
        revert_rate = revert_lookup.get(intent, 0.0)

        clamped_delta = max(0.0, min(1.0, delta))
        composite = confidence * clamped_delta * (1.0 - revert_rate)

        if composite > 0.7 and blast_radius == "low":
            action = "Promote"
        elif composite > 0.4:
            action = "Review"
        else:
            action = "Monitor"

        rows.append({
            "intent": intent,
            "current_role": current_role,
            "suggested_role": suggested_role,
            "confidence": confidence,
            "n": n,
            "delta": delta,
            "revert_rate": revert_rate,
            "composite": composite,
            "action": action,
        })

    rows.sort(key=lambda r: r["composite"], reverse=True)
    rows = rows[:10]

    lines.append("| Rank | Intent | Current Role | Suggested Role | Confidence | N | Shadow Delta | Revert Rate | Composite Score | Recommended Action |")
    lines.append("|------|--------|--------------|----------------|------------|---|-------------|-------------|-----------------|-------------------|")
    for rank, row in enumerate(rows, 1):
        delta_sign = "+" if row["delta"] >= 0 else ""
        lines.append(
            f"| {rank} | {row['intent']} | {row['current_role']} | {row['suggested_role']} | "
            f"{row['confidence']:.2f} | {row['n']} | {delta_sign}{row['delta']:.4f} | "
            f"{row['revert_rate']:.1%} | {row['composite']:.4f} | {row['action']} |"
        )
    lines.append("")

    footer_parts: list[str] = []
    if shadow_timestamp:
        footer_parts.append(f"Shadow eval window: {shadow_timestamp}")
    if quality_timestamp:
        footer_parts.append(f"Quality signal date: {quality_timestamp}")
    if footer_parts:
        lines.append(f"_{' | '.join(footer_parts)}_")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate(report: object, since: str, until: str) -> Path:
    """Generate a weekly digest markdown file at data/weekly-digest-{until}.md.

    Args:
        report: WindowReport from impact_calculator.compute_window
        since:  ISO date string for window start
        until:  ISO date string for window end

    Returns:
        Path to the written file.
    """
    counterfactual_total = report.total_spent + report.routing_savings
    fast_turns = round(report.routing_rate * report.total_turns)

    if report.prev_week_routing_rate is not None:
        direction = "up" if report.prev_week_routing_rate >= 0 else "down"
        trend_line = f"({direction} {abs(report.prev_week_routing_rate):.0%} vs. last week)"
    else:
        trend_line = "(gathering baseline data)"

    gate_lines = []
    for gate_type, (count, dollars) in report.gate_breakdown.items():
        display = GATE_DISPLAY_NAMES.get(gate_type, gate_type)
        gate_lines.append(f"  - {count}x {display} (${dollars:.2f} est.)")
    gate_section = "\n".join(gate_lines) if gate_lines else "  - No gate events recorded."

    if report.top_moments:
        moment_lines = []
        for i, m in enumerate(report.top_moments, 1):
            moment_lines.append(
                f"{i}. **Session {m['ts']}** -- {m['description']}, saved ${m['est_saved_usd']:.2f}"
            )
        moments_section = "\n".join(moment_lines)
    else:
        moments_section = "Not enough session variety for top moments yet."

    shadow_eval_section = _build_shadow_eval_section()
    routing_candidates_section = _build_routing_candidates_section()

    md = f"""# cost-router weekly digest -- {since} to {until}

## Summary
- **Est. saved this week:** ${report.total_est_saved:.2f}
- **Est. spent this week:** ${report.total_spent:.2f}
- **Sessions:** {report.total_sessions} | **Turns:** {report.total_turns}

## Savings Breakdown

### Routing Savings: ${report.routing_savings:.2f}
- {fast_turns} of {report.total_turns} turns ({report.routing_rate:.0%}) routed to fast tier
- Counterfactual (all-{report.baseline_model}): ${counterfactual_total:.2f} -> Actual: ${report.total_spent:.2f}

### Waste Prevention: ${report.waste_prevented:.2f}
{gate_section}

### Policy Trend
- Fast-tier routing rate: {report.routing_rate:.0%}
- {trend_line}

## Top Savings Moments
{moments_section}

{shadow_eval_section}

{routing_candidates_section}

## Notes
- All figures are estimates based on local token counting and pricing.json rates
- Baseline: all work on most expensive configured model ({report.baseline_model})
"""

    out_path = DATA_DIR / f"weekly-digest-{until}.md"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    return out_path
