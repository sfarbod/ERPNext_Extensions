# erpnext_extensions 5.5.1

**Version:** 5.5.1  
**Manufacture Costing Contract:** 5.3.43 (unchanged)  
**Type:** Patch — Job Card Manufacture Repair performance hotfix

## Job Card Manufacture Repair Performance

- Bounded synchronous valuation to repair-relevant Item × Warehouse roots.
- Deduplicated valuation roots inside one repair transaction.
- Prevented unrelated plant-wide future SLE expansion during historical Job Card repair.
- Preserved Core `update_entries_after` valuation for required roots.
- Retained Dry Run phase timing diagnostics (`phase_timings`, T17).
- Corrected ineffective client-side timeout handling for Manufacture Repair Dry Run / Apply:
  one-shot `$.ajax` wrapper sets `timeout: 600000` ms for that request only
  (Core `frappe.call` ignores `opts.timeout`; no Desk-wide `$.ajaxSetup`).

## Accounting

No accounting policy change.

`MANUFACTURE_COSTING_CONTRACT_VERSION` remains **5.3.43**.

## Existing 5.5.0 Features Preserved

- Golden Rule reconciliation
- Canonical Manufacture
- Manufacture merge
- Material Issue merge
- Component Scrap pairing
- Temporary Receipt Bridge
- Shared Logistics recreation
- Job Card Golden Rule Audit
- Atomic Dry Run / Apply
- Historical Repair Mode

## Migration

No new schema or business-data migration.

Normal `bench migrate` after operator deployment is sufficient.

## Compatibility

ERPNext 16.37.x  
Frappe 16.36.x

## Development validation (PO-JOB08760 Dry Run)

Pre-repair baseline fixture. Persistent Apply not run for this release.

| Run | Total | T17 | Result | Mutation |
|---|---:|---:|---|---|
| 1 | 56.77s | 46.72s | DRY_RUN_PASS | none |
| 2 | 54.82s | 45.27s | DRY_RUN_PASS | none |
| 3 | 54.77s | 45.07s | DRY_RUN_PASS | none |

Pre-fix baseline ≈ 178s (T17 ≈ 166s).
