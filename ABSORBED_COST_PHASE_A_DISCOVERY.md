# Phase A — Monthly Absorbed Labor/Overhead Allocation

**Story target:** v5.8.5  
**Phase:** A (read-only discovery + technical design)  
**Site inspected:** `development.localhost` (Dev)  
**App version (unchanged):** `5.8.3`  
**Branch tip:** `c5fa392` (`fix(costing): restore MCA Iran repost and worker processing`)  
**Date:** 2026-11-21  
**Mutations:** none (no DocType create, no GL/SE/MCA/RIV enqueue, no version bump, no commit)

Raw JSON: `/tmp/absorbed_cost_phase_a_gl_discovery.json`, `/tmp/absorbed_cost_phase_a_classified.json`

---

## 1. Verified account mapping (mandatory gate)

Queried live `tabAccount`, Expense Account Dimension Rules, GL behavior, and existing Costing classifiers.

| Account number | Account name (ERPNext) | Full name | Classification | Account type | Root | Company | Disabled |
|---|---|---|---|---|---|---|---|
| **211237** | کنترل سربار | `211237 - کنترل سربار - E` | **Overhead** | Expenses Included In Valuation | Liability | اسپاد فارمد دارو | No |
| **211238** | کنترل دستمزد | `211238 - کنترل دستمزد - E` | **Labor** | Expenses Included In Valuation | Liability | اسپاد فارمد دارو | No |

**Consistency checks**

- Parent: `2112 - سایر حسابها و اسناد پرداختنی غیر تجاری - E` (both).
- Expense Rule `account_control`: **2,380** rules → 211237 (overhead); **646** rules → 211238 (labor).
- Code already encodes the same mapping in `document_breakdown.py`:
  - `OVERHEAD_ACCOUNT_CONTROL_PREFIX = "211237"`
  - `LABOR_ACCOUNT_CONTROL_PREFIX = "211238"`
- Document Breakdown labels: overhead = کنترل سربار; labor = کنترل دستمزد.
- RELEASE_5_7_56 notes historical rename `511101 - سربار` → number `211237` (overhead), consistent with above.

**Verdict:** Verified ERPNext data **matches** the expected labor/overhead business classification.  
**No interchange.** Phase A does **not** stop for §2.1 mapping conflict.

---

## 2. Backup readiness

| Item | Value |
|---|---|
| File | `sites/development.localhost/private/backups/20261121_095630-erp_espadpharmed_com-database.sql.gz` |
| Size | ~557 MB |
| Readable | Yes (gzip header OK) |
| Original | Must remain unchanged |
| Isolated restore candidates | `i3-sandbox.localhost`, `payroll-e2e.localhost` (existing bench sites) |
| Production | Not touched |

Phase C must restore into an **isolated** site only. Discovery below is from **current Dev**, which already contains Costing recalculations and Manufacture additional-cost postings; backup figures may differ and are the historical validation source of truth.

---

## 3. Existing Costing architecture (reuse targets)

```text
Monthly FG Cost Calculation (MFGCC-*)
  → gl_extract (expense pools via Expense Account Dimension Rules + account_control)
  → production_extract / eligibility / normalize
  → allocate.py (batch share, product_share, residual-to-largest)
  → persist.py → MFG Cost Product Allocation (+ Document Breakdown)
  → (separate consumption path) posts Landed Cost lines on Manufacture SE
       expense_account = 211237 / 211238
       description ≈ "کنترل سربار/دستمزد (Account Control …) | 1405-MM | PA <id> | qty …"
```

Key modules under `erpnext_extensions/iran_accounting/fg_cost_allocation/`:

| Module | Role for this Story |
|---|---|
| `period.py` / `resolve_persian_month` | Persian month bounds |
| `eligibility.py` / `production_extract.py` | Eligible manufactured products |
| `allocate.py` | `product_share` + residual policy |
| `document_breakdown.py` | Account Control split + qty split across source SEs |
| `export_costing.py` | Document Breakdown Excel export |
| `engine.py` / `persist.py` | Monthly calculation lifecycle |

**Existing monthly calculations (Dev):** `MFGCC-2026-00001` … `00006` for 1405-01..06, status `Recalculated`, revision 2, product counts 8 / 21 / 73 / 56 / 78 / 82.

**No** existing DocType named Absorbed / Monthly Absorbed Cost Allocation.

---

## 4. Nature of GL credits on 211237 / 211238

**Source of truth check:** All period credits in 1405-01..06 are `voucher_type = Stock Entry`, purpose/type `Manufacture`, against `621301 - تعدیلات موجودی کالا - E`.  
Overhead also has historical Purchase Invoice **debits** (not credits).

Credits are created from Manufacture **Landed Cost Taxes and Charges** lines whose `expense_account` is the control account.

Two distinct populations appear in Dev:

