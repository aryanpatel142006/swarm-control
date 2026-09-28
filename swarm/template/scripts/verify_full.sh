#!/usr/bin/env bash
# Minutes. Everything in verify_fast plus integration and browser checks.
set -uo pipefail
cd "$(dirname "$0")/.."
bash scripts/verify_fast.sh || exit 1
status=0
if [ -f frontend/package.json ] && [ -d frontend/e2e ]; then
  (cd frontend && npx --no-install playwright test 2>&1 | tail -30) || status=1
fi
if [ -d eval/acceptance ]; then
  python -m pytest -q eval/acceptance 2>&1 | tail -30 || status=1
fi
exit $status
