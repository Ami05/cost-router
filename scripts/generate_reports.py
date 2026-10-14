"""generate_reports.py — Thin CLI dispatcher for cost-router impact reports.

Delegates all report logic to the scripts/reports/ package via REPORT_REGISTRY.

Usage:
    python3 scripts/generate_reports.py --card
    python3 scripts/generate_reports.py --digest
    python3 scripts/generate_reports.py --both
    python3 scripts/generate_reports.py --both --since 2026-08-18
    python3 scripts/generate_reports.py --dispatch
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from impact_calculator import compute_window, DEFAULT_MIN_SESSIONS  # noqa: E402
from reports import REPORT_REGISTRY  # noqa: E402
from reports.routing_report import DATA_DIR  # noqa: E402, F401


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate cost-router impact reports")
    parser.add_argument("--card", action="store_true", help="Generate HTML impact card")
    parser.add_argument("--digest", action="store_true", help="Generate markdown weekly digest")
    parser.add_argument("--dispatch", action="store_true", help="Generate dispatch denial report")
    parser.add_argument("--both", action="store_true", help="Generate card and digest")
    parser.add_argument("--since", default=None, help="Start date ISO (default: 7 days ago)")
    args = parser.parse_args()

    if not (args.card or args.digest or args.dispatch or args.both):
        parser.print_usage(sys.stderr)
        sys.exit(0)

    today = date.today()
    since = args.since if args.since else (today - timedelta(days=7)).isoformat()
    until = today.isoformat()

    report = compute_window(since, until)

    if not report.has_enough_data:
        print(
            f"Not enough data yet -- impact reports available after {DEFAULT_MIN_SESSIONS} sessions.",
            file=sys.stderr,
        )
        sys.exit(0)

    flags = {
        "card": args.card or args.both,
        "digest": args.digest or args.both,
        "dispatch": args.dispatch,
    }

    for flag, requested in flags.items():
        if not requested:
            continue
        generator = REPORT_REGISTRY.get(flag)
        if generator is None:
            print(f"Unknown report type: {flag}", file=sys.stderr)
            continue
        path = generator.generate(report, since, until)
        print(f"{flag.capitalize()} report: {path}")


if __name__ == "__main__":
    main()


def generate_html_card(report: object, since: str, until: str) -> Path:
    """Generate and return path to HTML impact card.
    
    Args:
        report: WindowReport object from impact_calculator.compute_window
        since: Start date (ISO format)
        until: End date (ISO format)
    
    Returns:
        Path to the generated HTML file
    """
    generator = REPORT_REGISTRY.get("card")
    if generator is None:
        raise ValueError("Card report generator not found in REPORT_REGISTRY")
    return generator.generate(report, since, until)


def generate_markdown_digest(report: object, since: str, until: str) -> Path:
    """Generate and return path to markdown weekly digest.
    
    Args:
        report: WindowReport object from impact_calculator.compute_window
        since: Start date (ISO format)
        until: End date (ISO format)
    
    Returns:
        Path to the generated markdown file
    """
    generator = REPORT_REGISTRY.get("digest")
    if generator is None:
        raise ValueError("Digest report generator not found in REPORT_REGISTRY")
    return generator.generate(report, since, until)
