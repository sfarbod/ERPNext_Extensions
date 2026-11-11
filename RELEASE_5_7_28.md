# Release 5.7.28 — Finished Goods Cost Allocation Enhancement & Audit

**Version:** **5.7.28**  
**Branch:** `develop`  
**Site validated:** `development.localhost` — Company `اسپاد فارمد دارو`, FY **1405** Farvardin–Shahrivar  
**Deployment status:** Local Development only. **No Production push/merge/deploy.**

## 1. Architecture and DocTypes

Module Def / package: **Costing** (Persian Workspace: **بهای تمام شده**).

| DocType | Role |
|---|---|
| Finished Goods Cost Allocation Settings | Company settings + UOM + expense rules |
| FG Item Primary UOM | Child: one Primary UOM per FG item + conversion status |
| Expense Account Dimension Rule | Child: Account × Cost Center × Department |
| Monthly FG Cost Calculation | Parent monthly calculation / revision header |
| MFG Cost Expense Pool | Level 1 pools |
| MFG Cost GL Line | Persisted GL source lines |
| MFG Cost Batch Allocation | Level 2 batches + shares |
| MFG Cost Batch Expense Allocation | Batch × Pool |
| MFG Cost Product Allocation | Level 3 products |
| MFG Cost Product Expense Allocation | Product × Pool |
| MFG Cost Calculation Revision | Audit / revision log |

Engine package: `erpnext_extensions.iran_accounting.fg_cost_allocation`  
Contract: `assert_consumable()` rejects Incomplete / provisional / inconsistent / zero-rule calculations.

## 2. Costing module organization

- All 11 related DocTypes assigned to module **Costing**.
- Public Workspace **بهای تمام شده** (`module=Costing`) for ERPNext navigation.
- No DocType duplication; existing records retained.

## 3. UOM configuration design

- One **Primary UOM** per FG item on Settings (`FG Item Primary UOM`).
- Conversion factor read only from Item **UOM Conversion Detail** (or 1.0 when Stock UOM = Primary UOM).
- Status: Valid / Missing / Ambiguous / Invalid — never invented.
- Action: validate/refresh Item UOM rows (does **not** modify Item masters).
- Missing UOM → product/batch marked exception; month **Incomplete**; shares **provisional** / non-consumable.

### Validation results (اسپاد فارمد دارو)

| Metric | Value |
|---|---|
| FG items scanned | 226 |
| Valid | 39 |
| Missing | 187 |
| Ambiguous | 0 |
| Invalid | 0 |
| Default Primary UOM | سرنگ |

## 4. Eligible items with Valid primary UOM (sample / count)

39 Valid items (Primary UOM = سرنگ). Examples:

| Item | Stock UOM | Primary UOM | Factor |
|---|---|---|---|
| 20010024 | BOX(2pfs) | سرنگ | 2 |
| 20100002 | BOX(2pfs) | سرنگ | 2 |
| 20100007 | BOX(2pfs) | سرنگ | 2 |
| 20100008 | BOX(2pfs) | سرنگ | 2 |
| 20100011 | BOX(2pfs) | سرنگ | 2 |

Full list is on Settings → Item Primary UOMs (status Valid).

## 5. Missing / invalid UOM exceptions (FY1405 production)

- **132** product allocation rows across months with `missing_uom_factor=1`.
- Distinct items (14): `20100006`, `20100009`, `20100015`, `20100042`, `20100043`, `20100068`, `20100102`, `20100103`, `20100144`, `20100190`, `20100209`, `20100210`, `20100212`, `20100213`.
- Typical cause: Stock UOM such as `BOX(1pfs)` / other boxes without a Conversion Detail to `سرنگ`.
- **User action required:** add Item UOM conversions (or change Primary UOM) — do not auto-edit masters.

## 6. Expense accounts beginning with `6`

- **176** leaf accounts discovered dynamically from Chart of Accounts (`account_number LIKE '6%'`).
- No hardcoded account list.

## 7. Department-level rule generation

From Settings `last_rule_generation_summary`:

| Metric | Value |
|---|---|
| Accounts with prefix 6 | 176 |
| Created rules | **988** |
| Duplicates skipped | 0 |
| Excluded اداری/ستادی combos | **615** |
| Blank-CC rules remaining | **0** |
| Period scanned | 2026-03-21 → 2026-09-22 |

Exact Account + Cost Center + Department matching; blank ≠ wildcard.

## 8. Three removed incorrect configuration rows

Unambiguously identified blank-Cost-Center seed rows (removed via `remove_blank_cc_seed_rules`):

| Account | Cost Center | Department |
|---|---|---|
| 611028 - حقوق پایه - E | *(blank)* | واحد فرمولاسیون و ساخت بیولوژیک - E |
| 611028 - حقوق پایه - E | *(blank)* | واحد فرمولاسیون و ساخت غیربیولوژیک - E |
| 611106 - هزینه آموزش کارکنان - E | *(blank)* | واحد بسته بندی - E |

