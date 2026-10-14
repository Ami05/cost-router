---
name: weekly-digest
description: Generate a detailed weekly savings digest in markdown. Use for /weekly-digest or when the user asks for a weekly breakdown, routing analysis, or detailed cost report.
context: fork
---

# Weekly Digest

1. Run `python3 scripts/generate_reports.py --digest` from plugin root and print output verbatim.
2. If output says "Not enough data yet": the minimum session count (DEFAULT_MIN_SESSIONS in scripts/impact_calculator.py) must be reached.
3. The digest is saved to `data/weekly-digest-YYYY-MM-DD.md`. It includes:
   - **Summary**: total est. saved, spent, sessions, turns
   - **Savings Breakdown**: routing savings detail + waste prevention per gate type
   - **Policy Trend**: week-over-week fast-routing rate change
   - **Top Savings Moments**: the 3 sessions with the highest individual savings
   - **Notes**: estimation methodology and baseline disclosure
4. Use `--since YYYY-MM-DD` to change the reporting window: `python3 scripts/generate_reports.py --digest --since 2026-08-01`
