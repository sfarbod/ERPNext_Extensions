# Cohort A — Production Preflight Recheck & Dry-Run

**Site:** `https://erp.espadpharmed.com`  
**Phase:** Read-only re-verification after reported 5.7.18 deploy  
**Required extension:** `5.7.18`  
**Mutated:** **No** · **RIV executed:** **No**  
**Campaign decision:** **BLOCKED — BACKUP NOT VERIFIED**  
**Also unverified:** **BLOCKED — WORKER PATCH NOT VERIFIED**

Evidence JSON: `CAMPAIGN_A_ASSET_PR_PREFLIGHT_PROD_RECHECK.json`

---

## Executive verdict

Authentication succeeded. Production now reports **`erpnext_extensions = 5.7.18`**. Asset PR correction APIs and the financial-correction campaign dry-run are available. Campaign dry-run reports **`fa_gl_amount_difference_patch_installed = True` on the web process**.

Execution remains **blocked**:

1. Fresh Production **DB and Files backups** still **cannot** be verified (API-only; no server FS/SSH).
2. FA patch activation in **background workers / RIV processes** is **NOT VERIFIED**.

All eight Purchase Receipts still carry **Land** GL debit on Production (campaign not executed). No document was modified. `ACC-JV-1405-169046` read-only; unmodified (`modified=2026-10-19 14:35:53.838699`).

---

## 1. Authentication

| Check | Result |
|---|---|
| HTTP | **200** |
| User | `Administrator` |
| Site | `https://erp.espadpharmed.com` |
| Company | `اسپاد فارمد دارو` |
| System Settings | Iran / en / Asia/Tehran |
| Credentials in report | **No** |

---

## 2. Versions (Production API — not inferred from Git)

| Component | This recheck | Previous preflight |
|---|---|---|
| Frappe | 16.54.0 | 16.54.0 |
| ERPNext | 16.53.0 | 16.53.0 |
| **erpnext_extensions** | **5.7.18** | **5.7.17** |
| HRMS | 16.53.0 | 16.53.0 |

**Version gate: PASS** (upgrade confirmed via `frappe.utils.change_log.get_versions`).

---

## 3. Module & patch verification

| Module / probe | Result |
|---|---|
| `iran_accounting.asset_purchase_receipt_correction` | **Available** — status API 200; dry-run API **8/8 success** |
| `iran_accounting.integration.fa_pr_gl_amount_difference` | Module reachable; direct whitelist call **403**; campaign flag **`True`** |
| `financial_correction_campaign` dry-run | **200**, `fa_gl_amount_difference_patch_installed=True` |
| Web FA patch activation | **Yes** (campaign flag on web handler) |
| Worker / RIV FA patch activation | **NOT VERIFIED** (no SSH/bench introspection) |

User-stated short paths (`erpnext_extensions.asset_purchase_receipt_correction`, `…iran_accounting.fa_pr_gl_amount_difference`) return 417; actual installed paths are under `iran_accounting.asset_purchase_receipt_correction` and `iran_accounting.integration.fa_pr_gl_amount_difference`.

---

## 4. Backup verification

| Backup | Verified? |
|---|---|
| Database path / timestamp / completion | **No** |
| Files path / timestamp / completion | **No** |

API File searches find only unrelated April 2026 Excel “Backup” uploads; no `.sql.gz` / site files tarball metadata. Scheduled job seen: `backups.delete_downloadable_backups` only.

**Backup gate: STOP.**

---

## 5. Conflicting jobs

| Object | Status |
|---|---|
| RIV `8tt4rqd11g` | Failed (item `15010547` / Class B on `MAT-PRE-2026-02045` — outside Cohort A) |
| HRC-00014 | Failed |
| JE denylist | Intact |

---

## 6. Dry-run (read-only) — eight PRs

Official dry-run API: **success_count=8**, `execution_status=dry_run_only`, **mutated=false**.

