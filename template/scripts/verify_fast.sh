#!/usr/bin/env bash
# ≤ 60 seconds. Lint + unit + smoke. Exit non-zero on any failure. Print only summaries.
set -uo pipefail
cd "$(dirname "$0")/.."
status=0
if [ -f frontend/package.json ]; then
  (cd frontend && npx --no-install tsc --noEmit -p . 2>&1 | tail -20) || status=1
fi
if [ -f backend/pyproject.toml ] || [ -d backend/tests ]; then
  (cd backend && python -m pytest -q -x --no-header 2>&1 | tail -20) || status=1
fi
if [ -d ml/tests ]; then
  (cd ml && python -m pytest -q -x --no-header -m "not slow" 2>&1 | tail -20) || status=1
fi
exit $status
