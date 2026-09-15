# ERPNext Extensions v5.2.11

## PM Clearance Return vs PM Request availability deadlock

### Fixed

- **PM Return for Correction** is no longer blocked when a Pending Clearance’s
  `allocated_amount` exceeds the **live** PM Request available balance
  (paid − other reserved/settled clearances).
- Same class of corrective deadlock as v5.1.4 (PI outstanding): the requester
  cannot edit allocation until Return succeeds, but Return was re-running the
  funding availability gate that requires the correction.

### Mechanism

- Reuses `frappe.flags.pm_return_for_correction` (set only for Clearance Return
  in `workflow_hooks.apply_workflow`).
- Also allows the existing pending **remark-only** save path (parity with v5.1.4).
- Skips **only**:
  - row gate: `allocated > available` in `stamp_allocation_snapshot` /
    `stamp_opening_allocation_snapshot`
  - policy gate: clearance total vs holder total available in
    `validate_clearance_policy`
- Still stamps live `available_amount` / `previously_allocated_amount`.
- Does **not** skip company/employee/holder/duplicate/required-link/zero-allocation
  checks, Finance Approve, or ordinary Draft saves.

### Target reproduction

`CLR-HR-EMP-0023-202609-96a49fb68b3a` — Return blocked by:

> allocated 1000000000.0 exceeds available PM Request balance 743398200.0
> for REQ-HR-EMP-0023-2026-08-00013

Throw site: `allocation_service.stamp_allocation_snapshot`.

### Intended corrective flow

Pending → Return → same document Draft → fix allocation → resubmit →
Manager → Finance Approve.

### Tests

- Unit/integration: `test_pm_clearance_return_funding_availability_v5211`
- Playwright: `playwright_pm_clearance_return_funding_availability_v5211.mjs`
