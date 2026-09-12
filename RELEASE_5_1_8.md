# Release 5.1.8 — Stock Repost / Valuation DECIMAL(30,9) Hardening

## Summary

- **Bug:** `Repost Item Valuation` failed with `MySQLdb.DataError: (1264, "Out of range value for column 'basic_amount' at row 1")` when ERPNext recalculated Stock Entry rows.
- **Observed value:** `Stock Entry Detail.basic_amount = -10277543566399.46`
- **Root cause:** Frappe default Currency/Float storage is `DECIMAL(21,9)` (~12 integer digits). Large IRR stock valuation amounts exceed that capacity during:

  `Repost Item Valuation` → `repost_future_sle` → `update_entries_after` → `recalculate_amounts_in_stock_entry` → `Stock Entry Detail.db_update()`

- **Fix:** Complete metadata-driven audit of the stock repost graph and idempotent migration of **118 DB-backed monetary amount/rate fields** to `DECIMAL(30,9)`.

## Repost graph audited

```
Repost Item Valuation
  → repost_future_sle / update_entries_after
  → Stock Ledger Entry (stock_value, stock_value_difference, incoming/outgoing/valuation_rate)
  → Stock Entry + Stock Entry Detail (basic_amount, amount, basic_rate, valuation_rate, additional_cost, …)
  → Delivery Note Item / Sales Invoice Item / Packed Item (incoming_rate)
  → Purchase Receipt/Invoice Item (+ Supplied) (valuation_rate, amounts, landed_cost_voucher_amount)
  → Subcontracting Receipt Item / Supplied Item
  → Serial and Batch Bundle / Entry
  → Bin (stock_value, valuation_rate)
  → Landed Cost Voucher / Item / Taxes / Purchase Receipt
  → GL Entry (accounting ledger repost; already DECIMAL(30,9) via accounting_core — re-asserted)
```

## Scope / counts

| Metric | Count |
|--------|------:|
| DocTypes in repost graph | 21 |
| Currency/Float/Percent fields inspected | 251 |
| Monetary fields hardened (allowlist) | 118 |
| Monetary Amount classification | 90 |
| Monetary Rate classification | 28 |
| Rate / qty / percentage intentionally excluded | 133 |

### Critical Stock Entry Detail fields

| Field | Classification |
|-------|----------------|
| `basic_amount` | Monetary Amount |
| `amount` | Monetary Amount |
| `additional_cost` | Monetary Amount |
| `landed_cost_voucher_amount` | Monetary Amount |
| `customer_provided_item_cost` | Monetary Amount |
| `basic_rate` | Monetary Rate |
| `valuation_rate` | Monetary Rate |

### Valuation-rate decision

`basic_rate`, `valuation_rate`, `incoming_rate`, `outgoing_rate`, and related per-unit valuation rates **are hardened** to `DECIMAL(30,9)` because they are written during repost and, in IRR with large absolute valuation magnitudes, can exceed `DECIMAL(21,9)` integer capacity when multiplied into amounts or stored directly. Price-list / transaction `rate` fields that are not rewritten by stock-ledger valuation remain excluded.

## Custom field audit (this site)

**Stock Entry Detail** custom numeric fields inspected:

| Field | Classification |
|-------|----------------|
| `custom_consignment_settlement_amount` | Monetary Amount — included |
| `custom_material_loan_issue_value` | Monetary Amount — included |
| `custom_material_loan_return_value` | Monetary Amount — included |
| `custom_material_loan_settlement_amount` | Monetary Amount — included |
| `custom_material_loan_issue_rate` | Monetary Rate — included |
| `custom_original_receipt_rate` | Monetary Rate — included |
| `custom_material_loan_*_qty`, `custom_*_returnable_qty`, `custom_original_receipt_qty` | Quantity — excluded |

## Schema protection

- **Pre-model-sync:** Property Setter `length=30` on all allowlisted fields
- **Post-model-sync:** `INFORMATION_SCHEMA` inspect + idempotent `ALTER … MODIFY DECIMAL(30,9)` preserving NULL/default
- **Guards:** `assert_repost_monetary_schema_targets()` + `assert_repost_field_classification_completeness()`

## Explicit non-goals

- No Frappe global Currency/Float type-map change
- No ERPNext core changes
- No display-precision changes
- **Negative-stock validation is not bypassed** — after this fix, ERPNext may still raise the legitimate “stock was negative …” valuation message; only `DataError 1264` is eliminated

## Relation to prior precision work

- Stock Reconciliation fields already at `DECIMAL(30,9)` via v4 — re-asserted idempotently
- Selling DN/SI Item amounts from v5.0.7 — re-asserted; **`incoming_rate` newly hardened** for repost writes
- GL Entry debit/credit from accounting_core — re-asserted
- Authoritative module for the **repost graph:** `stock_repost_decimal_precision_v518.py`

## Files

- `stock_repost_decimal_precision_v518.py`
- `patches/pre_model_sync/set_stock_repost_amount_decimal_metadata_v518.py`
- `patches/post_model_sync/expand_stock_repost_amount_precision_v518.py`
- `tests/test_stock_repost_decimal_precision_v518.py`
- `tests/test_stock_repost_decimal_precision_v518_sync_e2e.py`
- `scripts/audit_stock_repost_v518.py`
- `erpnext_extensions/__init__.py` — version `5.1.8`

## Regression coverage

- Exact failing value: `basic_amount = -10277543566399.46` (+ companion amount/rates)
- Fractional round-trip `1682808518031.123456789` on SLE / SED
- `recalculate_amounts_in_stock_entry` path without DataError 1264
- `updatedb` non-revert for Stock Entry / SED / SLE / Stock Reconciliation
- Migrate idempotency

## Compatibility

- ERPNext 16.x / Frappe 16.x
