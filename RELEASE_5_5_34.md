# erpnext_extensions 5.5.34

## Manufacturing Dimension Uniformity — before_submit only (Option B)

Draft Save of Manufacture / Material Transfer for Manufacture Stock Entries
must no longer be blocked by Manufacturing Dimension Uniformity.

The guard still runs authoritatively in `before_submit`, so mismatched
Department or Cost Center values cannot be submitted and cannot post
SLE / GL.

### Changed

- Removed `validate_manufacturing_dimension_uniformity()` from
  `validate_stock_entry()` so first Make Stock Entry Save can persist
  a Draft even when Cost Centers temporarily differ (for example
  packaging `1130` vs selling `2100` on scrap).
- Kept the existing call in `before_submit_stock_entry()`.
- Nested `validate_stock_entry()` from `before_submit` no longer
  re-invokes the dimension guard (single authoritative check).

### Preserved

- Guard rules (uniform Department + Cost Center for MTfM / Manufacture).
- Custom 8 blank-fill of Department / Cost Center from MTfM.
- I2 valuation integrity, rate, residual, zero-rate, posting-order,
  and all other Iran Accounting validate / before_submit checks.
- Non-guard purposes (Material Issue, Receipt, Transfer, Repack)
  remain outside this guard.

### Deployment Notes

- **Upgrade:** install / update `erpnext_extensions` to **5.5.34**.
- **`bench migrate`:** not required.
- **Custom 8:** unchanged. Operators may still need to correct a
  scrap selling Cost Center on Draft before Submit.
- **Production:** do not deploy until review. Development-only
  verification for this release.
