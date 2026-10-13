# Release 5.5.43 — Zero-FG STAGE_CO + Rate-Authoritative Stock Adjustment

**Version:** 5.5.43  
**Date:** 2026-10-13  
**Previous:** 5.5.42  
**Branch:** `develop`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (unmodified Core)

**Validation scope:** Unit tests for Iran Accounting STAGE_CO (zero-FG, rate-authoritative composition, TYPE C Stock Adjustment, ledger determinism); development-site Cancel + Amend of `MAT-STE-2026-41263`.  
**Production:** Not deployed by this release preparation. No bulk historical repair. No new patches.

---

## A. Summary of Changes

1. **Zero MAIN_FG STAGE_CO** — Manufacture entries with no finished-good row can run equivalent-unit stage allocation when eligible Co-/By-Products (and co-product rejects) participate.
2. **Allocation-share ownership** — One MAIN_FG still receives the leftover allocation **share**. Zero MAIN_FG → highest equivalent-qty participant (stable-key tie-break). Multiple MAIN_FG still fail-closed.
3. **Rate-authoritative STAGE_CO composition** — Each stage output: `basic_rate = ROUND_HALF_UP(share / qty)`, `basic_amount = qty × basic_rate`. Hidden R2 amount residuals are **not** allowed on STAGE_CO outputs (FG, By-Product, Co-Product, eligible Scrap).
4. **Explicit Stock Adjustment (TYPE C)** — Pool leftover after rate composition becomes header `value_difference` and posts via native ERPNext Stock Adjustment. No artificial Additional Cost.
5. **IRR Ledger Determinism** — STAGE_CO rows must satisfy `basic_amount == transfer_qty × integer basic_rate`. Prior secondary R2 amount-authoritative exemption removed (stub always returns False). Non-STAGE_CO single-FG TYPE B amount residual unchanged.

---

## B. Root Cause

Zero-FG STAGE_CO previously absorbed the manufacturing pool into `basic_amount` (amount-first) and allowed an R2 representation residual (`|amount − qty×rate| ≤ qty/2`) so submit could pass without Stock Adjustment.

That left inventory `stock_value` unequal to qty×integer rate. Example target before correction:

| Leg | Old (wrong contract) | Required |
|-----|----------------------|----------|
| Outgoing | 7 654 007 | 7 654 007 |
| Incoming | **7 654 007** (pool-exact amount) | **7 658 000** (= 14 000 × 547) |
| Value difference | 0 | **+3 993** → Stock Adjustment |

---

## C. Rate-Authoritative Accounting Contract

| Rule | Behavior |
|------|----------|
| Allocation factors | Unchanged equivalent-unit shares |
| Row rate | `ROUND_HALF_UP(allocated_material / transfer_qty)` |
| Row amount | `transfer_qty × integer basic_rate` (exact) |
| Residual | Document `value_difference` → Stock Adjustment (TYPE C) |
| Bound | `floor(gcd(output_qtys) / 2)` |
| Additional Cost | Real operating cost only — never a rounding plug |
| Integer IRR | Rates and amounts must be whole IRR |

Expected GL for the target shape:

- Dr SFG Inventory **7 658 000**  
- Cr WIP Inventory **7 654 007**  
- Cr Stock Adjustment **3 993**

---

## D. Cancel + Amend Correction (development.localhost)

| Step | Result |
|------|--------|
| Original `MAT-STE-2026-41263` | Cancelled (`docstatus=2`); SLE cancelled |
| Amend `MAT-STE-2026-41263-1` | Draft (`docstatus=0`), `amended_from` = original |
| Draft SED By-Product `30100096` | rate **547**, amount **7 658 000**, VD **+3 993**, additional_costs **0** |
| Draft SLE / GL | **Not posted** until Submit |
| Bin Quarantine after cancel | qty **0**, value **0** (stock reversed) |

**Release-prep status:** Draft economics match the contract. Full SLE / GL / Bin / Stock Adjustment reconciliation requires **Submit** of `MAT-STE-2026-41263-1` on development (not performed during this release-note commit).

Standard ERPNext Repost / Iran post-repost alone were **not** used as the correction path (see Known Issues).

---

## E. Tests and Verification

