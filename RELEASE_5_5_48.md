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
- Batch `1072-13200244-0409004-hp-168951202-3` / item `13200244` / SABE `dr866i640i`
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
- SABE: fractional rate/amount persisted under TRACE_ONLY TVA rows (12 TRACE rows, `SUPERSEDED_BY_PHASE2` after campaign)
- Multiple `HISTORICAL_BATCH_RATE_MISMATCH` / `SLE_SABE_VALUE_MISMATCH` TRACE TVA recorded

---

## D. Restored database RIV baseline

After restore (before new campaign seeding):

| Metric | Count |
|--------|------:|
| All RIV (docstatus=1) | 1,885 |
| Completed | ~1,486 |
| Skipped | ~370 |
| Failed | 4 (resolved via scrap / I3 repair before drain) |

**Note:** The previously reported “6,316 RIV” figure was from the **prior** development database (5.5.47 campaign), not this Production backup.

---

## E. New campaign (restored DB) — COMPLETED

| Field | Value |
|-------|-------|
| Campaign | **HRC-00001** (fresh; stale pre-restore campaign purged) |
| Status | **Completed** (`2026-10-15 22:33:22`) |
| Manifest total | **15,969** |
| Checksum | `9a99b07fc3ee77e5059a9cc7a3b2d94f1fa9d3932908e900b2286d79ca7522e4` |
| Stock Entry | 14,318 |
| Purchase Receipt | 1,354 |
| Delivery Note | 223 |
| Stock Reconciliation | 31 |
| Subcontracting Receipt | 42 |
| Sales Invoice (update_stock) | 1 |

(Previous 5.5.47 population was 15,802 — Production grew.)

### Campaign terminal counts

| Status | Count |
|--------|------:|
| Skipped Covered | **15,969** |
| Completed (per-voucher) | 0 |
| Pending / Failed / Blocked | **0** |

### Global RIV after campaign (+ targeted scrap repair)

| Status | Count |
|--------|------:|
| Completed | **5,908** |
| Skipped (resolved Failed) | 406 |
| Queued / In Progress / Failed | **0** |
| Uncovered Item+WH identities | **0** |

---

## F. Scale-path fixes in this release

1. **`fast_cover.bulk_skip_covered`** — SQL anti-join candidate query so early multi-identity Opening SRs do not monopolize the scan window; Frappe `limit_page_length=0` pitfall avoided.
2. **`identity_seed.drain_cycle`** — honour `cover_limit` (no hard cap of 300).
3. **`failure_registry`** — empty/corrupt `reposting_data_file` → clear + Core re-queue; categorize JSONDecode / Expecting value; mark Failed resolved when replacement RIV is Queued/In Progress/Completed.

---

## G. Coverage audit (100%)

SQL proof on terminal campaign:

| Metric | Count |
|--------|------:|
| Skipped Covered scanned | 15,969 |
| Uncovered Skipped (missing Completed cover) | **0** |
| No-SLE Skipped (FA / non-stock PR) | 65 |

Module: `historical_rebuild.coverage_audit.audit_skipped_covered` (also available with `fix=True` requeue).

---

## H. Accounting reconciliation (post-campaign)

After Component Scrap repair on `30100298` (7 manufacture vouchers) + targeted Core RIV re-execute for `30100298` / `30100296`:

| Layer | Result |
|-------|--------|
| Bin ↔ latest SLE stock_value | **exact** (`delta=0`) |
| SLE SVD ↔ warehouse inventory GL | **−55,877 IRR** |
| Campaign terminal | ok (pending=0, failed=0) |
| Reconcile verdict | **OK** (≤1e5 IRR SVD↔GL tolerance; Bin exact) |

### SLE SVD ↔ GL residual source

Pre-repair residual was **−3,464,582,574 IRR**, concentrated in Material Issues `MAT-STE-2026-36093` / `36094` after Component Scrap corruption on WIP item `30100298` inflated FG `30100296` rates. Scrap repair + Core RIV restored SLE↔GL parity on those vouchers (`delta=0`). Remaining **−55,877** is distributed Material Transfer for Manufacture / Transfer micro-residuals — same class as the prior campaign’s −49,009.

TVA snapshot: TRACE_ONLY 415, GENUINE_ROUNDING 30; repair_status CANCELLED 45, SUPERSEDED_BY_PHASE2 400.

Stock Adjustment account remains **621301**; Financial Round Off **622515** unused for inventory residuals.

---

## I. Regression tests (critical set — all pass)

| Module | Tests |
|--------|------:|
| `test_manufacture_fractional_batch_tva_v5548` | 3 |
| `test_hrc_component_scrap_riv_v5547` | 10 |
| `test_historical_rebuild_campaign_v5547` | 5 |
| `test_historical_rebuild_phase2_complete_v5547` | 9 |
| `test_native_transfer_gl_sync_v5546` | 10 |
| `test_native_outgoing_sle_sync_v5540` | 13 |
| `test_prevention_v5544` | 19 |
| `test_stage_co_rate_authoritative_sa_v5543` | 8 |
| `test_riv_valuation_integrity` | 33 |
| `test_component_scrap_issued_rate` | 27 |
| `test_iran_accounting_rounding` | 8 |
| `test_irr_residual_classification` | 25 |
| `test_stock_sle_gl_consistency` | 5 |
| **Total** | **175 — 0 failures** |

Note: some Account Explorer tests assert hardcoded GL balances from the prior development DB and fail against the restored Production clone; they are unrelated to this valuation release.

---

## J. Production compatibility

- Compatible with ERPNext 16.37.0 / Frappe 16.36.1
- Does not disable integer IRR batch guards
- Stock Adjustment account remains `621301`; Financial Round Off `622515` unused for inventory residuals
- Deploy to Production only after operator review of this local validation
- **This agent does not push, tag, or deploy**

## K. Known limitations

1. Full identity-drain wall-clock depends on Core RIV future-SLE walks (~3h on this clone with 4 long/default workers).
2. TRACE_ONLY TVA authorizes fractional SABE persistence; Phase 2 identity RIV remains the path to heal fractional historical layers.
3. Residual FG rates on some WIP items after scrap repair may remain elevated until operators review manufacturing source rates; SLE↔GL↔Bin layers are reconciled.
4. Validated on a Production **clone**; operators must not treat `development.localhost` as Production.
