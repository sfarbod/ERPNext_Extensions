# Release 5.7.28 — Finished Goods Cost Allocation (Costing)

**Version:** **5.7.28**  
**Branch:** `develop`  
**Site validated:** `development.localhost` — Company `اسپاد فارمد دارو`  
**Deployment status:** Local Development validated. Production migrate requires the Sidebar hotfix procedure below. **Do not rewrite published tag `v5.7.27`.**

This document consolidates Costing work previously tracked under interim notes 5.7.28 / 5.7.29 / 5.7.30 and the 5.7.27 QA + Sidebar repair cycle into a single application release version **5.7.28**.

Historical patch module names (e.g. `…_v5727`, `…_v5728`, `…_v5730`) are **identifiers only** and must remain for sites that already executed them.

---

## A. Costing Module

### Architecture

Module Def / package: **Costing**.

| DocType | Role |
|---|---|
| Finished Goods Cost Allocation Settings | Company settings, Primary UOMs, expense rules, audit summaries |
| FG Item Primary UOM | Child: Primary UOM + source/manual/applied conversion factors |
| Expense Account Dimension Rule | Child: Account × Department (Cost Center optional) |
| Monthly FG Cost Calculation | Parent monthly calculation / revision header |
| MFG Cost Expense Pool | Level 1 expense pools |
| MFG Cost GL Line | Persisted GL source lines |
| MFG Cost Batch Allocation | Level 2 batches + shares |
| MFG Cost Batch Expense Allocation | Batch × Expense Pool |
| MFG Cost Product Allocation | Level 3 products + material/selling economics |
| MFG Cost Product Expense Allocation | Historical Product × Pool (not written for new revisions) |
| MFG Cost Account Dimension Summary | Account × dimension rollup |
| MFG Cost Reconciliation Check | Structured reconciliation rows |
| MFG Cost Exception | Structured exceptions |
| MFG Cost Calculation Revision | Audit / revision log |

Engine: `erpnext_extensions.iran_accounting.fg_cost_allocation`  
Contract: `assert_consumable()` / `is_consumable()` reject Incomplete, provisional, inconsistent, zero-rule, or over-allocated calculations.

### Eligibility (Item Group)

Eligible Finished Goods are **only** Items in Item Group **`تزریقی`**, verified as a child of **`محصول نهایی`**.

| Metric | Count |
|---|---|
| Enabled Items in `تزریقی` | **220** |
| Sibling `خوراکی` under parent | Excluded |
| Outside groups (e.g. `اینسپکت`) | Excluded at source; reported, not silent |

Constants: `FG_ITEM_GROUP=تزریقی`, `FG_PARENT_ITEM_GROUP=محصول نهایی`. Hierarchy checked on calculation / UOM refresh.

### Expense Account Dimension Rules (current)

| Check | Result |
|---|---|
| Enabled rules | **3,026** |
| Unique accounts | **89** |
| Unique departments | **34** |
| Cost Center blank | **3,026 / 3,026** |
| Duplicate keys / overlaps | **0 / 0** |

Generation: Account × Department from Excel (`معین حساب` × `مرکز` as Department). Cost Center intentionally blank. Row `مشترک عمومی شرکت` excluded pending master data.

**Matching semantics (authoritative):**

- **Account** — exact mandatory match.
- **Blank Cost Center / Department on a rule** — unrestricted filter (wildcard) for that dimension.
- **Populated dimension** — exact match only.
- Overlapping enabled rules → extraction conflict → month **Incomplete** (no silent double allocation).
- Each GL line consumed at most once across pools.

> Historical note: an earlier interim design used exact blank≠wildcard and generated 988 Account×CC×Dept rules with اداری/ستادی exclusions. That configuration was superseded by the 3,026 Account×Department blank-CC model above.

### UOM normalization

- One **Primary UOM** per configured FG item (`FG Item Primary UOM`).
- Applied factor = Manual Override when set; else ERPNext Item UOM Conversion Detail (or 1.0 when Stock UOM = Primary).
- Status: Valid / Missing / Ambiguous / Invalid / Manual Override — never invent factors.
- Missing UOM → structured exceptions; month Incomplete; shares provisional; not consumable.
- **Item masters are never written** by Costing.

