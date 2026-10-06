#!/usr/bin/env bash
# ≤ 60 seconds. Lint + unit + smoke. Exit non-zero on any failure. Print only summaries.
set -uo pipefail
cd "$(dirname "$0")/.."
# Under a swarm runner: wait for one of this machine's verify slots, so N verifies at most run at once and timing
# tests are not starved (swarm-control hostlock; the harness's own verifies already hold one and set the HELD flag).
if [ -n "${SWARM_VERIFY_LOCK:-}" ] && [ -z "${SWARM_VERIFY_SLOT_HELD:-}" ] && [ -x "${SWARM_VERIFY_LOCK}" ]; then
  exec "${SWARM_VERIFY_LOCK}" -- bash "$0" "$@"
fi
status=0
failed=""
step() {   # step <name> <command…>: run one step, remember its verdict for the last line
  local name=$1; shift
  if "$@"; then :; else status=1; failed="$failed $name"; echo "$name: FAIL"; fi
}
if [ -f frontend/package.json ]; then
  step tsc bash -c 'cd frontend && npx --no-install tsc --noEmit -p . 2>&1 | tail -20; exit ${PIPESTATUS[0]}'
fi
if [ -f backend/pyproject.toml ] || [ -d backend/tests ]; then
  step backend-pytest bash -c 'cd backend && python -m pytest -q -x --no-header 2>&1 | tail -20; exit ${PIPESTATUS[0]}'
fi
if [ -d ml/tests ]; then
  step ml-pytest bash -c 'cd ml && python -m pytest -q -x --no-header -m "not slow" 2>&1 | tail -20; exit ${PIPESTATUS[0]}'
fi
# The verdict is the LAST line, so `verify_fast.sh | tail` never shows a green pytest tail over a failed lint
# step (Q-179).
if [ $status -eq 0 ]; then echo "verify_fast: OK"; else echo "verify_fast: FAIL —${failed}"; fi
exit $status
