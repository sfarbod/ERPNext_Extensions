#!/usr/bin/env bash
# Run Petty Management automated regression (unit + smoke). Playwright run separately.
set -euo pipefail
BENCH="${FRAPPE_BENCH_ROOT:-/workspace/development/frappe-bench}"
SITE="${FRAPPE_SITE:-development.localhost}"
OUT="${1:-/tmp/pm_regression_results.txt}"
LOCK="${PM_REGRESSION_LOCK:-/tmp/pm_regression.lock}"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "Another PM regression is running (lock: $LOCK). Run serially." >&2
  exit 2
fi
cd "$BENCH"

MODULES=(
  erpnext_extensions.petty_management.tests.test_pm_allocation_helpers
  erpnext_extensions.petty_management.tests.test_pm_accounting_parties
  erpnext_extensions.petty_management.tests.test_pm_accounting_remarks
  erpnext_extensions.petty_management.tests.test_approver_stamp_service
  erpnext_extensions.petty_management.tests.test_pm_assignment_rules
  erpnext_extensions.petty_management.tests.test_pm_business_status
  erpnext_extensions.petty_management.tests.test_pm_clearance
  erpnext_extensions.petty_management.tests.test_pm_clearance_naming
  erpnext_extensions.petty_management.tests.test_pm_clearance_settle_availability
  erpnext_extensions.petty_management.tests.test_pm_clearance_settlement_query
  erpnext_extensions.petty_management.tests.test_pm_clearance_smoke
  erpnext_extensions.petty_management.tests.test_pm_draft_pe_delete
  erpnext_extensions.petty_management.tests.test_pm_funding_history_report
  erpnext_extensions.petty_management.tests.test_pm_holder_ux
  erpnext_extensions.petty_management.tests.test_pm_multi_approval_integration
  erpnext_extensions.petty_management.tests.test_pm_auto_skip_approvals
  erpnext_extensions.petty_management.tests.test_pm_roles_autoskip_migration_v414
  erpnext_extensions.petty_management.tests.test_pm_clearance_draft_pi
  erpnext_extensions.petty_management.tests.test_pm_narration
  erpnext_extensions.petty_management.tests.test_pm_opening_advance
  erpnext_extensions.petty_management.tests.test_pm_opening_advance_over_allocation
  erpnext_extensions.petty_management.tests.test_pm_opening_allocation_validation
  erpnext_extensions.petty_management.tests.test_opening_advance_ledger
  erpnext_extensions.petty_management.tests.test_pm_production_hardening
  erpnext_extensions.petty_management.tests.test_pm_request_action_flags_scenarios
  erpnext_extensions.petty_management.tests.test_pm_request_action_flags_uat
  erpnext_extensions.petty_management.tests.test_pm_request_action_visibility
  erpnext_extensions.petty_management.tests.test_pm_request_allocation_security
  erpnext_extensions.petty_management.tests.test_pm_request_api_static_scan
  erpnext_extensions.petty_management.tests.test_pm_request_list_permission
  erpnext_extensions.petty_management.tests.test_pm_clearance_list_permission
  erpnext_extensions.petty_management.tests.test_pm_visibility_roles
  erpnext_extensions.petty_management.tests.test_pm_visibility_role_setting
  erpnext_extensions.petty_management.tests.test_pm_request_funding_status_ux
  erpnext_extensions.petty_management.tests.test_pm_request_multi_pe
  erpnext_extensions.petty_management.tests.test_pm_request_multi_pe_integration
  erpnext_extensions.petty_management.tests.test_pm_request_cancel_delete
  erpnext_extensions.petty_management.tests.test_pm_request_cancel_action_v485
  erpnext_extensions.petty_management.tests.test_pm_request_payment_entries_security
  erpnext_extensions.petty_management.tests.test_pm_request_ui_messages
  erpnext_extensions.petty_management.tests.test_pm_request_workflow
)

{
  echo "=== PM Unit/Integration modules $(date -Iseconds) ==="
  FAIL=0
  for m in "${MODULES[@]}"; do
    echo "--- MODULE $m ---"
    if bench --site "$SITE" run-tests --module "$m" --lightmode --skip-before-tests 2>&1; then
      echo "RESULT $m OK"
    else
      echo "RESULT $m FAIL"
      FAIL=1
    fi
  done

  echo "=== PM Smoke (bench execute) ==="
  SMOKE=(
    "erpnext_extensions.petty_management.smoke.pm_lifecycle_e2e.execute"
    "erpnext_extensions.petty_management.smoke.final_acceptance_opening_clearance.execute"
    "erpnext_extensions.petty_management.smoke.opening_advance_clearance_smoke.execute"
    "erpnext_extensions.petty_management.smoke.pm_multi_pe_e2e.execute"
    "erpnext_extensions.petty_management.smoke.pm_request_pe_list_api_smoke.execute"
    "erpnext_extensions.petty_management.smoke.desk_clearance_save_smoke.execute"
  )
  for s in "${SMOKE[@]}"; do
    echo "--- SMOKE $s ---"
    out="$(bench --site "$SITE" execute "$s" 2>&1)" || true
    echo "$out"
    # Prefer structured status when present (bench execute may exit 0 on logical failure).
    if echo "$out" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"(FAILED|failed)"' \
      || echo "$out" | grep -Eq '"pass"[[:space:]]*:[[:space:]]*false' \
      || echo "$out" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*false'; then
      echo "RESULT $s FAIL"
      FAIL=1
    elif echo "$out" | grep -Eq 'Traceback \(most recent call last\)'; then
      echo "RESULT $s FAIL"
      FAIL=1
    else
      echo "RESULT $s OK"
    fi
  done

  echo "=== SUMMARY exit=$FAIL ==="
  exit "$FAIL"
} | tee "$OUT"
