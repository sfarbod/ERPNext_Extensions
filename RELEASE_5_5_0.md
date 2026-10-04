# Release 5.5.0 — Job Card Golden Rule Manufacture Reconciliation

## Summary

Extends **Job Card Stock Rebuild** with a focused Manufacture Reconciliation
flow: Golden Rule detection, user-approved disposition of unexplained WIP,
one canonical Manufacture preview, multi-document merge, and atomic
Dry Run / Apply with synchronous valuation (no async RIV during repair).

## What this release does

- Golden Rule at Job Card × Item × Batch:
  `ISSUED = RETURNED + PHYSICAL_WIP_CONSUME + OTHER + LEGITIMATE_REMAINING_WIP`
- Component Scrap pairing fix: scrap output is not a second WIP drain when
  paired with a Manufacture CONSUME source row
- User-editable dispositions: Consumed / Scrap / Return / Still in WIP
- Documents to merge → one canonical Manufacture (issues/returns/logistics stay separate)
- Downstream FG logistics discovered via batch lineage; TEMP CANCEL / RECREATE
- Historical Repair Mode default (e.g. PO-JOB08760 → stamp 5.3.34)
- Dry Run and Apply share the same engine; Dry Run always rolls back
- Apply: one database transaction, suppress auto-RIV, sync valuation, verify, commit once
- MVP blocks Delivery Note / sales dependencies, stamp conflicts, unpaired scrap, etc.

## What this release does NOT do

- No temporary Product Receipt
- No ERPNext Core patch
- No SQL mutation of submitted accounting docs
- No async RIV inside the repair transaction
- No new Desk tool (extends Job Card Stock Rebuild)
- No change to `MANUFACTURE_COSTING_CONTRACT_VERSION` (**5.3.43**)

## Compatibility

- ERPNext **16.37.0** / Frappe **16.36.1**
- erpnext_extensions **5.5.0**
- Costing contract **5.3.43** (historical repair may reproduce older stamps)

## Primary canary — PO-JOB08760

- `13200544` Issued 1160 / Returned 12 / Consumed 0 / Remaining 1148
- Suggestion: CONSUMED 1148 (HIGH) — user approval required
- Canonical Manufacture adds consume + keeps FG 545 Quarantine / 29 Retain
- Dry Run must rollback; persistent Apply requires separate authorization

## Modules

- `job_card_stock_rebuild/scrap_pairing.py`
- `job_card_stock_rebuild/golden_rule.py`
- `job_card_stock_rebuild/manufacture_plan.py`
- `job_card_stock_rebuild/sync_valuation.py`
- `job_card_stock_rebuild/atomic_repair.py`
- `job_card_stock_rebuild/mi_ownership.py` — Material Issue ownership classifier
- API: `scan_manufacture_reconciliation`, `dry_run_manufacture_repair`, `apply_manufacture_repair`

## Hardening (same 5.5.0)

- Proven Material Issue → MERGE into canonical Manufacture (SLE outgoing rate)
- Shared multi-row MI → BLOCK (no row-split MVP)
- Downstream dependency graph is Manufacture FG Item×Batch scoped
- Shared Material Transfer recreate: `DEDICATED` / `SHARED_RECREATE_SAFE` / `SHARED_BLOCKED`
  via savepoint cancel probe (no row-split; no foreign-chain expansion)
- Physical equivalence check on recreated shared logistics
- PO-JOB08760: `31726` SAFE alone; `31725` BLOCKED (unrelated stock shortfall) → pre-apply BLOCKED
