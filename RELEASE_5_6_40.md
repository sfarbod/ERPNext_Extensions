# erpnext_extensions 5.6.40

## Critical Fix — One-Sided Stock Voucher GL Rows

### Defect

After v5.6.39 Stock Adjustment aggregation, Core `merge_similar_entries` could
present (or persist) a WIP/stock account row with **both** Debit and Credit
non-zero when an A2 counter-entry shared dimensions with a Core credit leg.

### Fix

1. **Same-side SA aggregation only** — debit aggregates with debit, credit with
   credit. Opposite sides on the same account stay separate (no netting).
2. **`ensure_one_sided_gl_entries`** — splits any mixed-side map row into two
   one-sided rows (company / account / transaction currency).
3. Hooked after Iran SA aggregation and after Core `merge_similar_entries` for
   IRR companies.
4. A2 WIP/stock partner legs set `_skip_merge=1` so they never fold into Core
   WIP credit.

### Invariants

- `NOT (debit > 0 AND credit > 0)` (and mirrors)
- `SUM(debit) == SUM(credit)`
- `621301` Core 461 + A2 1970 → one Credit 2,431 when dimensions match
- A2 WIP Debit 1,970 remains a separate debit-only row

### Tests

`test_stock_adjustment_gl_aggregate_v575` (same-side + mixed-split coverage)
