#!/usr/bin/env python3
"""
Baseline Collector: Capture current system metrics from real sessions.

Reads all data sources in cost-router/data/ and aggregates metrics across
5 sections: session_metrics, gate_metrics, classifier_metrics, cost_metrics,
routing_metrics. Outputs timestamped JSON snapshot to proof-gate/baseline/.

No external dependencies — stdlib only.
"""

import json
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict


def load_jsonl(file_path, max_records=None):
    """Load JSONL file with graceful error handling."""
    records = []
    if not os.path.exists(file_path):
        print(f"  WARNING: {file_path} not found")
        return records

    try:
        with open(file_path, 'r') as f:
            for i, line in enumerate(f):
                if max_records and i >= max_records:
                    break
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except Exception as e:
        print(f"  ERROR reading {file_path}: {e}")

    return records


def load_json(file_path):
    """Load JSON file with graceful error handling."""
    if not os.path.exists(file_path):
        print(f"  WARNING: {file_path} not found")
        return {}

    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"  ERROR reading {file_path}: {e}")
        return {}


def audit_gate_code(project_root):
    """Audit gate source code for decision paths."""
    import re
    
    # Gate files to scan
    gate_files = [
        "scripts/check_routing.py",
        "scripts/gates.py",
        "scripts/enforce_role_gates.py",
        "scripts/dispatch_context_gate.py",
        "scripts/file_graph_gate.py",
        "scripts/file_hash_guard.py",
        "scripts/check_read_repeat.py",
    ]
    
    scanned_files = []
    total_deny = 0
    total_nudge = 0
    total_allow = 0
    total_except = 0
    
    for gate_file in gate_files:
        file_path = os.path.join(project_root, gate_file)
        
        if not os.path.exists(file_path):
            continue
        
        scanned_files.append(os.path.basename(gate_file))
        
        try:
            with open(file_path, 'r') as f:
                content = f.read()
        except Exception as e:
            print(f"  WARNING: Could not read {gate_file}: {e}")
            continue
        
        # Count deny paths: return _deny(
        deny_count = len(re.findall(r'return\s+_deny\s*\(', content))
        total_deny += deny_count
        
        # Count nudge paths: return _allow(is_claude, _<something>)
        nudge_count = len(re.findall(r'return\s+_allow\s*\(\s*is_claude\s*,\s*_', content))
        total_nudge += nudge_count
        
        # Count pure allow paths: return _allow(is_claude, None) or return _allow(is_claude)
        pure_allow1 = len(re.findall(r'return\s+_allow\s*\(\s*is_claude\s*,\s*None\s*\)', content))
        pure_allow2 = len(re.findall(r'return\s+_allow\s*\(\s*is_claude\s*\)(?!\s*,)', content)) - pure_allow1
        allow_count = pure_allow1 + max(0, pure_allow2)
        total_allow += allow_count
        
        # Count exception handlers: except Exception
        except_count = len(re.findall(r'except\s+Exception', content))
        total_except += except_count
    
    # Count override keywords
    override_keywords = [
        "direct override",
        "dispatch-budget-override", 
        "dispatch-group-override",
        "no subagent dispatch"
    ]
    
    total_overrides = 0
    for gate_file in gate_files:
        file_path = os.path.join(project_root, gate_file)
        if not os.path.exists(file_path):
            continue
        
        try:
            with open(file_path, 'r') as f:
                content = f.read()
            
            for keyword in override_keywords:
                total_overrides += content.count(keyword)
        except:
            pass
    
    # Calculate ratio
    deny_to_nudge_ratio = (
        round(total_deny / total_nudge, 2) if total_nudge > 0 else 0.0
    )
    
    gate_code_audit = {
        "deny_paths": total_deny,
        "nudge_paths": total_nudge,
        "pure_allow_paths": total_allow,
        "fail_open_paths": total_except,
        "override_keywords": total_overrides,
        "deny_to_nudge_ratio": deny_to_nudge_ratio,
        "scanned_files": scanned_files
    }
    
    return gate_code_audit


