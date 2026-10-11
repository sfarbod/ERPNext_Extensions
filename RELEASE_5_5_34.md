# erpnext_extensions v5.5.34

**Release Type:** Bug Fix / Manufacturing Validation UX  
**Release Date:** 2026-10-11

---

## 1. Summary

Move Manufacturing Dimension Uniformity enforcement from Stock Entry
`validate` to `before_submit` only (Option B).

Manufacture and Material Transfer for Manufacture Draft Stock Entries can
be created and edited when Department or Cost Center values temporarily
differ. Submit remains blocked until all manufacturing rows share one
Department and one Cost Center. No SLE or GL is posted when Submit fails.

## 2. Problem Description

Manufacturing Dimension Uniformity previously ran during Stock Entry
`validate`. For Job Card Make Stock Entry (reference `PO-JOB08228`), a
Cost Center mismatch (for example packaging `1130` on finished goods vs
selling `2100` on scrap) blocked the first Draft Save. Users could not
persist the document to correct dimensions before Submit.

## 3. Changes

- Removed `validate_manufacturing_dimension_uniformity()` from
  `validate_stock_entry()`.
- Retained the authoritative call in `before_submit_stock_entry()`.
- Nested `validate_stock_entry()` invoked from `before_submit` no longer
  re-runs the dimension guard (single submit-time check).
- Added regression tests in
  `manufacturing_dimension/tests/test_dimension_guard_timing.py`.
- Version bumped to **5.5.34**.

**Unchanged:** guard rules, Custom 8, Item Defaults, buying/selling Cost
Centers, I2 valuation integrity, rate/residual/zero-rate/RIV/posting-order
logic, ERPNext Core.

## 4. Behavior Before

- Manufacturing Dimension Uniformity ran during Stock Entry `validate`.
- A mismatch between manufacturing Cost Centers prevented the first
  Draft Save after Make Stock Entry.
- Users could not open a Draft to correct Department / Cost Center.

## 5. Behavior After

- Manufacturing Dimension Uniformity runs during `before_submit` only.
- Draft Stock Entries can be created and edited even when manufacturing
  dimensions are inconsistent.
- Submit remains blocked until manufacturing dimensions are valid.
- After correction, the dimension guard passes; other validators still
  apply on Submit.

## 6. Accounting Safety

- No Stock Ledger Entries or GL Entries are posted when Submit fails.
- Draft Save does not create SLE or GL.
- Mismatched Manufacture / MTfM documents cannot be submitted while the
  guard is active in `before_submit`.
- API / ORM Submit follows the same `before_submit` path.

## 7. Custom 8 Compatibility

Custom 8 (`Custom 8 - Manufacture Header From Job Card`) is **unchanged**.

- Still runs on Before Validate.
- Still fills blank Department / Cost Center from MTfM.
- Still preserves non-empty dimensions and `fg_completed_qty` logic.
- Manual Cost Center corrections on non-blank rows are not overwritten
  by Custom 8.

## 8. Test Results

| Scenario | Result |
|---|---|
| Option B timing unit tests (6) | PASS |
| Manufacturing Dimension Guard G1–G8 (8) | PASS |
| Manufacturing Dimension Repair tests (9) | PASS |
| Manufacture rounding regression (10) | PASS |
| Draft Save with dimension mismatch (`PO-JOB08228`) | PASS |
| Submit blocked with dimension mismatch | PASS |
| Dimension guard passes after Scrap CC correction | PASS |
| Custom 8 unchanged | PASS |
| No SLE/GL on rejected Submit | PASS |

Full Submit of `PO-JOB08228` after dimension correction was **not**
conclusively successful due to a separate expense-account Server Script
check (`621301`). That failure is outside this release.

## 9. Known Limitations

- Option B does **not** automatically correct manufacturing Cost Centers.
- Item buying/selling Cost Center defaults remain unchanged.
- Custom 8 remains unchanged.
- Existing expense-account Server Script issue involving account
  `621301` is **not** resolved by this release.
- Full Submit of `PO-JOB08228` was not conclusively successful due to
  that separate issue.
- A second Draft Save may trigger `cost_center` `fetch_from`
  (`department.payroll_cost_center`, `fetch_if_empty=0`), which can
  overwrite a non-payroll Cost Center when Department is set.

## 10. Upgrade / Deployment Notes

- **Upgrade:** install / update `erpnext_extensions` to **5.5.34**.
- **`bench migrate`:** not required. No DocType or patch is added.
- **Custom 8 / Item Defaults:** no configuration changes required.
- **Production:** review before deploy. Operators may still need to
  correct scrap selling Cost Centers on Draft before Submit when Core
  defaults and Custom 8 leave a mismatch.
