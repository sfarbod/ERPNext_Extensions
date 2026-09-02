# Release 5.0.8 — PM Clearance Pending Remark Save Fix

## Summary

- **Bug:** PM Clearance remark-only save failed while Pending with *"Only Remarks may be edited while approval is pending."*
- **Root cause:** Desk posts derived/read-only field drift on save. The guard compared all DB columns and treated drift as illegal user edits.
- **Fix:** Compare only **user-editable** fields (`read_only=0` per DocType meta). Derived/read-only fields (balances, totals, allocation snapshots, `percent_of_total`, etc.) are excluded from comparison automatically — no growing ignore list.

## Rule (unchanged)

While Pending approval: only `remark` may change among user-editable parent fields; no user-editable child field may change; no-op save allowed.

## Scope

- Remark-only and no-op saves while Pending approval only.
- No permission broadening, no `ignore_permissions`, no workflow redesign, no new APIs.

## Files

- `petty_management/services/draft_approval_guards.py` — meta-based editable-field comparison
- `petty_management/tests/test_pm_pending_remark_clearance_drift_v508.py`
- `petty_management/tests/reproduce_clearance_remark_v508.py`

## Test coverage

- `test_pm_pending_remark_clearance_drift_v508` — Desk drift on read-only fields passes; editable field edits blocked.
- `test_pm_pending_remark_edit_v506` — remark/no-op/illegal-edit/return-resubmit (Clearance skips when PI fixture unavailable).

## Compatibility

- ERPNext 16.x / Frappe 16.x
- Builds on v5.0.7
