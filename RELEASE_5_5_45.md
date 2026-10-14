# Release 5.5.45 — Temporary Stock Adjustment Bridge (Phase 1)

**Version:** 5.5.45  
**Date:** 2026-10-14  
**Previous:** 5.5.44  
**Branch:** `hotfix/5.5.45-temporary-stock-adjustment-bridge`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (unmodified Core), Iran Accounting **5.5.44 → 5.5.45**  
**Site:** `development.localhost` only — **no production**, **no push**, **no tag**

**Backup:** `20261014_083123-development_localhost-database.sql.gz`  
(path: `sites/development.localhost/private/backups/`)

---

## A. Business objective

Warehouse operations were blocked because Stock Entries could not submit due to **historical valuation inconsistencies**. Phase 1 allows submission now by:

1. Detecting eligible valuation gaps (general rules — **no voucher hardcoding**).
2. Calculating the exact signed delta from authoritative values.
3. Posting Class A rate×qty residuals via Company **Round Off** (existing Iran contract) after native SLE↔SED sync.
4. Recording all temporary adjustments in DocType **Temporary Valuation Adjustment** for Phase 2 Full Historical Rebuild.
5. Narrowly authorizing fractional SABE only when stamped for historical batch / indivisible MA cases.

Phase 2 will reconstruct historical rates/SLE/SABE/GL. Phase 1 does **not** reverse adjustments automatically.

---

## B. Root causes (mandatory examples)

| Document | Purpose | Original blocker | Root cause |
|----------|---------|------------------|------------|
| MAT-STE-2026-41402 | Material Transfer | `StockEntryPreventionError` indivisible MA remainder 110.526 | Historical fractional batch pool on full deplete (batch 6163-20100094-…) |
| MAT-STE-2026-41489 | Material Issue | Ledger contract: missing Round Off GL (net −1) | Native SLE sync creates Class A residual after GL already posted |
| MAT-STE-2026-41492 | Material Issue | Composition `basic_amount ≠ qty×rate` + missing Round Off (−2) | Historical MA / zero-value poison → native SLE amount authoritative; R2 residual |
| MAT-STE-2026-41477 | Material Issue | Same as 41492 (−2) | Same Class A timing gap |

Additional real draft (general bridge): **MAT-STE-2026-41500** (Material Issue, 20 rows) — historical fractional batch rates → fractional SABE guard; bridged via `HISTORICAL_BATCH_RATE_MISMATCH`.

---

## C. Hotfix behavior

### Classification

| Kind | Meaning |
|------|---------|
| `NORMAL_TRANSACTION` | No bridge |
| `TEMPORARY_VALUATION_ADJUSTMENT` | Eligible historical valuation gap |
| `UNSUPPORTED_OR_UNSAFE_TRANSACTION` | Original validation still fails (qty/batch/negative stock/lost rate/etc.) |

### Reason codes (verified only)

- `HISTORICAL_VALUATION_MISMATCH`
- `INDIVISIBLE_MOVING_AVERAGE`
- `HISTORICAL_BATCH_RATE_MISMATCH`
- `SED_SLE_VALUE_MISMATCH`
- `SLE_SABE_VALUE_MISMATCH`
- `HISTORICAL_ZERO_VALUE_STOCK`
- `SOURCE_RATE_MISMATCH`
- `MANUFACTURING_VALUATION_RESIDUAL`
- `TRANSFER_VALUATION_RESIDUAL`

### Insertion points

1. **before_validate / validate** — early `prepare_before_submit_bridge` (Frappe sets `docstatus=1` before `before_submit`).
2. **before_submit prevention** — indivisible MA → stamp TVA + allow fractional SABE (requires Company Stock Adjustment Account).
3. **SABE monkey patch** — skip integer fractional throw when TVA allows; late reclassify if needed.
4. **on_submit after SED sync** — remake Round Off GL when Class A residual READY but missing; persist TVA rows.
5. **ledger contract** — native outgoing R2 amount-authoritative composition; skip fractional SLE/SABE checks when TVA fractional authorized.
6. **on_cancel** — mark TVA `repair_status=CANCELLED` (no automatic SA reversal in Phase 1).

### Accounting treatment

| Residual class | GL treatment | TVA `posting_mode` |
|----------------|--------------|--------------------|
| Class A amount vs qty×integer_rate (Issue/Receipt/…) | Company **Round Off** via `rebuild_irr_rate_rounding_residual_after_repost` | `ROUND_OFF_GL` |
| Indivisible MA / historical fractional batch on Transfer (same stock account / ZVT) | Inventory already balanced via SLE; remainder **traced** | `TRACE` |
| Large unsupported integrity errors | Still fail closed | — |

Company Stock Adjustment Account is always recorded on TVA for Phase 2 discoverability (`621301 - تعدیلات موجودی کالا - E` on the test company).

**No hardcoded** voucher names, items, batches, warehouses, or adjustment caps.

---

## D. Adjustment calculation

```
EXPECTED_VALUE  = ROUND_HALF_UP(qty × integer_rate)   # or rate_derived_amount
ACTUAL_VALUE    = authoritative movement (SLE SVD / pool / SED amount)
ADJUSTMENT_DELTA = ACTUAL_VALUE − EXPECTED_VALUE      # signed
```

Idempotency key: `sha256(hotfix_version|stock_entry|detail|reason|delta)[:40]`.

---

## E. Four-document test matrix (development.localhost)

