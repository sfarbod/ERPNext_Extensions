# Release 5.1.0 — Account Explorer Jalali Date Display

## Summary

- Account Explorer **Voucher/Documents** and **Grouped GL detail** now render dates through the active Frappe / Persian Calendar display formatter (`frappe.datetime.str_to_user`).
- Jalali-enabled users see Jalali dates in the voucher summary grid, GL detail grid, and GL detail header.
- **Backend/API/export dates remain canonical Gregorian ISO** (`YYYY-MM-DD`). No accounting, storage, or sort-semantics changes.

## Display policy

| Surface | Behavior |
|---------|----------|
| Grid / header (UI) | `format_ae_date()` → `frappe.datetime.str_to_user(value, false, true)` |
| API / row model | Canonical ISO, e.g. `"2026-09-29"` |
| CSV/XLSX export | Unchanged — canonical ISO from backend |
| Clipboard (cell copy) | Unchanged — raw canonical ISO from `source_row[column_id]` |

## Files

- `erpnext_extensions/page/account_explorer/core/ae_date_format.js` — shared formatter
- `erpnext_extensions/page/account_explorer/adapters/ae_datatable_adapter.js` — DataTable `_format_cell` Date path
- `erpnext_extensions/page/account_explorer/account_explorer.js` — legacy grid, GL detail grid, GL header
- `erpnext_extensions/page/account_explorer/core/test_ae_date_format.mjs` — Node unit tests
- `iran_accounting/tests/test_account_explorer_date_display.py` — structural + API regression tests
- `iran_accounting/e2e/playwright_account_explorer_jalali_dates.mjs` — Playwright E2E
- `iran_accounting/e2e/account_explorer_jalali_dates_prep.py` — E2E prep (enables Jalali Settings)

## Test coverage

- Node: `test_ae_date_format.mjs` — Jalali/Gregorian display, raw API invariant, DataTable/legacy parity
- Python: `test_account_explorer_date_display` — formatter wiring + canonical ISO API checks
- Playwright: voucher summary visible Jalali + API ISO; GL detail header/grid Jalali + API ISO

## Compatibility

- ERPNext 16.x / Frappe 16.x
- Requires Persian Calendar app for Jalali conversion (uses existing `str_to_user` patch)
- Builds on v5.0.9
