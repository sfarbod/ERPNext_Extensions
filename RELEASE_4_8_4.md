# Release 4.8.4 — Legacy Pending Lifecycle Migration

## Summary

- **Legacy Pending* rewind**: PM Request / PM Clearance documents stuck at `workflow_state=Pending*` with `docstatus=1` (pre-v4.7.2 submitted Pending lifecycle) are converted to `docstatus=0` while preserving document identity, workflow state, stamps, assignments, comments, versions, owner, and timeline.
- **Transactional cutover**: Migration validates every legacy document before converting any; failure rolls back the entire patch transaction (no partial conversion).
- **v4.7.2 completion**: After rewind, applies deferred v4.7.2 workflow cutover when not yet complete, unblocking v4.8.3 Return + remarks patches.

## v4.7.2 invariants preserved

Pending* = docstatus 0; Finance Approved / Approved Clearance = docstatus 1; Return atomic/idempotent; no financial documents on Pending* legacy docs.

## Patch

`migrate_pm_legacy_pending_lifecycle_v484` — runs after `migrate_pm_draft_approval_v472`, before `migrate_pm_clearance_return_remarks_v483`.

## Service

`legacy_pending_lifecycle_service.py` — discovery, validation, conversion, post-migration invariant checks.
