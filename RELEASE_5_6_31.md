# erpnext_extensions 5.6.31

## Safe full-lifecycle PDC rollback and Debt Purchase repayment consistency

### Summary
Enable **controlled** Post Dated Cheque workflow rollback from
`Assigned to Bank for Debt Purchase` back to `Registered`, cancelling the Debt
Purchase Assignment Journal Entry and restoring Cheques-in-Hand / DPIC balances.

Keep `Debt Purchase Settled` owned by Facility Repayment: PDC rollback exposes
**no** targets while Settled. Cancelling the linked Facility Repayment remains
the only path that restores `Assigned` and removes the settlement JE reference.

Desk workflow still forbids Assigned → Registered (operators must use
**Rollback Workflow State**). Bounce Cheque from Assigned is unchanged.

**Development only in this change set. No push / publish / production deploy.**

### Root cause
`get_rollback_target_states` intentionally returned `[]` for Assigned Debt
Purchase, so incorrectly assigned cheques could not be recovered through the
controlled rollback path. Settled was reachable via event history if not gated,
which would bypass Facility Repayment ownership.

### Fix
1. `pdc_workflow_rollback.get_rollback_target_states` — remove Assigned early
   empty return; block Settled with `[]` and a clear validation message directing
   operators to cancel Facility Repayment first.
2. `clear_outcome_fields_for_target` — clear `handover_date` when restoring
   Registered/Draft (snapshot path already restores pre-assignment values).
3. Desk validation copy updated to point at Rollback Workflow State.
4. Tests rewritten for Assigned → Registered rollback + Settled gate.
5. Facility Repayment Debt Purchase cancel — detach settlement Journal Reference
   **before** cancelling the settlement JE (re-attach on failure). Submitted PDCs
   previously raised `LinkExistsError` and could not restore Assigned.
6. `rollback_workflow_state` — filesystem `filelock` per PDC name so concurrent
   rollbacks cannot double-cancel the same transition Journal Entry.

### Tests
- `cheque_management.tests.test_pdc_debt_purchase_assignment_rollback`
- `cheque_management.tests.test_pdc_workflow_rollback` (unit)
- `cheque_management.tests.test_pdc_workflow_rollback_lifecycle_integration`
  (switched to `unittest.TestCase` to avoid ERPNext UOM fixture preload collision)
- `cheque_management.tests.test_pdc_lifecycle_rollback_state_coverage`
  (Bounce, Endorsed, ULA, Replaced, Cleared, Cancelled, Payable)
- Facility Debt Purchase E2E
  (`facility_management.tests.test_facility_debt_purchase_e2e`)

### Out of scope
- Production / push / tag / release publish
- Modification of reference PDC-2026-00688
- ERPNext / Frappe core changes
