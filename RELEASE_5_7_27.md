# Release 5.7.27 — Finished Goods Cost Allocation (Costing QA Hardening)

**Version:** **5.7.27** (frozen; no bump)  
**Branch:** `develop`  
**Site validated:** `development.localhost` only  
**Company:** `اسپاد فارمد دارو`  
**Deployment:** Local Development only. **No push, tag, publish, or Production deploy.**

## Summary

End-to-end Costing QA for Persian months **Farvardin 1405 (`1405-01`)** and **Ordibehesht 1405 (`1405-02`)**, with lifecycle/idempotency/share-edit/delete-recreate hardening, optional-dimension rule semantics, and regression tests.

## Expense Account Dimension Rules

| Check | Result |
| --- | --- |
| Enabled rules | **3,026** |
| Unique accounts | **89** |
| Unique departments | **34** |
| Cost Center blank | **3,026 / 3,026** (wildcard / unrestricted) |
| Duplicate keys | **0** |
| Overlapping rules | **0** |
| Account match | Exact mandatory |
| Blank Cost Center | Unrestricted filter (matches any GL cost center) |
| Blank Department | Unrestricted filter (matches any GL department) |

Generation: Account × Department from Excel (`معین حساب` × `مرکز` as Department). Cost Center intentionally blank. Row `مشترک عمومی شرکت` excluded pending master data. Batch id `EXCEL-20261112-135819`.

## Semantics (critical)

- **Blank Cost Center / Department on a rule** = no filter on that dimension (wildcard).
- **Populated dimension on a rule** = exact match against the GL line.
- **Account** is always an exact mandatory match.
- Overlapping enabled rules → extraction conflict → month **Incomplete** (no silent double allocation).
- GL lines are consumed at most once across Expense Pools.

## Backup (pre-calculation)

| Item | Value |
| --- | --- |
| Path | `/workspace/development/frappe-bench/sites/preserved_dev_backups/costing_20261112_140446` |
| Dump | `costing_tables.sql` (**1,797,749** bytes) |
| Manifest | `manifest.json` (`verified: true`, mysqldump rc 0) |
| Timestamp | 2026-11-12 14:04 |
| Rules at backup | 3,026 |
| Monthly at backup | 0 |
| Source ERP counts | GL 815,194 · SLE 182,831 · SE 20,806 · WO 491 · Batch 11,823 · Item 13,272 · UOM Conv 13,432 |

Source ERP records were **not** modified during QA.

## Farvardin 1405 (`1405-01`)

| Metric | Evidence |
| --- | --- |
| Period | 2026-03-21 → 2026-04-20 (`فروردین 1405`) |
| Doc (final) | `MFGCC-2026-00009` |
| Status | **Incomplete** (blocking UOM) |
| Identified / Allocated | **241,011,762,837** / **241,011,762,837** |
| Unallocated | 0 |
| Pools / GL lines | 421 / 1,730 |
| Batches / Products | 8 / 8 |
| Batch × Expense rows | 2,947 |
| Missing UOM / Exceptions | 2 / 3 (Blocking Missing UOM for item `20100213` `BOX(2Vial)`→`BOX(1pfs)`; provisional shares) |
| Idempotent re-run | Financial totals unchanged (revision advances) |
| Top product | Item `20100067` · material 3,326,953,459 · allocated expense 83,821,495,461 · total cost 87,148,448,920 |
| Selling price | `Missing` (Settings `selling_price_list` empty) |
| Consumable | Rejected (`assert_consumable`) — Incomplete |

## Ordibehesht 1405 (`1405-02`)

| Metric | Evidence |
| --- | --- |
| Period | 2026-04-21 → 2026-05-21 (`اردیبهشت 1405`) |
| Doc (final) | `MFGCC-2026-00008` |
| Status | **Recalculated** (consistent after restore) / was **Calculated** on first run |
| Identified / Allocated | **79,048,154,971** / **79,048,154,971** |
| Unallocated | 0 |
| Pools / GL lines | 99 / 1,674 |
| Batches / Products | 21 / 21 |
| Batch × Expense rows | 2,079 |
| Missing UOM / Exceptions | 0 / 0 |
| Idempotent re-run | Financial totals unchanged |
| Top product | Item `20100067` · material 4,540,148,472 · allocated expense 7,490,833,982 · total cost 12,030,982,454 |
| Consumable | Accepted when status Calculated/Recalculated + consistent |

## Lifecycle / repeated operations

### Idempotency

Both months: second calculation without source changes preserved identified/allocated/pool/GL counts (no double GL consumption).

### Applied Batch Share edit

Formula verified with company currency precision (IRR scale 0, `ROUND_HALF_UP`):

`Allocated Amount = Pool Monthly Net Expense × Applied Batch Share`

