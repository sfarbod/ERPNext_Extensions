# Release 5.7.27 — Finished Goods Cost Allocation (Costing QA Hardening)

**Version:** **5.7.27** (frozen; no bump)  
**Branch:** `develop`  
**Site validated:** `development.localhost` only  
**Company:** `اسپاد فارمد دارو`  
**Deployment:** Local Development only. **No push, tag, publish, or Production deploy.**

**Commits (local):**
1. `cd04667057016113b84d825f4eaf5049743ac5c7` — initial Costing QA hardening
2. Follow-up commit (this cycle) — UOM verification, grid standardization, export coverage

## Summary

End-to-end Costing QA for Persian months **Farvardin 1405 (`1405-01`)** and **Ordibehesht 1405 (`1405-02`)**, with optional-dimension rules (3,026), UOM Manual Override verification for Item `20100213`, native Desk list/export UX (no custom HTML grids), lifecycle hardening, and regression tests.

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

## Item `20100213` UOM resolution (verified)

| Item | Evidence |
| --- | --- |
| Where corrected | **Costing Settings → FG Item Primary UOM** row `n4sk1s1tg2` (parent company Settings) |
| Type | **Costing-specific Manual Override** — **not** Item Master / UOM Conversion Detail |
| Stock UOM | `BOX(2Vial)` |
| Primary UOM | `BOX(1pfs)` |
| ERPNext source factor | `0` (no `UOM Conversion Detail` for `BOX(1pfs)` on Item — master untouched) |
| Manual Override Factor | **2.0** |
| Applied Conversion Factor | **2.0** (`conversion_status=Manual Override`) |
| Packaging meaning | 1 Stock `BOX(2Vial)` → **2** Primary `BOX(1pfs)` normalized units |
| Farvardin product evidence | Batch `504154-20100213-KA261501E11` · actual **25** · normalized **50** · factor **2** · `missing_uom_factor=0` |

Recalculation after override: previous blocking Missing UOM exceptions cleared (not suppressed). Shares finalized to 100% (no longer provisional). Batch × Expense rows increased **2,947 → 3,368** (= 421 pools × 8 batches) once the eighth batch participates in complete allocation.

## Backup (pre-calculation)

| Item | Value |
| --- | --- |
| Path | `/workspace/development/frappe-bench/sites/preserved_dev_backups/costing_20261112_140446` |
| Dump | `costing_tables.sql` (**1,797,749** bytes) |
| Manifest | `manifest.json` (`verified: true`) |
| Source ERP | GL 815,194 · SLE 182,831 · SE 20,806 — **unchanged** through QA |

## Final monthly results

### Farvardin 1405 (`1405-01`) — `MFGCC-2026-00009`

| Metric | Result |
| --- | --- |
| Status / revision | **Recalculated** / 4 · `is_consistent=1` |
| Identified = Allocated | **241,011,762,837** = **241,011,762,837** |
| Pools / GL / Batches / Products | 421 / 1,730 / 8 / 8 |
| Batch × Expense | **3,368** |
| Missing UOM / Exceptions | **0 / 0** · shares not provisional |
| Idempotent re-run | Yes (totals unchanged; source GL/rules unchanged) |
| Consumable | **Yes** |
| Selling price | Still `Missing` (Settings `selling_price_list` empty) |

### Ordibehesht 1405 (`1405-02`) — `MFGCC-2026-00008`

| Metric | Result |
| --- | --- |
| Status / revision | **Recalculated** / 5 · `is_consistent=1` |
| Identified = Allocated | **79,048,154,971** = **79,048,154,971** |
| Pools / GL / Batches / Products | 99 / 1,674 / 21 / 21 |
| Batch × Expense | 2,079 |
| Missing UOM / Exceptions | **0 / 0** |
| Idempotent re-run | Yes |
| Consumable | **Yes** |
| Item `20100213` | Not produced in this month (no product row) |

## Grid audit & standardization

| Surface | Before | After |
| --- | --- | --- |
| Monthly Reconciliation | Custom HTML `<table>` (max 30 rows → misleading counts) | Native **summary panel** + standard **List View** + Connections + Export |
| Monthly Exceptions | Custom HTML table; empty-state could contradict status | Summary panel with accurate revision counts; List View for full rows |
| Account / Dimension Summary | Custom HTML table | Summary panel + List View |
| Settings Item Primary UOMs / Rules / summaries | Native child Table | Unchanged; Download + **XLSX** enabled |
| Detail DocTypes (Pool, GL, Batch, BxE, Product, …) | List View | List View + **Export CSV/XLSX** buttons + Connections from Monthly |

