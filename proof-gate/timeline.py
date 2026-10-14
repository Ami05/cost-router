#!/usr/bin/env python3
"""Timeline tracker: reads all baselines and generates a trend report."""

import json
import sys
from datetime import datetime
from pathlib import Path
from glob import glob

def load_baselines(baseline_dir):
    """Load all baseline JSON files, excluding test files."""
    baseline_path = Path(baseline_dir)

    # Get all JSON files, excluding test_before.json and test_after.json
    json_files = sorted(baseline_path.glob("*.json"))
    baselines = []

    for filepath in json_files:
        if filepath.name in ("test_before.json", "test_after.json"):
            continue

        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
                baselines.append({
                    'filepath': filepath.name,
                    'data': data
                })
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Failed to load {filepath.name}: {e}", file=sys.stderr)

    return baselines

def sort_by_timestamp(baselines):
    """Sort baselines by collected_at timestamp."""
    def get_timestamp(baseline):
        try:
            ts_str = baseline['data'].get('metadata', {}).get('collected_at', '')
            return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return datetime.min

    return sorted(baselines, key=get_timestamp)

def extract_metrics(baseline):
    """Extract key metrics from a baseline."""
    data = baseline['data']

    # Extract numeric metrics
    accuracy = data.get('classifier_metrics', {}).get('accuracy', None)
    fallback_rate = data.get('classifier_metrics', {}).get('fallback_rate', None)
    deny_count = data.get('gate_metrics', {}).get('deny_count', None)
    nudge_count = data.get('gate_metrics', {}).get('nudge_count', None)
    cost_usd = data.get('cost_metrics', {}).get('estimated_cost_usd', None)
    tokens = data.get('cost_metrics', {}).get('total_estimated_tokens', None)
    sessions = data.get('session_metrics', {}).get('total_sessions', None)

    # Extract role distribution as string summary
    role_dist = data.get('routing_metrics', {}).get('role_distribution', {})
    role_summary = ', '.join(f"{k}:{v}" for k, v in sorted(role_dist.items())) if role_dist else "none"

    # Extract timestamp
    timestamp = data.get('metadata', {}).get('collected_at', '')

    # Parse date for display
    try:
        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        date_str = dt.strftime('%Y-%m-%d %H:%M')
    except (ValueError, AttributeError):
        date_str = timestamp

    return {
        'date': date_str,
        'accuracy': accuracy,
        'fallback_rate': fallback_rate,
        'deny_count': deny_count,
        'nudge_count': nudge_count,
        'cost_usd': cost_usd,
        'tokens': tokens,
        'role_summary': role_summary,
        'sessions': sessions
    }

def find_best_worst(metrics_list, key):
    """Find indices of best and worst values for a metric."""
    values = [m.get(key) for m in metrics_list]
    valid_values = [(i, v) for i, v in enumerate(values) if v is not None]

    if not valid_values:
        return None, None

    # For rate/percentage metrics (lower is better)
    if key in ('fallback_rate',):
        best_idx = min(valid_values, key=lambda x: x[1])[0]
        worst_idx = max(valid_values, key=lambda x: x[1])[0]
    # For other metrics (higher is better)
    else:
        best_idx = max(valid_values, key=lambda x: x[1])[0]
        worst_idx = min(valid_values, key=lambda x: x[1])[0]

    return best_idx, worst_idx

def format_value(value, key):
    """Format metric value for display."""
    if value is None:
        return "—"

    if key == 'accuracy':
        return f"{value:.1%}"
    elif key == 'fallback_rate':
        return f"{value:.2%}"
    elif key == 'cost_usd':
        return f"${value:.4f}"
    elif key == 'tokens':
        return f"{int(value):,}"
    elif key == 'sessions':
        return f"{int(value):,}"
    elif key in ('deny_count', 'nudge_count'):
        return f"{int(value):,}"
    else:
        return str(value)

