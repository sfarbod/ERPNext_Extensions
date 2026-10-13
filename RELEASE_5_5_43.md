# Release 5.5.43 — Zero-FG STAGE_CO + Rate-Authoritative Stock Adjustment

**Version:** 5.5.43  
**Date:** 2026-10-13  
**Previous:** 5.5.42  
**Branch:** `develop`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (unmodified Core)

**Validation scope:** Unit tests for Iran Accounting STAGE_CO rate-authoritative composition + TYPE C Stock Adjustment; development-site analysis of `MAT-STE-2026-41263`.  
**Production:** Not deployed by this release preparation. No bulk historical repair.

---

## A. Summary

1. **Zero MAIN_FG STAGE_CO support** — Manufacture Stock Entries with no finished-good row can still run equivalent-unit stage allocation when eligible Co-/By-Products (and co-product rejects) participate.
2. **Allocation-share ownership** — With one MAIN_FG, leftover **share** still lands on MAIN_FG. With zero MAIN_FG, leftover share lands on the highest equivalent-qty participant (stable-key tie-break).
3. **Rate-authoritative STAGE_CO composition** — Every stage output sets `basic_rate = ROUND_HALF_UP(share / qty)` and `basic_amount = qty × basic_rate`. Hidden R2 amount residuals are **not** allowed on STAGE_CO outputs (FG, By-/Co-Product, eligible Scrap).
4. **Explicit Stock Adjustment** — Pool leftover after rate composition becomes Stock Entry `value_difference` and posts via native ERPNext Stock Adjustment (TYPE C). No artificial Additional Cost.
5. **IRR Ledger Determinism** — STAGE_CO rows must satisfy `basic_amount == transfer_qty × integer basic_rate`. The prior secondary R2 amount-authoritative exemption is removed (stub always returns False).

---

## B. Problem

Zero-FG STAGE_CO previously absorbed the manufacturing pool into `basic_amount` (amount-authoritative) and allowed an R2 representation residual (`|amount − qty×rate| ≤ qty/2`) so submit could pass without Stock Adjustment.

That left inventory `stock_value` unequal to qty×integer rate (e.g. `MAT-STE-2026-41263`: amount 7 654 007 vs 14 000×547 = 7 658 000). Required contract now:

| Leg | Value |
|-----|--------|
| Outgoing | 7 654 007 |
| Incoming | 14 000 × 547 = **7 658 000** |
| Value difference | **+3 993** → Stock Adjustment |

Expected GL:

- Dr SFG Inventory 7 658 000  
- Cr WIP Inventory 7 654 007  
- Cr Stock Adjustment 3 993  

---

## C. STAGE_CO Rounding Contract

| Rule | Behavior |
|------|----------|
| Allocation factors | Unchanged equivalent-unit shares |
| Row rate | `ROUND_HALF_UP(allocated_material / transfer_qty)` |
| Row amount | `transfer_qty × integer basic_rate` (exact) |
| Residual | `outgoing − Σ composed basics` → header `value_difference` → Stock Adjustment |
| Additional Cost | Real operating cost only — never used as rounding plug |
| Ledger | No STAGE_CO R2 amount-authoritative exemption |

---

## D. Target Document (expected after correction)

**Document:** `MAT-STE-2026-41263` (submitted under prior amount-authoritative R2; see correction plan)

| Field | By-Product `30100096` |
|-------|------------------------|
| qty / transfer_qty | 14 000 |
| basic_rate / valuation_rate | **547** |
| basic_amount / amount | **7 658 000** |
| value_difference | **+3 993** |
| TYPE C residual | `7 654 007 − 7 658 000 = −3 993` (within bound 7 000) |

---

## E. Submitted-Document Correction Plan (development.localhost)

Do **not** mutate SLE/GL rows in place.

1. Take a fresh DB backup.  
2. Cancel `MAT-STE-2026-41263` (standard cancel → reverse SLE/GL).  
3. Amend / re-open as draft (or recreate from Job Card if amend is unavailable).  
4. Save draft under 5.5.43 code → stage allocate recomposes amount 7 658 000, `value_difference` 3 993.  
5. Validate ledger composition + TYPE C classifier.  
6. Submit; verify GL: SFG 7 658 000 / WIP 7 654 007 / SA 3 993.  
7. Confirm Bin Quarantine `stock_value == 7 658 000` and `valuation_rate == 547`.

No production mutation. No bulk repost.

---

## F. Accounting Safeguards

- Integer IRR rates and amounts  
- Exact qty×rate row composition on STAGE_CO  
- TYPE C residual bounded by `floor(gcd(qtys)/2)`  
- SLE mirrors SED.amount; GL driven by SLE SVD + native difference account  
- Non-STAGE_CO single-FG TYPE B amount-authoritative residual unchanged  
- Product Reject bridge / SAME_ITEM_MULTI_FG / finance exclusion preserved  

---

## G. Tests

1. `test_zero_fg_stage_allocation_v5543.py` — zero-FG matrix (rate-authoritative)  
2. `test_stage_co_rate_authoritative_sa_v5543.py` — target shape, TYPE C, ledger, negatives  
3. `test_stage_secondary_r2_ledger_v5543.py` — R2 exemption rejected  
4. Regression: stage equivalent costing, manufacture IRR residual TYPE C, ledger composition, residual submit  

---

## H. Files

| Path | Change |
|------|--------|
| `manufacture_stage_costing.py` | Rate-authoritative `_apply_stage_row` + header VD refresh |
| `domain/manufacture_irr_residual.py` | STAGE_CO TYPE C classification |
| `domain/stock_entry_ledger_contract.py` | Remove STAGE_CO R2 exemption |
| `tests/test_zero_fg_stage_allocation_v5543.py` | Expect qty×rate + SA residual |
| `tests/test_stage_co_rate_authoritative_sa_v5543.py` | New SA contract suite |
| `tests/test_stage_secondary_r2_ledger_v5543.py` | R2 rejection negatives |
| `tests/test_stage_output_equivalent_costing.py` | Residual share + rate compose |
| `erpnext_extensions/__init__.py` | Remains **5.5.43** (no bump) |
| `RELEASE_5_5_43.md` | This document |

---

## I. Out of Scope

- In-place SLE/GL mutation of submitted vouchers  
- Bulk historical repair / repost campaigns  
- ERPNext Core changes  
- Decimal IRR rates  
- Artificial Additional Cost plugs  