Playwright evidence (`/tmp/costing_qa_v5727/ui_final/`): Monthly form **3** summary panels, **0** embedded `table-bordered` grids, Open List / Export CSV present, Consumable indicator shown; Settings **4** Download + **4** XLSX controls.

DocType Connections added on Monthly FG Cost Calculation for all detail DocTypes.

## Export coverage matrix

Server: `export_costing.export_costing_doctype` / `export_settings_child_table` (CSV + XLSX, UTF-8 BOM, all matching rows chunked).

| Grid / list | CSV | XLSX | Filters | Filename example | Row-count check |
| --- | --- | --- | --- | --- | --- |
| Expense Account Dimension Rules (Settings child) | Yes | Yes | All child rows | `Costing_Expense_Rules.csv` | Settings 3,026 |
| Item Primary UOMs | Yes | Yes | All child rows | `Costing_Item_Primary_UOMs.csv` | 220 |
| Rule / UOM Validation summaries | Yes | Yes | All child rows | `Costing_UOM_Validation_Summary.csv` | — |
| Reconciliation (monthly) | Yes | Yes | monthly + revision | `Costing_Reconciliation_1405-01.csv` | 100 = DB |
| Exceptions | Yes | Yes | monthly + revision | `Costing_Exceptions_1405-01.csv` | 0 = DB |
| Batch × Expense | Yes | Yes | monthly + revision | `Costing_Batch_Expense_1405-01.csv` | 3,368 |
| Product Allocation | Yes | Yes | monthly + revision | `Costing_Product_Allocation_1405-01.csv` | 8 |
| Expense Pools / GL / Batches / Account Dimension / Revisions | Yes | Yes | List filters + search | `Costing_*_{period}.csv` | Chunked export |

Empty export shows a Desk message (no silent empty file claimed as success).

## Lifecycle / integrity (prior + reconfirmed)

- Idempotency, Applied Share edit (currency `ROUND_HALF_UP`), negative share rejection, delete orphan cleanup (including revisions), overlap detection — covered in prior commit and still green.
- Identified = Allocated when complete; no source ERP mutations; 3,026 rules preserved.

## Automated tests (execution evidence)

| Module | Result |
| --- | --- |
| `test_fg_cost_uom_export_v5727` (**new**) | **7 passed** |
| `test_product_cost_v5727` (**fixed isolation**) | **4 passed** (no longer uses `FrappeTestCase` / ERPNext bootstrap) |
| `test_fg_cost_delete_and_share_v5727` | **4 passed** |
| `test_fg_cost_optional_dimensions_v5727` | **14 passed** |
| `test_fg_cost_allocation_v5727` | **6 passed** |
| `test_fg_cost_user_scope_v5727` | **7 passed** |
| `test_fg_cost_allocation_v5728` | **13 passed** |
| `test_fg_cost_eligibility_v5728` | **7 passed** |

**Total this cycle: 62 passed / 0 failed / 0 skipped.**

## Fixes in follow-up cycle

1. Verified Costing Manual Override for `20100213`; Farvardin recalculated to consumable Recalculated.
2. Removed custom HTML recon/exception/account tables; native summary panels + List/Connections.
3. Server-side Costing CSV/XLSX export with period filenames; List View + Settings XLSX buttons.
4. `is_consumable` Desk helper; `assert_consumable` shared evaluation.
5. `test_product_cost_v5727` fixture isolation; UOM override + export unit tests.

## Known limitations

1. Settings **`selling_price_list` still empty** → product `price_status=Missing`, selling_price 0 (configuration, not a silent suppress).
2. Item Master still lacks `BOX(1pfs)` UOM Conversion Detail for `20100213` — Costing uses Manual Override only (by design; masters not written).
3. Product × Expense Allocation rows are not written for new revisions (Batch × Expense is primary).
4. Material value for `20100213` Farvardin batch shows a very small material amount (150) vs peers — observed from source Manufacture SE/SLE attribution; not invented.

## Evidence artifacts (local, not in git)

- `/tmp/costing_qa_v5727/final_recalc.json`
- `/tmp/costing_qa_v5727/ui_final/`
- Backup: `sites/preserved_dev_backups/costing_20261112_140446/`

