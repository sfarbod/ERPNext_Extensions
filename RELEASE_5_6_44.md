# erpnext_extensions 5.6.44

## Delivery Note — Check Submission: future-ledger Negative Stock parity

### Base

Builds on strict read-only Check Submission (**v5.6.34**) and posting-date /
aggregated errors (**v5.6.37**). Unrelated Iran Accounting 5.6.38–5.6.43 work
is unchanged.

### Critical bug (MAT-DN-2026-00281)

Check Submission reported **READY** while ERPNext Submit raised
**Negative Stock Error** for Batch `3123-20100138-ss241607p22`
(needed **46.0** in warehouse `انبار آماده فروش -میلان پارس`).

**Root cause:** availability was evaluated only as of the Delivery Note posting
date (historical batch qty = 337 ≥ 55). Submit also validates that inserting
the outward entry does not drive **later** Serial/Batch ledger balances
negative (SABB `get_batchwise_available_qty` / future SLE shift). A later
Stock Entry consumed 328 of that batch, so Submit correctly required 46 more
units — Check Submission did not.

### Fix

Read-only equivalents of:

1. Warehouse future `qty_after_transaction` shift
   (`validate_negative_qty_in_future_sle`).
2. Batch cumulative ledger across legacy `SLE.batch_no` **and**
   Serial and Batch Entry rows (SABB path), projecting the DN outward.

Statuses are explicit: **READY** / **BLOCKED** / **INCOMPLETE**. Advisory
limitations no longer claim that future stock integrity is unchecked for
negative quantity.

### Tests

| Suite | Result |
|---|---|
| `test_delivery_note_check_submission_v5644` | **4 OK** |
| `test_delivery_note_check_submission_v5637` | **9 OK** |
| `test_delivery_note_check_submission_v5634` | **15 OK** |

Includes live regression for `MAT-DN-2026-00281` when present as Draft.

### Version

- `erpnext_extensions`: **5.6.44**

### Technical report

`CHECK_SUBMISSION_TECHNICAL_REPORT_5_6_44.md`
