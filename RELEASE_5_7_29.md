# Release 5.7.29 — FG Cost Eligibility: Item Group `تزریقی` only

**Version:** **5.7.29**  
**Branch:** `develop`  
**Site validated:** `development.localhost` — Company `اسپاد فارمد دارو`, FY **1405**  
**Deployment status:** Local Development only. **No Production deploy.**

## Scope correction

Eligible Finished Goods for costing are **only** Items whose `item_group` is **`تزریقی`**, verified as a child of **`محصول نهایی`**.  
Other groups under `محصول نهایی` (e.g. `خوراکی`) and all outside groups are excluded at the **source extraction layer**, with exclusion rows reported (never silently dropped).

## 1. Verified Item Group hierarchy

| Item Group | Parent | is_group |
|---|---|---|
| محصول نهایی | گروه کالا اسپاد فارمد اصلی | 1 |
| **تزریقی** | **محصول نهایی** | 0 |
| خوراکی | محصول نهایی | 0 |

Hierarchy check runs on every calculation / UOM refresh via `verify_fg_item_group_hierarchy()`.

## 2. Eligible `تزریقی` Items

| Metric | Count |
|---|---|
| Enabled Items in `تزریقی` (parent verified) | **220** |
| Disabled Items in `تزریقی` (excluded from UOM refresh) | 12 (232 total − 220) |

## 3. Excluded Items / Item Groups

| Item Group | Parent | Enabled Items | Role in costing |
|---|---|---|---|
| خوراکی | محصول نهایی | **6** | Sibling under parent — **excluded** from UOM + production |
| اینسپکت | نیمه ساخته | (semi-finished) | Outside parent — manufacture rows **reported as excluded** when present |

No Item Code / name heuristics; classification uses Item + Item Group masters only.

## 4. Eligible production quantities (FY1405)

| Month | Doc | Eligible SED rows | Eligible actual qty | Excluded rows | Excluded qty | Normalized qty |
|---|---|---|---|---|---|---|
| 1 | MFGCC-2026-00001 | 15 | 5,602 | 0 | 0 | 3,326 |
| 2 | MFGCC-2026-00002 | 42 | 26,274 | **3** | **13,959** (`اینسپکت`) | 5,576 |
| 3 | MFGCC-2026-00003 | 139 | 93,575 | 0 | 0 | 92,644 |
| 4 | MFGCC-2026-00004 | 116 | 92,456 | 0 | 0 | 67,282 |
| 5 | MFGCC-2026-00005 | 151 | 125,050 | 0 | 0 | 76,264 |
| 6 | MFGCC-2026-00006 | 81 | 110,383 | 0 | 0 | 104,144 |

Eligible source qty = persisted batch actual qty every month. Excluded qty is **not** in the month-wide denominator.

## 5. UOM counts (eligible Items only)

| Status | Count |
|---|---|
| Valid | **39** |
| Missing | **181** |
| Ambiguous | 0 |
| Invalid | 0 |
| Default Primary UOM | سرنگ |

Previous 226 / 187 figures are obsolete (included non-`تزریقی` FG under `محصول نهایی`).

## 6. Updated production UOM exception list

- **132** product rows with `missing_uom_factor` across FY1405 (eligible `تزریقی` only).
- Distinct items (**14**): `20100006`, `20100009`, `20100015`, `20100042`, `20100043`, `20100068`, `20100102`, `20100103`, `20100144`, `20100190`, `20100209`, `20100210`, `20100212`, `20100213`.
- Non-eligible Items do **not** create UOM exceptions.

## 7. Monthly allocation / reconciliation

All six months: status **Incomplete**, `shares_are_provisional=1`, `uom_complete=false` (missing UOM on eligible items).  
Money reconciliation still holds: pool net = batch allocated = product allocated; unallocated = 0.

| Month | Identified | Allocated | Excluded GL |
|---|---|---|---|
| 1 | 158,778,930,060 | 158,778,930,060 | 11,737,872,905 |
| 2 | 394,738,198,895 | 394,738,198,895 | 107,106,348,779 |
| 3 | 494,136,940,675 | 494,136,940,675 | 60,600,859,272 |
| 4 | 604,055,173,522 | 604,055,173,522 | 186,854,845,955 |
| 5 | 724,333,764,208 | 724,333,764,208 | 51,451,819,114 |
| 6 | 1,359,501,354,815 | 1,359,501,354,815 | 426,336,900,550 |

Exclusion detail is stored on each monthly doc under `reconciliation_summary.item_group_exclusions` and `exceptions_json.item_group_exclusions`.

## 8. Automated tests

| Module | Result |
|---|---|
| `test_fg_cost_eligibility_v5728` | **7/7 passed** |
| `test_fg_cost_allocation_v5728` | **13/13 passed** |
| `test_fg_cost_allocation_v5727` | **6/6 passed** |
| Total | **26/26 passed** |

Coverage: hierarchy verification; include `تزریقی`; exclude sibling / outside groups; mixed SE only keeps eligible rows with traceable exclusions; non-eligible qty not in denominator; non-eligible Items do not create UOM exceptions.

## Implementation notes

- Constants: `FG_ITEM_GROUP=تزریقی`, `FG_PARENT_ITEM_GROUP=محصول نهایی`
- Modules touched: `eligibility.py`, `production_extract.py`, `uom_config.py`, `engine.py`
- No JE / SE / GL / SLE / Item master writes
- Still blocked for consumable lock until eligible Item UOM conversions to `سرنگ` are completed
