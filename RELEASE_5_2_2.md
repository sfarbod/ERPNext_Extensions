# Release 5.2.2 — Fix false I4 zero-quantity valuation block on valid Stock Entries

## Title

Fix false I4 zero-quantity valuation block on valid Stock Entries

## Baseline

- Frappe **16.33.1**
- ERPNext **16.34.2**
- Previous erpnext_extensions **5.2.1**
- `erpnext_extensions.__version__` = **5.2.2**

## Root cause

I4 (`qty_after_transaction == 0` ⇒ `stock_value` within ±1 IRR) is a valid **final Moving Average warehouse-balance** invariant. It is not valid for every in-memory SLE that still carries the insert default `qty_after_transaction = 0`.

On Stock Entry submit:

1. SLE insert runs Iran `sync_irr_sle_from_stock_entry_row` (validate / before_insert / after_insert) **before** ERPNext `process_sle` copies running warehouse qty.
2. Insert defaults are `qty_after_transaction = 0`, `stock_value = 0`, `stock_value_difference = 0`.
3. Iran sync reconstructs `stock_value = previous + movement`. With both fields still 0, that writes `stock_value = movement` and `stock_value_difference = movement`.
4. I4 then treated that **incoming / transient** object as a persisted zero-qty leftover.

A valid full Material Transfer (including batch / SABB) can therefore raise:

```
Stock valuation integrity (I4).
qty_after_transaction is 0 but stock_value leftover exceeds ±1 IRR quantum
```

with `actual_qty > 0` and `stock_value == stock_value_difference`. That is movement value on an uncomputed running qty, not leftover warehouse value after quantity became zero.

## Fix

I4 now requires a **final processed consume-to-zero state**:

- Vanilla `update_entries_after.process_sle` must have assigned this SLE as the warehouse running state (`prev_sle_dict[(item, warehouse)] is sle`). Early negative-stock return does not.
- `prev_qty = qty_after_transaction - actual_qty` must be **positive** (positive stock consumed to zero).
- Incoming Iran-sync shape (`actual_qty > 0`, `qty_after = 0`, `stock_value == SVD`) is supporting evidence that this is not leftover value. It is **not** a global `stock_value == SVD` whitelist.

Iran sync math is unchanged. The release does **not** clamp `stock_value = 0` when `qty_after_transaction == 0`; that would hide real leftover-value corruption.

## What still fails closed

A **final processed** consume:

```
previous qty = 300
actual_qty = -300
qty_after_transaction = 0
stock_value = 100000
```

still raises `Stock valuation integrity (I4)`.

## Coverage

- Partial Material Transfer (299/300, 100/300)
- Full non-batch Material Transfer 300 @ 15,180,000 IRR (document difference 0; source qty/value 0)
- Full same-rate Material Transfer
- Full Material Issue to zero
- Manufacture full consume
- Incident-equivalent incoming transient unit fixture (no false I4)
- Real leftover-value Case G (I4 still blocks)
- **Batch / SABB** full Material Transfer 300 @ 15,180,000 (mandatory)
- Repost Item Valuation of a valid full consume-to-zero transfer

## Explicit non-goals / unchanged

- No change to v5.1.8 / v5.1.9 DECIMAL(30,9), currency field length, schema-drift, Stock Entry Detail precision, Stock Repost precision, updatedb drift correction, Property Setter precision, or Repost Accounting Ledger protections
- No production SLE / Bin / Stock Entry data repair or migration
- No tolerance inflation, silent repair, or bypass flags
- I1 / I2 / I3 / I5 were reviewed; they do not use insert-default `qty_after_transaction = 0` as a final warehouse balance. I4 is the only invariant changed.

## Version

- `erpnext_extensions.__version__` = `5.2.2`
