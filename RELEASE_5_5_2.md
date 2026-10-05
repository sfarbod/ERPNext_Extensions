# erpnext_extensions 5.5.2

**Version:** 5.5.2  
**Manufacture Costing Contract:** 5.3.43 (unchanged)  
**Type:** Patch — Job Card Stock Rebuild UI prefill

## Job Card Stock Rebuild — Scan Disposition Prefill

- Scan suggestions now prefill editable disposition fields (`Consumed*`, `Scrap*`, `Return*`, `Still WIP*`).
- Low-confidence recommendations remain editable and visibly flagged.
- User overrides are preserved across normal UI re-renders and passed to Dry Run.
- Ambiguous / MANUAL REVIEW / no-evidence rows stay unset (zeros) when the backend has no concrete disposition.
- No accounting or repair-engine policy change.

## Accounting

No accounting policy change.

`MANUFACTURE_COSTING_CONTRACT_VERSION` remains **5.3.43**.

## Out of Scope

Staging Dry Run HTTP 504 / proxy timeout is **not** addressed in this release.

## Migration

No new schema or business-data migration.

Normal `bench migrate` after operator deployment is sufficient.

## Compatibility

ERPNext 16.37.x  
Frappe 16.36.x
