# Release 5.5.45 — Temporary Stock Adjustment Bridge + Inventory Residual Policy

**Version:** 5.5.45  
**Date:** 2026-10-14  
**Previous:** 5.5.44  
**Branch:** `hotfix/5.5.45-temporary-stock-adjustment-bridge`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0** (unmodified Core), Iran Accounting **5.5.45**  
**Site:** `development.localhost` only — **no production**, **no push**, **no tag**

**Backup:** `20261014_083123-development_localhost-database.sql.gz`  
(path: `sites/development.localhost/private/backups/`)

---

## A. Business objective

Warehouse operations were blocked by historical valuation inconsistencies. Phase 1 allows submission via a Temporary Valuation Adjustment (TVA) bridge and — in this policy correction — routes **all inventory valuation residuals that require GL** to Company **Stock Adjustment**, never Financial Round Off.

Negative stock behavior is **out of scope** and unchanged.

---

## B. Corrected accounting policy (mandatory)

| Domain | Destination |
|--------|-------------|
| Inventory valuation difference (any amount, including ±1/±2 IRR Class A) | Company **Stock Adjustment** |
| Financial document rounding (SI / PI / PE / JE, etc.) | Company **Financial Round Off** |

**Transaction origin** determines treatment — not the size of the difference.

Root-cause labels remain separate from GL destination:

| Category | Meaning | GL treatment |
|----------|---------|--------------|
| **GENUINE_ROUNDING** | Authoritative amount − qty×integer_rate (Class A) | **Stock Adjustment** |
| **HISTORICAL_VALUATION** | Net inventory valuation gap requiring recognition | **Stock Adjustment** |
| **TRACE_ONLY** | Observable residual with **no** net inventory GL effect | No artificial GL |

Financial Round Off must **not** be used for stock valuation residuals. Missing Stock Adjustment Account fails closed (no silent Round Off fallback).

### TRACE_ONLY exception (41402)

- Material Transfer between warehouses sharing inventory account `111625`.
- Σ SLE = 0; Core posts no transfer GL.
- TVA records indivisible MA remainder (110.526) for Phase 2 — no SA/Round Off GL.

---

## C. Root causes (mandatory examples)

| Document | Purpose | Category | GL residual |
|----------|---------|----------|-------------|
| MAT-STE-2026-41402 | Material Transfer | TRACE_ONLY | none (0/0) |
| MAT-STE-2026-41489-2 | Material Issue | GENUINE_ROUNDING | SA Cr 1 |
| MAT-STE-2026-41492-1 | Material Issue | GENUINE_ROUNDING | SA Cr 2 |
| MAT-STE-2026-41477-2 | Material Issue | GENUINE_ROUNDING | SA Cr 2 |
| MAT-STE-2026-41500-1 | Material Issue | GENUINE_ROUNDING + TRACE | SA Cr 1 |

(Amended names from cancel→amend re-submit under corrected policy; originals cancelled with prior Round Off history preserved.)

---

## D. Hotfix behavior

### Residual GL algorithm (inventory)

1. Classify Class A rows: `residual = authoritative_amount − qty×integer_rate`.
2. Net signed debit → destination = `Company.stock_adjustment_account`.
3. Reclassify equal magnitude from a safe non-stock partner (expense/COGS; never inventory, never Financial Round Off, never SA as partner).
4. Post residual leg with remark `IRR inventory valuation residual` + in-memory marker.
5. Cost-center allocator **must not** restamp inventory residuals onto Round Off.
6. Ledger contract asserts SA destination and rejects Financial Round Off for inventory residuals.

### Insertion points

1. Early TVA stamp (`before_validate` / `validate`).
2. `before_submit` prevention — indivisible MA → TVA + fractional SABE allow.
3. SABE monkey patch — skip integer fractional throw when TVA allows.
4. `on_submit` after SED sync — remake residual GL if Class A READY but missing; persist TVA (`STOCK_ADJUSTMENT_GL`).
5. Ledger contract — native outgoing R2; SA residual assert.
6. `on_cancel` — TVA `repair_status=CANCELLED`.

---

## E. Test evidence (development.localhost)

| DOCUMENT | DOCSTATUS | DELTA | CATEGORY | GL | TVA | STATUS |
|----------|-----------|-------|----------|----|-----|--------|
| 41402 | 1 | +110.526 | TRACE_ONLY | 0/0 | TRACE | **PASS** |
| 41489-2 | 1 | −1 | GENUINE_ROUNDING | 877743/877743 + **SA Cr 1** | STOCK_ADJUSTMENT_GL | **PASS** |
| 41492-1 | 1 | −2 | GENUINE_ROUNDING | 68113638/68113638 + **SA Cr 2** | STOCK_ADJUSTMENT_GL | **PASS** |
| 41477-2 | 1 | −2 | GENUINE_ROUNDING | 8169645/8169645 + **SA Cr 2** | STOCK_ADJUSTMENT_GL | **PASS** |
| 41500-1 | 1 | −1 + frac TRACE | GENUINE_ROUNDING + TRACE | 21133114/21133114 + **SA Cr 1** | STOCK_ADJUSTMENT_GL + TRACE | **PASS** |

### Before / after GL account

| Case | Before (prior policy) | After (v5.5.45 correction) |
|------|----------------------|----------------------------|
| 41489 / 41492 / 41477 / 41500 Class A | Financial Round Off `622515` | Stock Adjustment `621301` |
| 41402 | no residual GL | unchanged TRACE |

SLE / SABE / Bin remain consistent; GL balanced; no duplicate residual legs.

---

## F. Regression

- `test_prevention_v5544` — **19 OK**
- `test_irr_residual_classification` — **25 OK**
- `test_irr_rounding_residual` — **19 OK**
- `test_gl_cost_center_allocation_irr` — **2 OK**
- Negative stock / lost outgoing rate fail-closed unchanged
- SI/PI/PE/JE Round Off paths untouched

---

## G. Phase 2 compatibility

Discover via DocType **Temporary Valuation Adjustment** where `hotfix_version=5.5.45`.

Per row: source voucher, item/warehouse/batch (when present), reason, `accounting_category`, expected/actual/delta, `posting_mode=STOCK_ADJUSTMENT_GL`, SA account, related `gl_entry`, repair_status, payload JSON.

Ordinary RIV does **not** auto-reverse these. Phase 2 must reconcile explicitly → `SUPERSEDED_BY_PHASE2`.

---

## H. Modified code paths (policy correction)

- `irr_rounding_residual.py` — inventory residual → Stock Adjustment masters/markers/remarks
- `irr_residual_classification.py` — evaluate destination SA for SE/PR/SR; protect Round Off from partner reclass
- `gl_cost_center_allocation.py` — do not restamp inventory residual onto Round Off
- `stock_entry_ledger_contract.py` — assert SA; reject Financial Round Off for inventory
- `temporary_valuation_adjustment.py` — Class A `posting_mode=STOCK_ADJUSTMENT_GL`
- Unit tests updated for SA destination

Preserved: `cancelled_voucher_repair.py` (untracked local tool).

---

## I. Deployment / rollback

1. Deploy 5.5.45; `bench migrate` if TVA schema pending.
2. Confirm Company Stock Adjustment Account (+ residual cost center via Round Off Cost Center).
3. Do not start historical Full Repost for Phase 1.
4. Rollback: restore backup `20261014_083123-…` and/or revert app.

---

## J. Final verdict

**HOTFIX_READY** (development.localhost)

Inventory valuation residuals use Stock Adjustment when GL is required; Financial Round Off excluded from stock valuation; TRACE_ONLY preserved; regressions green; Phase 2 discoverable.
