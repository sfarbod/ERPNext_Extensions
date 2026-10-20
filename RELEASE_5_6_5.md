# RELEASE 5.6.5 — Payroll Accrual IRR Precision

**App:** `erpnext_extensions`  
**Version:** `5.6.5`  
**Requires:** ERPNext ≥ 16.51.1 / Frappe ≥ 16.52.0 / HRMS ≥ 16.51.0  
**Verified on:** `development.localhost` only. Not deployed to Production.

---

## 1. What this release does

The custom Payroll Entry accrual Journal Entry builder used `frappe.get_precision("Journal Entry Account", …)` without account-currency context, which defaults to **2** decimals. On Journal Entry validate, IRR account-currency fields re-round to **0** (company currency number format when System Settings currency precision is empty).

When an accrual pair books **aggregated** expense rows and **per-employee** liability rows (e.g. ذخیره سنوات), that second rounding produced a debit/credit imbalance (observed: **18 IRR** on شهریور 1405).

This release resolves accounting precision from the company currency the same way Journal Entry Account fields do, quantizes emitted rows before round-off, and keeps the company Round Off account only for genuine residuals.

---

## 2. Code changes

| File | Change |
|------|--------|
| `extentionhrms/payroll_entry_override.py` | `_currency_precision()` uses System Settings / currency number format (IRR → 0) |
| `extentionhrms/payroll_accrual_grouping.py` | `_quantize_emitted_rows()` before `_append_round_off()` |
| `extentionhrms/tests/test_payroll_accrual_grouping.py` | Regression tests for fractional years, per-employee liability, multi-CC, عیدی-style, insurance-style, precision 0 and 2 |

---

## 3. Recommended Salary Component configuration (site data)

Not shipped as a patch; apply on each site as needed:

- `ذخیره سنوات (هزینه)` / `ذخیره سنوات (پرداختنی)`: `round_to_the_nearest_integer = 1`
- `ذخیره عیدی (هزینه)` / `ذخیره عیدی (پرداختنی)`: `round_to_the_nearest_integer = 1`
- Align بیمه بیکاری پرداختنی Salary Structure formula to `EmploymentInsurance` when the structure overrides the component master

---

## 4. Tests

```bash
bench --site development.localhost run-tests --app erpnext_extensions --module erpnext_extensions.extentionhrms.tests.test_payroll_accrual_grouping
# or
pytest apps/erpnext_extensions/erpnext_extensions/extentionhrms/tests/test_payroll_accrual_grouping.py -q
```
