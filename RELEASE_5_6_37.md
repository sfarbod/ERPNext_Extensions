# erpnext_extensions 5.6.37

## Delivery Note — Check Submission: posting-date stock/batch + aggregated errors

### Base

Built on the strict read-only architecture from **v5.6.34** (no savepoint dry-run,
no SLE/GLE/Bin mutations). Unrelated **v5.6.35** Account Explorer work is unchanged.

### Issue 1 — Batch expiry & historical stock use Delivery Note posting date

**Root cause**

- Batch qty used ERPNext `get_batch_qty` without `posting_date`, and core
  `get_available_batches` filters `expiry_date >= today()`, so batches still valid
  on the DN date but expired “today” appeared as **available = 0**.
- Item stock used current `Bin.actual_qty`, not SLE balance as of DN posting.

**Fix**

- Batch expiry follows ERPNext: expired iff `expiry_date < posting_date`
  (equality is valid). Comparison uses the DN `posting_date` field (Gregorian
  storage; Jalali UI converts consistently to the same field).
- Historical item qty via `_get_stock_balance(..., posting_date, posting_time)`.
- Historical batch qty via `get_batch_qty(..., posting_date, posting_time,
  for_stock_levels=True)` so today’s expiry filter is not applied; expiry is
  enforced separately against the DN date.
- Reservations use SRE rows with `creation <= posting_datetime`.

### Issue 2 — Aggregate all independent validation errors

**Root cause**

- One top-level `try/except` stopped at the first `frappe.throw`.

**Fix**

- Independent validation groups run separately; expected exceptions are collected.
- Per-row collectors for stock, serial/batch, warehouse, over-delivery, accounting.
- Deduplicated structured errors with `category`, `item_code`, `row`, `warehouse`,
  `batch_no`, and original message.
- Response: `status` (`pass` / `fail` / `incomplete`), `errors`, `warnings`,
  `limitations`, `summary`.
- Desk dialog lists every error grouped by category; never shows Ready when
  incomplete/fail.

### Read-only guarantees

Write guard + fingerprint unchanged. No submit, SLE/GLE, Bin, SO, SRE, SABB,
Serial/Batch mutations, jobs, or notifications.

### Tests

| Suite | Result |
|---|---|
| `test_delivery_note_check_submission_v5637` | 9 OK |
| `test_delivery_note_check_submission_v5634` (regression) | 15 OK |

Coverage: expiry after/before/equal DN date; today-after-expiry but DN-before → valid;
historical stock available/insufficient; multi-error aggregation; dedupe; response
`status`/`summary`; write-guard + fingerprint immutability.

### Version

- `erpnext_extensions`: **5.6.37**

### Technical report

`CHECK_SUBMISSION_TECHNICAL_REPORT_5_6_37.md`
