# Release 5.4.1 — Job Card Stock-State Rebuild (completed)

## Summary

- **Job Card Stock Rebuild** desk tool: user selects **one Job Card** and reconstructs material / secondary **tracking state** from authoritative submitted Stock Entry / SLE evidence.
- **Secondary Item Type Reconciliation (completion):** detects business-type inconsistencies (e.g. Co-Product → Scrap → COMPONENT_SCRAP), suggests with evidence/confidence, and applies **only** after explicit **per-row** user approval.
- Lifecycle: **SCAN → PREVIEW → DRY RUN → CONFIRM → APPLY → VERIFY**, plus full selected-Job-Card **Manufacture readiness** via the normal Make Stock Entry path (savepoint / not persisted).
- Apply is fail-closed, atomic, Job-Card-scoped fingerprint-gated, and **never** mutates Manufacture, SLE, GL, or valuation.

## What this release does

- Reconstructs Job Card Item tracking fields from stock movements (`transferred_qty`, `consumed_qty`, custom issued/returned/WIP fields) using ERPNext 16.37 ManufactureEntry semantics (`qty = transferred_qty − consumed_qty` under Material-Transferred backflush).
- Deterministic backfill of `Stock Entry Detail.job_card_item` when ownership is unique and proven.
- Batch-aware and UOM-aware reconciliation; ambiguous ownership / batch mismatch / missing UOM mapping block Apply for the **selected** Job Card dependency closure only.
- Secondary Items inventory with explicit classifications (Component Scrap, Product Reject, Co-/By-Product, Additional Finished Good, Ordinary Scrap) — **no class merging**, no invented equivalent factors.
- **Suggested Secondary Type Change** UI section with per-row Change Type checkbox (**unchecked by default**). Backend revalidates row identity, current type, suggested type, and evidence fingerprint.
- Full selected-JC Dry Run / Apply Manufacture readiness: `make_stock_entry_for_semi_fg_item` → ManufactureEntry → `set_secondary_items_from_job_card` → Iran classifiers → `allocate_stage_output_cost`.
- Runtime Apply gates are **Job Card scoped**. Global Iran Accounting regression suites remain development/release tests, not Apply blockers.
- Audit DocType: **Job Card Stock Rebuild Log** (includes secondary suggestions/approvals).

## What this release does NOT do

- No automatic Secondary Item type change without explicit per-row approval.
- No Manufacture cancel / amend / regenerate / submit.
- No Material Issue creation or fake stock movements.
- No SLE / GL / rate changes.
- No weakening of Iran Accounting or stage-output guards.
- No invented common UOM / equivalent factor = 1.
- No change to `MANUFACTURE_COSTING_CONTRACT_VERSION` (**5.3.43**).
- App version remains **5.4.1** (no bump to 5.4.2).

## Compatibility

- ERPNext **16.37.0** / Frappe **16.36.1**
- erpnext_extensions **5.4.1**

## Primary canary — PO-JOB10492

- Secondary `13100134` × 125 Nos was mislabeled **Co-Product** / **CO_PRODUCT** and incorrectly joined the stage-equivalent set with `30100026` MAIN_FG + MAIN_PRODUCT_REJECT.
- Tool suggests **Co-Product → Scrap** with expected Iran class **COMPONENT_SCRAP** (confidence HIGH/PROVEN).
- Unapproved Dry Run: type unchanged; Manufacture readiness **BLOCKED** (real stage UOM error reproduced).
- Approved Dry Run / Apply: type → Scrap; stage set is MAIN_FG + MAIN_PRODUCT_REJECT only; `13100134` classified COMPONENT_SCRAP; no Custom 14 duplicate scrap.

## Controls

- **PO-JOB10433** — `13100134` already Scrap / COMPONENT_SCRAP → NO TYPE CHANGE.
- **PO-JOB08760** — raw tracking `13200544`: issued 1160 / returned 12 / consumed 0 / WIP 1148 preserved.
- **PO-JOB08761** — item-scoped `13200544`: issued 2920 / returned 30 / consumed 2890 / remainder 0 → BALANCED / NO CHANGE.

## Modules

- `erpnext_extensions.iran_accounting.job_card_stock_rebuild` (`service`, `secondary_type`, `readiness`, `evidence`, `api`, …)
- Desk page: `job-card-stock-rebuild`
- DocType: `Job Card Stock Rebuild Log`
