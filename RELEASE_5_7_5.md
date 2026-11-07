# erpnext_extensions 5.7.5

## Asset Request — Request Purchase date validation

### Incident

Request Purchase on approved Asset Request `AUD-AR-2026-00049` failed with:

`Row #1: Required By cannot be before Transaction Date.`

Displayed (Jalali) dates were valid relative to each other:

| Field | Jalali | Gregorian (DB) |
|-------|--------|----------------|
| Transaction Date | 1405-08-05 | 2026-10-27 |
| Required Date | 1405-08-06 | 2026-10-28 |

### Root cause

`create_material_request` set:

- Material Request `transaction_date` = **`today()`** (e.g. 2026-11-07)
- Material Request Item `schedule_date` = Asset Request `required_date` (2026-10-28)

ERPNext `BuyingController.validate_schedule_date` then correctly rejected
`schedule_date < transaction_date`. The UI dates on the Asset Request were fine;
the generated Material Request invented a later Transaction Date.

Not a Jalali string comparison bug — Gregorian DB values were correct on the AR.
Persian Calendar display was a red herring.

### Fix

In `fulfillment_service.create_material_request` / new helper
`material_request_dates_from_asset_request`:

1. MR `transaction_date` = Asset Request `transaction_date` (fallback today).
2. Item `schedule_date` = item/header `required_date` (fallback transaction_date).
3. If schedule would precede transaction_date, clamp to transaction_date
   (does not disable ERPNext validation).

No ERPNext core changes. Unrelated Material Request validations unchanged.

### Tests

`test_request_purchase_dates_v575.py`

- Helper: equal / after / multi-day / item-level / clamp / missing / past-vs-today
- Integration: synthetic past AR dates → MR save+submit
- Fixture: `AUD-AR-2026-00049` Jalali display case

### Version

`erpnext_extensions.__version__` → **5.7.5**
