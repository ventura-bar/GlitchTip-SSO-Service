#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# run.sh — Orchestrate all k6 load tests and save results.
#
# Prerequisites: k6 (brew install k6)
#
# Usage:
#   bash load-tests/run.sh [dsn|web|full|all]
#   PROXY_URL=http://localhost:8090 bash load-tests/run.sh all
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SUITE="${1:-all}"
RESULTS_DIR="load-tests/results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESULTS_DIR"

PROXY_URL="${PROXY_URL:-http://localhost:8090}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8180}"
PROJECT_ID="${PROJECT_ID:-1}"
DSN_KEY="${DSN_KEY:-5d17c8a90895453f87e42b14d768f10c}"

export PROXY_URL KEYCLOAK_URL PROJECT_ID DSN_KEY

echo "────────────────────────────────────────────────────────"
echo "  GlitchTip SSO Proxy — Load Test Suite"
echo "  Proxy:    $PROXY_URL"
echo "  Results:  $RESULTS_DIR"
echo "────────────────────────────────────────────────────────"
echo ""

run_test() {
  local name="$1"
  local file="$2"
  echo "▶ Running $name..."
  k6 run \
    --out "json=${RESULTS_DIR}/${name}.json" \
    --summary-export "${RESULTS_DIR}/${name}-summary.json" \
    "$file" \
    && echo "  ✓ $name PASSED" \
    || echo "  ✗ $name FAILED (check thresholds in summary)"
  echo ""
}

case "$SUITE" in
  dsn)  run_test "dsn"  load-tests/k6-dsn.js ;;
  web)  run_test "web"  load-tests/k6-web.js ;;
  full) run_test "full" load-tests/k6-full.js ;;
  all)
    run_test "dsn"  load-tests/k6-dsn.js
    run_test "web"  load-tests/k6-web.js
    run_test "full" load-tests/k6-full.js
    ;;
  *)
    echo "Usage: $0 [dsn|web|full|all]"
    exit 1
    ;;
esac

echo "────────────────────────────────────────────────────────"
echo "  Results saved to: $RESULTS_DIR"
echo "  View summary:     cat $RESULTS_DIR/*-summary.json | python3 -m json.tool"
echo "────────────────────────────────────────────────────────"