## Production migration incident (Workspace Sidebar LinkValidationError)

### Root cause

`ensure_costing_english_desk_v5727` called `sync_module_sidebar_items()` → `_sync_workspace_sidebar()` → `Workspace Sidebar.save()` **without** ensuring Costing DocTypes exist and **without** pre-validating Dynamic Link `link_to` targets.

During migrate, Frappe 16.52 still allows writes to the archived `Workspace Sidebar`. Child row `link_to` is a Dynamic Link (`options=link_type`). For `link_type=DocType`, Frappe resolves `tabDocType.name == link_to`. Missing DocType rows raise:

`frappe.exceptions.LinkValidationError: Could not find Row #N: Link To: <DocType>`

The earlier patch `ensure_costing_desk_module_v5730` created the Persian Desk shell using `ignore_links=True`, so it **masked** missing Costing DocTypes and could complete even when Link targets were unresolved. DocType sync progress at 100% does **not** guarantee every Costing DocType row exists.

Missing Link To targets reported in production matched the sidebar item list:

- Finished Goods Cost Allocation Settings
- Monthly FG Cost Calculation
- MFG Cost Expense Pool / GL Line / Batch Allocation / Batch Expense Allocation / Product Allocation
- MFG Cost Calculation Revision

Tag `v5.7.27` **includes** all Costing DocType JSON files under module `Costing`; this was a **patch-order/ensure gap**, not a missing-file packaging gap.

### Fix (backward compatible, version stays 5.7.27)

1. `_ensure_costing_doctypes()` force-imports required DocType JSON then asserts `tabDocType` rows exist (clear cache first).
2. `assert_sidebar_link_targets_exist()` fails with an explicit missing-target list **before** save (does not disable link validation).
3. Idempotent repair patch `repair_costing_english_desk_sidebar_v5727` re-applies English desk sync on sites that already recorded the original patch.

### Development evidence

- Unit tests: `test_costing_desk_sidebar_v5727` — **5 passed**
- Reproduced `LinkValidationError` with a fake missing Link To on Workspace Sidebar
- Full `execute()` of fixed English desk patch: Workspace Sidebar + Sidebar each **19** items; Desktop Icon `Costing`; rules **3026** / GL **815194** untouched
- `bench migrate` applied `repair_costing_english_desk_sidebar_v5727` successfully (`Success: Done in 0.726s`)

### Production recovery (do not run until authorized)

State after the failed migrate:

- Completed (keep): `expand_fg_cost_amount_precision_v5728`, `ensure_costing_desk_module_v5730`
- Failed / not recorded as successful: `ensure_costing_english_desk_v5727` (unless `--skip-failing` marked skipped — check Patch Log)
- Pending after deploy: fixed `ensure_costing_english_desk_v5727` (if not completed) + `repair_costing_english_desk_sidebar_v5727`

Recommended sequence on `erp.espadpharmed.com` after deploying the hotfix commit (no force-push of tag; ship hotfix commit/tag only with explicit approval):

```bash
# 1) Confirm Patch Log (read-only)
bench --site erp.espadpharmed.com mariadb -e \
  "SELECT patch, skipped, creation FROM \`tabPatch Log\` WHERE patch LIKE '%costing%' ORDER BY creation;"

# 2) Confirm whether Costing DocTypes exist (read-only)
bench --site erp.espadpharmed.com mariadb -e \
  "SELECT name FROM \`tabDocType\` WHERE module='Costing' ORDER BY name;"

# 3) Deploy hotfix code for erpnext_extensions (pull/checkout approved commit). Do not rewrite v5.7.27.

# 4) Resume migrate — completed patches are skipped automatically
bench --site erp.espadpharmed.com migrate

# 5) Verify
bench --site erp.espadpharmed.com execute \
  erpnext_extensions.patches.post_model_sync.ensure_costing_english_desk_v5727.ensure_now
```

Expected outcomes:

- Migrate does **not** re-run completed precision / Persian desk patches
- Failed English desk patch (if absent from Patch Log) runs with DocType ensure and succeeds
- Repair patch runs once and is idempotent
- No GL / SLE / Stock Entry / rule-table mutations from these desk patches

**Do not** manually insert a Patch Log row for the failed patch. **Do not** delete production Workspace/Sidebar rows before retry.

## Production

Unauthorized for push/deploy/repair execution during investigation. User reviews local hotfix commit, then authorizes production steps.