**Verified Costing Manual Override — Item `20100213`:**

| Field | Value |
|---|---|
| Stock UOM | `BOX(2Vial)` |
| Primary UOM | `BOX(1pfs)` |
| ERPNext source factor | `0` (no Conversion Detail for Primary) |
| Manual Override / Applied | **2.0** |
| Farvardin evidence | actual **25** → normalized **50**; `missing_uom_factor=0` |

### Allocation model

1. **Expense Pools** from matched GL lines (Account × optional dims).
2. **Batch Share** = Batch Normalized Qty / Σ eligible FG batches in the Persian month.
3. **Batch × Expense Pool:** `Allocated Amount = Pool Monthly Net × Applied Batch Share` (currency `ROUND_HALF_UP`).
4. **Calculated Batch Share** remains read-only; **Applied Batch Share** editable with audit; no silent rebalance of other batches.
5. **Product Share** within batch; Total Product Cost = Material Consumption Value + Product Allocated Expense.
6. Unit Cost on Actual / Normalized; Selling Price from Settings Selling Price List when configured.

No JE, Stock Entry, GL, SLE, or valuation postings from Costing.

### Desk navigation

| Layer | Canonical name | Notes |
|---|---|---|
| Module Def | `Costing` | App `erpnext_extensions` |
| Desktop Icon / Workspace / Sidebar | `Costing` (English) | Evolved from interim Persian tile `بهای تمام شده` |
| Patch (historical) | `ensure_costing_desk_module_v5730` | Seeded Persian Desk shell |
| Patch (historical) | `ensure_costing_english_desk_v5727` | Migrates to English Costing labels |
| Patch (repair) | `repair_costing_english_desk_sidebar_v5727` | Idempotent Sidebar/DocType ensure |

Sections: Settings · Monthly Calculations · Reports & Details · Management & History.

### UI grids and export

- Custom HTML recon/exception/account tables replaced with **summary panels** + standard **List Views** + DocType **Connections**.
- Settings child tables remain native Frappe Tables with Download + XLSX.
- Server CSV/XLSX export (`export_costing.py`) for detail DocTypes and Settings children; period-aware filenames (e.g. `Costing_Reconciliation_1405-01.csv`); all matching rows (chunked), UTF-8 BOM; empty export messaging.

### Lifecycle hardening

- Idempotent recalculation (financial totals stable; revision advances).
- Applied Batch Share edit + negative rejection; delete clears detail rows **and** revision orphans (`clear_details`).
- Delete/recreate financial match verified; source ERP fingerprint unchanged.

### Final calculation evidence (development.localhost)

#### Farvardin 1405 (`1405-01`) — `MFGCC-2026-00009`

| Metric | Result |
|---|---|
| Status | **Recalculated**, consistent, consumable |
| Identified = Allocated | **241,011,762,837** |
| Pools / GL / Batches / Products | 421 / 1,730 / 8 / 8 |
| Batch × Expense | **3,368** |
| Missing UOM / Exceptions | **0 / 0** |

#### Ordibehesht 1405 (`1405-02`) — `MFGCC-2026-00008`

| Metric | Result |
|---|---|
| Status | **Recalculated**, consistent, consumable |
| Identified = Allocated | **79,048,154,971** |
| Pools / GL / Batches / Products | 99 / 1,674 / 21 / 21 |
| Batch × Expense | 2,079 |
| Missing UOM / Exceptions | **0 / 0** |

Source ERP during QA: GL 815,194 · SLE 182,831 · SE 20,806 — **unchanged**. Rules **3,026** preserved.

---

## B. Production Migration Hotfix (Workspace Sidebar)

### Incident

Production site `erp.espadpharmed.com` failed during:

`erpnext_extensions.patches.post_model_sync.ensure_costing_english_desk_v5727`

at `_ensure_sidebars()` → `sync_module_sidebar_items()` → Workspace Sidebar save with:

