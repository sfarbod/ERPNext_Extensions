# Release 5.2.23 — ERPNext 16.35.0 / Frappe 16.34.0 compatibility

## Summary

- **Compatibility:** Explicit allow-lists extended for **ERPNext 16.35.x** and **Frappe 16.34.x**. ERPNext **16.36.x** remains blocked.
- **UVR / RIV fingerprints:** Unchanged vs 16.34.x (Class A). Version allow-list only.
- **Ledger preview:** Iran `get_accounting_ledger_preview` now passes company currency into ERPNext 16.35 `get_columns(raw_columns, fields, currency)` while remaining two-arg compatible on 16.29–16.34.
- **Accounting policy:** No change.

## Upstream deltas revalidated (16.34.2 → 16.35.0)

| Area | Impact |
|------|--------|
| UVR / RIV fingerprinted methods | Identical hashes and signatures |
| `get_columns` ledger preview | Required `currency` argument — Iran preview wrapper adapted |
| `get_valuation_rate` | Additive `posting_datetime` / `creation` kwargs; Iran does not call this API |
| Stock Entry additional-cost allocation | Qty fallback when incoming `basic_amount` is 0; Iran wraps original `StockEntry.get_gl_entries` |
| Payment Entry / Dunning | Additive unpaid-dunning split; Iran does not wrap `get_payment_entry` |
| `AccountsController.validate_price_list` | Additive disabled-price-list check |
| Frappe `flt` rounding | Decimal fallback when epsilon ≥ quarter-step; Iran IRR rounding remains Decimal-owned |

## Compatibility stack

- ERPNext **16.35.0**
- Frappe **16.34.0**
- erpnext_extensions **5.2.23**

## Files

- `iran_accounting/domain/riv_rate_guard.py`
- `iran_accounting/domain/uvr_regional_guard.py`
- `iran_accounting/integration/monkey_patches.py`
- `iran_accounting/tests/test_riv_rate_first_preserve.py`
- `iran_accounting/tests/test_uvr_regional_guard.py`
- `iran_accounting/tests/test_stock_entry_gl_delegation.py`
- `RELEASE_5_2_23.md`
- `__init__.py` version bump
