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

Warehouse operations were blocked because Stock Entries could not submit due to **historical valuation inconsistencies** (not quantity / negative-stock issues). Phase 1 allows submission by:

1. Detecting eligible valuation gaps with **general rules** (no voucher hardcoding).
2. Calculating the exact signed delta from authoritative values.
3. Applying the correct accounting category (Round Off / Stock Adjustment / TRACE).
4. Tracking every temporary adjustment for Phase 2 Full Historical Rebuild.
5. Preserving SLE, SABE, Bin, GL balance, batch identity, and quantity validation.

Negative stock behavior is **out of scope** and unchanged.

---

## B. Accounting policy (root-cause based)

Classification is by **root cause**, not by amount size.

| Category | Meaning | GL treatment | TVA `accounting_category` / `posting_mode` |
|----------|---------|--------------|---------------------------------------------|
| **1. GENUINE_ROUNDING** | Authoritative amount − qty×integer_rate within R2 bound (Iran Class A) | Company **Round Off** | `GENUINE_ROUNDING` / `ROUND_OFF_GL` |
| **2. HISTORICAL_VALUATION** | Net inventory valuation gap that must be recognized in P&L | Company **Stock Adjustment** | `HISTORICAL_VALUATION` / `STOCK_ADJUSTMENT_GL` |
| **3. TRACE_ONLY** | Observable residual with **no** net inventory GL effect | No artificial GL | `TRACE_ONLY` / `TRACE` |

### Why ±1 / ±2 on 41489 / 41492 / 41477 stay Round Off

After native SLE↔SED sync, SLE amount is authoritative.  
`residual = SLE_amount − qty × ROUND_HALF_UP(SLE_amount/qty)`.  
That residual is **Class A representation** of an already-accepted inventory movement — Category 1.  
Historical poison may have produced the SLE amount; it does **not** convert the Class A residual into Stock Adjustment merely because |delta| is small.

### Why 41402 (110.526) stays TRACE

- Material Transfer between warehouses that share inventory account `111625`.
- Core posts **no transfer GL** (zero-value transfer / same account).
- Σ SLE = 0; inventory value relocates; no net inventory change.
- Posting Stock Adjustment would invent artificial P&L → **forbidden**.
- TVA records the indivisible MA remainder for Phase 2.

### Stock Adjustment contract (Category 2)

When a future eligible case has a **real** net valuation gap requiring recognition:

```
EXPECTED_VALUE / ACTUAL_VALUE / SIGNED_DELTA
→ Company.stock_adjustment_account (never hardcoded)
→ balanced GL debit/credit
→ TVA posting_mode=STOCK_ADJUSTMENT_GL
```

Manufacture TYPE C / `value_difference` Stock Adjustment paths remain the existing Iran contracts (unchanged by this bridge).

---

## C. Root causes (mandatory examples)

| Document | Purpose | Original blocker | Category |
|----------|---------|------------------|----------|
| MAT-STE-2026-41402 | Material Transfer | Indivisible MA remainder 110.526 | TRACE_ONLY |
| MAT-STE-2026-41489 → 41489-1 | Material Issue | Missing Class A Round Off (−1) | GENUINE_ROUNDING |
| MAT-STE-2026-41492 | Material Issue | R2 composition + missing Round Off (−2) | GENUINE_ROUNDING |
| MAT-STE-2026-41477 → 41477-1 | Material Issue | Same (−2) | GENUINE_ROUNDING |
| MAT-STE-2026-41500 | Material Issue (20 rows) | Fractional historical batch SABE + Class A (−1) | TRACE + GENUINE_ROUNDING |

---

## D. Hotfix behavior

### Eligibility

General-purpose; no voucher / item / batch / warehouse hardcoding.

Supported purposes (when valuation bridge is valid): Material Issue/Receipt/Transfer, MTfM, Manufacture, Repack, Send to Subcontractor, Material Consumption for Manufacture, source-linked returns.

Unsupported (fail closed, unchanged): negative stock, lost outgoing rate zero, batch identity failures, quantity errors.

### Insertion points

1. `before_validate` / `validate` — early TVA stamp (Frappe sets docstatus=1 before `before_submit`).
2. `before_submit` prevention — indivisible MA → TVA + fractional SABE allow.
3. SABE monkey patch — skip integer fractional throw when TVA allows.
4. `on_submit` after SED sync — remake Round Off if Class A READY but missing; persist TVA.
5. Ledger contract — native outgoing R2 amount-authoritative; fractional SLE/SABE skip when TVA-authorized.
6. `on_cancel` — TVA `repair_status=CANCELLED` (no automatic SA reversal).