def estimate_entry_cost(entry, pricing):
    """Estimate USD cost for a dispatch context estimate entry."""
    if not entry or not entry.get("est_tokens"):
        return 0.0

    model_str = entry.get("model")
    if not model_str:
        model = ""
    else:
        model = str(model_str).lower()

    est_tokens = entry.get("est_tokens", 0)

    # Find matching model in pricing
    rates = pricing.get("default", {})
    for model_key in pricing:
        if model_key != "default" and model_key in model:
            rates = pricing[model_key]
            break

    input_rate = rates.get("input_per_1k", 0.003)
    output_rate = rates.get("output_per_1k", 0.015)

    # Estimate: split tokens 30% input, 70% output
    input_tokens = int(est_tokens * 0.3)
    output_tokens = int(est_tokens * 0.7)

    cost = (
        (input_tokens / 1000.0) * input_rate +
        (output_tokens / 1000.0) * output_rate
    )
    return cost


def compute_metrics(data_dir):
    """Compute all baseline metrics from data sources."""

    print(f"Reading data from: {data_dir}")

    # Load all data sources
    signal_entries = load_jsonl(os.path.join(data_dir, "signal.jsonl"))
    reward_log = load_jsonl(os.path.join(data_dir, "reward_log.jsonl"))
    policy_changelog = load_jsonl(os.path.join(data_dir, "policy_changelog.jsonl"))
    semantic_low_conf = load_jsonl(os.path.join(data_dir, "semantic_low_confidence.jsonl"))
    classifier_log = load_jsonl(os.path.join(data_dir, "claude_classifier.jsonl"))
    routing_state = load_json(os.path.join(data_dir, "routing_state.json"))
    convergence_state = load_json(os.path.join(data_dir, "convergence_state.json"))
    calibration_log = load_jsonl(os.path.join(data_dir, "calibration.jsonl"))
    usage_log = load_jsonl(os.path.join(data_dir, "usage.jsonl"))
    pricing = load_json(os.path.join(data_dir, "..", "scripts", "pricing.json"))

    # SESSION_METRICS
    session_ids = set()
    turn_ids = set()

    for entry in signal_entries:
        session_ids.add(entry.get("session_id", "unknown"))
        if entry.get("turn_id"):
            turn_ids.add(entry.get("turn_id"))

    session_metrics = {
        "total_sessions": len(signal_entries),
        "total_turns": len(turn_ids),
        "unique_session_ids": len(session_ids),
        "entries_per_session": round(len(signal_entries) / max(1, len(session_ids)), 2)
    }

    # GATE_METRICS
    deny_count = 0
    nudge_count = 0
    deny_by_gate_id = defaultdict(int)

    for entry in signal_entries:
        event = entry.get("event", "").lower()
        session_id = entry.get("session_id", "").lower()

        # Count denies: event="deny" or "deny" in session_id
        if event == "deny":
            deny_count += 1
            gate_id = entry.get("gate_id") or entry.get("rule_id") or "unknown"
            deny_by_gate_id[gate_id] += 1

        # Count nudges: event="nudge" or "nudge" in session_id
        if event == "nudge":
            nudge_count += 1

    gate_metrics = {
        "total_signal_entries": len(signal_entries),
        "deny_count": deny_count,
        "nudge_count": nudge_count,
        "deny_by_gate_id": dict(deny_by_gate_id)
    }

    # CLASSIFIER_METRICS
    # PG-001: fallback_rate denominator = all classifier entries, not low-confidence subset
    fallback_count = sum(1 for e in classifier_log if e.get("was_fallback", False))
    fallback_rate = fallback_count / len(classifier_log) if classifier_log else 0.0

    accuracy = convergence_state.get("current_accuracy", 0.0)
    verdict = convergence_state.get("verdict", "UNKNOWN")

    classifier_metrics = {
        "accuracy": accuracy,
        "fallback_rate": fallback_rate,
        "low_confidence_count": len(semantic_low_conf),
        "convergence_verdict": verdict
    }

    # COST_METRICS
    total_estimated_tokens = 0
    total_actual_tokens = 0

    # Sum from calibration.jsonl
    for entry in calibration_log:
        total_estimated_tokens += entry.get("estimated_tokens", 0)
        total_actual_tokens += entry.get("actual_tokens", 0)

    # Sum from usage.jsonl
    for entry in usage_log:
        total_estimated_tokens += entry.get("est_tokens", 0)

    calibration_ratio = (
        total_actual_tokens / total_estimated_tokens
        if total_estimated_tokens > 0 else 0.0
    )

    # Estimate cost using pricing.json
    estimated_cost_usd = 0.0
    if total_estimated_tokens > 0 and pricing:
        # Use default rates if available
        default_rates = pricing.get("default", {})
        input_rate = default_rates.get("input_per_1k", 0.003)
        output_rate = default_rates.get("output_per_1k", 0.015)

        # Estimate: split tokens 30% input, 70% output (rough average)
        input_tokens = int(total_estimated_tokens * 0.3)
        output_tokens = int(total_estimated_tokens * 0.7)
        estimated_cost_usd = (
            (input_tokens / 1000.0) * input_rate +
            (output_tokens / 1000.0) * output_rate
        )

    cost_metrics = {
        "total_estimated_tokens": total_estimated_tokens,
        "total_actual_tokens": total_actual_tokens,
        "calibration_ratio": round(calibration_ratio, 4),
        "estimated_cost_usd": round(estimated_cost_usd, 4)
    }

    # ROUTING_METRICS
    role_distribution = defaultdict(int)
    for entry in reward_log:
        role = entry.get("routing_role", "unknown")
        role_distribution[role] += 1

    # PG-003: tier_transitions = actual model-tier switches, not proposals
    tier_transitions = sum(
        1 for e in policy_changelog
        if e.get("event_type") in ["canary_started", "applied", "promoted", "demoted"]
    )

    policy_proposals = sum(
        1 for e in policy_changelog
        if e.get("event_type") == "proposed"
    )

    routing_metrics = {
        "role_distribution": dict(role_distribution),
        "tier_transitions": tier_transitions,
        "policy_proposals": policy_proposals
    }

    # GATE_EVENT_METRICS - read from gate_events.jsonl
    gate_events = load_jsonl(os.path.join(data_dir, "gate_events.jsonl"))

    total_events = len(gate_events)
    deny_events = sum(1 for e in gate_events if e.get("action") == "deny")
    allow_events = sum(1 for e in gate_events if e.get("action") == "allow")
    nudge_events = sum(1 for e in gate_events if e.get("action") == "nudge")

    deny_by_gate = defaultdict(int)
    deny_by_tool = defaultdict(int)
    events_by_chain = defaultdict(int)
    unique_sessions = set()

    for event in gate_events:
        if event.get("action") == "deny":
            gate = event.get("gate_name", "unknown")
            deny_by_gate[gate] += 1
            tool = event.get("tool", "unknown")
            deny_by_tool[tool] += 1

        chain = event.get("gate_chain", "unknown")
        events_by_chain[chain] += 1

        session_id = event.get("session_id")
        if session_id:
            unique_sessions.add(session_id)

    gate_event_metrics = {
        "total_events": total_events,
        "deny_events": deny_events,
        "allow_events": allow_events,
        "nudge_events": nudge_events,
        "deny_by_gate": dict(deny_by_gate),
        "deny_by_tool": dict(deny_by_tool),
        "events_by_chain": dict(events_by_chain),
        "unique_sessions": len(unique_sessions)
    }


    # GATE_CODE_AUDIT
    project_root = str(Path(__file__).parent.parent)
    gate_code_audit = audit_gate_code(project_root)

    # COST_VISIBILITY_METRICS
    # Compute cost visibility metrics from usage.jsonl (dispatch context estimates)
    total_sessions_logged = len(usage_log)

    # Sessions with cost data (those with est_tokens > 0)
    sessions_with_cost_data = sum(1 for e in usage_log if e.get("est_tokens", 0) > 0)

    # Estimate total cost and track sessions with savings
    total_estimated_cost_usd = 0.0
    sessions_with_savings_shown = 0
    total_estimated_savings_usd = 0.0

    # Unique sessions in usage log
    usage_sessions = set()

    for entry in usage_log:
        usage_sessions.add(entry.get("session_id", "unknown"))

        # Accumulate cost
        entry_cost = estimate_entry_cost(entry, pricing)
        total_estimated_cost_usd += entry_cost

        # Track savings: a session shows savings if we routed to a cheaper model
        # Calculate savings by comparing fast-tier routed cost vs opus counterfactual
        est_tokens = entry.get("est_tokens", 0)
        tier = entry.get("tier", "")
        if tier == "fast" and est_tokens > 0:
            sessions_with_savings_shown += 1
            # Compute opus counterfactual cost
            opus_rates = pricing.get("opus", {})
            opus_input_rate = opus_rates.get("input_per_1k", 0.003)
            opus_output_rate = opus_rates.get("output_per_1k", 0.015)
            input_tokens = int(est_tokens * 0.3)
            output_tokens = int(est_tokens * 0.7)
            opus_cost = (
                (input_tokens / 1000.0) * opus_input_rate +
                (output_tokens / 1000.0) * opus_output_rate
            )
            # Actual routed cost
            actual_cost = estimate_entry_cost(entry, pricing)
            # Savings is difference, clipped to non-negative
            delta = max(0.0, opus_cost - actual_cost)
            total_estimated_savings_usd += delta

    avg_cost_per_session = (
        total_estimated_cost_usd / len(usage_sessions)
        if usage_sessions else 0.0
    )

    avg_savings_per_session = (
        total_estimated_savings_usd / len(usage_sessions)
        if usage_sessions else 0.0
    )

    savings_display_rate = (
        sessions_with_savings_shown / total_sessions_logged
        if total_sessions_logged > 0 else 0.0
    )

    cost_visibility_metrics = {
        "total_sessions_logged": total_sessions_logged,
        "sessions_with_cost_data": sessions_with_cost_data,
        "total_estimated_cost_usd": round(total_estimated_cost_usd, 4),
        "total_estimated_savings_usd": round(total_estimated_savings_usd, 4),
        "avg_cost_per_session": round(avg_cost_per_session, 4),
        "avg_savings_per_session": round(avg_savings_per_session, 4),
        "savings_display_rate": round(savings_display_rate, 4),
        "baseline_model": "claude-opus"
    }

    audit_data = gate_code_audit
    return {
        "metadata": {},
        "session_metrics": session_metrics,
        "gate_metrics": gate_metrics,
        "gate_event_metrics": gate_event_metrics,
        "classifier_metrics": classifier_metrics,
        "cost_metrics": cost_metrics,
        "routing_metrics": routing_metrics,
        "cost_visibility_metrics": cost_visibility_metrics
    }


