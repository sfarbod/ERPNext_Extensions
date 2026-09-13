# Release 5.2.3 — Allow incoming stock movements that heal an existing negative balance

## Title

Allow incoming stock movements that heal an existing negative balance

## Baseline

- Frappe **16.33.1**
- ERPNext **16.34.2**
- Previous erpnext_extensions **5.2.2**
- `erpnext_extensions.__version__` = **5.2.3**

## Summary

ERPNext `update_entries_after.validate_negative_stock` rejects any SLE whose resulting warehouse qty is negative. A legitimate **incoming** movement into a warehouse that is already negative is therefore blocked even when the movement **reduces** the deficit:

```
previous = -565.17
incoming = +300
result   = -265.17
```

That receipt is a healing movement. v5.2.3 allows it. It does **not** enable global negative stock, and it does **not** allow an outgoing SLE to create or worsen a negative balance.

## Healing predicate

```
previous_qty < 0
AND actual_qty > 0
AND new_qty > previous_qty
```

`previous_qty` is the running qty ERPNext already holds in `validate_negative_stock` (`wh_data.qty_after_transaction`), not `Bin.actual_qty`.

## What still blocks

| Movement | Example | Result |
|---|---|---|
| Outgoing creates negative | +100 then −300 | Insufficient Stock |
| Outgoing from zero | 0 then −100 | Insufficient Stock |
| Worsens existing negative | −565.17 then −100 | Insufficient Stock |
| Source shortage on a transfer | source 34.83, transfer 300 | Insufficient Stock **on SOURCE** (265.17) |
| Outward SABB shortage | batch 100, transfer 300 | `BatchNegativeStockError` |

## Scope of the change

v5.2.3 changes **warehouse + incoming-batch (SABB) healing validation**:

- **Warehouse** `update_entries_after.validate_negative_stock`: skip enqueue/raise only for a qualifying incoming healing SLE (IRR companies). The running qty is ERPNext's `wh_data.qty_after_transaction`, never `Bin.actual_qty`.
- **Incoming batch / SABB**: the same predicate is applied only when `type_of_transaction == "Inward"`. Vanilla `validate_batch_inventory` would still throw `throw_error_message` after `validate_negative_batch`, so Inward `validate_batch_inventory` is skipped only when every currently-negative target batch is reduced by this inward bundle. **Outward** SABB validation is unchanged.

v5.2.2 I4 lifecycle handling is unchanged. I1–I5 are not weakened.

## Explicit non-goals

- No `Stock Settings.allow_negative_stock` / Item / batch negative-stock enablement
- No production SLE / Bin / SABB / Item 18000007 repair
- No valuation_rate / stock_value rewrite
- Historical negative stock is **not** silently repaired by this release; only future incoming healing movements may post

## Valuation

Allowing a healing receipt does not rewrite valuation. After `-565.17 + 300 → -265.17` at rate `1,000,000`:

- `qty_after_transaction` = `-265.17`
- `valuation_rate` = `1,000,000`
- `stock_value` = `-265,170,000` (`qty × rate`)
- `stock_value_difference` = `+300,000,000`

That is vanilla Moving Average math on a still-negative qty. I4 does not apply (`qty_after ≠ 0`). No integrity bypass was added.

## Coverage

- Production-shaped healing (`-565.17 + 300 → -265.17`)
- Heal to positive / exactly to zero
- Normal incoming into zero and into positive
- Outgoing creates / from-zero / worsens negative (blocked)
- Source shortage names SOURCE (`34.83 - 300` → `265.17`)
- Batch/SABB healing Material Transfer + RIV replay
- Outward SABB source shortage (`BatchNegativeStockError`)
- I4 Case G still blocks; v5.2.2 transient incoming is not I4

## Version

- `erpnext_extensions.__version__` = `5.2.3`
