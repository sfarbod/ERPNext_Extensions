# erpnext_extensions 5.6.29

## Source-linked partial return integer valuation (MAT-STE-2026-43017)

### Summary
For source-linked Material Receipt returns (`custom_is_returned` +
`custom_reference_no`), keep the **source outgoing valuation rate** on the return
row and keep the inventory amount as `ROUND_HALF_UP(qty × source_rate)`.

Fractional return qty can make `ROUND_HALF_UP(amount / qty)` diverge from the
source rate without creating a Class A monetary residual. The ledger composition
contract gets a **narrow exception** only when that residual is exactly zero.

**No Stock Adjustment** is posted for the zero-residual case. Ordinary receipts
and PR/PI/LCV paths are unchanged.

**Development only in this change set. No push / publish / production deploy.**

### Root cause
`align_stock_entry_item_amounts` derived `valuation_rate = ROUND(amount / qty)`.
For `0.19 × 121055292 → amount 23000505 → derived VR 121055289`, while the
source outgoing rate is `121055292`. `assert_source_linked_return_valuation`
rejected the mismatch. Class A residual
`amount − ROUND(qty × source_rate)` is **0**; the 3 IRR/unit gap is
representational only. Sub-IRR fragment `0.48` is not a ledger quantum.

### Fix
1. `qty_rate_amount.align_stock_entry_item_amounts` — source-linked incoming
   return rows set `valuation_rate = basic_rate` (source rate).
2. `source_linked_return_keeps_rate_authoritative` — gate requiring Class A = 0.
3. `stock_entry_ledger_contract._assert_row_composition` — skip
   `VR == ROUND(amount/qty)` only when that gate is true.

### Tests
- `test_source_linked_return_fractional_qty.py`
- `test_prevention_v5544.py` (partial 0.19 + ordinary receipt control)
