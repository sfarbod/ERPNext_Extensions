# erpnext_extensions 5.5.11

## Job Card Stock Rebuild — isolate Workstation concurrency (MariaDB 1020)

### Production failure

Queued Apply on `PO-JOB08760` failed with:

`(1020, "Record has changed since last read in table 'tabWorkstation'")`

Dry Run had passed. `mutated=false`, `committed=false`.

### Root cause

MariaDB `innodb_snapshot_isolation=ON` + long repair transaction + Core Job Card side effect:

```
Stock Entry cancel/submit
  → JobCard.set_transferred_qty / set_manufactured_qty / set_status
    → JobCard.update_workstation_status
      → frappe.db.set_value("Workstation", …, "status", …)
```

Concurrent Production updates to the same Workstation (e.g. `Packaging Rooms`) then raise MariaDB ER_CHECKREAD (1020).

Workstation runtime status/modified is **not** part of stock reconciliation truth (Golden Rule, JCI, SLE/GL, scrap, valuation).

### Fix (narrow)

- New `workstation_isolation.py`: repair-scoped flag + guarded wrappers on Core
  `JobCard.update_workstation_status` / `update_status_in_workstation`.
- `atomic_repair.run_repair` sets the skip flag for the repair transaction only.
- Does **not** use `ignore_version`, does **not** catch/suppress 1020 globally,
  does **not** overwrite concurrent Workstation values.

### Non-goals / unchanged

- Golden Rule physical semantics
- 5.5.10 Job Card × Item audit grain
- 5.5.9 tracking reconstruction / Custom 6 enabled
- `MANUFACTURE_COSTING_CONTRACT_VERSION = 5.3.43`
- Accounting / Component Scrap / Product Reject

### Canary (restored `20261006_001111`)

| Check | Result |
|-------|--------|
| Dry Run + concurrent WS update | DRY_RUN_PASS, mutated=false, WS value survived |
| Apply + concurrent WS update | APPLY_PASS, COMMITTED, WS value survived |
| 13200544 | Issued 1160 / Returned 12 / Consumed 1148 / Remaining 0 |
| JCI | 17/17 unique consume links |
| Custom 6 | enabled |
| 13200190 scrap | consume 580 / scrap 5 / rem 0 |

### Tests

- `test_workstation_concurrency_v5511` WS01–WS16
- Restored-fixture gate `_gate_workstation_concurrency_v5511`
- Playwright smoke `playwright_job_card_workstation_v5511.mjs`
