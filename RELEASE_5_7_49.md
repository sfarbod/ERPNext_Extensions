# erpnext_extensions 5.7.49

## Facility Balance Query Report — Enhanced Columns & Period Logic

### Summary

Version **5.7.49** upgrades the **Facility Balance** Script Report with a fixed
14-column layout, period opening balances, in-period principal/profit payments,
last payment date, remaining installment count, and maturity date.

Report identity and route are unchanged:
`Facility Management → Facility Balance`

This release is **read-only reporting**. No Facility documents, schedules,
Journal Entries, or GL balances are modified.

### Previous limitations

- Columns mixed master data (type/bank/status) with paid/remaining totals.
- No period opening vs in-period payment split (only optional `as_on_date`).
- No last payment date, remaining installment count, or maturity date.
- Persian labels were not aligned to the finance reporting vocabulary.

### Columns (exact order)

| # | Fieldname | English label | Persian label |
|---|---|---|---|
| 1 | `facility` | Facility Number | شماره تسهیلات |
| 2 | `facility_name` | Facility Name | نام تسهیلات |
| 3 | `principal_amount` | Facility Principal Amount | مبلغ اصل |
| 4 | `profit_amount` | Facility Profit Amount | مبلغ فرع |
| 5 | `opening_principal_balance` | Opening Principal Balance | مبلغ افتتاحیه اصل |
| 6 | `opening_profit_balance` | Opening Profit Balance | مبلغ افتتاحیه فرع |
| 7 | `period_paid_profit` | Profit Paid During Period | پرداخت شده طی دوره فرع جمع |
| 8 | `period_paid_principal` | Principal Paid During Period | پرداخت شده طی دوره اصل جمع |
| 9 | `remaining_principal` | Facility Remaining Principal | مانده اصل |
| 10 | `remaining_profit` | Facility Remaining Profit | مانده فرع |
| 11 | `remaining_total` | Facility Remaining Total | مانده کل |
| 12 | `last_payment_date` | Last Payment Date | تاریخ آخرین پرداخت |
| 13 | `remaining_installments` | Remaining Installments | تعداد اقساط مانده |
| 14 | `maturity_date` | Facility Maturity Date | تاریخ سررسید وام |

English source strings are deliberately specific so older generic translations
(e.g. `Profit Amount` → مبلغ بهره) do not override the Facility Finance vocabulary.

### Calculation definitions

Source of truth (unchanged accounting model):

- Contract: `Facility.principal_amount` / `profit_amount`
- Pre-migration paid: `opening_paid_principal_amount` / `opening_paid_profit_amount`
- Actual payments: submitted `Facility Repayment` (`docstatus = 1`) by `posting_date`
- **Not** from GL / schedule-only inference for paid amounts

With filters `from_date` / `to_date` (`as_on_date` aliases `to_date`):

```
paid_before = opening_paid + Σ repayments (posting_date < from_date)
opening_balance = contract − paid_before

period_paid = Σ repayments (from_date ≤ posting_date ≤ to_date)
              # if from_date empty: all repayments with posting_date ≤ to_date

paid_to_date = opening_paid + Σ repayments (posting_date ≤ to_date)
remaining = contract − paid_to_date
remaining_total = remaining_principal + remaining_profit
```

Invariant:

`مانده کل = مانده اصل + مانده فرع`

and

`opening_balance − period_paid = remaining` (principal and profit separately).

**Last payment date:** `MAX(posting_date)` of submitted repayments with
`posting_date ≤ to_date`.

**Remaining installments:** sequential allocation of paid principal against
`Facility Repayment Schedule` rows (opening paid excluded when the schedule is
partial). If no schedule rows exist but amounts remain, fall back to
`installment_count` (or 1).

**Maturity date:** `Facility.maturity_date`.

### Filters

Preserved: Company (required), Bank, Status, Facility, Facility Name, Facility Type.

Added:

- `from_date` — period start (optional)
- `to_date` — period end (default today)
- `as_on_date` — retained for bookmarks/E2E; aliased to `to_date`

### Files

- `facility_management/report/facility_balance/facility_balance.py`
- `facility_management/report/facility_balance/facility_balance.js`
- `facility_management/tests/test_facility_balance_report_v5749.py`
- `translations/fa.csv`
- `erpnext_extensions/__init__.py` → `5.7.49`

### Tests

| Check | Result |
|---|---|
| Column order (14) | PASS |
| Remaining total invariant | PASS |
| Period reconciliation identity | PASS |
| Company isolation | PASS |
| Installment counter unit cases | PASS |
| Live FAC-2026-06489 (settled) | remaining 0 / installments 0 |
| Live FAC-2026-06498 (partial) | period identity OK |
| Live FAC-2026-07063 (no repayments) | period paid 0 |
| Closed facility | remaining 0 |

### Out of scope

- No Production changes
- No push / merge / deploy / tag
- No Facility / JE / GL mutations
