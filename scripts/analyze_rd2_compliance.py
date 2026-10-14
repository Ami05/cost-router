#!/usr/bin/env python3
"""Analyze GATE-RD-2 (structural similarity) nudge compliance rate.

Reads usage.jsonl for gate_rd2_structural_similarity and gate_rd2_compliance
events, computes override vs. comply rates, and prints a summary table.

Usage: python3 scripts/analyze_rd2_compliance.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import config_get, read_events  # noqa: E402


def analyze() -> None:
    events = read_events()
    nudges = [e for e in events if e.get("type") == "gate_rd2_structural_similarity"]
    outcomes = [e for e in events if e.get("type") == "gate_rd2_compliance"]

    complied = [o for o in outcomes if o.get("complied")]
    overrode = [o for o in outcomes if not o.get("complied")]

    total_nudges = len(nudges)
    total_outcomes = len(outcomes)
    comply_count = len(complied)
    override_count = len(overrode)

    if total_nudges == 0:
        print("No GATE-RD-2 events found in usage.jsonl. Collect data for 2 weeks before revisiting.")
        return

    avg_sim = sum(n.get("similarity", 0) for n in nudges) / total_nudges
    avg_latency = (
        sum(o.get("latency_turns", 0) for o in outcomes) / total_outcomes
        if total_outcomes > 0 else 0
    )

    override_rate = (override_count / total_outcomes * 100) if total_outcomes > 0 else 0
    comply_rate = (comply_count / total_outcomes * 100) if total_outcomes > 0 else 0
    pending = total_nudges - total_outcomes

    print("=" * 60)
    print("GATE-RD-2 Structural Similarity — Compliance Report")
    print("=" * 60)
    print(f"  Total nudges fired:        {total_nudges}")
    print(f"  Outcomes tracked:          {total_outcomes}")
    print(f"  Pending (no outcome yet):  {pending}")
    print("-" * 60)
    print(f"  Complied (used Task):      {comply_count}  ({comply_rate:.1f}%)")
    print(f"  Overrode (used Write/Edit):{override_count}  ({override_rate:.1f}%)")
    print(f"  Avg similarity at trigger: {avg_sim:.1%}")
    print(f"  Avg latency (turns):       {avg_latency:.1f}")
    print("-" * 60)

    threshold = config_get("routing.rd2_promote_threshold", 0.30) * 100
    if override_rate > threshold:
        print(f"  RECOMMENDATION: Override rate {override_rate:.1f}% > {threshold:.0f}% threshold")
        print(f"                  → PROMOTE RD-2 to hard deny")
    elif total_outcomes < 10:
        print(f"  RECOMMENDATION: Insufficient data ({total_outcomes} outcomes)")
        print(f"                  → Continue collecting for 2 weeks")
    else:
        print(f"  RECOMMENDATION: Override rate {override_rate:.1f}% ≤ {threshold:.0f}% threshold")
        print(f"                  → KEEP as nudge")
    print("=" * 60)


if __name__ == "__main__":
    analyze()
