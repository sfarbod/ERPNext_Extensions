# Release 5.4.2 — Job Card Component Return on Active Work Order

## Summary

Allow validated **Job Card component returns** while the parent Work Order is
still **In Process**, using Job Card / item / batch / WIP ownership and
returnable-quantity guards.

## Business rule

A Work Order does **not** need to be Completed or Closed merely because a
submitted Job Card needs to return unused components.

```
ISSUED − RETURNED − CONSUMED − VALID CLASSIFIED WIP OUTFLOW
= CURRENT STILL IN WIP / RETURNABLE
```

A return may proceed while the Work Order is active **only** when the backend
proves:

- valid submitted Job Card
- matching Work Order
- genuine Job Card component
- genuine WIP ownership
- correct item / batch
- correct source / destination warehouse flow
- return qty ≤ current returnable
- no ambiguity
- no unrelated / mixed rows

## What this release does

- Fingerprint-guarded wrap of ERPNext 16.37
  `StockEntry.validate_work_order_status_for_return`
- Narrow bypass for Job-Card-scoped Material Transfer for Manufacture returns
  (`is_return=1`) that pass evidence checks (reuses v5.4.1 Job Card Stock
  Rebuild evidence semantics)
- Fail-closed on unknown Core fingerprint / unsupported ERPNext minor
- Preserves Completed / Closed Work Order Core behavior unchanged
- Blocks manual / forged / mixed / over-return / wrong-batch / wrong-warehouse
  Stock Entries from receiving the exception

## What this release does NOT do

- Does **not** disable Core “Work Order Not Finished” globally
- Does **not** allow arbitrary Stock Entries that merely set `work_order` or
  `job_card`
- Does **not** change `MANUFACTURE_COSTING_CONTRACT_VERSION` (**5.3.43**)
- Does **not** reclassify Scrap / Component Scrap / Product Reject / Co- /
  By-Product
- Does **not** patch ERPNext Core source files

## Compatibility

- ERPNext **16.37.0** / Frappe **16.36.1**
- erpnext_extensions **5.4.2**
- Costing contract **5.3.43**

## Primary canary — PO-JOB10492 / MFG-WO-2026-00837

- Job Card: Work In Progress (submitted)
- Work Order: In Process
- Return Components (Client Script + Custom 3 API) builds return draft
- Valid return qty (e.g. `18000001` × 1 within Still in WIP) validates without
  “Work Order Not Finished”
- Over-return blocked
- Secondary `13100134` × 125 Scrap → COMPONENT_SCRAP unchanged

## Modules

- `erpnext_extensions.stock_extensions.job_card_component_return`
  (`patch`, `eligibility`, `returnable`, tests)
- Playwright: `iran_accounting/e2e/playwright_job_card_component_return_v542.mjs`