def generate_markdown_table(baselines):
    """Generate markdown table with metrics and trend line."""
    if not baselines:
        return "No baselines found"

    metrics_list = [extract_metrics(b) for b in baselines]

    # Single baseline case
    if len(metrics_list) == 1:
        m = metrics_list[0]
        table = "| Date | Accuracy | Fallback% | Denies | Nudges | Cost($) | Tokens | Sessions |\n"
        table += "|------|----------|-----------|--------|--------|---------|--------|----------|\n"
        table += (f"| {m['date']} | {format_value(m['accuracy'], 'accuracy')} | "
                  f"{format_value(m['fallback_rate'], 'fallback_rate')} | "
                  f"{format_value(m['deny_count'], 'deny_count')} | "
                  f"{format_value(m['nudge_count'], 'nudge_count')} | "
                  f"{format_value(m['cost_usd'], 'cost_usd')} | "
                  f"{format_value(m['tokens'], 'tokens')} | "
                  f"{format_value(m['sessions'], 'sessions')} |\n")
        table += "\n*Single baseline — no trend data*\n"
        return table

    # Multiple baselines case
    table = "| Date | Accuracy | Fallback% | Denies | Nudges | Cost($) | Tokens | Sessions |\n"
    table += "|------|----------|-----------|--------|--------|---------|--------|----------|\n"

    # Find best/worst indices for each metric
    best_acc, worst_acc = find_best_worst(metrics_list, 'accuracy')
    best_fb, worst_fb = find_best_worst(metrics_list, 'fallback_rate')
    best_deny, worst_deny = find_best_worst(metrics_list, 'deny_count')
    best_nudge, worst_nudge = find_best_worst(metrics_list, 'nudge_count')
    best_cost, worst_cost = find_best_worst(metrics_list, 'cost_usd')
    best_tokens, worst_tokens = find_best_worst(metrics_list, 'tokens')
    best_sessions, worst_sessions = find_best_worst(metrics_list, 'sessions')

    # Build rows
    for i, m in enumerate(metrics_list):
        acc_val = format_value(m['accuracy'], 'accuracy')
        if i == best_acc:
            acc_val += ' *'
        elif i == worst_acc:
            acc_val += ' **'

        fb_val = format_value(m['fallback_rate'], 'fallback_rate')
        if i == best_fb:
            fb_val += ' *'
        elif i == worst_fb:
            fb_val += ' **'

        deny_val = format_value(m['deny_count'], 'deny_count')
        if i == best_deny:
            deny_val += ' *'
        elif i == worst_deny:
            deny_val += ' **'

        nudge_val = format_value(m['nudge_count'], 'nudge_count')
        if i == best_nudge:
            nudge_val += ' *'
        elif i == worst_nudge:
            nudge_val += ' **'

        cost_val = format_value(m['cost_usd'], 'cost_usd')
        if i == best_cost:
            cost_val += ' *'
        elif i == worst_cost:
            cost_val += ' **'

        tokens_val = format_value(m['tokens'], 'tokens')
        if i == best_tokens:
            tokens_val += ' *'
        elif i == worst_tokens:
            tokens_val += ' **'

        sessions_val = format_value(m['sessions'], 'sessions')
        if i == best_sessions:
            sessions_val += ' *'
        elif i == worst_sessions:
            sessions_val += ' **'

        table += (f"| {m['date']} | {acc_val} | {fb_val} | {deny_val} | {nudge_val} | "
                  f"{cost_val} | {tokens_val} | {sessions_val} |\n")

    # Add trend line
    table += "\n**Trend (first → last):**\n"

    def trend_arrow(first_val, last_val, higher_is_better=True):
        """Generate trend arrow."""
        if first_val is None or last_val is None:
            return "→ —"

        if higher_is_better:
            if last_val > first_val:
                return "↑"
            elif last_val < first_val:
                return "↓"
            else:
                return "→"
        else:
            if last_val < first_val:
                return "↑"
            elif last_val > first_val:
                return "↓"
            else:
                return "→"

    first = metrics_list[0]
    last = metrics_list[-1]

    # Accuracy: higher is better
    acc_trend = trend_arrow(first['accuracy'], last['accuracy'], higher_is_better=True)
    # Fallback rate: lower is better
    fb_trend = trend_arrow(first['fallback_rate'], last['fallback_rate'], higher_is_better=False)
    # Denies: context dependent, but assume lower is better
    deny_trend = trend_arrow(first['deny_count'], last['deny_count'], higher_is_better=False)
    # Nudges: context dependent, but assume lower is better
    nudge_trend = trend_arrow(first['nudge_count'], last['nudge_count'], higher_is_better=False)
    # Cost: lower is better
    cost_trend = trend_arrow(first['cost_usd'], last['cost_usd'], higher_is_better=False)
    # Tokens: lower is better (efficiency)
    tokens_trend = trend_arrow(first['tokens'], last['tokens'], higher_is_better=False)
    # Sessions: higher is better (throughput)
    sessions_trend = trend_arrow(first['sessions'], last['sessions'], higher_is_better=True)

    table += f"| Accuracy | {acc_trend} | Fallback% | {fb_trend} | Denies | {deny_trend} |\n"
    table += f"| Nudges | {nudge_trend} | Cost($) | {cost_trend} | Tokens | {tokens_trend} |\n"
    table += f"| Sessions | {sessions_trend} |\n"

    return table

def main():
    """Main entry point."""
    baseline_dir = Path(__file__).resolve().parent / "baseline"

    if not baseline_dir.exists():
        print("Error: Baseline directory not found", file=sys.stderr)
        sys.exit(1)

    # Load and sort baselines
    baselines = load_baselines(baseline_dir)

    if not baselines:
        print("No baselines found")
        sys.exit(0)

    baselines = sort_by_timestamp(baselines)

    # Generate markdown table
    markdown = generate_markdown_table(baselines)

    # Print to stdout
    print(markdown)

    # Save to evaluationReport directory
    eval_report_dir = Path(__file__).resolve().parent / "evaluationReport"
    eval_report_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename with current date
    now = datetime.now()
    filename = f"timeline_{now.strftime('%Y-%m-%d')}.md"
    output_path = eval_report_dir / filename

    with open(output_path, 'w') as f:
        f.write(markdown)

    print(f"\nReport saved to: {output_path}")

if __name__ == "__main__":
    main()
