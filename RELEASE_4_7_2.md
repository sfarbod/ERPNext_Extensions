# ERPNext Extensions v4.7.2

Draft approval until final Finance submit for PM Request and PM Clearance.

## Behaviour

- **Pending*** workflow states stay at **docstatus=0** (not submitted).
- **Finance Approve** / **Clearance Approve** are the only transitions that **submit** (docstatus=1).
- **PM Return for Correction** sends Pending* → **Draft** on the **same document** (no Cancel / Amend).
- Timeline comment records: returned by user, previous stage, timestamp, optional reason.
- Pre-finance **PM Reject** from Pending* is replaced by Return for Correction.
- Request **PM Reject** from **Finance Approved → Rejected** remains for post-submit rejection.
- Approver stamps are applied on **Draft → Pending Manager** (submit-for-approval), not as the first stamp on Finance submit.
- Pending* documents are **not editable** and **not deletable** until returned to Draft.
- Payment Entry remains blocked until finance-approved **and** docstatus=1.
- Clearance reservation / settle / JE remain blocked until Approved **and** docstatus=1.
- Return for Correction never auto-skips.

## Hard cutover

- Patch `migrate_pm_draft_approval_v472` **aborts** if any Pending* Request or Clearance exists.
- No grandfathering, no permanent feature flags, no legacy workflow branches.
- Clear or finish in-flight Pending* documents before migrating.

## Migration / rebuild

- `migrate_pm_workflow_v402` rebuild helpers now define Pending* with `doc_status=0` and Return transitions so `after_migrate` cannot flip Pending back to submitted.
- Assignment Rule close conditions also close when `workflow_state == Draft`.
- Bulk assignment apply includes pending docs at docstatus 0 or 1.

## Unchanged

- Multi-level Manager → CEO → Finance chain
- Clearance finance role queue (v4.5.3)
- Auto-skip consecutive same-user Approves (v4.1.4)
- Cancel/Delete eligibility (v4.6.8) for submitted/final documents
- Draft PI readiness (v4.1.5)
- PE Desk cancel ignore (v4.6.7)

## Release-blocker hardenings (post-audit)

- Return for Correction is fully atomic: assignment/timeline/stamp failures raise and roll back the request transaction (no swallowed assign errors).
- Return acquires `SELECT … FOR UPDATE`, re-reads state, rejects second caller when already Draft.
- `approve_pm_clearance_for_reservation` / settlement whitelist apply **PM Finance Approve** via workflow only — no raw `docstatus=1` writes.
- v4.7.2 migration no longer calls `frappe.db.commit()`; Frappe patch handler owns commit/rollback.
