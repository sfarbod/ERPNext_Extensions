# Cohort A — Asset Purchase Receipt Preflight & Dry-Run (Production)

**Site:** `https://erp.espadpharmed.com`  
**Phase:** Authentication + mandatory preflight + read-only dry-run  
**Required extension:** `5.7.18`  
**Mutated:** **No** · **RIV executed:** **No**  
**Final status:** **BLOCKED**

Evidence JSON: `CAMPAIGN_A_ASSET_PR_PREFLIGHT_PROD.json`

---

## Executive verdict

Authentication to Production **succeeded**, and all eight Purchase Receipts were inspected read-only.

Execution is **BLOCKED**. Mandatory gates failed:

1. Installed `erpnext_extensions` is **`5.7.17`**, not **`5.7.18`**.
2. Cohort A correction module and FA GL amt_diff module are **missing** on Production.
3. Worker/RIV patch activation **cannot** be verified (and web already lacks the modules).
4. Fresh Production **DB and Files backups** could **not** be verified via API (no SSH/server FS access).

No Purchase Receipt, Asset, SLE, GL, or Journal Entry was modified. `ACC-JV-1405-169046` was only read and remains untouched (`modified=2026-10-19 14:35:53.838699`).

---

## 1. Authentication

| Check | Result |
|---|---|
| HTTP auth | **200** |
| User | `Administrator` |
| Site URL used | `https://erp.espadpharmed.com` |
| Company visible | `اسپاد فارمد دارو` |
| System Settings | country=Iran, language=en, time_zone=Asia/Tehran |
| Credentials in this report | **Not included** |

---

## 2. Versions

| Component | Production | Required |
|---|---|---|
| Frappe | 16.54.0 | (v16) |
| ERPNext | 16.53.0 | (v16) |
| **erpnext_extensions** | **5.7.17** | **5.7.18** |
| HRMS | 16.53.0 | — |

**Version gate: FAIL.**

---

## 3. Patch / module verification

| Probe | HTTP | Result |
|---|---:|---|
| `asset_purchase_receipt_correction.api.get_asset_pr_correction_api_status` | 417 | `No module named 'erpnext_extensions.iran_accounting.asset_purchase_receipt_correction'` |
| `fa_pr_gl_amount_difference.is_fa_pr_gl_amount_difference_patch_installed` | 417 | `No module named 'erpnext_extensions.iran_accounting.integration.fa_pr_gl_amount_difference'` |
| Financial correction campaign status API | 417 | Missing on this install |

| Process | FA / correction patch |
|---|---|
| Web | **Not present** (module import fails) |
| Background workers / RIV | **NOT VERIFIED** — no SSH/bench; cannot infer worker load from web, and web modules are absent |

Monkey-patch bootstrap for FA amt_diff is therefore **not active** on Production today.

---

## 4. Backup verification

| Backup | Verified? | Evidence |
|---|---|---|
| Database | **No** | No API path to `/home/frappe/backups` or completed DB dump metadata |
| Files | **No** | File doctype “backup” hits are unrelated April 2026 Excel uploads only; no `.sql.gz` / files tarball found |

**Backup gate: STOP.** Do not mutate until operators provide verifiable DB + Files backup path, timestamp, and completion for this Production site.

---

## 5. Conflicting jobs / campaigns

| Object | Status | Notes |
|---|---|---|
| RIV `8tt4rqd11g` | **Failed** | Item `15010547` / Class B residual on `MAT-PRE-2026-02045` (outside Cohort A) |
| HRC-00014 | **Failed** | Same residual failure family; modified 2026-11-09 |
| `ACC-JV-1405-169046` | Submitted, unmodified | Denylist intact |

These failures are **not** Cohort A vouchers, but they show a failed rebuild state that should be acknowledged before any new RIV work.

---

## 6. Approved target accounts (resolved on Production)

| Prefix | Account | Disabled |
|---|---|---|
| 121013 | `121013 - تجهیزات کامپیوتری - E` | No |
| 121006 | `121006 - اثاثیه ومنصوبات - E` | No |
| 121003 | `121003 - ماشین آلات و تجهیزات تولید - E` | No |
| 121000 (current Land) | `121000 - زمین - E` | No |