| Suite | Result |
|-------|--------|
| `test_zero_fg_stage_allocation_v5543` (16) | OK |
| `test_stage_co_rate_authoritative_sa_v5543` (8) | OK |
| `test_stage_secondary_r2_ledger_v5543` (2) | OK |
| `test_stage_output_equivalent_costing` (33) | OK |
| `test_manufacture_irr_residual_v5334` (43) | OK |
| `test_ledger_contract_composition` (3) | OK |
| `test_manufacture_residual_submit_v5330` (14) | OK |
| `test_scrap_absorbed_costing` (33) | OK |

**152 unit tests passed** in the release regression set above.

---

## F. Known Issues and Limitations

### 1. Bin refresh after Iran SLE resync (open)

Iran `_reconcile_stock_entry_after_repost` can rewrite SED to qty×rate, resync SLE, and remake GL, but **does not refresh Bin** from the final SLE balance. Simulated repair left Bin stale at 7 654 007 while SLE showed 7 658 000.

**Status:** Known issue — **not fixed** in 5.5.43. Do not rely on Iran post-repost alone to correct submitted STAGE_CO vouchers.

### 2. TYPE C GL magnitude validation (open)

`assert_stock_entry_ledger_determinism` compares GL magnitude to `|Σ SLE signed net|`. For Manufacture with both legs + Stock Adjustment, net SLE equals `value_difference` (e.g. 3 993) while GL magnitude equals incoming (e.g. 7 658 000), so post-repost deterministic validate can **FAIL** even when SED/SLE/GL are accounting-correct.

**Status:** Known issue — **not fixed** in 5.5.43. `collect_ledger_contract_failures` / TYPE C composition paths remain the stricter STAGE_CO guards for submit.

### 3. Closed-plan stamps are in-memory

STAGE_CO ownership stamps do not survive `get_doc` reload. Post-repost align therefore rate-recomposes unprotected STAGE_CO rows (desired for the new contract) but must not be confused with a full `allocate_stage_output_cost` re-run.

### 4. Historical submitted vouchers

Already-submitted amount-first STAGE_CO documents are **not** auto-repaired by migrate. Correct case-by-case (Cancel + Amend preferred when safe).

---

## G. Compatibility

| Contract | Status |
|----------|--------|
| STAGE_CO one-FG / zero-FG / multi-output | Supported (rate-authoritative) |
| Product Reject bridge | Preserved |
| SAME_ITEM_MULTI_FG / output family | Preserved |
| TYPE A / B / C residual classifier | TYPE C extended for STAGE_CO |
| IRR Ledger Determinism | STAGE_CO must be qty×rate |
| Manufacture costing stamp | Remains `5.3.43` family |
| Manufacturing dimension guard | Untouched |

---

## H. Files

| Path | Change |
|------|--------|
| `erpnext_extensions/__init__.py` | Version **5.5.43** |
| `manufacture_stage_costing.py` | Zero-FG residual share + rate-authoritative `_apply_stage_row` |
| `domain/manufacture_irr_residual.py` | STAGE_CO TYPE C classification |
| `domain/stock_entry_ledger_contract.py` | Remove STAGE_CO R2 exemption |
| `tests/test_zero_fg_stage_allocation_v5543.py` | Zero-FG matrix |
| `tests/test_stage_co_rate_authoritative_sa_v5543.py` | SA / TYPE C / ledger suite |
| `tests/test_stage_secondary_r2_ledger_v5543.py` | R2 rejection negatives |
| `tests/test_stage_output_equivalent_costing.py` | Residual share + rate compose |
| `RELEASE_5_5_43.md` | This document |

---

## I. Migration / Deployment Notes

- **`patches.txt`:** No new patches for 5.5.43. No data migration script.
- **Deploy:** Install app update; restart workers as usual. No `bench migrate` patch required for this correction.
- **After deploy:** New/draft Manufacture STAGE_CO documents follow rate-authoritative + Stock Adjustment automatically.
- **Historical repair:** Optional, case-by-case Cancel + Amend (or approved repair tooling). Do not bulk-repost STAGE_CO to “fix” amounts until Known Issues #1–#2 are addressed.
- **Do not push from agents** — operator pushes `develop` when ready.
- Unrelated untracked `erpnext_extensions/scripts/cancelled_voucher_repair.py` is **not** part of this release.

---

## J. Out of Scope

- ERPNext Core modifications  
- Version bump beyond 5.5.43  
- Git tag / push  
- Production data changes  
- Bulk historical SLE/GL repair  
- Fixing Bin-refresh / TYPE C GL-magnitude known issues in this release commit  
