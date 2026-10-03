# Release 5.4.0 — ERPNext 16.37.0 / Frappe 16.36.1 compatibility

## Summary

- **Compatibility:** Explicit allow-lists extended for **ERPNext 16.36.x–16.37.x** and **Frappe 16.35.x–16.36.x** after source-level audit and Development canaries.
- **UVR fingerprint:** `BuyingController.update_valuation_rate` gained an alternate digest for 16.36+/16.37 (`get_internal_transfer_qty` for internal PR). Regional stub + RIV + Pattern A fingerprints unchanged.
- **Accounting policy:** No change. `MANUFACTURE_COSTING_CONTRACT_VERSION` remains **5.3.43**.
- **App version** `5.4.0` is a compatibility release only.

## Compatibility stack (tested)

| Component | Version |
|-----------|---------|
| ERPNext | **16.37.0** |
| Frappe | **16.36.1** |
| HRMS | **16.16.0** |
| erpnext_extensions | **5.4.0** |

## Supported version guard (NEW)

| Guard | OLD | NEW |
|-------|-----|-----|
| ERPNext major.minor | `16.29`–`16.35` | `16.29`–`16.37` |
| Frappe major.minor | `16.29`–`16.34` | `16.29`–`16.36` |

- **Tested / reviewed:** ERPNext **16.35.0** (prior), ERPNext **16.37.0** (this release) with Frappe **16.36.1**.
- **Untested but allow-listed by fingerprint continuity:** ERPNext **16.36.x** (UVR body matches 16.37 digest; RIV/Pattern A identical).
- **Blocked:** ERPNext **16.38+**, Frappe **16.37+**, lower than **16.29**, malformed versions (fail-closed).

## Guard policy why

Opening the guard is not a blind bump: RIV recalculate / update_rate / SABB fingerprints match 16.35; Pattern A `make_precision_loss_gl_entry` digest unchanged; UVR still terminates in `update_regional_item_valuation_rate` with a measured alternate sha for the internal-PR qty helper. StockEntry.validate pre-Core bridge lifecycle remains valid.

## Iran Accounting / Manufacture costing

| Domain | Status |
|--------|--------|
| Stage-equivalent Co-/By-Product (v5.3.35) | COMPATIBLE — C01 PO-JOB07352 |
| Late Product Reject pre-Core bridge (v5.3.37) | COMPATIBLE — C02 PO-JOB10094 draft + submit SLE/GL |
| Type B residual (MAT-STE-2026-37736) | COMPATIBLE — ledger contract + GL balanced |
| Type C residual → Stock Adjustment (MAT-STE-2026-37762) | COMPATIBLE — SA GL present, contract ok |
| Component Scrap source-side issued rate | COMPATIBLE — unit suite OK |
| Whole-IRR / stamp-aware historical | COMPATIBLE — stamps preserved; no economic-policy change |
| UVR / RIV / Pattern A monkey patches | COMPATIBLE — allow-list + fingerprints |

## Monkey-patch audit result

All active UVR / RIV / Pattern A / StockEntry pre-Core bridge patches install once on a fresh process against ERPNext 16.37.0 / Frappe 16.36.1. Re-apply is idempotent.

## Migration result

`bench migrate` on Development against the target stack completed successfully (fixtures / Custom Fields / Property Setters / patches). No schema errors. Search-index rebuild queued (benign).

## Critical canaries (Development, rollback-controlled)

| ID | Case | Result |
|----|------|--------|
| C01 | PO-JOB07352 stage By-Product | PASS — residual 0, stamp 5.3.43, baseline unchanged |
| C02 | PO-JOB10094 Product Reject | PASS — pool 694,406,200; FG 933×738,730; Reject 7×738,730; SA 0; GL balanced; SLE exact |
| C03 | MAT-STE-2026-37736 Type B | PASS — GL balanced; no SA policy |
| C04 | MAT-STE-2026-37762 Type C | PASS — GL balanced; Stock Adjustment present |
| C05 | Component Scrap | PASS — suite |
| C06 | Standard Manufacture path | PASS — validate bridge present |

## Automated tests (targeted)

| Suite | Result |
|-------|--------|
| `test_uvr_regional_guard` | PASS (30, 1 skipped) |
| `test_riv_rate_first_preserve` | PASS (19) |
| `test_fx_pi_pattern_a_precision_loss_guard` | PASS (20, 6 skipped) |
| `test_product_reject_bridge_v5337` | PASS (40) |
| `test_stage_equivalent_byproduct_bridge_v5335` | PASS (40) |
| `test_component_scrap_issued_rate` | PASS (27) |
| `test_manufacture_irr_residual_v5334` | PASS (43) |

Integration fixtures temporarily relax site `Buying Settings.po_required` / `pr_required` (site config `Yes`; Core API unchanged 16.35→16.37).

## Playwright

| Suite | Result |
|-------|--------|
| `manufacture-stage-byproduct-v5335.spec.ts` | **PASS** |
| `manufacture-product-reject-v5337.spec.ts` | **PASS** |

Expectations updated to current contract stamp **5.3.43** and destination-warehouse-agnostic Product Reject WH (Quarantine vs scrap).

## Known limitations

- ERPNext **16.38+** and Frappe **16.37+** remain **blocked** until re-audited.
- Costing contract stamp is **not** bumped to 5.4.0 (no economic-policy change).
- ERPNext **16.36.x** is allow-listed via fingerprint continuity with 16.37.0 but was not a separate live canary target on this bench.

## Files

- `erpnext_extensions/__init__.py` — version `5.4.0`
- `iran_accounting/domain/riv_rate_guard.py`
- `iran_accounting/domain/uvr_regional_guard.py`
- `iran_accounting/integration/monkey_patches.py` (comments)
- `iran_accounting/tests/test_riv_rate_first_preserve.py`
- `iran_accounting/tests/test_uvr_regional_guard.py`
- `iran_accounting/tests/test_fx_pi_pattern_a_precision_loss_guard.py`
- `RELEASE_5_4_0.md`
