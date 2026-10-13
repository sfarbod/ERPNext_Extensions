# Release 5.5.43 — Zero-FG STAGE_CO Equivalent-Unit Allocation

**Version:** 5.5.43  
**Date:** 2026-10-13  
**Previous:** 5.5.42  
**Branch:** `develop`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (unmodified Core)

**Validation scope:** Unit tests for Iran Accounting STAGE_CO + in-memory simulation of `MAT-STE-2026-41263` (no document mutation).  
**Production:** Not deployed by this release preparation. No automatic mutation of historical documents.

---

## A. Summary

1. **Zero MAIN_FG STAGE_CO support** — Manufacture Stock Entries with no finished-good row can still run equivalent-unit stage allocation when eligible Co-/By-Products (and co-product rejects) participate.
2. **Residual ownership** — With one MAIN_FG, residual still lands on MAIN_FG (unchanged). With zero MAIN_FG, residual lands on the highest equivalent-qty participant; ties break by stable row key (`name` → `idx` → identity).
3. **Fail closed** — Multiple MAIN_FG still blocked. Zero FG with only finance-excluded secondaries raises a clear validation error (no silent cost disappearance).
4. **Existing contracts preserved** — Product Reject bridge, SAME_ITEM_MULTI_FG, finance exclusion, component scrap, bulk scrap, and manufacture costing stamp `5.3.43` are unchanged.

---

## B. Problem

`allocate_stage_output_cost` required exactly one `MAIN_FG` whenever stage-participating Co-/By-Products existed. Removing the only finished-good row on a Job Card Manufacture entry (e.g. `MAT-STE-2026-41263` row 8 / `30100023`) failed Draft Save with:

> Equivalent-unit Manufacture costing requires exactly one finished good.

ERPNext Core Job Card validation allows zero FG; Iran STAGE_CO did not, because MAIN_FG was the hard-coded IRR residual sink.

---

## C. New Zero-FG Allocation Contract

| Case | Behavior |
|------|----------|
| **A — One MAIN_FG** | Identical to pre-5.5.43: proportional stage allocation; **MAIN_FG always owns residual** |
| **B — Zero MAIN_FG** | Eligible participating outputs share the pool via existing equivalent-unit factors; residual → max `equivalent_qty`, then stable key |
| **C — Zero MAIN_FG + no eligible stage output** | Fail closed: *Manufacturing has no eligible output to receive the production cost.* |
| **D — Multiple MAIN_FG** | Unchanged fail-closed for STAGE_CO |

Eligible participants (unchanged classification):

- `CO_PRODUCT` / By-Product / Additional Finished Good (not finance-excluded)
- `CO_PRODUCT_REJECT`
- `MAIN_PRODUCT_REJECT` when already on the stage list with participating co-products

Finance-excluded secondaries (`Valuation Rate` / Manual / `% of Component Cost`) never absorb the stage pool.

---

## D. Target Document Simulation

**Document:** `MAT-STE-2026-41263` (read-only; not saved)

| Scenario | FG | By-Product `30100096` | Pool | Result |
|----------|----|----------------------|------|--------|
| Original | 1 × `30100023` @ 243 | 14 000 ml @ 7 653 764 | 7 654 007 | Unchanged |
| In-memory remove row 8 | 0 | Receives **7 654 007** | 7 654 007 | STAGE_CO applies; value difference 0 |

No Stock Ledger / GL / document writes were performed by this release validation.

---

## E. Accounting Safeguards

- `SUM(stage output amounts) == material + operating stage pools` (IRR integer rounding)
- No silent cost drop when no eligible absorber
- No change to expense/exclusion rules for finance-excluded scrap or secondaries
- No Core ERPNext patches
- No historical SLE/GL repair

---

## F. Compatibility

| Contract | Status |
|----------|--------|
| STAGE_CO one-FG | Preserved |
| Product Reject bridge | Preserved (STAGE_CO does not claim without participating co) |
| SAME_ITEM_MULTI_FG / output family | Preserved |
| Manufacture costing stamp | Remains `5.3.43` (same allocator family; zero-FG is additive) |
| Manufacturing dimension guard | Untouched |

---

## G. Tests

Module: `erpnext_extensions/iran_accounting/tests/test_zero_fg_stage_allocation_v5543.py`

| ID | Coverage |
|----|----------|
| T1 | One FG + By-Product residual on FG |
| T2 | Zero FG + one By-Product absorbs 100% pool |
| T3–T4 | Zero FG multi By-/Co-Product proportional |
| T5 | Zero FG + Co-Product Reject joins stage |
| T6–T8 | Finance exclusion / no eligible → fail closed |
| T9 | Multi MAIN_FG still blocked |
| T10–T11 | IRR residual + deterministic tie-break |
| T12–T13 | Product Reject / Multi-FG non-regression |
| T14–T15 | Draft/submit consistency + idempotent re-validate |
| Target | `MAT-STE-2026-41263` zero-FG simulation |

Also re-run existing stage-output equivalent costing suite.

---

## H. Files

| Path | Change |
|------|--------|
| `erpnext_extensions/iran_accounting/manufacture_stage_costing.py` | Zero-FG residual owner + fail-closed message |
| `erpnext_extensions/iran_accounting/tests/test_zero_fg_stage_allocation_v5543.py` | New regression matrix |
| `erpnext_extensions/__init__.py` | `5.5.42` → `5.5.43` |
| `RELEASE_5_5_43.md` | This document |

---

## I. Out of Scope

- Automatic mutation of `MAT-STE-2026-41263`
- Bulk historical repair
- ERPNext Core changes
- New allocation-factor methodology
