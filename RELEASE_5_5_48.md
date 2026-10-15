# Release 5.5.48 — Fractional Manufacture Batch TRACE + Fresh Historical Rebuild

**Version:** 5.5.48  
**Date:** 2026-10-15  
**Previous:** 5.5.47  
**Branch:** `feature/5.5.48-fractional-batch-historical-rebuild` → merge `develop`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0**, Iran Accounting **5.5.48**  
**Site:** `development.localhost` only — **no push**, **no tag**, **no Production deploy**

**Production backup restored:** `20261015_190819-erp_espadpharmed_com-database.sql.gz`  
**Pre-restore local backup:** `20261015_193332-development_localhost-database.sql.gz`  
**Prior HRC-00001 (5.5.47) evidence preserved under:**  
`sites/development.localhost/private/files/hrc_5547_evidence/`

---

## A. Objective

1. Restore latest Production backup into development.
2. Fix submit failure on Manufacture Stock Entry with historical fractional batch SABE.
3. Run a **fresh** full Historical Rebuild Campaign on the restored database.
4. Prove 100% Skipped Covered audit + accounting reconciliation.
5. Ship production-ready 5.5.48 locally on `develop`.

---

## B. Root cause — MAT-STE-2026-40972-1

**Document:** Manufacture Stock Entry (draft → submitted on development).

**Failing Serial and Batch Entry pattern:**
- Batch `1072-13200244-0409004-hp-168951202-3` / item `13200244`
- Core SABE rate ≈ `139357.32234007`, amount ≈ `-209593412.7994649`

**SED was already integer IRR** (`basic_rate=139357`, `amount=209592928` for qty 1504).

**Cause:** Historical warehouse SABE net for the batch is fractional (avg ≈ 136729.60…). Core batch valuation stamps that fractional average onto new SABE during submit. The IRR integer SABE guard correctly blocked persistence.

**Gap:** `classify_historical_fractional_batch_row` and `classify_observed_native_fractional_sabe` **excluded** Manufacture / Repack / Material Consumption for Manufacture, so the Temporary Valuation Adjustment TRACE bridge never authorized fractional SABE for manufacturing **consume** rows.

Manufacturing SLE movement remains SED-authoritative (integer). TRACE only authorizes persisting Core’s inherited fractional SABE layer — no Financial Round Off, no invented Stock Adjustment for TRACE_ONLY.

---

## C. Fix (smallest reusable)

In `temporary_valuation_adjustment.py`:

- Allow historical fractional-batch TRACE classify for Manufacture consume (`s_warehouse` set).
- Allow observed native fractional SABE TRACE for manufacturing purposes.
- Keep `classify_indivisible_ma_row` Manufacture exclusion (Class A Stock Adjustment bridge stays out of manufacturing TYPE A/B/C contracts).

Guard remains enabled — submit succeeds only with approved TRACE stamps.

**Verified on development.localhost:**
- `MAT-STE-2026-40972-1` docstatus=1
- SLE consume 13200244: svd=`-209592928` (integer), rate=`139357`
- SABE: fractional rate/amount persisted under TRACE_ONLY TVA rows
- Multiple `HISTORICAL_BATCH_RATE_MISMATCH` / `SLE_SABE_VALUE_MISMATCH` TRACE TVA recorded

---

## D. Restored database RIV baseline

After restore (before new campaign seeding):

| Metric | Count |
|--------|------:|
| All RIV (docstatus=1) | 1,885 |
| Completed | ~1,486 |
| Skipped | ~370 |
| Failed | 4 (3× 13200190 scrap WH + 1× 16400017 I3) |
| Queued / In Progress | variable |

**Note:** The previously reported “6,316 RIV” figure was from the **prior** development database (5.5.47 campaign), not this Production backup. Exact mapping is proven via the new campaign, not by equating RIV count to Stock Entry count.

---

## E. New campaign (restored DB)

| Field | Value |
|-------|-------|
| Campaign | **HRC-00001** (fresh; stale pre-restore campaign purged) |
| Manifest total | **15,969** |
| Checksum | `9a99b07fc3ee77e5059a9cc7a3b2d94f1fa9d3932908e900b2286d79ca7522e4` |
| Stock Entry | 14,318 |
| Purchase Receipt | 1,354 |
| Delivery Note | 223 |
| Stock Reconciliation | 31 |
| Subcontracting Receipt | 42 |
| Sales Invoice (update_stock) | 1 |

(Previous 5.5.47 population was 15,802 — Production grew.)

---

## F. Coverage audit

Module: `historical_rebuild.coverage_audit.audit_skipped_covered`

Scans **100%** of Skipped Covered items; requires `verify_voucher_basic` + Completed covering RIV per SLE identity. Invalid rows can be requeued to Pending (`fix=True`).

---

## G. Regression tests

- `test_manufacture_fractional_batch_tva_v5548` — Manufacture fractional batch TRACE classify
- Existing HRC / scrap / sticky-rate / integrity suites retained

---

## H. Production compatibility

- Compatible with ERPNext 16.37.0 / Frappe 16.36.1
- Does not disable integer IRR batch guards
- Stock Adjustment account remains `621301`; Financial Round Off `622515` unused for inventory residuals
- Deploy to Production only after operator review of this local validation

## I. Known limitations

1. Full identity-drain wall-clock depends on Core RIV future-SLE walks.
2. TRACE_ONLY TVA authorizes fractional SABE persistence; Phase 2 identity RIV remains the path to heal fractional historical layers.
3. This release was validated on a Production **clone**; operators must not treat development.localhost as Production.
