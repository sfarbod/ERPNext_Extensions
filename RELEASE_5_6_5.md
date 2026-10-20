# RELEASE 5.6.5 — Payroll Accrual IRR Precision

**App:** `erpnext_extensions`  
**Version:** `5.6.5`  
**Requires:** ERPNext ≥ 16.51.1 / Frappe ≥ 16.52.0 / HRMS ≥ 16.51.0  
**Verified on:** `development.localhost` only. Not deployed to Production.

---

## 1. What this release does

The custom Payroll Entry accrual Journal Entry builder used `frappe.get_precision("Journal Entry Account", …)` without account-currency context, which defaults to **2** decimals. On Journal Entry validate, IRR account-currency fields re-round to **0** (company currency number format when System Settings currency precision is empty).

When an accrual pair books **aggregated** expense rows and **per-employee** liability rows (e.g. ذخیره سنوات), that second rounding produced a debit/credit imbalance (observed: **18 IRR** on شهریور 1405).

This release:

- Resolves accounting precision from the company currency the same way Journal Entry Account fields do (IRR → **0** decimals).
- Quantizes emitted debit/credit rows **before** Round Off, preventing unintended double rounding.
- Preserves employee-level payable amounts and cost-center / department expense allocations.
- Uses the company Round Off account only for legitimate residuals — never to hide formula or mapping errors.
- Remains compatible with ERPNext / HRMS **v16** without modifying ERPNext or HRMS core.

---

## 2. Code changes

| File | Change |
|------|--------|
| `extentionhrms/payroll_entry_override.py` | `_currency_precision()` uses System Settings / currency number format (IRR → 0); accrual JE saved as Draft |
| `extentionhrms/payroll_accrual_grouping.py` | `_quantize_emitted_rows()` before `_append_round_off()` |
| `extentionhrms/tests/test_payroll_accrual_grouping.py` | Regression tests for fractional سنوات, per-employee liability, multi-CC, عیدی-style, insurance-style, precision 0 and 2, JE balance |

---

## 3. Site-level configuration (not deployed by Git merge)

Payroll data corrections and Salary Component / Salary Structure settings are **site-level** changes. Merging or installing this app version does **not** automatically apply them.

Apply on each site as needed:

| Change | Detail |
|--------|--------|
| ذخیره سنوات (هزینه) / (پرداختنی) | `round_to_the_nearest_integer = 1` (preserve employee-process flags) |
| ذخیره عیدی (هزینه) / (پرداختنی) | `round_to_the_nearest_integer = 1` |
| بیمه بیکاری سهم کارفرما پرداختنی (Salary Structure) | Align formula to `EmploymentInsurance` when the structure overrides the component master (expense side already includes `TRANSPORTE2`) |
| Server Script `محاسبه تسری سنوات` | Round `total_zakhire` with `int(round(...))` so Before Save does not overwrite component integer rounding |

---

## 4. Verified payroll recreation result (`development.localhost`)

Target Payroll Entry: **شهریور 1405**

| Item | Result |
|------|--------|
| Salary Slips | **267** submitted |
| Accrual Journal Entry | `ACC-JV-1405-169090` — Debit = Credit = **284,850,182,887** IRR, Difference = **0**, status = **Draft** |
| Net salary vs baseline | **−36,300,000** IRR — supplemental insurance deductions (`بیمه تکمیلی کارمند` + `بیمه تکمیلی افراد تحت تکفل`) for **HR-EMP-0540** and **HR-EMP-0542** |
| Acceptance | This net difference has been **accepted by the user** |
| Journal Entry | Remains **Draft** — not submitted by this release process |

---

## 5. Tests

```bash
bench --site development.localhost run-tests --app erpnext_extensions --module erpnext_extensions.extentionhrms.tests.test_payroll_accrual_grouping
# or
pytest apps/erpnext_extensions/erpnext_extensions/extentionhrms/tests/test_payroll_accrual_grouping.py -q
```

Expected: **60** tests passed.
