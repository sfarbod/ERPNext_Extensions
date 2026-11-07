# erpnext_extensions 5.7.9

## Sales Invoice custom gross/discount overflow (DN → SI)

### Incident

Creating a Sales Invoice from Delivery Note `MAT-DN-2026-00049` fails on save:

```text
MySQLdb.DataError: (1264, "Out of range value for column 'custom_gross_total' at row 1")
```

| Item | Value |
|------|-------|
| DocType | `Sales Invoice` |
| Field | `custom_gross_total` |
| Value | `1,818,483,388,563` IRR |
| Related | `custom_total_discount` = `381,268,484,274` |
| DN | `MAT-DN-2026-00049` (~581 items) |
| Posting date | `2026-04-21` |

Standard SI totals (`grand_total` ≈ `1,559,506,909,551`) already fit in
`DECIMAL(30,9)` from selling precision v5.0.7. The overflow is on **custom**
discount-calculation fields only.

### Root cause

1. Client Script **Discount Calculation v02** (Sales Invoice Form) computes:
   - row `custom_gross_amount` = `qty × price_list_rate`
   - parent `custom_gross_total` = Σ row gross
   - parent `custom_total_discount` = Σ `(discount_percentage/100) × gross`
2. Those Custom Fields were Currency with default Frappe SQL
   `DECIMAL(21,9)` (12 integer digits).
3. `1,818,483,388,563` has **13** integer digits → MariaDB rejects insert.
4. v5.0.7 selling allowlist already hardened
   `custom_sales_growth_discount`, `custom_total_foc_amount`,
   `custom_total_raw_discount_amount`, and SI item payment/discount customs —
   but **missed** `custom_gross_total`, `custom_total_discount`, and
   `custom_gross_amount`.
5. Selling healer was not in the central `after_migrate` coordinator, so
   later Custom Fields could remain unprotected after migrate/updatedb.

Not an Iran Accounting rounding bug. Not an SLE/GL valuation change.
Standard SO/DN/SI monetary columns were already `DECIMAL(30,9)`.

### Fix

1. Extend `selling_documents_decimal_precision_v507` allowlist with:
   - `Sales Invoice.custom_gross_total`
   - `Sales Invoice.custom_total_discount`
   - `Sales Invoice Item.custom_gross_amount`
2. Align `tabCustom Field.length = 30` + Property Setter `length=30`.
3. Idempotent `ALTER … MODIFY DECIMAL(30,9)` preserving NULL/default/data.
4. v5.7.9 pre/post patches call the selling repair + completeness guard.
5. Add selling repair to `decimal_precision_after_migrate` coordinator so
   every `bench migrate` re-asserts selling + stock + purchasing schemas.

Does **not** change Discount Calculation business logic, tax math, IRR
integerization, or SLE/GL paths. Extra decimal scale capacity does not force
fractional IRR values; existing integer IRR amounts remain `.000000000`.

### Schema (before → after)

| DocType | Field | Before | After |
|---------|-------|--------|-------|
| Sales Invoice | `custom_gross_total` | `decimal(21,9)` | `decimal(30,9)` |
| Sales Invoice | `custom_total_discount` | `decimal(21,9)` | `decimal(30,9)` |
| Sales Invoice Item | `custom_gross_amount` | `decimal(21,9)` | `decimal(30,9)` |

Also classified/hardened while closing the selling completeness guard against
current ERPNext metadata (were still `decimal(21,9)`):

| DocType | Field |
|---------|-------|
| Sales Order Item / Delivery Note Item / Sales Invoice Item | `mapped_additional_discount_amount` |
| Sales Invoice Item | `asset_gross_value`, `asset_value_after_depreciation` |

Excluded (qty): `Packed Item.received_qty`.

Already `decimal(30,9)` (re-asserted, not newly owned):  
`custom_sales_growth_discount`, `custom_total_foc_amount`,
`custom_total_raw_discount_amount`, item
`custom_raw_discount_amount` / `custom_additional_discount_share` /
`custom_base_amount_for_payment` / `custom_net_amount_for_payment`.

### Tests

`test_sales_invoice_custom_amount_precision_v579.py`

- Schema expand + idempotency + Custom Field / Property Setter metadata
- `updatedb()` non-narrow
- Coordinator heals forced `21,9` drift
- Persist production values `1818483388563` / `381268484274`
- DN `MAT-DN-2026-00049` → SI save with large custom totals

Also update v5.0.7 unit allowlist count **127 → 130**.

### Version

`erpnext_extensions` → **5.7.9**