### Fractional valuation

| Concept | Treatment |
|---------|-----------|
| Fractional MA / batch **rate** | May exist historically; authorized SABE under TVA |
| Intermediate calculation | Not silently truncated |
| Persisted SLE monetary (IRR) | Integer via existing rounding helpers |
| Final GL (IRR) | Integer; Class A residual → Round Off |
| Quantity fields | **Never** modified to absorb monetary residuals |

---

## E. Adjustment calculation

```
EXPECTED_VALUE   = ROUND_HALF_UP(qty × integer_rate)   # or rate_derived_amount
ACTUAL_VALUE     = authoritative movement (SLE SVD / pool / SED amount)
ADJUSTMENT_DELTA = ACTUAL_VALUE − EXPECTED_VALUE       # signed
```

Idempotency key: `sha256(hotfix_version|stock_entry|detail|reason|delta)[:40]`.

---

## F. Test evidence (development.localhost)

| DOCUMENT | DOCSTATUS | DELTA | CATEGORY | GL | TVA | STATUS |
|----------|-----------|-------|----------|----|-----|--------|
| 41402 | 1 | +110.526 | TRACE_ONLY | 0/0 (same inv. account) | INDIVISIBLE_MOVING_AVERAGE | **PASS** |
| 41489-1 | 1 | −1 | GENUINE_ROUNDING | 877743/877743 + RO Cr 1 | SED_SLE_VALUE_MISMATCH ROUND_OFF | **PASS** |
| 41492 | 1 | −2 | GENUINE_ROUNDING | 68113638/68113638 + RO Cr 2 | ROUND_OFF | **PASS** |
| 41477-1 | 1 | −2 | GENUINE_ROUNDING | 8169645/8169645 + RO Cr 2 | ROUND_OFF | **PASS** |
| 41500 | 1 | −1 + frac TRACE | GENUINE_ROUNDING + TRACE | 21133114/21133114 | batch mismatch + RO | **PASS** |

Additional: 41488/41487/41475/41472 Class A bridge PASS (prior probes).  
Negative stock (41491) still **NegativeStockError** — unchanged.  
Lost outgoing rate (41454/41465) still fail closed.

Cancel/retry: originals cancelled → TVA CANCELLED; amend re-submit recreates TVA with new keys.

---

## G. Regression

- `test_prevention_v5544` — **19 OK**
- Indivisible MA bridges when SA account configured; fails closed without it
- Manufacture / SAME_ITEM_MULTI_FG / returns / RIV skip guards intact
- No quantity or negative-stock logic changes

---

## H. Phase 2 compatibility

Discover via DocType **Temporary Valuation Adjustment** where `hotfix_version=5.5.45`.

Per row: source voucher, reason, `accounting_category`, expected/actual/delta, posting_mode, SA account, related `gl_entry`, repair_status, payload JSON.

Ordinary RIV does **not** auto-reverse these. Phase 2 must reconcile explicitly → `SUPERSEDED_BY_PHASE2`.

---

## I. Modified files (finalization)

- `temporary_valuation_adjustment.py` — accounting categories; TRACE/Round Off root-cause remarks; no Class A→historical overwrite
- `temporary_valuation_adjustment.json` — `accounting_category` field
- `RELEASE_5_5_45.md` — policy + evidence

Prior commit `7c0977e` delivered the bridge; this finalization only clarifies accounting policy metadata.

---

## J. Deployment / rollback

1. Deploy 5.5.45; `bench migrate` (TVA DocType + `accounting_category`).
2. Confirm Company Stock Adjustment + Round Off accounts.
3. Do not start historical RIV for Phase 1.
4. Rollback: restore backup `20261014_083123-…` and/or revert app to 5.5.44.

---

## K. Known limitations

1. Same-account transfers: TRACE-only for indivisible remainder (no transfer GL).
2. Class A uses Round Off (Category 1), not SA — by root cause.
3. Manufacture TYPE A/B/C SA contracts unchanged.
4. `HISTORICAL_VALUATION` / `STOCK_ADJUSTMENT_GL` path is reserved for true net valuation gaps; example suite did not require inventing SA GL.
5. Workflow-gated amend may need UI on some sites.

---

## L. Final verdict

**HOTFIX_READY** (development.localhost)

Generalized valuation bridge; known examples submitted; Round Off vs TRACE classified by root cause; negative stock untouched; Phase 2 discoverable.