| DOCUMENT | ORIGINAL ERROR | ROOT CAUSE | EXPECTED | ACTUAL | DELTA | SA ACCOUNT | GL | SUBMIT | SLE | SABE | BIN | TVA | STATUS |
|----------|----------------|------------|----------|--------|-------|------------|----|--------|-----|------|-----|-----|--------|
| 41402 | Indivisible MA | Fractional batch pool full deplete | 1809165600 | 1809165710.526 | +110.526 | 621301… | 0 (same WH account ZVT) | docstatus=1 | 8 rows Σ0 | 8 (frac allowed) | source qty/value 0 | INDIVISIBLE_MOVING_AVERAGE TRACE | **PASS** |
| 41489 | Missing Round Off −1 | Class A after SED sync | rate_derived | SLE amount | −1 | 621301… (RO posted) | Dr/Cr 877743 balanced; RO credit 1 | original + amend `MAT-STE-2026-41489-1` docstatus=1 | 1 | n/a | OK | SED_SLE_VALUE_MISMATCH ROUND_OFF_GL | **PASS** |
| 41492 | Composition + RO −2 | Native MA / R2 | 68113638 | 68113636 | −2 | 621301… | Dr/Cr 68113638; RO credit 2 | docstatus=1 | 1 Σ−68113636 | n/a | OK | SED_SLE_VALUE_MISMATCH | **PASS** |
| 41477 | Composition + RO −2 | Same | 8169645 | 8169643 | −2 | 621301… | Dr/Cr 8169645; RO credit 2 | original + amend `MAT-STE-2026-41477-1` docstatus=1 | 1 Σ−8169643 | n/a | OK | SED_SLE_VALUE_MISMATCH | **PASS** |

Cancel/retry: originals cancelled → TVA `CANCELLED`; amend re-submit (workflow temporarily inactive) recreated TVA with new idempotency keys — **PASS**.

### Additional general cases

| Document | Result |
|----------|--------|
| MAT-STE-2026-41500 | **PASS** — HISTORICAL_BATCH_RATE_MISMATCH + SED_SLE_VALUE_MISMATCH; GL balanced |
| MAT-STE-2026-41488 / 41487 / 41475 / 41472 | **PASS** (rollback probes) — Class A Round Off bridge |
| MAT-STE-2026-41491 / 41460 | Still **NegativeStockError** (correct — not valuation bridge) |
| MAT-STE-2026-41454 / 41465 | Still **ValuationIntegrityError** lost outgoing rate (correct — unsupported) |

---

## F. Regression

- `test_prevention_v5544` — **19 OK** (indivisible MA now bridges when SA account configured; still fails closed without SA account).
- Negative stock / lost outgoing rate / return divergence still block.
- Native outgoing R2 composition allowed within mathematical bound.
- TVA idempotency: duplicate `idempotency_key` reuses the same row.
- Cancel sets TVA `repair_status=CANCELLED`.
- v5.5.44 prevention contracts otherwise intact.

---

## G. Future Full Rebuild (Phase 2) compatibility

Do **not** implement Phase 2 here. Phase 2 can:

1. **Identify** all `Temporary Valuation Adjustment` with `hotfix_version=5.5.45` and `repair_status in (OPEN, SUBMITTED)`.
2. **Recalculate** corrected valuation from reconstructed source rates / SABE / SLE.
3. **Determine** remaining valid delta vs temporary delta.
4. **Reverse or settle** Round Off / SA legs without duplication (use `gl_entry` + idempotency key).
5. **Verify** final inventory GL; set `repair_status=SUPERSEDED_BY_PHASE2`.

Ordinary RIV will **not** automatically reverse these adjustments.

---

## H. Known limitations

1. Transfer indivisible / fractional batch remainders on **same inventory account** warehouses produce **no Stock Adjustment GL** (ERPNext posts no transfer GL); remainder is TRACE-only.
2. Class A residuals use **Round Off**, not Stock Adjustment GL (ledger contract / existing Iran residual policy). SA account is still stored on TVA.
3. Manufacture / Repack TYPE A/B/C contracts are **not** replaced by this bridge.
4. `lost_outgoing_rate_zero` and negative stock remain hard failures.
5. Workflow-gated amend re-submit may require UI workflow actions on this site.
6. Fractional SABE under TVA means integer IRR SLE may differ slightly from SABE; `SLE_SABE_VALUE_MISMATCH` rows capture that for Phase 2.

---

## I. Modified files

- `erpnext_extensions/__init__.py` — version 5.5.45
- `erpnext_extensions/iran_accounting/domain/temporary_valuation_adjustment.py` — **new**
- `erpnext_extensions/erpnext_extensions/doctype/temporary_valuation_adjustment/*` — **new** DocType
- `erpnext_extensions/iran_accounting/domain/stock_entry_prevention.py`
- `erpnext_extensions/iran_accounting/domain/stock_entry_ledger_contract.py`
- `erpnext_extensions/iran_accounting/stock_entry.py`
- `erpnext_extensions/iran_accounting/integration/monkey_patches.py`
- `erpnext_extensions/hooks.py` — on_cancel hook
- `erpnext_extensions/iran_accounting/tests/test_prevention_v5544.py`
- `RELEASE_5_5_45.md`

Unrelated untracked: `erpnext_extensions/scripts/cancelled_voucher_repair.py` (preserved, not committed).

---

## J. Deployment notes

1. Deploy app 5.5.45; `bench migrate` (creates Temporary Valuation Adjustment).
2. Confirm Company **Stock Adjustment Account** and **Round Off Account** are set.
3. Scheduler / RIV queue unchanged — do not start historical RIV for Phase 1.
4. Submit blocked Stock Entries normally from Desk.

## K. Rollback

1. Restore DB from `20261014_083123-development_localhost-database.sql.gz` **or** cancel affected Stock Entries and remove TVA rows.
2. Revert app to 5.5.44.

---

## L. Final verdict

**HOTFIX_READY** (development.localhost)

Mandatory examples submitted with calculated, traceable adjustments; general bridge validated on additional drafts; unrelated validations still fail closed; Phase 2 discoverability documented.
