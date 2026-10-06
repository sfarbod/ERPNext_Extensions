# erpnext_extensions 5.5.15

## Job Card Stock Rebuild — queued Batch Offset approval propagation

### Summary

- Preserve user-approved zero-net Batch Offset decisions through the queued Job Card Stock Rebuild path.
- No accounting contract change.

`queued_repair._normalize_plan` now keeps `batch_offset_approvals` so Desk → long worker does not drop `ACCEPT_BATCH_OFFSET_NO_REPAIR`.

### Non-goals / unchanged

- `MANUFACTURE_COSTING_CONTRACT_VERSION = 5.3.43`
- Golden Rule remains Job Card × Item × Batch
- Audit remains Job Card × Item
- No change to eligibility, fingerprint, Department, rates, Scrap, MI, or Workstation isolation
