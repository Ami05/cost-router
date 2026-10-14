"""dispatch_report — Dispatch denial summary report.

Exposes:
    generate(report, since: str, until: str) -> Path

Reads data/dispatch_denials.jsonl and writes a markdown summary to
data/dispatch-report-{until}.md.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = _ROOT / "data"

# Dispatch-JSON formatting failures (schema/validator subtypes from
# validate_dispatch_schema.py) — noise about prompt shape, not about which
# model/tier was routed to.
_FORMAT_SUBTYPES = {
    "code_block",
    "not_json",
    "unknown_worker",
    "missing_fields",
    "schema_stuffing",
    "invalid_examples",
    "invalid_field_value",
    "token_limit",
    "orchestrator_plan_field",
    "orchestrator_multi_phase",
    "token_limit_auto_extracted",
}

# gate_events.jsonl gate_name values that represent real model/worker-tier
# routing violations (enforce_explicit_model, enforce_worker_model,
# explore_model_tier_gate, and similar) rather than dispatch-format noise.
_ROUTING_VIOLATION_GATES = {
    "enforce_explicit_model",
    "enforce_worker_model",
    "explore_model_tier_gate",
    "opus_subagent",
}


def _load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def generate(report: object, since: str, until: str) -> Path:
    """Generate a dispatch denial summary at data/dispatch-report-{until}.md.

    Splits denials into two sections so the small number of real
    model/worker-tier routing violations isn't buried under the much larger
    volume of dispatch-JSON formatting failures:
      - "Dispatch Format Failures": subtypes from data/dispatch_denials.jsonl
        that are about prompt/JSON shape (not_json, schema_stuffing, etc.)
      - "Model/Worker Routing Violations": gate_name values from
        data/gate_events.jsonl (action == "deny") for real routing gates
        (enforce_explicit_model, enforce_worker_model,
        explore_model_tier_gate, opus_subagent).

    Args:
        report: WindowReport (unused by this report; kept for protocol consistency)
        since:  ISO date string for window start
        until:  ISO date string for window end

    Returns:
        Path to the written file.
    """
    denials_path = DATA_DIR / "dispatch_denials.jsonl"
    records = _load_jsonl(denials_path)

    # Filter to window
    window_records = [r for r in records if since <= r.get("ts", "") <= until + "Z"]

    format_records = [r for r in window_records if r.get("subtype", "unknown") in _FORMAT_SUBTYPES]
    other_records = [r for r in window_records if r.get("subtype", "unknown") not in _FORMAT_SUBTYPES]

    format_counts: Counter[str] = Counter(r.get("subtype", "unknown") for r in format_records)

    gate_events_path = DATA_DIR / "gate_events.jsonl"
    gate_records = _load_jsonl(gate_events_path)
    window_gate_records = [
        r for r in gate_records
        if since <= r.get("timestamp", "") <= until + "Z"
        and r.get("action") == "deny"
        and r.get("gate_name") in _ROUTING_VIOLATION_GATES
    ]
    routing_violation_counts: Counter[str] = Counter(
        r.get("gate_name", "unknown") for r in window_gate_records
    )

    lines: list[str] = [
        f"# Dispatch Denial Report — {since} to {until}",
        "",
        f"**Dispatch-schema denials (dispatch_denials.jsonl):** {len(window_records)}",
        f"**Routing-violation denials (gate_events.jsonl):** {len(window_gate_records)}",
        f"**Total denials in window:** {len(window_records) + len(window_gate_records)}",
        "",
        "## Dispatch Format Failures",
        "",
        f"_Dispatch-JSON formatting/schema failures — not model-routing issues._ "
        f"**Count:** {len(format_records)}",
        "",
    ]

    if format_counts:
        lines += [
            "| Subtype | Count |",
            "|---------|-------|",
        ]
        for subtype, count in format_counts.most_common():
            lines.append(f"| {subtype} | {count} |")
        lines.append("")
    else:
        lines.append("_No dispatch format failures recorded in this window._")
        lines.append("")

    lines += [
        "## Model/Worker Routing Violations",
        "",
        "_Real model/worker-tier mis-routing — enforce_explicit_model, "
        "enforce_worker_model, explore_model_tier_gate, opus_subagent denials "
        "from data/gate_events.jsonl._ "
        f"**Count:** {len(window_gate_records)}",
        "",
    ]

    if routing_violation_counts:
        lines += [
            "| Gate | Count |",
            "|------|-------|",
        ]
        for gate_name, count in routing_violation_counts.most_common():
            lines.append(f"| {gate_name} | {count} |")
        lines.append("")
    else:
        lines.append("_No model/worker routing violations recorded in this window._")
        lines.append("")

    if other_records:
        other_counts: Counter[str] = Counter(r.get("subtype", "unknown") for r in other_records)
        lines += [
            "## Other Denial Subtypes",
            "",
            "_Denial subtypes not classified as format failures (uncategorized)._",
            "",
            "| Subtype | Count |",
            "|---------|-------|",
        ]
        for subtype, count in other_counts.most_common():
            lines.append(f"| {subtype} | {count} |")
        lines.append("")

    lines += [
        "## Notes",
        "- Format-failure subtypes: code_block, not_json, unknown_worker, missing_fields,",
        "  schema_stuffing, invalid_examples, invalid_field_value, token_limit,",
        "  orchestrator_plan_field, orchestrator_multi_phase, token_limit_auto_extracted",
        "- Routing-violation gates: enforce_explicit_model, enforce_worker_model,",
        "  explore_model_tier_gate, opus_subagent",
        "- Only category metadata is stored — no prompt content.",
    ]

    out_path = DATA_DIR / f"dispatch-report-{until}.md"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
