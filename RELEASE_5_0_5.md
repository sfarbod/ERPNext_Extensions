# Release 5.0.5 — Sales Order Commission Amount Overflow Hotfix

## Summary

- **Bug:** Saving a Sales Order could fail with `MySQLdb.DataError: (1264, "Out of range value for column 'amount_eligible_for_commission' at row 1")` for large IRR values.
- **Root cause:** Frappe default Currency storage is `DECIMAL(21,9)`, which is insufficient for large commission-eligible totals on high-value Sales Orders.
- **Fix:** Expand storage for the failing column only from `DECIMAL(21,9)` to `DECIMAL(30,9)`, with migrate-safe Property Setter metadata so later `bench migrate` does not revert the column.

## Scope

- **Changed:** `tabSales Order.amount_eligible_for_commission` only.
- **Not changed:** Other Sales Order monetary fields, Frappe global Currency/Float SQL mapping, or any broad Sales Order amount audit.

## Files

- `sales_order_commission_decimal_precision.py` — single-field allowlist + schema/metadata helpers
- `patches/pre_model_sync/set_sales_order_amount_eligible_for_commission_decimal_metadata.py`
- `patches/post_model_sync/expand_sales_order_amount_eligible_for_commission_precision.py`
- `tests/test_sales_order_commission_decimal_precision.py` — unit coverage
- `tests/test_sales_order_commission_decimal_precision_sync_e2e.py` — large IRR regression + idempotency
- `erpnext_extensions/__init__.py` — version `5.0.5`

## Compatibility

- ERPNext 16.x / Frappe 16.x
- Builds on v5.0.4 PM workflow fixes
