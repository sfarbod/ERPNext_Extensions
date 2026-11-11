# Release 5.7.26 — Payroll Entry Accrual JE Regeneration After Cancel

**Version:** **5.7.26**  
**Branch:** `develop`  
**Runtime scope:** HRMS Payroll Entry accrual Journal Entry generation guard  
**Site validated:** `development.localhost` — Payroll Entry `اردیبهشت 1405`  
**Deployment status:** Local Development only. **No Production push/merge/deploy without separate authorization.**

## Objective

Allow a Submitted Payroll Entry to generate a new **Draft** accrual Journal Entry when no *active* Journal Entry exists, without cancelling or resubmitting the Payroll Entry.

## Root cause

1. Accrual JE creation is tied to Salary Slip submission (`submit_salary_slips` → `make_accrual_jv_entry`). After slips are submitted, Desk offers only bank-entry actions — there is no supported regenerate action.
2. HRMS `get_sal_slip_list` excludes any Salary Slip with a non-empty `journal_entry`, including cancelled or deleted JE names.
3. Blocking was effectively “any historical JE reference”, not “any Draft/Submitted JE”.

## Behaviour (authoritative)

| Existing Journal Entry condition | Allow new JE? |
|---|---|
| No linked JE | YES |
| Linked JE Cancelled (`docstatus=2`) | YES |
| Linked JE Draft (`0`) or Submitted (`1`) | NO |
| Stale link to missing JE | YES (after safe cleanup) |
| Multiple cancelled only | YES |
| Cancelled + any Draft/Submitted | NO |

## Implementation

- `payroll_accrual_je_guard.py` — resolve JE links from Salary Slips **and** Journal Entry Account references; classify active/cancelled/missing; clear inactive slip links; row-lock Payroll Entry.
- `PayrollEntryWithAccountingDimensions.generate_accrual_journal_entry` — whitelist regenerate path (Draft via existing override).
- Guard inside `make_accrual_jv_entry` against duplicate active JEs.
- Desk button `Create Accrual Journal Entry` when Submitted and no active JE.
- `_active_jes` in `payroll_correction.py` now ignores cancelled/missing names.

Cancelled Journal Entries are preserved for audit. Payroll calculation / GL posting math is unchanged.

## Tests

`extentionhrms/tests/test_payroll_accrual_je_guard.py` — cancelled / draft / submitted / missing / multi-cancelled / cancelled+draft / cancelled+submitted / inactive link clear / concurrent prepare.

## Dev validation (Ordibehesht 1405)

| Check | Result |
|---|---|
| Payroll Entry | `اردیبهشت 1405` remains **Submitted** |
| Old JE `ACC-JV-1405-137987` | **Cancelled** (`docstatus=2`), preserved |
| Generation while Submitted JE existed | **Blocked** |
| New JE | `ACC-JV-1405-172501` **Draft** (`docstatus=0`), balanced 215,831,885,225 |
| Salary Slips linked to new JE | 240 / 240 |
| Second generation attempt | **Blocked** (active Draft) |
| Duplicate active JE | None |

No Production push/merge/deploy.
