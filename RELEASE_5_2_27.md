# Release 5.2.27 — IRR GL precision alignment before vanilla debit/credit validate

## Problem

RIV `l7fhvbp01r` (item `13100134`) completed SLE recalculation, then failed GL repost on
`MAT-STE-2026-34391` with:

`Debit and Credit not equal … Difference is 1.0`

### Exact arithmetic

SLE stock_value_difference (post-RIV):

| Item | SVD |
|------|-----|
| outgoing RMs (mostly integer) + `30100177` | sum = −50,671,372.03 |
| FG `30100087` | +50,671,372.51 |

Built at precision 2 (fractional map):

- WIP Cr `50,671,372.03`
- Stock Adj Cr `0.48`
- Inventory Dr `50,671,372.51`

Vanilla `process_debit_credit_difference` with IRR Currency precision **0**:

- WIP Cr → `50,671,372`
- Stock Adj Cr → `0` (0.48 rounds away)
- Inventory Dr → `50,671,373`
- **Diff = 1.0**

Vanilla Stock Entry allowance is hardcoded **0.5**, so Diff 1.0 throws and never reaches
`make_round_off_gle`.

## Fix

`align_irr_gl_map_to_currency_precision` (called from Iran `StockController.make_gl_entries`):

1. Quantize GL money fields to IRR precision (0).
2. If the remaining net is exactly one IRR quantum, absorb it into Company
   `stock_adjustment_account` (standard Manufacture valuation residual account).
3. Larger gaps remain fail-closed for vanilla validation.

Does **not** widen tolerance globally. Does **not** disable Iran guards.

## Files

- `iran_accounting/domain/irr_gl_precision_align.py` (new)
- `iran_accounting/integration/monkey_patches.py`
- `iran_accounting/tests/test_irr_gl_precision_align.py`

## Note on data repair

Failed RIV can leave corrected SLE with stale GL (ERPNext commits SLE mid-flight;
GL chunk rolls back on failure). Downstream `13100134` vouchers still need a
controlled accounting/repost pass — see investigation report. This release only
unblocks the 1-IRR validate failure.
