# MFG Cost Control Credit Allocation — Implementation Report

**Version:** `erpnext_extensions 5.8.5`  
**DocType:** `MFG Cost Control Credit Allocation`  
**Environment:** `development.localhost` (isolated Dev)  
**Date:** 2026-11-21  
**Push / tag / deploy:** none  

---

## 1. Architecture and design

Credit-side Labor/Overhead Control allocation extends Finished Goods Costing without a parallel engine.

```text
GL Entry credits on resolved control accounts (211238 labor / 211237 overhead)
  → line-level Landed Cost Taxes and Charges attribution
  → exclude amounts linked to MFG Cost Product Allocation (PA <id> exists)
  → eligible operating-cost lines remain allocatable
  → distribute using applied_batch_share from MFG Cost Batch Allocation
       (same residual-to-largest policy as allocate_pools_to_batches /
        MFG Cost Batch Expense Allocation)
  → product_share → document qty split (build_document_breakdown)
  → write allocated_* / unallocated_* on Document Account Control Breakdown
```

No GL / SLE / SED / SABE / RIV mutations.

### Reused components

| Component | Reuse |
|---|---|
| `document_breakdown.classify_account_control` | Account kind |
| `allocate._money` / residual policy | Via `_distribute_pool` mirroring `allocate_pools_to_batches` |
| `MFG Cost Batch Allocation.applied_share` | Allocation ratios |
| `MFG Cost Product Allocation.product_share` | Product split |
| `build_document_breakdown` | Source-document qty split |
| `resolve_persian_month` | Period bounds |
| Costing permissions / workspace | Desk UX |

---

## 2. New and modified files

### New

- `iran_accounting/fg_cost_allocation/control_credit/` (`accounts`, `identify`, `allocate_credit`, `engine`, `integrate_pa`)
- DocTypes: `MFG Cost Control Credit Allocation` + Source / Batch Row / Product Row children
- `iran_accounting/tests/test_control_credit_allocation_v585.py`
- `iran_accounting/fg_cost_allocation/e2e/playwright_control_credit_v585.py`
- This report

### Modified

- `MFG Cost Product Allocation Document Breakdown` — four fields
- `MFG Cost Product Allocation` — four total fields
- `export_costing.py` — Excel columns
- `persist.py` / `refresh_product_document_breakdown.py` — defaults
- `mfg_cost_product_allocation.js` — grid/footer
- Costing workspace link
- `__version__` → `5.8.5`

---

## 3. DocType / field definitions

**Parent:** `MFG Cost Control Credit Allocation` (submittable)  
Unique active `(company, persian_year, persian_month)`.

**Children:** Sources (GL + landed-cost line), Batch Rows (shares), Product Rows (PA + source document).

**Document Breakdown additions:**

| Field | Label |
|---|---|
| `allocated_labor_share` | سهم تخصیص‌یافته دستمزد |
| `allocated_overhead_share` | سهم تخصیص‌یافته سربار |
| `unallocated_labor` | تخصیص‌نیافته دستمزد |
| `unallocated_overhead` | تخصیص‌نیافته سربار |

`Unallocated = Calculated Control − Allocated Share` (row-level).

---

## 4. Allocation methodology (Batch Expense reuse)

For each eligible pool amount (labor / overhead independently):

`allocated_amount = pool × applied_batch_share`

When Σ applied shares ≈ 1, currency residual absorbs into largest normalized-qty batch — same rule as `allocate_pools_to_batches`.

---

## 5. Source credit identification rules

1. `SUM(GL Entry.credit)` per control account in Persian month (not balance / not net).
2. Attribute each credit to `Landed Cost Taxes and Charges` lines on the voucher with matching `expense_account`.
3. **Consumed by Product Allocation** when description contains `PA <name>` and that Product Allocation exists (line-level; voucher not wholly dropped).
4. **Eligible** when `has_operating_cost` / `operation_id` / `operating_component`.
5. **Unresolved** otherwise (including GL↔line amount mismatch, Non stock items) — visible; blocks submit.
6. Prior active credit allocations consume remaining via source child ledger.

