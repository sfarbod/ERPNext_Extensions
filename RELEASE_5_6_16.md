# erpnext_extensions 5.6.16

## Payroll Correction API — replace accrual JE as Draft

### Summary
Adds a narrowly scoped, permission-gated whitelisted API to cancel and delete a
submitted payroll accrual Journal Entry, then regenerate a **Draft** replacement
through `PayrollEntryWithAccountingDimensions.make_accrual_jv_entry`.

The replacement is **never submitted**. Employee Details and Salary Slip amounts
are not modified.

Endpoint:

`erpnext_extensions.extentionhrms.payroll_correction.payroll_replace_accrual_je_draft`

### Capabilities
- `action=dry_run` (read-only) and `action=execute`
- Exact identity gates: Payroll Entry, expected JE, employee count, JE debit/rows
- Optional Salary Slip fingerprint gate
- Required JE posting date via in-memory `pe.posting_date` override (PE DB date unchanged)
- Redis SET NX concurrency lock per Payroll Entry
- Idempotency key with `ALREADY_COMPLETED` only when Draft JE + zero active GL verified
- Forces synchronous JE `_cancel` (ERPNext queues when accounts > 100)
- Explicit refusal if regen unexpectedly yields a submitted JE

### Safety
- `allow_guest=False`; roles: System Manager / Accounts Manager / HR Manager
- No credentials in code/logs
- No direct SQL deletion of accounting documents
- Employee master never modified
- Employee Details count unchanged
- Salary Slip amounts unchanged
- Intermediate lifecycle checkpoints; failure after a checkpoint requires backup restore

### Not covered by this endpoint
- Employee removal (use `payroll_remove_employee_accrual` / 5.6.15)
- Automatic submit of the Draft JE

### Salary Slip link behavior
HRMS `make_journal_entry(..., submit_journal_entry=False)` still calls
`set_journal_entry_in_salary_slips`, so all submitted slips are linked to the
**Draft** JE name. This is supported and verified in E2E.

Naming series may reuse the cancelled JE name for the new Draft. Idempotency /
`ALREADY_COMPLETED` treats that as success when the linked document is Draft
with matching totals and zero active GL.

### Permissions
Roles: System Manager / Accounts Manager / HR Manager (`allow_guest=False`).

### Idempotency
Cache key prefix `draft_replace:{idempotency_key}`. Re-execute returns
`ALREADY_COMPLETED` without mutation after successful Draft verification.

### Expected execute response (success)
- `status`: `EXECUTED`
- `mutated`: `true`
- `result.new_je.docstatus`: `0`
- `result.new_je.posting_date`: required date
- `result.new_je.total_debit` / `total_credit` / `rows`: fingerprint match

### Recovery limitations
Lifecycle checkpoints commit after cancel/delete/regen. Mid-flight failure after
a checkpoint is **not** fully rolled back; restore from a verified DB backup.
This API does not perform Production restore.

### Tests
- Unit: `extentionhrms/tests/test_payroll_correction.py` (draft-replace param gates)
- E2E: `scripts/run_payroll_replace_draft_e2e.py` on `payroll-e2e.localhost`
  — 266 employees, debit `283672596839`, rows `1363`, Draft, posting `2026-09-22`

### Deployment
1. Confirm fresh Production DB backup.
2. Deploy / migrate this app version (**5.6.16**).
3. Confirm endpoint exists:
   `POST /api/method/erpnext_extensions.extentionhrms.payroll_correction.payroll_replace_accrual_je_draft`
4. Call dry-run against Production; compare fingerprints.
5. Execute only after explicit operator approval.
6. Do **not** submit the Draft JE unless separately authorized.

### Example Production payload (after deploy)

```json
{
  "action": "dry_run",
  "payroll_entry": "شهریور 1405",
  "expected_je": "ACC-JV-1405-172143",
  "expected_employees": 266,
  "expected_je_debit": 283672596839,
  "expected_je_rows": 1363,
  "required_je_posting_date": "2026-09-22",
  "idempotency_key": "shahrivar1405-replace-draft-ACC-JV-1405-172143"
}
```

### Migration
No schema change. Version **5.6.16**.
