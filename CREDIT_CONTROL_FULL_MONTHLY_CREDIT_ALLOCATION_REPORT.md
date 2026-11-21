# Credit Control: Full Monthly Credit Allocation Base

**App:** `erpnext_extensions` **5.8.5** (unchanged)  
**Site:** `development.localhost`  
**Mode:** Implement + verify + local commit only  
**Date:** 2026-11-21

## Overall: **PASS**

---

## 1. Previous allocation behavior

- Identification computed `labor_period_credit` / `overhead_period_credit` = gross GL credits (Monthly Credit).
- Identification also computed `labor_eligible` / `overhead_eligible` from LCV lines classified `eligible`.
- **`build_credit_allocation_plan` distributed `identification.labor_eligible` / `overhead_eligible`.**
- Remaining and consistency were measured against Eligible Credit.
- Unresolved LCV amounts (e.g. Non stock items, Corrective Operation Cost without flags) were **excluded** from the allocation pool.

## 2. New allocation behavior

- **Allocation base = Monthly Credit Amount** (`labor_period_credit` / `overhead_period_credit`).
- Eligible Credit remains **informational classification only** and does **not** reduce the pool.
- Unresolved / PA-linked / excluded classifications stay visible on Source rows but their amounts are included via the gross monthly GL total.
- Remaining = Monthly Credit − Allocated.
- Consistency = Allocated equals Monthly Credit (within IRR precision).
- Product Allocation Document Breakdown:  
  `unallocated_* = calculated_control − allocated_*_share` (negatives preserved; over-allocation warnings emitted).

## 3. Code paths and functions changed

| File | Change |
|---|---|
| `control_credit/allocate_credit.py` | Pool = `labor_period_credit` / `overhead_period_credit` |
| `control_credit/engine.py` | Remaining & consistency vs period credit; source contrib tracking; over-allocation detect/warn; return monthly fields |
| `control_credit/reporting.py` | Pool status keyed off Monthly Credit (`No Monthly Credit`) |
| `mfg_cost_control_credit_pool.py/.js` | Column labels: Allocation Base / Informational Eligible |
| `mfg_cost_control_credit_allocation.json/.js` | Field labels/descriptions; calculate alert shows allocated from monthly base |
| `persist.py` | Detail name length 16 (prior PK collision fix; required for large months) |
| Tests + Playwright | Updated/added for monthly base |

## 4. Treatment of Eligible Credit

- Still calculated and persisted (`*_eligible_credit`).
- Shown on form and Pool report as **Eligible Credit (Informational)**.
- **Not** used as `labor_pool` / `oh_pool`.

## 5. Treatment of unresolved sources

- Classification unchanged (`unresolved`, reasons preserved).
- Amounts included in Monthly Credit → distributed with batch/product shares.
- Example 1405-06: unresolved 60,544,572 IRR now inside Allocated 75,218,486,393.

## 6. PA duplicate-prevention strategy

| Mechanism | Status |
|---|---|
| One active Credit Allocation per company+period | Existing uniqueness validation |
| Recalc clears PA breakdown credit fields then re-applies | `apply_credit_shares_to_product_allocations` |
| No stacking on recalculate | Verified (idempotent totals; product row counts stable) |
| PA-linked LCV status | Informational only; **0** such lines in 1405-01…06 |
| Same source→target double apply | **Not observed**; stop condition not triggered |

## 7. Six-month before/after (Overhead)

| Period | Before Allocated | After Allocated | Monthly Credit | Eligible (info) | Unresolved |
|---|---:|---:|---:|---:|---:|
| 1405-01 | 0 | 0 | 0 | 0 | 0 |
| 1405-02 | 0 | 0 | 0 | 0 | 0 |
| 1405-03 | 351,288 | **351,300** | 351,300 | 351,288 | 12 |
| 1405-04 | 188 | **190** | 190 | 188 | 2 |
| 1405-05 | 6,971,596 | 6,971,596 | 6,971,596 | 6,971,596 | 0 |
| 1405-06 | 75,157,941,821 | **75,218,486,393** | 75,218,486,393 | 75,157,941,821 | 60,544,572 |

Labor: Monthly = Allocated = 0 for all six months.

## 8. Monthly Credit vs actual allocated reconciliation

| Period | New Allocation Base | Actual Allocated | Remaining | Product Σ | Batch Σ | Consistent |
|---|---:|---:|---:|---:|---:|---|
| 1405-01…06 | = Monthly | = Monthly | 0 | = Monthly | = Monthly | **PASS** |

Identity: `New Allocation Base = Monthly Credit Amount` — **PASS**  
`Actual Allocated Amount = Monthly Credit Amount` — **PASS** (IRR residual handled inside `_distribute_pool`)

## 9. Labor and Overhead / Product Allocation

Breakdown formula held for all months:

`Unallocated = Calculated Control − Allocated Share` (Labor & Overhead) — **PASS**

| Period | OH Control | OH Allocated Share | OH Unallocated |
|---|---:|---:|---:|
| 1405-03 | 147,473,724,938 | 351,300 | 147,473,373,638 |
| 1405-06 | 215,387,614,097 | 75,218,486,393 | 140,169,127,704 |

## 10. Over-allocation cases

Server-side scan after apply: **`over_allocation_count = 0`** for all six months on this dataset (allocated share did not exceed per-row calculated control).  
Infrastructure to warn without clamping is in place (`_detect_control_over_allocation` / `_emit_over_allocation_warnings`).

## 11. Backend test results

| Suite | Result |
|---|---|
| `test_control_credit_monthly_base_v585` (11) | **PASS** |
| `test_control_credit_allocation_v585` (11) | **PASS** |
| `test_control_credit_reporting_v585` (8) | **PASS** |

## 12. Playwright results and screenshots

| Suite | Result |
|---|---|
| Form-login legacy UI suite | **FAIL** (login page / Continue button — auth UI; not product logic) |
| API-cookie robust e2e (`/tmp/control_credit_monthly_base_e2e`) | **PASS 10/10** |

Evidence: `/tmp/control_credit_monthly_base_e2e/`  
Screenshots: workspace, credit list/form, pool report (Monthly 351,300 vs Eligible 351,288), batch×product, PA four fields, 1405-06 form, post-recalc.

Verified in browser:

- Labels “Overhead Monthly Credit (Allocation Base)” / “Eligible Credit (Informational)”
- Allocated = Monthly on 1405-03 and 1405-06
- Pool report columns
- PA breakdown four fields + Credit nav
- Recalc idempotent (no duplicate rows)

## 13. Known limitations

- Legacy Playwright login selectors fail on current Sign-In “Continue” UI; use API `sid` cookie for automation.
- Eligible vs unresolved classification rules unchanged; only pool selection changed.
- No PA-linked LCV rows in these six months — duplicate PA path exercised via clear/re-apply only.
- Excel export uses same report columns (labels updated); no separate export code path.

## 14. Commit hash and Git status

- Version remains **`5.8.5`**
- No push / tag / deploy
- Commit: see git log after local commit in this session

### Completion checklist

- [x] Monthly Credit Amount is allocation base  
- [x] Eligible Credit informational only  
- [x] Full monthly credits distributed with existing ratios  
- [x] No double allocation observed  
- [x] Four PA breakdown fields correct  
- [x] Six months recalculated and reconciled  
- [x] Backend tests pass  
- [x] Playwright (API-cookie) pass  
- [x] Report created  
- [x] Version 5.8.5  
- [x] Local commit only  
