#!/usr/bin/env bash
# Run the dispatch economics pytest slice (DISPATCH-7).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

exec python3 -m pytest \
  tests/e2e/test_round_trip_budget.py \
  tests/e2e/test_dispatch.py \
  tests/e2e/test_routing_gate.py \
  -k dispatch \
  "$@"