No silent substitution performed.

---

## 7. Dry-run manifest (read-only)

All eight documents exist, `docstatus=1`, company `اسپاد فارمد دارو`. SLE count = **0** for all (Fixed Asset PRs — expected). GL is currently balanced for each voucher, but FA value sits on **Land** for the actionable rows.

| Purchase Receipt | Target | Land rows | Land GL debit | Target GL debit | amt_diff | Linked PI via API | Assets found | GL balanced | Readiness |
|---|---|---:|---:|---:|---:|---|---:|---|---|
| MAT-PRE-2026-01907 | 121013 Computer | 1 | 6,630,000 | 0 | 0 | none found | 0 | Yes | **BLOCKED** |
| MAT-PRE-2026-00953 | 121013 Computer | 1 | 1,500,000,000 | 0 | 0 | none found | 0 | Yes | **BLOCKED** |
| MAT-PRE-2026-01311 | 121006 Furniture | 1 | 1,290,000,000 | 0 | 0 | none found | 0 | Yes | **BLOCKED** |
| MAT-PRE-2026-00747 | 121006 Furniture | 1 | 1,290,000,000 | 0 | **−64,500,000** | none found | 0 | Yes* | **BLOCKED** |
| MAT-PRE-2026-01413 | 121003 Machinery | 1 | 11,781,250,000 | 0 | 0 | none found | 0 | Yes | **BLOCKED** |
| MAT-PRE-2026-00880 | 121006 Furniture | 1 | 158,000,000 | 0 | 0 | none found | 0 | Yes | **BLOCKED** |
| MAT-PRE-2026-01241 | 121003 Machinery | 1 | 1,030,000,000 | 0 | 0 | none found | 0 | Yes | **BLOCKED** |
| MAT-PRE-2026-01266 | 121006 Furniture | 1 | 1,160,000,000 | 0 | 0 | none found | 0 | Yes | **BLOCKED** |

\* `00747` GL today: Dr Land **1,290,000,000** / Cr GRNI (`211230`) **1,290,000,000**. Item `valuation_rate` already reflects PI-adjusted **122,550,000** with `amt_diff=−64,500,000`, but FA debit does **not** include amt_diff (consistent with missing FA patch). After 5.7.18 + patch + RIV, expected FA debit/GRNI ≈ **1,225,500,000**.

### Notable Production vs Development differences

- Production still has **Land expense_account + Land GL** on all eight actionable rows — Cohort A has **not** been applied on Production.
- Development had already corrected several vouchers and reconciled `00747` with FA patch; **do not reuse Dev fingerprints**.
- No Asset documents were returned for these PRs via `purchase_receipt` / `purchase_invoice` / `purchase_receipt_item` filters (Administrator session). Asset/depreciation Case E for `01311` remains **unconfirmed on Production** until Asset linkage is located or confirmed absent.
- Purchase Invoice links were not present on PR item `purchase_invoice` fields / PI Item reverse lookup for this allowlist via API (amt_diff still present on `00747`).

### Expected after-state (when authorized later)

For each Land row: source `expense_account` → approved target; Transaction RIV `repost_only_accounting_ledgers`; Land GL debit → 0; target FA debit posted; commercial PR rate/amount preserved; no manual JE; no depreciation rewrite.

---

## 8. Per-document readiness

All eight: **BLOCKED** (shared gates: version, missing modules, backups, worker patch).

Counts: READY FOR REVIEW **0** · BLOCKED **8** · FAILED **0**.

---

## 9. Remaining blockers (must clear before execution ask)

1. Deploy **`erpnext_extensions` 5.7.18** to Production (replace 5.7.17).  
2. Confirm import of `asset_purchase_receipt_correction` and `fa_pr_gl_amount_difference` on **web and workers**.  
3. Provide verified **DB backup** path + timestamp + completion.  
4. Provide verified **Files backup** path + timestamp + completion.  
5. Re-run this preflight; only then request explicit execution authorization.

---

## 10. Explicit non-actions this task

No RIV · no repost · no SLE/GL mutation · no PR/Asset edits · no JE · no Cohort B · no git commit/push · version not changed by this task.
