# Release 5.0.3 — Preserve Accounting Dimensions on Return Stock Entries

## Summary

- **Bug:** `make_consignment_return_from_receipt` and `make_material_loan_return_from_issue` rebuilt Stock Entry Detail rows with a fixed field whitelist and omitted Accounting Dimensions (for example Department), Cost Center, and Project. On sites where SED dimensions are mandatory, return insert failed (`Value missing for: Department`) before a draft was persisted.
- **Fix:** Shared helper copies non-empty Accounting Dimension values (via ERPNext `get_dimensions(with_cost_center_and_project=True)`, filtered to Stock Entry Detail meta) from each source row onto the generated return row. Existing target values are not overwritten. No dimension is hardcoded.
- **Wired into:** Consignment Return-from-Receipt and Material Loan Return-from-Issue APIs only.

## Scope (intentionally locked)

- Return Stock Entry Detail dimension propagation only.
- No change to accounting formulas, GL posting, valuation, party mapping, warehouse resolution, Journal Entry builders, cancel/repost, or reports.

## Known Limitation / Known Follow-up

Material Loan Settlement Journal Entries currently propagate **Cost Center** only.

On sites where Accounting Dimensions (for example Department) are mandatory for Profit and Loss accounts, Settlement Journal Entries containing Valuation Difference (`D ≠ 0`) may require additional Accounting Dimension propagation on the Difference line.

Recognition and normal Settlement Balance Sheet lines are unaffected.

This limitation is intentionally outside the scope of 5.0.3, whose purpose is Accounting Dimension propagation from source Stock Entry Detail rows to Return Stock Entry Detail rows.

## Future Enhancement

**Accounting Dimension propagation for Material Loan Settlement Difference Journal Entry lines.**

Do not implement in 5.0.3.

## Compatibility

- ERPNext 16.x / Frappe 16.x
- Sites with mandatory Accounting Dimensions on Stock Entry Detail and/or GL P&L accounts

## Files

- `consignment_stock/accounting.py` — dimension field discovery + copy helper
- `consignment_stock/api.py` — Consignment return-from-receipt
- `consignment_stock/material_loan/api.py` — Material Loan return-from-issue
- `consignment_stock/tests/test_return_accounting_dimensions.py`
- Related test helper updates for site constraints
- `RELEASE_5_0_3.md`
