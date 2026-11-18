# erpnext_extensions 5.7.57

## Iran Accounting — Subcontracting Receipt Clearing Account Fix

### Fixed

- Subcontracting Receipt clearing account imbalance caused by IRR integer
  valuation rounding (Core `divisional_loss` double-counting Δ on shared
  clearing when SLE `stock_value_difference` = document `amount` + Δ).
- Incorrect residual movements in account
  `111421 - واسط کالای امانی (برونسپاری)`.
- Zero-net clearing GL rows are now removed using the established Iran
  Accounting mechanism (`drop_net_zero_merged_gl_rows`).

### Technical

- New domain normalizer:
  `iran_accounting/domain/subcontracting_receipt_gl_balance.py`
- Wired into IRR `StockController.make_gl_entries` after Core
  `get_gl_entries` / before residual · SABE · aggregate
  (`iran_accounting/integration/monkey_patches.py`).
- Reuses Manufacture / Repack helper `drop_net_zero_merged_gl_rows`.
- Aligns clearing to document RM / (`amount − service`), then drops the
  balanced clearing account (no persisted row when net movement is zero).
- Rewrites Stock Adjustment to residual Σ(`amount − SVD`):
  credit Δ when SVD > amount; debit \|Δ\| when SVD < amount.
- Fixed-total subcontracting service cost allocation unchanged.
- Idempotent under reverse / repost (`doc.flags.irr_scr_clearing_balanced`).

### Verification

- 12 regression tests in
  `iran_accounting/tests/test_scr_clearing_balance_v5757.py` (positive /
  negative rounding, zero-net, equal nonzero debit/credit, fractional
  service, repeated repost, reversal reconstruction, voucher balance).
- DEV reference `MAT-SCR-2026-00112` (qty 1,962; service 1,230,000 IRR):
  - Account 111421: **no** persisted active GL rows
  - 621301 Stock Adjustment: credit **174**
  - 611250 service: credit **1,230,000**
  - Total GL debit = total GL credit = **1,239,193,314**
  - SLE and document submitted state unchanged

Do not claim broader site-wide or production validation beyond the above.
