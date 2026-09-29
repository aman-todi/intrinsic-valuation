#!/usr/bin/env bash
# Per-package coverage gate: the valuation engine (pure math, where a silent bug is most costly) must
# stay at >= 85% line coverage. Other packages are covered by the overall floor only. Run after
# `pytest --cov=app` has written .coverage:
#
#   cd backend && .venv/bin/pytest --cov=app && scripts/check_coverage.sh
#
# The overall floor (75%) is enforced by pytest itself (--cov-fail-under in CI).
set -euo pipefail
cd "$(dirname "$0")/.."
COVERAGE="${COVERAGE:-.venv/bin/coverage}"
FLOOR="${VALUATION_COVERAGE_FLOOR:-85}"

echo "== app/valuation/* (floor ${FLOOR}%)"
if ! "$COVERAGE" report --include="app/valuation/*" --show-missing --fail-under="$FLOOR"; then
  echo "FAIL: app/valuation is below ${FLOOR}% line coverage" >&2
  exit 1
fi
