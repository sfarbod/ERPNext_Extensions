# Release 5.4.1 — Job Card Stock-State Rebuild (Phase 1)

## Summary

- **Job Card Stock Rebuild** desk tool: user selects one Job Card and reconstructs material / secondary **tracking state** from authoritative submitted Stock Entry / SLE evidence.
- Lifecycle: **SCAN → PREVIEW → DRY RUN → CONFIRM → APPLY → VERIFY**.
- Apply is fail-closed, atomic, fingerprint-gated, and **never** mutates Manufacture, SLE, GL, or valuation.

## What this release does

- Reconstructs Job Card Item tracking fields from stock movements (`transferred_qty`, `consumed_qty`, custom issued/returned/WIP fields) using ERPNext 16.37 ManufactureEntry semantics (`qty = transferred_qty − consumed_qty` under Material-Transferred backflush).
- Deterministic backfill of `Stock Entry Detail.job_card_item` when ownership is unique and proven.
- Batch-aware and UOM-aware reconciliation; ambiguous ownership / batch mismatch / missing UOM mapping block Apply.
- Secondary Items inventory with explicit classifications (Component Scrap, Product Reject, Co-/By-Product, Additional Finished Good, Ordinary Scrap) — **no class merging**, no invented equivalent factors.
- Reuses Iran Accounting / stage-output helpers for guard simulation before Apply.
- Downstream Manufacture **simulation only** (not persisted) to prove raw-material candidates (e.g. `13200544 × 1148` for PO-JOB08760).
- Audit DocType: **Job Card Stock Rebuild Log**.

## What this release does NOT do

- No Manufacture cancel / amend / regenerate / submit.
- No Material Issue creation or fake stock movements.
- No SLE / GL / rate changes.
- No weakening of Iran Accounting or stage-output guards.
- No change to `MANUFACTURE_COSTING_CONTRACT_VERSION` (no economic-policy change).
- Phase 2 Manufacture repair remains out of scope.

## Compatibility

- ERPNext **16.37.0** Job Card / ManufactureEntry field contract retained.
- erpnext_extensions **5.4.1**.

## Canaries

- **PO-JOB08760** — tracking incomplete; WIP remainder 1148; Manufacture repair required = YES after rebuild.
- **PO-JOB08761** — BALANCED / NO CHANGE.
