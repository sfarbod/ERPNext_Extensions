# ERPNext Extensions v5.2.9

## Payment Request → Post Dated Cheque (Purchase Order)

### Fixed

- Creating a Post Dated Cheque from a Payment Request no longer requires the Payment Request to
  reference a Purchase Invoice or Sales Invoice.
- Prefill allocates **directly to the Payment Request** (settlement capacity / traceability key).
- Purchase Order–based Payment Requests (no PI yet) can create a Payable PDC without fabricating a
  Purchase Invoice reference.
- Payable / Receivable JE invoice slices:
  - PR → Invoice still resolves invoice refs at Register / Payable Issue.
  - PR → Order (or empty) returns no invoice slices so the JE posts as unallocated party settlement
    and does **not** mark an invoice paid early.

### Preserved

- Invoice Payment Request → PDC still works (allocation on PR; invoice settled via JE slice resolution).
- Advance Mode PDC from Purchase Order / Sales Order unchanged.
- Settlement remaining on Payment Request continues to use:
  `grand_total − Payment Entry − effective direct PDC`.

## PM Clearance recovery after cancel

### Fixed

- Cancelling a PM Clearance while its settlement Journal Entry is still **draft** now deletes that
  orphan draft JE (submitted JEs remain blocked until the user cancels them first).
- **Amend** of a cancelled clearance resets `workflow_state`, `status`, and `journal_entry` so the
  new document starts as Draft (Frappe copy otherwise kept Approved + Cancelled).
- `settle_petty_cash` treats a stale link to a **cancelled/missing** JE as absent and recreates
  settlement; active draft/submitted JEs remain idempotent.

### Recovery path

- Cancelled clearance (`docstatus=2`): use standard **Amend** (do not reopen docstatus in place).
- Submitted clearance with cancelled JE: Settle again once the link is cleared.

### Data repair (development.localhost)

- Deleted orphan draft Journal Entry `ACC-JV-1405-139395` linked to cancelled
  `CLR-HR-EMP-0023-202609-31949896664a`.
- Created amended draft clearance `CLR-HR-EMP-0023-202609-31949896664a-1`
  (status/workflow Draft, 4 PI lines + PM Request allocation preserved).
  User can continue approval → Settle on the amended document.
- Original cancelled clearance left immutable (`docstatus=2`).