`frappe.exceptions.LinkValidationError`

Earlier patches completed: `expand_fg_cost_amount_precision_v5728`, `ensure_costing_desk_module_v5730`.

### Root cause

Workspace Sidebar Item `link_to` is a **Dynamic Link** (`options=link_type`). For `link_type=DocType`, Frappe requires the DocType row to exist. Costing DocTypes were **not guaranteed** present before Sidebar sync. The Persian desk patch used `ignore_links=True`, masking the gap. Sync progress “100%” does not prove every Costing DocType row exists. Tag contents **did** include the DocType JSON files — this was a patch ensure gap, not a missing-file packaging gap.

### Fix (application version 5.7.28; historical patch ids retained)

1. **`_ensure_costing_doctypes()`** — force-import required Costing DocType JSON, clear cache, assert `tabDocType` rows.
2. **`assert_sidebar_link_targets_exist()`** — explicit missing-target list before save; **link validation is not disabled**.
3. **`repair_costing_english_desk_sidebar_v5727`** — idempotent re-apply for sites that already recorded the original English desk patch.

### Safe production recovery (authorized deploy only)

1. Confirm Patch Log (read-only) for completed vs pending Costing patches.
2. Confirm Costing DocTypes in `tabDocType` (read-only).
3. Deploy **5.7.28** code (do **not** rewrite tag `v5.7.27`).
4. `bench --site <site> migrate` — completed patches skipped; failed English desk patch retries if not logged; repair patch runs once.
5. Verify with `ensure_costing_english_desk_v5727.ensure_now`.

Do **not** manually mark the failed patch complete. Do **not** delete Workspace/Sidebar rows before retry. Desk patches do not mutate GL/SLE/Stock Entry/rules.

---

## C. Tests and QA Evidence

| Suite | Result |
|---|---|
| Costing final QA unit modules (optional dims, delete/share, allocation, user scope, eligibility, product cost, UOM/export) | **62 passed / 0 failed / 0 skipped** |
| Sidebar regression `test_costing_desk_sidebar_v5727` | **5 passed** |
| Development `bench migrate` after Sidebar fix (`repair_costing_english_desk_sidebar_v5727`) | **Success** |
| Farvardin / Ordibehesht consumable recalculation | **Verified** (see §A) |
| Production migrate after hotfix | **Not claimed** — pending authorized deploy |

Interim eligibility/allocation modules also green historically: `test_fg_cost_eligibility_v5728` 7/7, `test_fg_cost_allocation_v5728` 13/13, `test_fg_cost_allocation_v5727` 6/6.

### Known limitations

1. Settings **`selling_price_list` unconfigured** → product `price_status=Missing`, selling_price 0.
2. Item Master may still lack Primary UOM Conversion Detail where Costing uses Manual Override (by design; masters not written).
3. Product × Expense rows not written for new revisions (Batch × Expense is primary).
4. Expense absorption / JE posting remains out of scope.

---

## Patch compatibility review

| Patch id (historical) | Role | Action in 5.7.28 |
|---|---|---|
| `expand_fg_cost_amount_precision_v5728` | DECIMAL width for IRR totals | Keep; already executed on many sites |
| `ensure_costing_desk_module_v5730` | Persian Desk seed | Keep; do not rename |
| `ensure_costing_english_desk_v5727` | English Desk + DocType ensure (fixed) | Keep; retryable if failed |
| `repair_costing_english_desk_sidebar_v5727` | Idempotent repair | Keep; required for recovery |

No executed patch modules renamed or removed. Application `__version__` is independent of patch filename suffixes.

---

## Version metadata

- `erpnext_extensions/__init__.py` → `__version__ = "5.7.28"`
- `pyproject.toml` → `dynamic = ["version"]` (reads `__init__.py` via flit)

---

## Superseded interim notes

Content from `RELEASE_5_7_29.md` (تزریقی eligibility) and `RELEASE_5_7_30.md` (Persian Desk tile) is merged above. Those files are removed as part of this consolidation. Earlier baseline audit detail remains available in git history for those paths.
