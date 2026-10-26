# erpnext_extensions 5.6.18

## HRMS — Exclusive Relieving Date for Payment Days

### Summary
Pays salary through the day **before** `Employee.relieving_date`. HRMS Core
treats relieving as inclusive; this release composes an exclusive rule onto
Salary Slip via Frappe v16 `extend_doctype_class` (no HRMS Core edits).

### Business rules
- Last payable date = `relieving_date - 1`
- Relieving on payroll period start → employee **stays** on Payroll Entry /
  Salary Slip generation with `payment_days = 0` and normal `total_working_days`
- Relieving before the period → unchanged HRMS eligibility (`relieving >= start`)
- Joining date remains inclusive; LWP / attendance / holidays stay single-counted

### Implementation
- `extentionhrms/payroll/exclusive_relieving.py` — pure date helpers
- `extentionhrms/payroll/salary_slip_relieving.py` — Salary Slip extension
  (`actual_end_date`, `get_payment_days`, `_get_days_outside_period`, Leave LWP)
- `extentionhrms/payroll_entry_override.py` — unmarked-attendance window aligned
  (accrual-dimension override preserved)
- `hooks.py` — `extend_doctype_class` for Salary Slip
- Tests: `extentionhrms/tests/test_exclusive_relieving.py`

### Verification (development.localhost / مهر 1405)
- Backup: `20261026_123023-development_localhost-database.sql.gz`
- `HR-EMP-0543`: payment_days **6 → 5**, total_working_days 30
- 8 first-day relieving employees: on PE list, slips kept, payment_days **1 → 0**
- 11 draft slips recalculated; no submitted slips / JEs / GL touched

### Non-goals
- No Core HRMS / ERPNext / Frappe patches
- No automatic rewrite of submitted historical Salary Slips
- No Production deploy in this release

### Version
**5.6.18**
