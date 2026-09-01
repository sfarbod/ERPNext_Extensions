# Release 5.0.7 — Selling Documents Monetary DECIMAL(30,9) Hardening

## Summary

- **Root cause:** Frappe default Currency/Float storage is `DECIMAL(21,9)`, which is insufficient for large IRR selling transactions across Sales Order, Delivery Note, and Sales Invoice parent/child tables.
- **Risk:** Saves could fail with `MySQLdb.DataError: (1264, "Out of range value for column …")` on any monetary column still at `DECIMAL(21,9)`.
- **Fix:** Complete metadata-driven audit and idempotent migration to `DECIMAL(30,9)` for **127 DB-backed monetary fields** across the three selling document families and their child tables.

## Scope

### Parent DocTypes
- Sales Order (`tabSales Order`)
- Delivery Note (`tabDelivery Note`)
- Sales Invoice (`tabSales Invoice`)

### Child / related DocTypes (14)
- Sales Order Item, Sales Invoice Item, Delivery Note Item
- Sales Taxes and Charges, Payment Schedule
- Sales Team, Tax Withholding Entry, Item Wise Tax Detail
- Sales Invoice Advance, Sales Invoice Payment, Sales Invoice Timesheet
- PDC Invoice Application
- Packed Item, Pricing Rule Detail (graph nodes; no monetary amount columns)

### Supersedes
- Partial `approved_decimal_precision` selling allowlists (Facility-only remain there)
- v5.0.5 single-field Sales Order `amount_eligible_for_commission` hotfix (now part of complete coverage)

## Audit results

| Metric | Count |
|--------|------:|
| DocTypes in selling graph | 17 |
| Currency/Float/Percent fields inspected | 242 |
| Monetary amount fields hardened | 127 |
| Rate/percentage/qty fields intentionally excluded | 115 |
| Site custom monetary fields included | 7 |

### Custom monetary fields (this site)
- **Sales Invoice:** `custom_sales_growth_discount`, `custom_total_foc_amount`, `custom_total_raw_discount_amount`
- **Sales Invoice Item:** `custom_raw_discount_amount`, `custom_net_amount_for_payment`, `custom_additional_discount_share`, `custom_base_amount_for_payment`

## Excluded (unchanged at DECIMAL(21,9) or non-decimal)

Rates, exchange factors, percentages, quantities, weights — e.g. `rate`, `price_list_rate`, `conversion_rate`, `commission_rate`, `qty`, `discount_percentage`, `allocated_percentage`, `invoice_portion`, etc.

## Schema protection

- **Pre-model-sync:** Property Setter `length=30` on all 127 allowlisted fields
- **Post-model-sync:** `INFORMATION_SCHEMA` inspect + idempotent `ALTER … MODIFY DECIMAL(30,9)` preserving NULL/default semantics
- **Guards:** `assert_schema_targets()` + `assert_field_classification_completeness()` (fails on unclassified new Currency fields)

## No changes to

- Frappe global Currency/Float SQL type map
- ERPNext core
- Currency display precision
- Accounting calculations / business logic

## Files

- `selling_documents_decimal_precision_v507.py` — authoritative allowlist + helpers
- `patches/pre_model_sync/set_selling_documents_amount_decimal_metadata_v507.py`
- `patches/post_model_sync/expand_selling_documents_amount_precision_v507.py`
- `tests/test_selling_documents_decimal_precision_v507.py` — unit + completeness guard
- `tests/test_selling_documents_decimal_precision_v507_sync_e2e.py` — SO/DN/SI large IRR regression
- `scripts/audit_selling_v507.py` — metadata audit helper
- `approved_decimal_precision.py` — selling fields removed (Facility only)
- `erpnext_extensions/__init__.py` — version `5.0.7`

## Regression coverage

Large IRR values `1445552233069` and `1682808518031`; fractional round-trip `1682808518031.123456789` on parent columns; child item amounts; migrate/idempotency; `updatedb` non-revert.

## Compatibility

- ERPNext 16.x / Frappe 16.x
- Builds on v5.0.5 Sales Order commission hotfix and prior partial selling patches
