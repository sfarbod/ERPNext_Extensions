# erpnext_extensions 5.5.25

## Manufacture finished-good pool residual

5.5.24 is already on Production. This patch is the next local release
candidate. It does not change FIX A, FIX B, or FIX B2.

### Summary

An indivisible component-scrap finished-good pool was being rewritten
during in-submit repost. The Manufacture contract set the finished-good
amount to the pool (25333: 157725) and the integer rate to
ROUND_HALF_UP(pool / qty) (3943). A later rate-first align then replaced
basic_amount with qty × that rate (157720). The stock ledger followed
157720, and the 5 IRR difference was posted to Stock Adjustment. Submit
then restored only the document.

The amount owns that residual. It is not a real value_difference.

`apply_irr_stock_entry_contract_after_calculate` now runs rate-first
alignment before the Manufacture contract, and does not align again
after it. A later `align_stock_entry_item_amounts` keeps basic_amount
only when the row already is the single finished good of a v5.3.3+
component-scrap pool whose integer rate does not reproduce the pool.
Source rows, scrap rows, Repack, and every other Stock Entry row still
use qty × integer rate.

### Local acceptance

- Exact 25333: FG amount = SLE = 157725, rate 3943, Stock Adjustment 0, GL 175250 = 175250
- Non-divisible matrix (qty 39/41/45, rate 3504/3506, scrap 4/6) keeps document = SLE
- No-scrap rate gap still posts Stock Adjustment 10
- Repack does not take the Manufacture pool
- Second recalculate / RIV does not collapse 157725 to 157720
- Valuation integrity 33/33, including test_25333
- FIX A 11/11, FIX B/B2 15/15, rate-first 19/19, pickle-safe guard 18/18
- oua59fnjrf Completed; leftover stamp 0, skipped MANUFACTURE_FLOW; rebuild calls 0;
  MAT-STE-2026-36697 remains INVALID_PROPAGATED_ECONOMICS with no negative FG left behind

### Production

Not deployed by this release cut.
