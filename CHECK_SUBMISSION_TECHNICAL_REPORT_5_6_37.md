# Technical Report — Delivery Note Check Submission (v5.6.37)

## Base

Extends the strict read-only architecture from **v5.6.34**. No savepoint
mutation-and-rollback. Write guard + fingerprint unchanged.

## Root causes fixed

### 1. Batch expiry / stock used “today”, not DN posting date

- `get_batch_qty` without `posting_date` + core `get_available_batches` filter
  `expiry_date >= today()` → batches valid on DN date but expired today showed
  **available = 0**.
- Item qty used current `Bin.actual_qty` instead of SLE balance as of posting.

### 2. First exception aborted the whole check

- A single outer `try/except` around Category A meant only the first
  `frappe.throw` was returned to the Desk.

## Fixes

### Historical stock & batch (Category B)

| Concern | Implementation |
|---|---|
| Item qty | `_get_stock_balance(item, warehouse, posting_date, posting_time)` |
| Batch qty | `get_batch_qty(..., posting_date, posting_time, for_stock_levels=True)` so today’s expiry filter is skipped; expiry checked separately |
| Expiry rule | ERPNext: expired iff `getdate(expiry_date) < getdate(posting_date)` (equality valid) |
| Reservations | SRE sum with `creation <= posting_datetime` |
| Dates | DN `posting_date` / `posting_time` only (Gregorian DB fields; Jalali UI normalizes into them) |

Collectors return structured errors per row (`collect_stock_availability_errors`,
`collect_serial_and_batch_errors`) instead of raising on the first failure.

### Aggregated validation

1. Independent Category A groups in `_run_category_a_groups` — each wrapped by
   `catch_validation`.
2. Per-field document validators run separately.
3. Per-row warehouse / stock / serial-batch / over-delivery / accounting collectors.
4. `dedupe_errors` removes duplicate messages.
5. Response shape:

```text
status: pass | fail | incomplete
ok / ready_for_submission: true only when status == pass
errors[], warnings[], limitations[]
summary: { error_count, warning_count, limitation_count, blocking_limitation_count }
```

Incomplete (blocking limitations, no errors) never returns Ready.

### Frontend

`delivery_note_check_submission.js` lists every error with category label, item,
warehouse, batch, required/available when present; keeps limitations section;
distinguishes blocking vs advisory.

## Explicitly unchanged from v5.6.34

- No `doc.submit()`, SLE/GLE, Bin, SO delivered qty, SRE, SABB, Serial/Batch writes
- No DB mutation + rollback
- No unsafe Server Scripts / hooks / enqueue / notifications
- `validate_serialized_batch` not used as the sole batch path (Category B per-row
  collector so all rows are reported); Category A still runs other DN validators

## Testing

| Suite | Result |
|---|---|
| `test_delivery_note_check_submission_v5637` | 9 tests OK |
| `test_delivery_note_check_submission_v5634` | 15 tests OK |

v5637 covers: expiry after/before/equal DN date; current date after expiry but DN
before → valid; historical stock available/insufficient; multi-error aggregation;
dedupe; response `status`/`summary`; immutability fingerprints.

## Modified modules

- `stock_availability.py`, `serial_batch.py`, `accounting.py`, `validator.py`
- `errors.py` (new)
- `public/js/delivery_note_check_submission.js`
- `tests/test_delivery_note_check_submission_v5637.py` (new)
- `tests/test_delivery_note_check_submission_v5634.py` (patch target rename)
- `RELEASE_5_6_37.md`, `__init__.py` → `5.6.37`
