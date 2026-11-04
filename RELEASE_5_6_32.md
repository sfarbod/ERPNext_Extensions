# erpnext_extensions 5.6.32

## PM Clearance — PM Request Eligibility Query (Partial Payment)

### Summary

PM Clearance Request Link search no longer requires `payment_status = 'Paid'` only.
Eligibility now requires **both**:

```
(payment_status IN ('Paid', 'Partially Paid'))
AND
(status IN ('Closed', 'Paid'))
```

Save validation (`pm_request_passes_clearance_filters`) uses the same gate so Link
search and Desk Save stay aligned.

All other filters remain: submitted Request, non-blocked holder, employee /
company / holder / petty-cash match, submitted Payment Entry pointer, and
available balance &gt; 0 (paid − active Clearance reservations).

### Root cause

`pm_request_query_for_pm_clearance` filtered `payment_status = 'Paid'`, which hid
valid Partially Paid + Closed Requests (e.g. `REQ-HR-EMP-0023-2026-09-00001`)
that still had funded available balance. Save validation did not require full
`Paid`, so search and Save disagreed.

### Fix

1. `allocation_service.pm_request_query_for_pm_clearance` — payment_status IN
   (`Paid`, `Partially Paid`) AND status IN (`Closed`, `Paid`).
2. `allocation_service.pm_request_passes_clearance_filters` — same payment_status
   × status checks before paid/available gates.
3. Multi-PE / PE-cancel fixtures close Partially Paid Requests before Clearance
   allocation (matches new business gate).
4. Clearance PI fixtures set Buying Settings `pr_required=No` (with `po_required`)
   so site Buying Settings do not block fixture PIs.

### Tests

| Suite | Result |
|---|---|
| `test_pm_clearance_request_eligibility_v5632` (20) | OK — all payment×status combinations + partial-pay safety |
| `playwright_pm_clearance_request_eligibility_v5632.mjs` A–E | OK — Desk search (incl. live `REQ-HR-EMP-0023-2026-09-00001`), Save within paid, over-alloc blocked, Open/Unpaid hidden |
| `test_pm_clearance` (49) | OK |
| `test_pm_request_multi_pe` (20) | OK |
| `test_pm_pe_cancel_funding` (12) | OK |
| `test_pm_clearance_active_reservation_v555` (18) | OK |
| `test_pm_clearance_return_funding_availability_v5211` (7) | OK |
| `test_pm_request_allocation_security` (5) | OK |

### Partial payment safety

Available for Clearance = submitted PE paid amount − active Clearance reservations.
Unpaid Request amount is never allocatable. Over-allocation is rejected by Save
validation.

### Version

- `erpnext_extensions`: **5.6.32**
