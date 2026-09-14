# ERPNext Extensions v5.2.10

## PM Clearance vs settlement Journal Entry lifecycle

### Fixed

- Cancelling or deleting a **settlement Journal Entry** no longer leaves the PM Clearance
  stuck, and **never** cascade-cancels the clearance.
- After JE cancel/delete (CASE B):
  - PM Clearance remains `docstatus=1`
  - `workflow_state` stays Approved
  - business status returns to Approved (pre-settlement issuance)
  - stale `journal_entry` links are healed
  - **Settle** is available again on the **same** clearance
- Draft JE **delete** (trash) now unlinks like cancel (previously only `before_cancel` ran).
- `can_settle` / `clearance_is_approved` use **active JE** detection (draft/submitted), not a
  raw non-empty `journal_entry` string.

### Preserved

- Explicit PM Clearance cancel (CASE A) still sets `docstatus=2` / `status=Cancelled`.
- Submitted JE still blocks clearance cancel until the JE is cancelled first.
- Active draft/submitted JE still prevents duplicate Settle (idempotent).

### Data note (development.localhost)

`CLR-HR-EMP-0023-202609-31949896664a` was **explicitly cancelled** by the user
(`docstatus` 1→2 in Version history). That is CASE A history and was **not** rewritten.
Unused amended draft `…664a-1` from the v5.2.9 workaround is removed when unused.