| Class | Recognition | Meaning |
|---|---|---|
| **A. Costing PA posting** | Description contains `Account Control 211237/211238` + period + `PA <id>` | Downstream **consumption** of Monthly FG Cost Product Allocation already computed by Costing |
| **B. Operational absorption** | e.g. `هزینه … Operating Cost for operation …` | Costs absorbed into FG via additional costs **outside** / beside the PA consumption pattern |
| **C. Non stock items** | Description `Non stock items` | Tiny residuals (often 2–6 IRR) — flag for review |
| **D. Mixed SE** | One GL credit; multiple landed lines (PA + operational) | Same voucher aggregates classes — needs line-level split |

Story example Khordad `211237` ≈ **351,300** matches Dev operational line **351,288** on `MAT-STE-2026-40332` (`هزینه بسته بندی ماشین … لیبلینگ`) plus **12** IRR `Non stock items` → **351,300** exactly in the &lt;1M credit bucket.

---

## 5. Six-month monthly credit summary (Dev)

Period bounds via Jalali calendar (aligned with existing MFGCC start/end).

**Rule applied for columns:** Period Credit = `SUM(credit)` only (debits and opening **not** subtracted). Opening / Period Debit are informational.

### 5.1 Raw GL (all non-cancelled credits)

| Month | Labor Period Credit | OH Period Credit | Labor #Cr | OH #Cr | Labor Period Debit | OH Period Debit |
|---|---:|---:|---:|---:|---:|---:|
| 1405-01 Farvardin | 109,996,234,008 | 21,138,998,816 | 8 | 8 | 0 | 0 |
| 1405-02 Ordibehesht | 19,291,356,217 | 9,822,311,033 | 3 | 3 | 0 | 0 |
| 1405-03 Khordad | 3,809,378,396 | 3,251,048,853 | 2 | 7 | 0 | 0 |
| 1405-04 Tir | 143,653,660 | 82,832,036 | 1 | 2 | 0 | 0 |
| 1405-05 Mordad | 19,837,412,599 | 24,151,389,850 | 5 | 13 | 0 | 0 |
| 1405-06 Shahrivar | 11,476,641,174 | 84,610,843,305 | 6 | 287 | 0 | 0 |

### 5.2 Classified credits (Dev provisional)

Excluding class A (Costing PA postings) as **already allocated by Costing** — candidate “absorbed” pool for this Story:

| Month | Labor PA-posting | Labor Non-PA | OH PA-posting | OH Non-PA | Story expectation |
|---|---:|---:|---:|---:|---|
| 1405-01 | 109,996,234,008 | **0** | 21,138,998,816 | **0** | No absorbed ✓ (if PA excluded) |
| 1405-02 | 19,291,356,217 | **0** | 9,822,311,033 | **0** | No absorbed ✓ (if PA excluded) |
| 1405-03 | 3,809,378,396 | **0** | 3,250,697,553 | **351,300** | Yes — OH only (labor Non-PA = 0 on Dev) |
| 1405-04 | 143,653,660 | **0** | ~82.8M mostly PA-on-mixed SE | **~190** operational + 2 non-stock (after line split) | Yes — tiny OH operational if line-split |
| 1405-05 | 19,837,412,599 | **0** | 24,144,418,254 | **6,971,596** | Yes — OH operational only |
| 1405-06 | 11,476,641,174 | **0** | 2,014,961,428 | **~82.6B** | Yes — large OH operational |

**Labor Non-PA = 0 in all six months on current Dev.** All 211238 credits observed are Costing PA consumption postings.

---

## 6. Anomalies / approval gates (must resolve before Phase B)

### Gate G1 — Eligibility vs raw `SUM(credit)` (blocking)

Literal §3.2 (“sum every monthly credit”) on Dev would treat **Costing’s own PA consumption postings** as newly allocatable absorbed cost → **circular re-allocation** of amounts Costing already distributed and posted.

Business month expectations (01–02 = No, 03–06 = Yes) only reconcile if **class A PA postings are excluded** (or marked previously allocated / ineligible).

**Required decision before implementation:**

1. **Recommended:** Eligible absorbed credit = monthly GL credits on 211237/211238 **minus** landed-cost lines identified as Costing PA consumption (`Account Control … | PA …`), with exclusions flagged; still reconcile GL credit totals informationally.
2. Or: Eligible = full `SUM(credit)` and treat PA postings as `Previously Allocated` via linkage to Product Allocation IDs in descriptions.
3. Or: Other business filter (voucher series, cost center, dimension, MCA, etc.).

### Gate G2 — Labor absorbed amounts missing on Dev (blocking for labor scope)

Under recommended filter, **no labor (211238) Non-PA credits** exist in 1405-01..06 on Dev.  
Either labor absorption is posted to **211237** in practice, backup differs, or labor eligibility needs another source.

### Gate G3 — Mixed Landed Cost lines under one GL Entry (blocking for idempotent source grain)

Example Tir `MAT-STE-2026-40689-1`: one GL credit **82,832,034** = PA line **82,831,846** + operating **188**.  
Story traceability is GL-entry-centric, but eligibility requires **landed-cost line decomposition** (or proportional split) so PA and operational portions are not conflated.

