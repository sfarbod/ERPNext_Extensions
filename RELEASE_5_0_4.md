# Release 5.0.4 — PM Workflow Repeated Return / Resubmit

## Summary

- **Bug:** After a PM Request or PM Clearance was returned once, a second Return for Correction on the same document (after requester resubmit) failed with `ValidationError: Return for Correction timeline already recorded for {name}.`
- **Root cause (Class E — return service side effect):** `add_return_timeline_comment()` in `return_for_correction_service.py` incorrectly treated any existing return timeline comment plus `workflow_state == Draft` as a duplicate Return, blocking legitimate repeated approval cycles.
- **Fix:** Remove the erroneous timeline idempotency guard. Concurrent / duplicate Return on the same transition remains protected by `lock_pm_document_for_return()` and `assert_return_allowed_under_lock()`.

## Business rule (unchanged)

Return for Correction must work repeatedly on the same document:

`Draft → Pending → Return → Draft → Pending → Return → …`

## Scope

- PM Request and PM Clearance Return for Correction only.
- No permission broadening, no `ignore_permissions`, no workflow redesign.

## Files

- `petty_management/services/return_for_correction_service.py` — remove bad timeline guard
- `petty_management/tests/test_pm_repeated_return_v504.py` — unit/integration coverage
- `petty_management/e2e/pm_workflow_v504_prep.py` — Playwright prep
- `petty_management/e2e/playwright_pm_repeated_return_v504.mjs` — browser E2E
- `erpnext_extensions/__init__.py` — version `5.0.4`

## Compatibility

- ERPNext 16.x / Frappe 16.x
- Builds on v5.0.2 approver read validation and v5.0.3 unrelated consignment fixes
