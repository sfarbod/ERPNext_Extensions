# erpnext_extensions 5.6.39

## Enhancement — Aggregate Stock Adjustment GL Entries by Account

### Objective

When Core Manufacture Stock Adjustment and Iran A2 (and compatible residuals)
post to the same `Company.stock_adjustment_account` with identical accounting
dimensions and currencies, collapse them into **one net debit or credit GL
row** before persistence — with structured source traceability.

### Example (`MAT-STE-2026-43536`)

| Before | After |
|---|---|
| Core SA Cr 461 + A2 SA Cr 1,970 (two 621301 rows) | One 621301 Cr **2,431** |
| A2 WIP Dr 1,970 (unchanged) | A2 WIP Dr 1,970 (unchanged) |

### Rules

- Aggregate only when company, voucher, account, account currency, cost center,
  project, finance book, party, and accounting dimensions match.
- Net debit/credit (and account-/transaction-currency mirrors); drop zero nets.
- Do **not** merge WIP/stock counter-entries or incompatible dimension keys.
- Prefer A2 SA dimensions from the in-map Core SA leg so aggregation can fire.
- Structured audit via `IRR_SA_AGG|{json}` remarks + `doc.flags.iran_sa_aggregate`
  + `_irr_sa_source_components` on the aggregated map row.
- Applied at final GL preparation (live `make_gl_entries` and RIV remake).

### Files

- `iran_accounting/domain/stock_adjustment_gl_aggregate.py` (new)
- `iran_accounting/domain/sabe_integer_valuation.py` (A2 SA dimension align)
- `iran_accounting/integration/monkey_patches.py` (pre-persist hook)
- `iran_accounting/manufacturing_dimension/repair.py` (preview parity)
- `iran_accounting/tests/test_stock_adjustment_gl_aggregate_v575.py`
- `iran_accounting/scripts/_fix_43536_aggregate_sa_gl.py` (Dev GL regen)

### Tests

`bench run-tests --app erpnext_extensions --module erpnext_extensions.iran_accounting.tests.test_stock_adjustment_gl_aggregate_v575`
