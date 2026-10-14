# Release 5.5.46 — Native Transfer GL Synchronization

**Version:** 5.5.46  
**Date:** 2026-10-14  
**Previous:** 5.5.45  
**Branch:** `hotfix/5.5.46-native-transfer-gl-sync`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (unmodified Core), Iran Accounting **5.5.46**  
**Site:** `development.localhost` only — **no production**, **no push**, **no tag**

**Backup:** `20261014_151727-development_localhost-database.sql.gz`

---

## A. Root cause

Material Transfer for Manufacture (incl. returns) with **inter-account** warehouses:

1. Core `process_sle` + Iran native-outgoing sync write authoritative BatchNoValuation / SABB amounts onto SED **in the DB**.
2. `make_gl_entries` → balanced-transfer GL still consumed **stale in-memory** SED (draft integer qty×rate).
3. Post-submit ledger contract compared posted GL to refreshed SED / native Σ → **IRR Ledger Determinism** fail (`GL magnitude != expected transfer GL`).

v5.5.45 TVA Class A does not rebuild GL for transfer rows (`s_warehouse` + `t_warehouse`), so the magnitude gap was never healed.

Secondary blocker: warehouse SABE qty for shared batch `5631-13100023-240508` nets **negative** (historical corruption). Pre-submit fractional TVA classify required `bal_qty > 0`, so fractional SABE was never authorized and post-submit prevention rejected native BatchNoValuation rates.

Example: batch rate `659957.263789869` vs document `659957` → 1400 qty yields **+369 IRR** native movement vs integer SED.

---

## B. Fix (minimal)

### B.1 SED → GL memory sync

Before Stock Entry `get_gl_entries` in the Iran `make_gl_entries` wrapper:

1. `sync_native_outgoing_sed_into_memory_for_gl(doc)`
2. Reloads SED amounts/rates from DB for native-outgoing purposes (Issue / Transfer / MTfM / Send to Subcontractor).
3. Realigns zero-value transfer header totals.

Inventory GL then posts the **authoritative native movement**. No blind GL reverse/remake.

### B.2 Fractional SABE authorization (TRACE)

1. `resolve_fractional_batch_pool`: when warehouse SABE net qty ≤ 0 but Bin covers the move, fall back to **inward-positive SABE average** so `HISTORICAL_BATCH_RATE_MISMATCH` TRACE can stamp.
2. `classify_observed_native_fractional_sabe`: after `process_sle`, authorize TRACE when Core persisted fractional SABE (idempotent; no Stock Adjustment GL).

---

## C. Accounting behavior

| Situation | Treatment |
|-----------|-----------|
| Inter-account transfer; native ≠ draft integer SED | Sync SED + GL to **native** magnitude |
| Same-account ZVT | TRACE_ONLY unchanged (no invented P&L) |
| Genuine Class A residual after sync (Issue/Receipt) | Stock Adjustment **621301** (v5.5.45 policy) |
| Financial documents | Round Off **622515** unchanged |
| Integer draft rate ≠ fractional native batch rate, GL already native | **No** Stock Adjustment |

---

## D. Five-document evidence (development.localhost)

All five submitted successfully after backup. Work Orders temporarily set Completed for Core return gate, then restored to In Process.

| Document | Posting date | Qty (13100023) | SED Σ | SLE +leg | GL mag | Dr 111620 | Cr 111701 | SA | RO | TVA |
|----------|--------------|----------------|-------|----------|--------|-----------|-----------|----|----|-----|
| MAT-STE-2026-41559 | 2026-10-13 | 1400 | 1207278169 | 1207278169 | 1207278169 | 1207278169 | 1207278169 | 0 | 0 | TRACE (HISTORICAL_BATCH + SLE_SABE 369) |
| MAT-STE-2026-41462 | 2026-10-13 | 1000 | 912555764 | 912555764 | 912555764 | 912555764 | 912555764 | 0 | 0 | TRACE (HISTORICAL_BATCH + SLE_SABE 264) |
| MAT-STE-2026-41504 | 2026-10-13 | 1400 | 1239630169 | 1239630169 | 1239630169 | 1239630169 | 1239630169 | 0 | 0 | TRACE (HISTORICAL_BATCH + SLE_SABE 369) |
| MAT-STE-2026-41497 | 2026-10-13 | 1000 | 808457264 | 808457264 | 808457264 | 808457264 | 808457264 | 0 | 0 | TRACE (HISTORICAL_BATCH + SLE_SABE 264) |
| MAT-STE-2026-41447 | 2026-10-12 | 900 | 736967037 | 736967037 | 736967037 | 736967037 | 736967037 | 0 | 0 | TRACE (HISTORICAL_BATCH + SLE_SABE 237) |

Shared batch: `5631-13100023-240508`. Native transfer amounts match predicted targets. GL balanced. SLE net = 0. No Financial Round Off. No Stock Adjustment GL.

---

## E. Regression

Automated (all OK):

- `test_prevention_v5544` (19)
- `test_irr_residual_classification` (25)
- `test_irr_rounding_residual` (19)
- `test_gl_cost_center_allocation_irr` (2)
- `test_native_transfer_gl_sync_v5546` (10) — native SED refresh, cross-account magnitude, fractional pool fallback, observed SABE TRACE, idempotent refresh

Prior 5.5.45 evidence (not cancelled):

- MAT-STE-2026-41402 — submitted; TRACE_ONLY TVA retained
- MAT-STE-2026-41492 — submitted; Class A Stock Adjustment TVA retained
- MAT-STE-2026-41477 / 41489 / 41500 — prior campaign docs left untouched (41500 remains draft)

---

## F. Phase 2 compatibility

- No historical Full Repost
- No permanent manual valuation overrides
- TVA `hotfix_version=5.5.46` for new bridge rows; prior 5.5.45 rows remain discoverable
- Native batch layers and posting chronology preserved for future rebuild
- Corrupt warehouse SABE nets remain Phase 2 rebuild targets

---

## G. Known limitations

1. Historical fractional batch dust / negative warehouse SABE nets remain until Phase 2 rebuild.
2. Transfer-row Class A residual GL is still not posted via TVA (not required when GL already carries native magnitude).
3. Ordinary Core RIV alone cannot repair corrupt source SED / manufacturing history.
4. Core Work Order return gate still requires Completed/Closed for component returns (ops constraint; not changed).

---

## H. Final verdict

**HOTFIX_READY** (development.localhost)