### Gate G4 — Dev ≠ backup

Discovery used live Dev (post-Costing recalc / MCA work). Phase C must repeat §5 against restored `20261121_095630-…sql.gz` on an isolated site before accepting historical numbers.

### Gate G5 — Accounting side effects

Story §8: allocation DocType must **not** create/modify GL, SLE, SED, batch valuation, or RIV unless a separate accounting design is approved. Design below assumes **costing-only traceability**.

---

## 7. Technical design (proposed — pending G1–G3)

### 7.1 DocType naming (Costing module)

Follow existing `MFG Cost …` prefix:

| DocType | Purpose |
|---|---|
| **MFG Monthly Absorbed Cost Allocation** | Parent (company + persian period unique among non-cancelled) |
| **MFG Monthly Absorbed Cost Account Summary** | Child: per account labor/overhead summary |
| **MFG Monthly Absorbed Cost GL Source** | Child: source GL (+ optional landed-cost line ref) |
| **MFG Monthly Absorbed Cost Product Row** | Child: product / batch / shares / amounts |

Uniqueness: `(company, persian_period)` where `docstatus < 2` and status ∉ {Cancelled}; DB unique index + validate. Revisions via new draft document or revision counter mirroring `Monthly FG Cost Calculation` (prefer explicit new draft + link `amended_from` / `revision`).

Lifecycle: `Draft` → `Calculated` → `Submitted` → `Cancelled`.  
Submitted amounts immutable; recalculation only on Draft/Calculated; cancel releases consumption.

### 7.2 Services (no parallel engine)

New package: `erpnext_extensions/iran_accounting/fg_cost_allocation/absorbed_monthly/` (or `costing/absorbed_monthly/`).

| Service | Reuses |
|---|---|
| `identify_source_gl(company, period)` | GL Entry query; period via `resolve_persian_month`; eligibility classifier (G1) |
| `load_product_basis(monthly_calculation)` | Existing `MFG Cost Product Allocation` / batch shares for that persian period — **same** `product_share` |
| `allocate_amounts(eligible_labor, eligible_oh, products)` | Same money rounding (`get_currency_precision`) + residual-to-largest policy as `allocate.py` / `document_breakdown.py` |
| `consumption_ledger` | Single ledger: GL source consumption ↔ product/document rows (no dual books) |
| `refresh_allocatable_columns` | Updates Document Breakdown display fields only |

### 7.3 MFG Cost Product Allocation — Document Details extension

Child DocType `MFG Cost Product Allocation Document Breakdown` — **add columns** (keep existing):

| Field | Label (FA) | Meaning |
|---|---|---|
| `allocatable_overhead` | سربار قابل تخصیص | `overhead_amount − submitted absorbed OH for this source doc` |
| `allocatable_labor` | دستمزد قابل تخصیص | `labor_amount − submitted absorbed labor for this source doc` |

Also parent totals: `breakdown_total_allocatable_overhead/labor`.  
Negative remaining → exception row / status, **not** silent zero.  
Excel export (`export_product_document_breakdown`) extended with the two columns + footer.  
JS footer in `mfg_cost_product_allocation.js` extended similarly.

### 7.4 Persistence / integrity

- No GL/SLE writes.
- `UNIQUE(parent, gl_entry[, landed_cost_row])` on source child.
- On submit: lock consumption keys in ledger table or hashed unique `(gl_entry|line, allocation_name)`.
- Draft/Cancelled do not consume.
- Idempotent calculate: rebuild draft children from GL + prior submitted consumption.

### 7.5 Tests (Phase B)

Isolated fixtures covering §9 of the Story; regress existing `test_document_account_control_breakdown_v5755`, Account Control, MCA gates, Excel export.

### 7.6 Version / release

Implement on `develop` without bumping `__version__` until Phase D acceptance. Target release notes **5.8.5** only after validation.

---

## 8. Phase A deliverable checklist

| Deliverable | Status |
|---|---|
| Verified labor/overhead account mapping | **Done** — 211238 labor, 211237 overhead |
| Inspect Costing / Document Breakdown / Excel | **Done** |
| Backup exists & restore path identified | **Done** (restore not executed) |
| Six-month credit summaries | **Done** (Dev; backup pending Phase C) |
| Technical design | **Draft above** — blocked on G1–G3 |
| Business documents created/modified | **None** |
| Version bump / push / tag / deploy | **None** |

---

## 9. Recommended next step

**Do not start Phase B** until product/accounting owners confirm:

1. **G1** eligibility rule (exclude or pre-mark Costing PA postings).
2. **G2** expected labor absorbed source if 211238 Non-PA remains empty after backup restore.
3. **G3** source grain = GL Entry + Landed Cost line when mixed.

After confirmation: restore backup to `i3-sandbox.localhost` (or dedicated site), re-run §5 classification, then implement Phase B against the approved eligibility contract.
