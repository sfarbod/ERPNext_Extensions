# Release 4.8.3 — PM Clearance Workflow & Remarks Enhancement

## Summary

- **Return for Correction** on PM Clearance: workflow sync patch ensures Return transitions exist from **Pending Manager Approval** and **Pending Finance Review** (workflow-only; no document mutation).
- **Remarks** (`remark` field): editable while `docstatus=0` (including Pending*); read-only after Finance Approve (`docstatus=1`); visible/searchable/filterable in list view.
- Stricter `is_draft_approval_workflow_applied()` checks Return from **each** Pending* state (not merely anywhere in the workflow).

## v4.7.2 invariants preserved

Pending* = docstatus 0; Finance Approve = only submit; Return atomic/idempotent; no Cancel/Amend/duplicate on Return.

## Patch

`migrate_pm_clearance_return_remarks_v483` — idempotent PM Clearance workflow rebuild **only after** authoritative v4.7.2 cutover completion (Patch Log + `pm_draft_approval_v472_applied`). Aborts if v4.7.2 was deferred or not applied.