| Month | Expected | Actual | Calculated Share read-only | Other batches rebalanced? |
| --- | --- | --- | --- | --- |
| Farvardin | 2,639,926,180 | 2,639,926,180 | Yes | No |
| Ordibehesht | 2,605,705,600 | 2,605,705,600 | Yes | No |

Negative Applied Share rejected: `Applied Batch Share cannot be negative.`  
Manual override marks parent **Incomplete** until recalculation (share variance); recalculation restores totals.

### Delete + recreate

After hardening `clear_details()`:

- All detail doctypes **and** `MFG Cost Calculation Revision` rows cleared on full delete.
- Orphans after delete: **0**.
- Source ERP fingerprint unchanged on delete.
- Recreated Farvardin financial totals matched pre-delete baseline.

### Failure / recovery

| Case | Result |
| --- | --- |
| Missing UOM | Blocking exceptions; Incomplete; provisional shares; not consumable |
| Overlap detection | `detect_overlapping_rules` returns pairs for duplicate Account×Dept blank CC |
| Empty selling price list | Products `price_status=Missing`, selling_price 0; no crash |
| Incomplete extraction / UOM | Not treated as completed/consumable |

## Accounting integrity

- No duplicate GL Entry rows within a monthly calculation (spot-checked).
- Pool GL line sums reconcile (`gl_lines_to_pool` Passed for both months).
- Farvardin failed recon checks only for `exceptions` / provisional share sums (expected under UOM Incomplete).
- Product Total Cost = Material Consumption Value + Product Allocated Expense (sampled).
- Product Share is within-batch (single-product batches → share 1.0).
- No JE / Stock Entry / GL / SLE / valuation postings from Costing.
- Product × Expense detail rows are intentionally **not** written for new revisions (Batch × Expense is primary).

## UI / export

Playwright Desk checks on `http://127.0.0.1:8000` (`_browser_verify_qa_v5727.py` + follow-up grid probe):

| Check | Result |
| --- | --- |
| Costing workspace `/desk/costing` | OK |
| Settings UX | No Excel attach fields; `rule_generation_summary` + `uom_validation_summary` Tables; `selling_price_list` present; **4 Download** buttons |
| Expense rules child grid | Present with CSV Download; paginated rows; `Excel Generated` source visible |
| Monthly list | Shows `1405-01` / `1405-02` |
| Farvardin form | Persian Period `1405-01`; Exceptions + Reconciliation sections present |
| Ordibehesht form | Persian Period `1405-02` |
| Batch × Expense / Product lists | OK |
| Child-table CSV helper | `public/js/costing_grid_download.js` |

Screenshots under `/tmp/costing_qa_v5727/ui/` (not committed).

## Automated tests (execution evidence)

Environment: `bench --site development.localhost run-tests --app erpnext_extensions --module …`

| Module | Result |
| --- | --- |
| `test_fg_cost_optional_dimensions_v5727` | **14 passed** |
| `test_fg_cost_delete_and_share_v5727` (**new**) | **4 passed** |
| `test_fg_cost_allocation_v5727` | **6 passed** |
| `test_fg_cost_user_scope_v5727` | **7 passed** |
| `test_fg_cost_allocation_v5728` | **13 passed** |
| `test_fg_cost_eligibility_v5728` | **7 passed** |
| `test_product_cost_v5727` | **Blocked** by ERPNext test bootstrap `DuplicateEntryError` on `UOM Conversion Factor` (site fixture collision — not a Costing assertion failure) |

**Costing unit tests executed successfully: 51 passed / 0 failed / 1 module skipped (env).**

New regressions cover: full `clear_details` removes revision orphans; per-revision clear keeps logs; Allocated Amount currency rounding; manual override does not rebalance other batches.

## Fixes in this QA cycle

1. **`clear_details()`** — full clear now deletes `MFG Cost Calculation Revision` rows so `delete_monthly` leaves no orphans.
2. QA Applied Share expectation aligned to Decimal `ROUND_HALF_UP` with company currency precision.
3. Regression module `test_fg_cost_delete_and_share_v5727.py`.

## Known limitations

1. Farvardin Incomplete: item `20100213` missing `BOX(2Vial)` → `BOX(1pfs)` UOM Conversion Detail (source master not modified per safety rules).
2. Settings `selling_price_list` unset → all product selling prices Missing / 0.
3. Product × Expense Allocation not persisted for new revisions (by design).
4. `test_product_cost_v5727` cannot run on this site until ERPNext bootstrap UOM fixture collision is resolved independently.

## Evidence artifacts (local, not in git)

- `/tmp/costing_qa_v5727/qa_report.json`
- `/tmp/costing_qa_v5727/lifecycle_retest.json`
- `/tmp/costing_qa_v5727/ord_share.json`
- `/tmp/costing_qa_v5727/ui/`
- Site copy: `sites/development.localhost/private/costing_qa_v5727/`
- Backup: `sites/preserved_dev_backups/costing_20261112_140446/`

## Production

Unauthorized. User will push the local commit manually after review.
