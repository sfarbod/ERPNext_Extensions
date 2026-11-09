# Cohort A Readiness — Asset PR FA GL Fix v5.7.18

**This task outcome:** tested implementation + evidence-backed **future** execution plan.  
**Cohort A is not claimed fully corrected.** No source/RIV/Asset mutation was performed here.

---

## Implementation status

| Item | Status |
|---|---|
| Version pinned `5.7.18` | Pass |
| ERPNext 16.53.0 / Frappe 16.54.0 | Confirmed |
| FA GL amt_diff patch | Implemented + bootstrapped |
| Live `get_gl_entries` for `00747` balanced at 1,225,500,000 | Pass (in-memory only) |
| Unit tests (`test_fa_pr_gl_amount_difference_v5718`) | **20/20 pass** |
| JE `ACC-JV-1405-169046` denylist | Intact; unmodified (`modified=2026-10-19 14:35:53`) |
| Git commit/push | **None** |
| Document / GL / SLE / Asset mutation | **None** |
| Cohort B | Not executed |

---

## Test results

See `ASSET_PR_FA_GL_FIX_TESTS_V5718.json`.

Covered: negative/positive/zero amt_diff; setting disabled; tax+LCV+amt_diff without double-count; stock-item skip; patch idempotency; denylist; Asset Cases B/C/D/E; IRR integer VR; non-IRR rounding.

---

## Dry-run

Refreshed: `CAMPAIGN_A_ASSET_PR_DRYRUN_DEV.json`

Allowlist (8) + approved account map preserved. Each voucher includes fingerprints, PI details, amt_diff, asset impact, and **execution_blockers**.

---

## Per-voucher financial readiness

| PR | Target | Financial readiness | Blockers |
|---|---|---|---|
| 01907 | 121013 | Already reconciled on Dev | Do not re-run |
| 00953 | 121013 | Already reconciled on Dev | Do not re-run |
| 01311 | 121006 | GL account reconciled; **Asset cost Case E** | No auto depr rebuild |
| 00747 | 121006 | FA GL fix ready; source likely on 121006; **posted GL still Land** | Future: verify source → accounting RIV only |
| 01413 | 121003 | Ready for future execute | Not this task |
| 00880 | 121006 | Ready for future execute | Not this task |
| 01241 | 121003 | Ready for future execute | Not this task |
| 01266 | 121006 | Ready for future execute | Not this task |

---

## Per-Asset depreciation readiness

| PR | Rebuild now? | Notes |
|---|---|---|
| 01907, 00953, 01413, 00880, 01241, 01266 | No | Case D (or matched costs) |
| 01311 | **Not auto** | Case E + posted depr — Finance design required |
| 00747 | N/A | No Assets |

Full matrix: `ASSET_PR_DEPRECIATION_IMPACT_DEV.md` / `.json`.

---

## Remaining blockers before claiming Cohort A complete

1. Controlled execute for `00747` (source verify + accounting RIV) after FA GL patch is live in workers.  
2. Controlled execute for `01413`, `00880`, `01241`, `01266` one-by-one with stop-on-fail.  
3. Separate Finance decision for `01311` Asset unit cost 122.55M vs 129M with posted depreciation.  
4. Confirm no unintended GL outside each voucher after each RIV.  
5. Never touch `ACC-JV-1405-169046`. Never Cohort B from this path.

---

## Exact safe future execution order

1. Confirm bootstrap installs FA GL patch (`is_fa_pr_gl_amount_difference_patch_installed()`).  
2. **Skip** `01907`, `00953`, `01311` (already reconciled accounts).  
3. **`MAT-PRE-2026-00747`:** confirm `expense_account=121006`; run accounting Transaction RIV only; expect FA Dr / GRNI Cr **1,225,500,000**; no Asset create/rebuild.  
4. **`01413` → `00880` → `01241` → `01266`:** source correction (approved map) then accounting RIV; stop on first failure.  
5. After each: reconcile GL; review Asset impact JSON; do not auto-rebuild depreciation with posted history.  
6. Produce updated execution evidence MD/JSON.  
7. Denylist JE check after every voucher.

---

## Deliverables checklist

| File | Present |
|---|---|
| `ASSET_PR_FA_GL_FIX_V5718.md` | Yes |
| `ASSET_PR_FA_GL_FIX_TESTS_V5718.json` | Yes |
| `ASSET_PR_DEPRECIATION_IMPACT_DEV.md` | Yes |
| `ASSET_PR_DEPRECIATION_IMPACT_DEV.json` | Yes (supporting) |
| `CAMPAIGN_A_ASSET_PR_DRYRUN_DEV.json` | Yes (refreshed) |
| `ASSET_PR_COHORT_A_READINESS_V5718.md` | Yes |