def main():
    """Main entry point."""

    # Determine script location and data directory
    script_dir = Path(__file__).parent
    data_dir = script_dir.parent / "data"
    baseline_dir = script_dir / "baseline"

    # Ensure baseline directory exists
    baseline_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("BASELINE COLLECTOR v1.0.0")
    print("=" * 70)

    # Compute metrics
    metrics = compute_metrics(str(data_dir))

    # Add metadata
    now = datetime.utcnow()
    metrics["metadata"] = {
        "collected_at": now.isoformat() + "Z",
        "data_dir": str(data_dir.resolve()),
        "script_version": "1.0.0"
    }

    # Generate filename with timestamp
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    output_file = baseline_dir / f"baseline_{timestamp}.json"

    # Write output
    try:
        with open(output_file, 'w') as f:
            json.dump(metrics, f, indent=2)
        print(f"\nBaseline written to: {output_file}")
    except Exception as e:
        print(f"ERROR writing baseline: {e}")
        return 1

    # Write gate code audit to separate file
    audit_dir = baseline_dir.parent / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_file = audit_dir / f"audit_{timestamp}.json"
    with open(audit_file, "w") as f:
        json.dump(metrics.pop("gate_code_audit", {}), f, indent=2)
    print(f"Gate code audit written to: {audit_file}")

    # Print summary
    print("\n" + "=" * 70)
    print("BASELINE SUMMARY")
    print("=" * 70)
    print(f"\nSession Metrics:")
    for k, v in metrics["session_metrics"].items():
        print(f"  {k}: {v}")

    print(f"\nGate Metrics:")
    for k, v in metrics["gate_metrics"].items():
        if k != "deny_by_gate_id":
            print(f"  {k}: {v}")
        else:
            print(f"  {k}: {dict(v)}")

    print(f"\nGate Event Metrics:")
    for k, v in metrics["gate_event_metrics"].items():
        if isinstance(v, dict):
            print(f"  {k}: {dict(v)}")
        else:
            print(f"  {k}: {v}")

    print(f"\nClassifier Metrics:")
    for k, v in metrics["classifier_metrics"].items():
        print(f"  {k}: {v}")

    print(f"\nCost Metrics:")
    for k, v in metrics["cost_metrics"].items():
        print(f"  {k}: {v}")

    print(f"\nRouting Metrics:")
    for k, v in metrics["routing_metrics"].items():
        if k != "role_distribution":
            print(f"  {k}: {v}")
        else:
            print(f"  {k}: {dict(v)}")

    print(f"\nCost Visibility Metrics:")
    for k, v in metrics["cost_visibility_metrics"].items():
        print(f"  {k}: {v}")

    print("\n" + "=" * 70)
    print("Collection complete.")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    exit(main())
