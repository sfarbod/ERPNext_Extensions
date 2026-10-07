# erpnext_extensions 5.5.19

## Iran Accounting — Unified Manufacture Output Contract (Phase 1)

### Summary

1. **Architecture** — Introduce one Iran Manufacture accounting contract spine
   (`AllocationPlan` / `NormalizedOutputRow`) with multiple allocation
   strategies. Economic amounts are authoritative; IRR rates are representation
   only (R2 residuals never become Stock Adjustment).

2. **SAME_ITEM_MULTI_FG** — First new strategy on the unified spine. When a
   Manufacture has ≥2 MAIN_FG rows of the same item across different destination
   warehouses (Retain / Quarantine both remain MAIN_FG), allocate the
   allocatable material and operating pools by stock quantity with deterministic
   remainder. Closes the Core multi-FG rate bug class exemplified by
   MAT-STE-2026-40687 (fixture + dry-run only; no historical repair).

3. **Closed-plan protection** — After a strategy finalizes, later rate-first
   (`align_stock_entry_item_amounts`) and FG residual aligners must not reopen
   strategy-owned economics. STAGE_CO claims ownership so the 5.5.18 Co-Product
   residual guard remains and is complemented by closed-plan semantics. RIV
   post-calculate order converges to the same Multi-FG economics as submit.

4. **Preserved** — Existing STAGE_CO / CO_PRODUCT / CO_PRODUCT_REJECT,
   independent By-Product, Product Reject, and TYPE C (R3) economics are
   unchanged. No ERPNext Core patch. No historical SLE/GL/RIV mutation in this
   release.

### Scope

| Area | Change |
|------|--------|
| `manufacture_output_contract.py` | Spine, SAME_ITEM_MULTI_FG, finalizer, verifier, closed-plan |
| `scrap_costing.py` | Strategy priority + ownership claims |
| `domain/qty_rate_amount.py` | Skip rate-first rewrite for closed owned rows |
| `manufacture_rounding.py` | Closed-plan / STAGE_CO residual protect (keep 5.5.18 guard) |
| `domain/riv_valuation_guard.py` | Closed-plan polish after RIV align |
| Tests | `test_manufacture_output_contract_v5519` (U1–U25); CO residual env patches |

### Non-goals

- No repair of MAT-STE-2026-40687 (dry-run / in-memory only).
- No migration of STAGE_CO onto the new finalizer.
- No Multi-FG + Product Reject economics (BLOCK).
- Historical +116 SA GL artifact on 40687 remains a separate unexplained item.

### Tests

- New: `test_manufacture_output_contract_v5519`
- Existing green: CO residual 5.5.18, stage equivalent, by-product bridge,
  Product Reject residual, TYPE C / manufacture IRR residual, residual submit,
  manufacture rounding, scrap absorbed