Control accounts resolved via classifiers (`211237` overhead / `211238` labor), not free-form hardcodes alone.

---

## 6. Historical monthly results (Dev site)

Source JSON: `/tmp/control_credit_six_month_results.json`

| Month | Labor Period Credit | OH Period Credit | Labor Eligible | OH Eligible | Labor Allocated | OH Allocated | Unresolved # | Recon Δ |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1405-01 | 109,996,234,008 | 21,138,998,816 | 0 | 0 | 0 | 0 | 0 | 0 |
| 1405-02 | 19,291,356,217 | 9,822,311,033 | 0 | 0 | 0 | 0 | 0 | 0 |
| 1405-03 | 3,809,378,396 | 3,251,048,853 | 0 | **351,288** | 0 | **351,288** | 4 | 0 |
| 1405-04 | 143,653,660 | 82,832,036 | 0 | 188 | 0 | 188 | 1 | 0 |
| 1405-05 | 19,837,412,599 | 24,151,389,850 | 0 | 6,971,596 | 0 | 6,971,596 | 0 | 0 |
| 1405-06 | 11,476,641,174 | 84,610,843,305 | 0 | 75,157,941,821 | 0 | 75,157,941,821 | 25 | 0 |

Notes:

- Farvardin/Ordibehesht have zero eligible credits after PA attribution (matches business expectation).
- Khordad OH eligible **351,288** matches story example (~351,300) with 12 IRR Non-stock unresolved.
- Labor eligible is **0** on this Dev dataset for all six months (all 211238 credits are PA-consumed).
- Documents left in **Calculated** (not Submitted) where unresolved sources remain.
- Original backup `20261121_095630-…sql.gz` was **not** restored/overwritten; figures are from current Dev.

---

## 7. Reconciliation evidence

- `labor_allocated == labor_eligible` and `overhead_allocated == overhead_eligible` for all six months (`reconciliation_difference_* = 0`, `is_consistent = 1`).
- Khordad: 72 Product Allocation breakdown rows received `allocated_overhead_share > 0`.
- Idempotent recalculation verified on Farvardin (unit test).

---

## 8. Automated tests

| Suite | Result |
|---|---|
| `test_control_credit_allocation_v585.py` | **11 passed**, 0 failed, 0 skipped |
| `test_document_account_control_breakdown_v5755.py` (regression) | **9 passed** |

Executed via site-connected `unittest` runner (FrappeTestCase fixture bootstrap is broken on this site for unrelated UOM duplicates).

---

## 9. Playwright E2E

Script: `e2e/playwright_control_credit_v585.py`  
Evidence: `/tmp/control_credit_v585/ui/` (`01`…`06` screenshots + `results.json`)

| Check | Result |
|---|---|
| credit_list | PASS |
| credit_form_khordad (sources/batches/products, OH eligible 351,288) | PASS |
| zero_credit_month (1405-01) | PASS |
| pa_breakdown_four_fields visible | PASS |
| batch_expense_regression | PASS |
| credit_new_form | PASS |

**Totals: 6 passed / 0 failed** (browser executed).

Excel toolbar button text probe returned false (label mismatch); server export API columns were updated and are covered by code review — **not claimed as a separate Playwright Excel download PASS**.

---

## 10. Known limitations

1. Dev dataset ≠ untouched backup; Phase C restore to sandbox still recommended for formal historical sign-off.
2. Labor control credits are fully PA-consumed on Dev — no labor credit allocation rows generated.
3. Unresolved sources (Non stock / GL residual) block submit until classified.
4. Playwright Excel download click not verified end-to-end.

---

## 11. Git

See commit message after local commit. Version remains **5.8.5**. No push/tag/deploy.
