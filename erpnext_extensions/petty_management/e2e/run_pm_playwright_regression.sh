#!/usr/bin/env bash
# Run all Petty Management Playwright E2E scripts; capture exit codes and log paths.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
E2E_DIR="$ROOT/erpnext_extensions/petty_management/e2e"
OUT="${1:-/tmp/pm_playwright_regression.txt}"
LOCK="${PM_REGRESSION_LOCK:-/tmp/pm_regression.lock}"
MIN_ULIMIT="${PM_PLAYWRIGHT_MIN_ULIMIT:-4096}"
SCRIPT_ULIMIT="${PM_PLAYWRIGHT_ULIMIT:-8192}"
PREFLIGHT_PROBE="$E2E_DIR/playwright_pm_request_form_smoke.mjs"

exec 8>"$LOCK"
if ! flock -n 8; then
  echo "PM bench regression lock held ($LOCK). Run Playwright after unit/smoke finishes." >&2
  exit 2
fi

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/home/frappe/.cache/ms-playwright}"
export FRAPPE_E2E_BASE_URL="${FRAPPE_E2E_BASE_URL:-http://development.localhost:8000}"
export FRAPPE_E2E_SITE="${FRAPPE_E2E_SITE:-development.localhost}"
export FRAPPE_BENCH_ROOT="${FRAPPE_BENCH_ROOT:-/workspace/development/frappe-bench}"

check_ulimit() {
  local current
  current="$(ulimit -n 2>/dev/null || echo 0)"
  if [[ "$current" != "unlimited" && "$current" -lt "$MIN_ULIMIT" ]]; then
    echo "WARNING: ulimit -n is ${current} (recommended >= ${MIN_ULIMIT})." >&2
  fi
}

check_emfile_preflight() {
  if ! python3 -c "open('${PREFLIGHT_PROBE}').close()" 2>/dev/null; then
    echo "INFRASTRUCTURE ERROR: EMFILE — cannot open Playwright scripts under ${E2E_DIR}." >&2
    echo "Restart the dev environment or raise ulimit before running the Playwright pack." >&2
    exit 3
  fi
}

run_playwright_script() {
  local script="$1"
  local rc=0
  (
    cd "$E2E_DIR"
    ulimit -n "$SCRIPT_ULIMIT" 2>/dev/null || true
    node "$script"
  ) || rc=$?
  # Allow the kernel to reclaim browser descriptors before the next script.
  sleep 1
  return "$rc"
}

SCRIPTS=(
  playwright_pm_request_form_smoke.mjs
  playwright_pm_request_pe_list_e2e.mjs
  playwright_pm_pe_desk_cancel.mjs
  playwright_pm_request_cancel_delete.mjs
  playwright_pm_request_cancel_action_v485.mjs
  playwright_pm_request_delete_action_v486.mjs
  playwright_pm_request_finalize_v486.mjs
  playwright_pm_request_actions_visibility.mjs
  playwright_pm_multi_pe.mjs
  playwright_pm_clearance_search_link_network_debug.mjs
  playwright_pm_clearance_settlement_lines_e2e.mjs
  playwright_pm_clearance_multi_approval.mjs
  playwright_pm_clearance_draft_pi_e2e.mjs
  playwright_pm_clearance_finance_role_queue.mjs
  playwright_pm_clearance_return_remarks_v483.mjs
  playwright_pm_request_multi_approval.mjs
  playwright_pm_request_list_permission.mjs
  playwright_pm_clearance_list_permission.mjs
  playwright_pm_visibility_role_setting.mjs
  playwright_pm_request_funding_status_ux.mjs
)

{
  echo "=== PM Playwright regression $(date -Iseconds) ==="
  check_ulimit
  check_emfile_preflight
  FAIL=0
  for s in "${SCRIPTS[@]}"; do
    echo "--- $s ---"
    if run_playwright_script "$s" 2>&1; then
      echo "RESULT $s OK"
    else
      rc=$?
      if [[ "$rc" -eq 3 ]]; then
        echo "RESULT $s FAIL (infrastructure EMFILE)"
      else
        echo "RESULT $s FAIL"
      fi
      FAIL=1
    fi
  done
  echo "=== PLAYWRIGHT SUMMARY exit=$FAIL ==="
  exit "$FAIL"
} | tee "$OUT"
