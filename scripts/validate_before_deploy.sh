#!/usr/bin/env bash
# Pre-deploy validation: run routing scenarios before any PR that
# modifies routing_matrix.json, hooks, or the decision table.
# HG-301: Exits non-zero if any scenario fails.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Check if routing-related files changed
ROUTING_FILES=(
    "config/routing_matrix.json"
    "hooks/hooks.json"
    "scripts/enforce_explicit_model.py"
    "scripts/enforce_role_gates.py"
    "scripts/gates.py"
    "rules/model-router.md"  # substring match, also covers .claude/rules/model-router.md
)

changed=false
for f in "${ROUTING_FILES[@]}"; do
    if git diff --cached --name-only 2>/dev/null | grep -q "$f"; then
        changed=true
        break
    fi
done

if [ "$changed" = false ]; then
    echo "No routing-related files changed. Skipping validation."
    exit 0
fi

echo "Routing files changed. Running validation scenarios..."
python3 "$SCRIPT_DIR/run_routing_validation.py" \
    --scenarios "$PLUGIN_ROOT/tests/routing_scenarios.json" \
    --runs 3 \
    --output "$PLUGIN_ROOT/data/routing_validation.json"
