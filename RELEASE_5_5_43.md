# Release 5.5.43 — Zero-FG STAGE_CO Equivalent-Unit Allocation

**Version:** 5.5.43  
**Date:** 2026-10-13  
**Previous:** 5.5.42  
**Branch:** `develop`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (unmodified Core)

**Validation scope:** Unit tests for Iran Accounting STAGE_CO + R2 ledger recognition; controlled development-site validation of `MAT-STE-2026-41263`.  
**Production:** Not deployed by this release preparation. No bulk historical repair.

---

## A. Summary

1. **Zero MAIN_FG STAGE_CO support** — Manufacture Stock Entries with no finished-good row can still run equivalent-unit stage allocation when eligible Co-/By-Products (and co-product rejects) participate.
2. **Residual ownership** — With one MAIN_FG, residual still lands on MAIN_FG (unchanged). With zero MAIN_FG, residual lands on the highest equivalent-qty participant; ties break by stable row key (`name` → `idx` → identity).
3. **Fail closed** — Multiple MAIN_FG still blocked. Zero FG with only finance-excluded secondaries raises a clear validation error (no silent cost disappearance).
4. **R2 ledger recognition for STAGE_CO secondaries** — IRR Ledger Determinism accepts amount-authoritative `basic_amount` on eligible By-/Co-Product and Product-Reject scrap rows when the representational residual is within `r2_representation_bound(transfer_qty)` and `basic_rate == ROUND_HALF_UP(amount/qty)`.
5. **Existing contracts preserved** — Product Reject bridge, SAME_ITEM_MULTI_FG, finance exclusion, component scrap, bulk scrap, and manufacture costing stamp `5.3.43` are unchanged.

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

## D. Target Document

**Document:** `MAT-STE-2026-41263` (zero-FG after MAIN_FG removal)

| Field | By-Product `30100096` |
|-------|------------------------|
| qty / transfer_qty | 14 000 |
| basic_rate / valuation_rate | **547** (ROUND_HALF_UP) |
| basic_amount / amount | **7 654 007** (pool-exact) |
| R2 residual | `7 654 007 − 14 000×547 = −3 993` |
| R2 bound | `14 000 / 2 = 7 000` |
| Ledger composition | **PASS** (amount authoritative) |

No artificial Stock Adjustment or Additional Cost is introduced for this R2 residual.

---

## E. R2 Secondary Eligibility

Amount-authoritative when **all** hold:

1. Purpose `Manufacture` + v5.3.3+ manufacture costing contract (`uses_v533_contract`)
2. Incoming row classified `CO_PRODUCT` / `CO_PRODUCT_REJECT` / `MAIN_PRODUCT_REJECT`
3. Not finance-excluded
4. Integer IRR rates and amounts
5. `basic_rate == ROUND_HALF_UP(basic_amount / transfer_qty)`
6. `|basic_amount − transfer_qty × basic_rate| ≤ r2_representation_bound(transfer_qty)`

Durable after `get_doc` reload via classification fields (`secondary_item_type`, scrap family, stamped `custom_output_class`) — not in-memory ownership stamps alone.

**Excluded:** Component Scrap, Bulk Scrap, OTHER_OUTPUT, finance-excluded secondaries, non-Manufacture, forged HALF_UP rates, residuals beyond R2.

---

## F. Accounting Safeguards

- `SUM(stage output amounts) == material + operating stage pools` (IRR integer rounding)
- R2 is representation tolerance only — SLE/GL follow authoritative `amount`
- No silent cost drop when no eligible absorber
- No change to expense/exclusion rules for finance-excluded scrap or secondaries
- No Core ERPNext patches
- No bulk historical SLE/GL repair

---

## G. Compatibility

| Contract | Status |
|----------|--------|
| STAGE_CO one-FG | Preserved |
| STAGE_CO zero-FG | Supported |
| Product Reject bridge | Preserved |
| SAME_ITEM_MULTI_FG / output family | Preserved |
| IRR Ledger Determinism (integer rates) | Preserved; R2 exemption narrowed to eligible secondaries |
| Manufacture costing stamp | Remains `5.3.43` |
| Manufacturing dimension guard | Untouched |

---

## H. Tests

1. `test_zero_fg_stage_allocation_v5543.py` — zero-FG STAGE_CO matrix  
2. `test_stage_secondary_r2_ledger_v5543.py` — R2 ledger recognition / negatives  

Also re-run: stage-output equivalent costing, ledger composition, manufacture residual submit.

---

## I. Files

| Path | Change |
|------|--------|
| `erpnext_extensions/iran_accounting/manufacture_stage_costing.py` | Zero-FG residual owner + fail-closed message |
| `erpnext_extensions/iran_accounting/domain/stock_entry_ledger_contract.py` | STAGE_CO secondary R2 amount-authoritative gate |
| `erpnext_extensions/iran_accounting/tests/test_zero_fg_stage_allocation_v5543.py` | Zero-FG regression matrix |
| `erpnext_extensions/iran_accounting/tests/test_stage_secondary_r2_ledger_v5543.py` | R2 ledger regression matrix |
| `erpnext_extensions/__init__.py` | `5.5.42` → `5.5.43` |
| `RELEASE_5_5_43.md` | This document |

---

## J. Out of Scope

- Bulk historical repair / repost campaigns
- ERPNext Core changes
- New allocation-factor methodology
- Decimal IRR rates
