# Extentionhrms

HRMS extensions for ERPNext. Currently ships the **payroll accrual accounting
dimension** fix.

## Problem

When **Department** (or any Accounting Dimension) is configured as **mandatory
for P&L (Income/Expense) accounts**, the stock HRMS payroll accrual Journal Entry
fails validation. HRMS keys the accrual by `(account, cost_center)` and copies
accounting dimensions from the *Payroll Entry* itself (a single value), so the
aggregated expense rows come out with a **blank Department**.

A common work-around — a `Journal Entry` `before_validate` Server Script that
**deletes every Expense row and rebuilds it from earnings only** — has two
defects:

1. It drops the credit rows of deductions mapped to **expense** accounts (a
   deduction sharing an earning's expense account, or a penalty expense),
   unbalancing the entry.
2. It ignores per-employee cost-centre **percentage splits**
   (`Salary Structure Assignment.payroll_cost_centers`).

## Approach

Override `PayrollEntry.make_accrual_jv_entry` (via `override_doctype_class`) and,
when `Process Payroll Accounting Entry based on Employee` is **OFF**, build the
accounts in one loss-less pass:

- split every earning **and** deduction across the employee's payroll cost
  centres by percentage (reusing HRMS's own resolver);
- aggregate by `(account, cost_center, department)`;
- take **Department from each Salary Slip** (not the Employee master);
- stamp Department on **P&L rows only** — Balance-Sheet rows (payable, advances,
  loans) are left blank, where the dimension is not mandatory;
- absorb the per-split rounding residue with a single balancing line to the
  company **Round-Off account**, which carries the round-off Department from the
  Company custom field `custom_payroll_round_off_department`.

The resulting rows are handed back to HRMS's native `make_journal_entry`, so
multi-currency handling, Salary-Slip linking and submission are unchanged.

## Configuration

Set **`custom_payroll_round_off_department`** on the Company to the Department
that should carry the accrual rounding residue (the round-off account is P&L, so
the line needs a Department). If it is not set, accrual generation raises a clear
error rather than producing an entry that fails the mandatory-dimension check.

### Per-employee components (loans / advances)

Tick **Process Based on Employee** (`custom_process_based_on_employee`) on a
Salary Component to keep employee-level granularity for that component: instead
of being aggregated by cost centre / department, it is booked as **a separate
row per employee with the Employee as Party**. Use it for employee-tied
components such as loans (`وام`) and advances (`مساعده`) so repayments reconcile
per person. All other components keep the group-by behaviour. (P&L flagged
components still carry their Department; balance-sheet ones don't.)

### Fixed Party on the account (SSO / tax suppliers)

The **Salary Component Account** child table gains **Party Type** / **Party**
columns (`custom_party_type` / `custom_party`). Set them on the per-company
account mapping to stamp a fixed Party — e.g. the Social-Security / tax
**Supplier** — on the generated rows for that account. The override applies it
after grouping (without overriding an employee Party), which lets you **delete
the old party-assignment Server Script** and keep everything in one place. Set
the same Party on every component that maps to a shared payable account.

## Layout

| File | Runs without bench? | Purpose |
| --- | --- | --- |
| `payroll_accrual_grouping.py` | ✅ pure Python | grouping / splitting / round-off logic |
| `tests/test_payroll_accrual_grouping.py` | ✅ `pytest` | 50 unit + randomised property tests |
| `payroll_entry_override.py` | ❌ needs Frappe/HRMS | DB read + `make_accrual_jv_entry` override + exclusive relieving attendance window |
| `payroll/exclusive_relieving.py` | ✅ pure Python | exclusive relieving payable-end helpers |
| `payroll/salary_slip_relieving.py` | ❌ needs Frappe/HRMS | `extend_doctype_class` Salary Slip mixin |
| `tests/test_exclusive_relieving.py` | ✅ `pytest` | exclusive relieving payment-day matrix |

## Running the tests

```bash
pytest erpnext_extensions/extentionhrms/tests/test_payroll_accrual_grouping.py
```

The tests import **no Frappe**; the fixtures use fictional identifiers but
reproduce the real data shapes (zero-decimal currency, employer-contribution
expense earnings, deductions mapped to both liabilities and expense accounts,
multi-cost-centre splits, and a P&L round-off account).

## Out of scope (follow-up)

A retired Server Script also set `party = Employee` on advance/loan deductions.
With employee-based accounting OFF the accrual is aggregated and cannot carry a
per-employee party, so that behaviour is **not** reproduced here. If per-employee
advance/loan tracking is required, enable `Process Payroll Accounting Entry based
on Employee` (the override defers to stock HRMS in that mode) or handle it as a
separate change.

---

## Hourly leave (مرخصی ساعتی)

`leave_application_override.py` (`override_doctype_class` on **Leave
Application**) adds hourly leave on top of stock full/half-day leave — no
separate Leave Type; the deduction hits the same entitlement balance.

- `custom_is_hourly` + `custom_from_time`/`custom_to_time` on Leave
  Application; the duration lands in `custom_leave_hours` and
  `total_leave_days = hours / Employee.custom_daily_working_hours`
  (default 8.75 — the scheduled 08:30–17:15 day, i.e. what a full leave day
  covers; not the 7.33 legal average used for overtime rates), rounded to
  3 decimals. Leave Ledger Entry accepts fractional leaves, so submit/cancel
  work unchanged (e.g. 26 days − 2h = 25.771).
- The balance check is re-run against the fractional value, so an employee
  with less than one day remaining can still take hourly leave.
- Attendance is left alone in hourly mode: an existing Present attendance
  does not block the application, and submit does not convert the day to
  "On Leave" (protects checkin working-hours / overtime pipelines).
- Guard rails: single-day only, `to_time > from_time`, hours must stay below
  the daily working hours, LWP leave types rejected (payroll LOP only
  understands 0.5/1 day).
- Pure math lives in `hourly_leave_calc.py` (pytest:
  `tests/test_hourly_leave_calc.py`); custom fields are created idempotently
  on `after_migrate` (`custom_fields.HOURLY_LEAVE_CUSTOM_FIELDS`).

Known v1 limitation: one leave application per calendar day (the stock
overlap validation is kept), so a second hourly leave on the same day is
rejected.

---

## Exclusive relieving date (payment days)

HRMS Core treats `Employee.relieving_date` as the **last payable day**. Local
policy treats it as the **first non-payable day**.

- Wired with `extend_doctype_class` on **Salary Slip**
  (`payroll/salary_slip_relieving.py`) — does not replace the HRMS controller.
- Shortens `actual_end_date` to `relieving_date - 1` when relieving falls in the
  slip period (including the last day of the period).
- Relieving on the payroll **start** date yields `payment_days = 0` while the
  employee remains eligible for Payroll Entry (stock filter already uses
  `relieving_date >= start_date`) and keeps a Salary Slip with normal
  `total_working_days`.
- Leave-based LWP skips the relieving day so it is not deducted twice.
- Payroll Entry unmarked-attendance windows use the same exclusive end via
  `PayrollEntryWithAccountingDimensions.get_payroll_dates_for_employee`.

Pure helpers + pytest: `payroll/exclusive_relieving.py`,
`tests/test_exclusive_relieving.py`.
