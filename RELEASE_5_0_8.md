# Release 5.0.8 — PM Clearance Pending Remark Save Fix

## Summary

- **Bug:** PM Clearance remark-only save still failed while Pending with *"Only Remarks may be edited while approval is pending."* even though v5.0.6 allowed remark-only saves for PM Request.
- **Root cause:** Desk sends derived parent and child field drift on save (holder balances, totals, allocation snapshots, `amount_plus_tax`, etc.). The pending-edit guard treated that drift as illegal user edits. PM Request only ignored `percent_of_total`; PM Clearance has many more derived fields.
- **Fix:** Extend pending-save derived-field ignore lists for PM Clearance (and request total snapshots) so only real business-field changes are blocked.

## Scope

- Remark-only and no-op saves while Pending approval only.
- No permission broadening, no `ignore_permissions`, no workflow redesign, no new APIs.

## Files

- `petty_management/services/draft_approval_guards.py` — derived parent/child ignore fields
- `petty_management/tests/test_pm_pending_remark_clearance_drift_v508.py`
- `petty_management/tests/reproduce_clearance_remark_v508.py` — diagnostic reproducer

## Test coverage

- **Unit:** `test_pm_pending_remark_clearance_drift_v508` — simulated Desk derived-field drift on remark save; illegal `allocated_amount` edit still blocked.
- **Unit:** `test_pm_pending_remark_edit_v506` — existing remark/no-op/illegal-edit coverage (Clearance cases skip when PI fixture unavailable).
- **Diagnostic:** `reproduce_clearance_remark_v508.diagnose_client_child_drift` — verified on production-like pending clearance.

## Compatibility

- ERPNext 16.x / Frappe 16.x
- Builds on v5.0.7
