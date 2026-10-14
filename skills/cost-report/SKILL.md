---
name: cost-report
description: Render estimated token/cost report from plugin log. Use for /cost-report or questions about token usage, cost, or spend.
context: fork
---

# Cost Report

1. Run `python3 scripts/session_summary.py --report --sessions 10` from plugin root and print output verbatim.
2. If output says "No session summaries yet": a full session (stop event) must complete first.
3. Adjust `--sessions N` to change the rolling window.
4. The report now includes a **Session Type Breakdown** (Q&A / Agentic / Mixed) — useful for understanding whether question-answering or implementation sessions dominate your token spend.
5. Run `python3 scripts/analyze_routing.py --gate-health-only` from plugin root and print its one-line output right after the session summary report. This mirrors PROD-4's gate-health counters (denies, hit-rate, `fail_open`) from `data/routing_state.json` so the user sees whether the routing gate is actually enforcing without running the full `tune-routing` analysis. If it prints "Gate health: no gate counters recorded yet.", show that line as-is (not an error).
6. Run `python3 scripts/analyze_routing.py --dispatch-tier-only` and print its one-line output right after the gate-health line. This is PROD-19's dispatched-Task tier-mix summary — count and estimated tokens per tier (fast/reasoning/unknown) across every `Task` dispatch recorded in `data/usage.jsonl`, the concrete evidence for whether mechanical work is actually landing on a fast-tier model. If it prints "Dispatch tier mix: no Task dispatches recorded yet.", show that line as-is (not an error).
7. Run `python3 scripts/session_summary.py --signal-stats --sessions 10` from plugin root and print the **Routing Quality** section verbatim. This shows acceptance rate (routing followed vs overridden) and revert count from `data/signal.jsonl`. If it says "No signal data yet", show that line as-is — signal populates after the first routed session completes.

8. Run `python3 scripts/session_summary.py --receipt` from plugin root and print the output verbatim. This shows the **Policy Changelog Receipt** — auto-applied routing changes, estimated savings, rolled-back changes, and high-blast-radius candidates pending your review. If the output is "No policy changes recorded yet.", show that line as-is.

9. Run `python3 scripts/session_summary.py --warnings` from plugin root and print the **Silent Failures** section verbatim. This surfaces any hook-chain errors that were caught silently (fail-open) during sessions. If it says "No silent failures recorded.", show that line as-is.

**Disclaimer:** Heuristic estimates only (~4 chars/token); not real billing data; excludes prompt-cache discounts.