## 9. Excluded اداری / ستادی Cost Centers

Structured leaf Cost Center matching exclusion tokens: **`3000 - اداری و ستاد - E`**.  
Generation excluded **615** Account×CC×Dept GL combinations tied to that classification during FY1405 scan.

## 10. Six-month FY1405 production reconciliation

Source Manufacture SE detail qty vs persisted batch actual qty — **exact match** every month. Normalized qty is lower where UOM is missing (expected; Incomplete).

| Month | Doc | Source rows | Source qty | Batch qty | Normalized | UOM exceptions |
|---|---|---|---|---|---|---|
| 1 | MFGCC-2026-00001 | 15 | 5,602 | 5,602 | 3,326 | 5 |
| 2 | MFGCC-2026-00002 | 42 | 26,274 | 26,274 | 5,576 | 19 |
| 3 | MFGCC-2026-00003 | 139 | 93,575 | 93,575 | 92,644 | 29 |
| 4 | MFGCC-2026-00004 | 116 | 92,456 | 92,456 | 67,282 | 20 |
| 5 | MFGCC-2026-00005 | 151 | 125,050 | 125,050 | 76,264 | 35 |
| 6 | MFGCC-2026-00006 | 81 | 110,383 | 110,383 | 104,144 | 24 |

All months: `shares_are_provisional=1`, status **Incomplete**, `assert_consumable` blocked.

## 11. Six-month expense extraction / allocation reconciliation

Pool net = header identified = batch allocated = product allocated (IRR). Excluded GL = account-6 activity not matching an enabled rule dimension.

| Month | Identified | Allocated | Unallocated | Excluded GL | Active pools | GL lines | Batches |
|---|---|---|---|---|---|---|---|
| 1 | 158,778,930,060 | 158,778,930,060 | 0 | 11,737,872,905 | 163 | 2,647 | 8 |
| 2 | 394,738,198,895 | 394,738,198,895 | 0 | 107,106,348,779 | 196 | 3,097 | 21 |
| 3 | 494,136,940,675 | 494,136,940,675 | 0 | 60,600,859,272 | 239 | 3,960 | 73 |
| 4 | 604,055,173,522 | 604,055,173,522 | 0 | 186,854,845,955 | 560 | 3,953 | 56 |
| 5 | 724,333,764,208 | 724,333,764,208 | 0 | 51,451,819,114 | 274 | 3,884 | 78 |
| 6 | 1,359,501,354,815 | 1,359,501,354,815 | 0 | 426,336,900,550 | 737 | 4,721 | 81 |

Money columns widened to `DECIMAL(30,3)` (patch `expand_fg_cost_amount_precision_v5728`) for trillion-scale IRR totals.  
`recon_ok=False` solely because UOM incomplete (provisional shares); money levels reconcile.

## 12. Test results

| Module | Result |
|---|---|
| `test_fg_cost_allocation_v5727` | **6/6 passed** |
| `test_fg_cost_allocation_v5728` | **13/13 passed** |
| Failed / skipped | **0** |

Coverage includes: primary UOM resolution, missing/ambiguous/invalid conversion, provisional shares, zero-pool skip, admin/staff exclusion tokens, exact three seed-rule preview, consumable gate, month-wide shares, residuals, manual override non-renorm.

## 13. Remaining master-data / user-configuration actions

1. **Add Item UOM Conversion Detail** to `سرنگ` (or set alternate Primary UOM) for the 187 Missing FG items — especially the 14 items appearing in FY1405 production.
2. Review **excluded GL** amounts: add Department rules for legitimate manufacturing dimensions if intended to be pooled.
3. After UOM fixes: **Recalculate** all months; lock only when `is_consistent=1` and shares not provisional.
4. Confirm whether Cost Center exclusion should use a dedicated Cost Center category field (currently name/token based on `اداری` / `ستادی` / `ستاد`).

## 14. Commit hash and version

- **Version:** 5.7.28  
- **Commit:** *(see git after release commit)*

## 15. Unresolved defects / limitations

| Item | Status |
|---|---|
| FY1405 months Incomplete due to missing UOM | **Blocked by master data** |
| Shares provisional (successful-only denominator for diagnostics) | **Implemented & tested**; non-consumable by contract |
| Zero-activity rules not persisted as empty pools | **Implemented** (cardinality); configured rules remain on Settings |
| Prior-revision cleanup post-commit best-effort | **Implemented**; orphans possible under lock contention |
| Expense absorption / JE posting | **Out of scope** (by design) |
| Production documents / Item masters auto-fix | **Not done** (forbidden) |

### Classification legend

- **Implemented and tested:** Costing module, UOM config engine, department rule generation, seed removal, bulk persist, atomic recalc commit, consumable gate, unit tests.
- **Implemented but not fully validated:** Multi-revision archive comparison UI; full 187-item conversion master-data completeness.
- **Blocked by missing master data:** Complete (non-provisional) FY1405 calculations.
- **Requiring user approval:** Any Item master UOM edits; production deploy; future absorption feature.
