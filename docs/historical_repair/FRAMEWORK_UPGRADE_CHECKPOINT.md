# Framework Upgrade Checkpoint — Historical Repair

**Status:** DEVELOPMENT NOT YET PROVEN — CONTINUE DEVELOPMENT  
**Release:** erpnext_extensions **5.3.47** (pre-framework-upgrade checkpoint)  
**Production:** UNTOUCHED — no Production token, no Production access, no apply.

This document freezes the exact campaign state before a Frappe / ERPNext /
HRMS framework upgrade. It is **not** a Production-ready or Clean A/B
convergence claim.

---

## CURRENT CODE

| Component | Version |
|-----------|---------|
| erpnext_extensions | **5.3.47** |
| Frappe | 16.34.0 |
| ERPNext | 16.35.0 |
| HRMS | 16.16.0 |

Branch at freeze work: `develop`  
HEAD before this checkpoint commit: `23805c669684199472ef777d4b9978151cded8d6`  
(5 commits ahead of `upstream/develop` at freeze time; plus this checkpoint commit.)

Iran Accounting (in-app) remains the **valuation authority** for stock/
manufacture economics.

---

## AUTHORITATIVE REHEARSAL SOURCE

Do **not** commit the database backup.

- File: `20260930_093401-erp_espadpharmed_com-database.sql.gz`
- SHA256: `55ee640d7ec149aa2eecf3f3138c21877ffa5a4dab46f3cf4d147bff1b9efd8d`
- Site used for Development rehearsal: `development.localhost`

---

## PROVEN CONTRACTS

### Iran Accounting authority

Current Iran Accounting + Historical Repair bridge owns valuation. Do not
reintroduce obsolete duplicate Historical Repair valuation logic.

### Bulk Scrap — LEGITIMATE_ZERO

- Canary: `MAT-STE-2026-37603` / item `30100101`
- Contract: `BULK_SCRAP`, rate = 0, not Product Reject / Co-product / etc.
- Implementation must remain **generic** (not voucher-hardcoded).

### Pending Purchase Invoice valuation

- Canary: `MAT-PRE-2026-00793-1` / item `15010444`
- Classification: `PENDING_PURCHASE_INVOICE_VALUATION` /
  `LEGITIMATE_PENDING_UPSTREAM_ACCOUNTING`
- Contract: PR rate stays 0; qty unchanged; no invented rate; PI pending;
  classification clears when submitted PI becomes authoritative.
- **I3 remains fail-closed** for unexplained negative incoming SVD.
- Narrow RIV proven: `incoming_rate = 0`, `SVD = 0`, PR rate 0.
- L6 passed this canary near identity **1182** and continued
  `failed_n = 0` through ~**2775**.

### Leftover MA

- Preserve canaries: `16100066`, `16100226`
- Current Iran MA/RIV behavior remains authoritative.

### Manufacture output valuation

Preserve Iran rules for: MAIN_FG, COMPONENT_SCRAP, PRODUCT_REJECT,
CO_PRODUCT, BY_PRODUCT, AFG, STAGE_EQUIVALENT, FINANCE_EXCLUDED.

Canary: `36934` (CO_PRODUCT).

### IRR residual

- Legitimate stock residual: **621301** Stock Adjustment
- **622515** Round Off must not become the stock residual dump account.

### RIV / integrity / workers

- Fail-closed I1/I2/I3/I4 where applicable
- `worker_queue_status` (RQ2-aware)
- Sync narrow RIV (`create_and_run_narrow_riv`, atomic path)
- Fail-closed on open campaign RIV
- **No SQL-Skip** of RIV lifecycle
- Orchestration additions in 5.3.47 (partial): Redis RIV exec lock, company
  GL lock, bounded DB concurrency retry classifier, dual-exec guard on
  `execute_reposting_entry`. **Not proven end-to-end through Clean A/B.**

---

## PROVEN CANARIES

| Canary | Item / voucher | Contract |
|--------|----------------|----------|
| Co-product | 36934 | CO_PRODUCT |
| Leftover MA valued | 16100066 | tip healthy |
| Post-depletion | 16100226 | zero-layer contract |
| Bulk Scrap | 30100101 / MAT-STE-2026-37603 | rate 0 |
| Pending PI | 15010444 / MAT-PRE-2026-00793-1 | PR rate 0, SVD 0 |

---

## LATEST REHEARSAL STATUS

| Step | Status |
|------|--------|
| Fresh restore from Sep-30 dump | Performed (Development) |
| Frozen plan `SEPT30_DETERMINISTIC_REPAIR_PLAN_V1` | Applied in latest Clean A attempt |
| Full Repost preflight | READY path used for L6 |
| L6 #1 (latest Clean A) | Completed with hard gates 0 (I1/neg/bin/GL/open RIV) — **not** accepted as final Clean A proof |
| L6 #2 | Completed gates 0 but **economic fingerprint drifted vs L6 #1** (idempotency FAIL) |
| Identity ~2781 / `17000003` earlier stop | MariaDB **1020** on `tabGL Entry` DELETE during GL repost (`MAT-STE-2026-36823`); classified `INFRASTRUCTURE_GL_DEADLOCK` |
| Clean B | **Not started** for this checkpoint |
| A/B convergence | **Not proven** |

Development DB at freeze: **PARTIALLY_MUTATED** (rehearsal interrupted; not a release artifact).

---

## KNOWN UNFINISHED WORK

1. Full root-cause / lock-owner proof for GL 1020/deadlock at ~identity 2781 / item `17000003`
2. Transaction-boundary audit sign-off for retry safety under new framework
3. Prove atomic/idempotent identity retry + orchestration under fresh restore
4. Fresh Clean A with L6 #1 / #2 **economically idempotent**
5. Fresh Clean B from same backup
6. A/B economic convergence
7. Production read-only preflight (after Development proven)

**Do not claim DEVELOPMENT PROVEN or PRODUCTION READY.**

---

## PRODUCTION STATUS

- Production untouched
- Production read-only preflight **not** started
- Production apply **not** started
- No Production token used in this campaign

---

## POST-FRAMEWORK-UPGRADE NEXT STEP

Before resuming Historical Repair:

1. Version / environment inventory (new Frappe, ERPNext, HRMS, extensions)
2. `bench migrate`
3. Iran Accounting compatibility audit
4. Historical Repair hook / monkey-patch compatibility audit
5. Focused regression tests (Bulk Scrap, Pending PI, Leftover MA, I3, RIV, workers)
6. Canary verification on Development
7. Only then resume GL-deadlock diagnosis and Clean A/B

Recommended tag after this checkpoint commit (manual): **`v5.3.47`**
