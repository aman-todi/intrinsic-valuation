#!/usr/bin/env bash
# Per-package coverage gates (SPEC §15 Ticket 14): the modules where a silent bug is most costly must
# stay at >= 90% line coverage. Run after `pytest --cov=app` has written .coverage:
#
#   cd backend && .venv/bin/pytest --cov=app && scripts/check_coverage.sh
#
# The overall floor is enforced by pytest itself (--cov-fail-under in CI).
set -euo pipefail
cd "$(dirname "$0")/.."
COVERAGE="${COVERAGE:-.venv/bin/coverage}"
FLOOR="${CRITICAL_COVERAGE_FLOOR:-90}"

status=0
for include in "app/valuation/*" "app/classify/*" "app/assumptions/bounds.py"; do
  echo "== ${include} (floor ${FLOOR}%)"
  if ! "$COVERAGE" report --include="$include" --show-missing --fail-under="$FLOOR"; then
    echo "FAIL: ${include} is below ${FLOOR}% line coverage" >&2
    status=1
  fi
done
exit "$status"
