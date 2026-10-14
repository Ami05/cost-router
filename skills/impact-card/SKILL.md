---
name: impact-card
description: Generate a shareable HTML impact card showing this week's estimated cost savings. Use for /impact-card or when the user asks to see their weekly savings summary, share cost impact, or generate an impact card.
context: fork
---

# Impact Card

1. Run `python3 scripts/generate_reports.py --card` from plugin root and print output verbatim.
2. If output says "Not enough data yet": the minimum session count (DEFAULT_MIN_SESSIONS in scripts/impact_calculator.py) must be reached.
3. The card is saved to `data/impact-card.html`. Open it in a browser or share in Slack.
4. The hero number shows **est. saved this week** — the sum of routing savings (work routed to fast tier) and waste prevented (gate denials). All numbers are estimates.
