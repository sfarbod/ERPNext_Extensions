# erpnext_extensions 5.5.21

## Job Card Stock Rebuild — User-approved Partial Batch Offset

### Summary

Extend Batch Offset so an operator can explicitly approve an exact
quantity pair between two Batches on the same Job Card × Item even when:

- other Batches for that Item still have unresolved Remaining;
- the two paired Batches have different valuation rates.

This is a **quantity attribution exception** for repair planning.
It does **not** rewrite SLE/GL, create Stock Adjustment / Additional Cost,
or silently net the whole Item.

### Decision

`ACCEPT_PARTIAL_BATCH_OFFSET_NO_REPAIR`

Classification when rates differ:

`QUANTITY_SAFE_VALUE_DIFFERENT`

(Value delta is calculated, displayed, and audited — not a hard block.)

### Primary canary

`PO-JOB08604` / Item `13200091`

- `5638…` +306 ↔ `929…` −306 → approved partial pair
- `5738…` +332 → remains `MISSING CONSUMPTION` (independent disposition)

Value delta: **2,091,510 IRR** (72,600 vs 65,765) — auditable, no valuation repair.

### Preserved

- Full Batch Offset (`ACCEPT_BATCH_OFFSET_NO_REPAIR`) for zero-net Items
  (e.g. `PO-JOB08773` +303/−303 `QUANTITY_AND_VALUE_SAFE`)
- Queued `_normalize_plan` propagation for both approval payloads
- MI / Scrap / Serial / ownership / STALE_PLAN gates
- `MANUFACTURE_COSTING_CONTRACT_VERSION` = **5.3.43** (unchanged)
