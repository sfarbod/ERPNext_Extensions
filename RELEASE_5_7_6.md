# erpnext_extensions 5.7.6

## Post Dated Cheque — Journal References exclusively system-managed

### Root cause

`journal_references` was `allow_on_submit` without server-side immutability for create,
edit, or delete. Lifecycle Events and Workflow Rollback Logs were already immutable;
Journal References were not. Desk/API users could append or remove accounting identity
rows and orphan submitted Journal Entries.

### Fix

1. **Server** — `pdc_journal_reference_guard.py`
   - Reject **manual create**, delete, and change of `journal_entry`, `purpose`,
     `pdc_transition_key`, `posting_date`, `amount` on PDC save / child `before_insert` /
     `validate` / `on_trash`.
   - Clear errors directing users to system workflows / Rollback Workflow State.
   - Trusted **create** via `authorize_journal_reference_creation` used by:
     - PDC transition JE posting (`pdc_journal_entry_service`)
     - Facility Debt Purchase settlement append
   - Trusted **removal** via `authorize_journal_reference_removal` used by:
     - workflow rollback JE detach (`rollback_journal_reference_row`)
     - Facility Debt Purchase settlement cancel cleanup
   - Authorization lives only on `frappe.flags`, cleared in `finally` on success and
     failure; client payloads cannot enable it.

2. **UI** — Journal References grid cannot add/delete rows; identity fields read-only;
   description states system-managed.

3. **DocType** — child fields marked `read_only`; parent table
   `cannot_add_rows` / `cannot_delete_rows`.

### Backward compatibility

- Normal PDC accounting transitions still **append** new Journal References under
  scoped create authorization.
- Workflow Rollback still removes only the JE/reference for reversed steps.
- Facility Repayment settlement still appends/removes only Settlement references;
  Assignment references remain unless explicitly authorized.

### Tests

`test_pdc_journal_reference_guard_v576.py`

- Unauthorized create / delete / JE / key / purpose edits rejected
- System create under scoped auth succeeds
- Scoped removal auth allows intended row only
- Assignment preserved when Settlement authorized for removal
- Create vs remove auth are not interchangeable
- Auth cleared after success and failure; legacy flag names ignored
- Rollback helper sets remove auth; cancel-failure path still calls detach first
- JE service and Facility settlement append wire create auth
- PDC `validate` and child `before_insert` wire the guard

### Version

`erpnext_extensions.__version__` → **5.7.6**

### Deploy note

Sync DocType metadata (`bench migrate` or equivalent) so JSON `read_only` /
`cannot_add_rows` / `cannot_delete_rows` apply on target sites.
