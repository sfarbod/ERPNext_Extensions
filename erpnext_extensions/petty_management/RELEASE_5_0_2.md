# Release 5.0.2 — PM Workflow Approver Validation

## Summary

Architectural hardening only. Workflow approvers are validated **before** a PM Request or PM Clearance enters the approval workflow, and acting approvers are re-checked before Approve / Return actions.

This prevents runtime `PermissionError` when a manager or finance user receives a ToDo / Workflow Action but lacks the roles required to read the document and execute workflow transitions.

## What changed

- New shared service: `workflow_approver_validation_service`
  - `validate_workflow_approvers()` — validates every stamped approver on submit-for-approval
  - `validate_acting_approver_can_read()` — blocks Approve / Return when roles or company access changed after submit
  - Required workflow roles inferred **only** from pending approval transitions (not terminal Reject / Approved states)
  - Verifies user exists, is enabled, holds required roles, and has effective document **read** permission
- PM Clearance finance queue validation at submit
  - Configured `Petty Management Clearance Reviewer` role must exist
  - At least one **enabled** User must hold that role
- Return for Correction ordering fix
  - Requester assignment now runs **before** approver stamps are cleared
  - Preserves acting approver read context for `assign_to.add()` without `ignore_permissions`
  - Return remains atomic — assignment failure rolls back the full transaction
- Integrated into `approver_stamp_service` at all stamp / re-stamp entry points for PM Request and PM Clearance

## What did **not** change

- Frappe permission model (no broadening, no `ignore_permissions`, no sharing)
- Workflow transitions or conditions
- Return for Correction business scope — **pre-final-approval only**
  - `Finance Approved` (PM Request) and `Approved` (PM Clearance) remain without Return for Correction
  - This is intentional lifecycle behaviour, not a permission bug
- Assignment Rules or ToDo creation timing (validation runs **before** workflow entry so invalid assignments are never reached)

## Operator action

If submit fails with **Workflow approver cannot execute**, assign the listed role(s) to the named user (typically `Petty Management User` for manager/CEO approvers and `Petty Management Accountant` for finance approvers on PM Request).

If PM Clearance submit fails with **Finance reviewer required**, assign `Petty Management Clearance Reviewer` to at least one enabled user.

## Tests

- `test_workflow_approver_validation_v502`
- `test_pm_return_atomicity_v472` (regression)
- `test_approver_stamp_service`
- `test_approver_stamp_clearance_v453`

## Version

`erpnext_extensions.__version__` = **5.0.2**
