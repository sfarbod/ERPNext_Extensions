# Release 5.0.6 — Pending Approval Remark-Only Save

## Summary

- **Bug:** `assert_pending_not_editable()` blocked every Desk save while PM Request / PM Clearance were in a Pending approval state, including remark-only updates.
- **Fix:** Refactored the pending-edit guard to use an explicit allow-list (`remark` only). PM Request and PM Clearance now accept saves that change only Remarks while Pending; all other parent fields and child tables remain blocked.
- **Client:** Shared Desk helper locks every field except Remarks while Pending (form stays read-only except remark + Save).

## Pending states

**PM Request:** Pending Manager Approval, Pending CEO Approval, Pending Finance Approval

**PM Clearance:** Pending Manager Approval, Pending Finance Review

## v5.0.6 hardening (release blockers)

- **No-op save:** Pending documents with zero business-field changes now save successfully (no misleading rejection).
- **Clearance Desk drift:** `can_mutate_derived_fields()` returns false while Pending remark-only mode, so `refresh_holder_pending` / allocation refresh no longer dirties derived fields when the user edits only Remarks.

## Scope

- Remark-only save during Pending approval only.
- No permission broadening, no `ignore_permissions`, no workflow redesign.

## Files

- `petty_management/services/draft_approval_guards.py` — allow-list refactor
- `petty_management/services/clearance_service.py` — guard before row normalization
- `public/js/pm_desk_workflow_actions.js` — client remark-only lock
- `petty_management/doctype/pm_request/pm_request.js` — apply lock on refresh
- `petty_management/doctype/pm_clearance/pm_clearance.js` — apply lock on workflow_state
- `petty_management/tests/test_pm_pending_remark_edit_v506.py`
- `petty_management/e2e/pm_pending_remark_v506_prep.py`
- `petty_management/e2e/playwright_pm_pending_remark_v506.mjs`

## Compatibility

- ERPNext 16.x / Frappe 16.x
- Builds on v5.0.5

## Test coverage

- **Unit:** `test_pm_pending_remark_edit_v506` — Request paths fully covered; Clearance paths skip when Purchase Invoice fixture is unavailable on the site.
- **E2E:** `playwright_pm_pending_remark_v506.mjs` — PM Request (noop save, remark save, illegal edit blocked); PM Clearance skipped on dev site when PI insert fails (mandatory `remarks` on PI fixture).