| Purchase Receipt | Target | Land GL debit (now) | Target GL | amt_diff | Expected FA debit after* | Linked Assets (campaign) | Depr note | Doc status |
|---|---|---:|---:|---:|---:|---:|---|---|
| MAT-PRE-2026-01907 | 121013 | 6,630,000 | 0 | 0 | 6,630,000 | 1 (`4026`) | Case D | **BLOCKED** |
| MAT-PRE-2026-00953 | 121013 | 1,500,000,000 | 0 | 0 | 1,500,000,000 | 3 | Case D; posted depr preserve | **BLOCKED** |
| MAT-PRE-2026-01311 | 121006 | 1,290,000,000 | 0 | 0 | 1,290,000,000 | **10** | **Case E** cost 122.55M vs 129M + posted depr | **BLOCKED** |
| MAT-PRE-2026-00747 | 121006 | **1,290,000,000** | 0 | **−64,500,000** | **1,225,500,000** | 0 (Case A) | No asset | **BLOCKED** |
| MAT-PRE-2026-01413 | 121003 | 11,781,250,000 | 0 | 0 | 11,781,250,000 | 1 (`3750`) | Case D; posted depr | **BLOCKED** |
| MAT-PRE-2026-00880 | 121006 | 158,000,000 | 0 | 0 | 158,000,000 | 1 (`3766`) | Case D; posted depr | **BLOCKED** |
| MAT-PRE-2026-01241 | 121003 | 1,030,000,000 | 0 | 0 | 1,030,000,000 | 1 (`3833`) | Case D | **BLOCKED** |
| MAT-PRE-2026-01266 | 121006 | 1,160,000,000 | 0 | 0 | 1,160,000,000 | 1 (`3838`) | Case D; posted depr | **BLOCKED** |

\* Land-row `base_net + tax + LCV + amt_diff` (FA patch inclusive). Requires future source correction + Transaction RIV with patch active in the **RIV worker**.

### `MAT-PRE-2026-00747` detail

| Field | Current Production |
|---|---|
| Expense account | `121000 - زمین - E` |
| `amount_difference_with_purchase_invoice` | **−64,500,000** |
| `valuation_rate` | 122,550,000 (qty 10) |
| GL | Dr Land **1,290,000,000** / Cr GRNI `211230` **1,290,000,000** |
| Expected after correction + FA patch RIV | FA debit **1,225,500,000** / GRNI **1,225,500,000** |
| Linked Asset | **None** (Case A) |
| Linked PI (campaign) | `ACC-PINV-2026-00331` |

### Asset lookup note

Plain `GET /api/resource/Asset?filters=purchase_receipt=…` returned **0** rows earlier. Campaign `asset_impact` / `linked_assets` **does** resolve Assets (PRI / internal helpers). Prefer campaign inspect for Asset linkage; do not conclude “no Asset” from a single resource filter.

### `MAT-PRE-2026-01311` Case E

Ten submitted Assets at **122,550,000** acquisition vs PR economic **129,000,000** per unit equivalent; posted depreciation exists. **Do not auto-rebuild / cancel historical depr GL.** Separate Finance design required even after Land→Furniture GL correction.

---

## 7. Differences vs previous preflight

| Topic | Before | Now |
|---|---|---|
| Extension version | 5.7.17 | **5.7.18** |
| Correction module | Missing | **Present** |
| FA patch module | Missing | **Present; web flag True** |
| Dry-run API | N/A | **8/8 ok** |
| Assets visible | 0 via resource API | **Found via campaign API** |
| Land GL on cohort | Present | **Still present** (not executed) |
| Backups | Unverified | **Still unverified** |
| Worker patch | Unverified | **Still unverified** |

---

## 8. Counts & decision

| Metric | Count |
|---|---:|
| Documents | 8 |
| READY FOR REVIEW | **0** |
| BLOCKED | **8** |
| FAILED | **0** |

**Campaign decision: BLOCKED — BACKUP NOT VERIFIED**  
(Additional: worker/RIV patch NOT VERIFIED.)

---

## 9. Resume requirements

1. Verifiable **DB backup** path + timestamp + completion for `erp.espadpharmed.com`.  
2. Verifiable **Files backup** path + timestamp + completion.  
3. Proof FA patch is loaded in **workers/RIV** (SSH/bench), not only web.  
4. Re-run preflight → then separate **explicit execution authorization**.

---

## 10. Explicit non-actions

No RIV · no PR/Asset/GL/SLE mutation · no JE · no Cohort B · no git commit/push · denylist JE untouched.
