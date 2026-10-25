# erpnext_extensions 5.6.15

## Payroll Correction API — remove employee + regenerate accrual JE

### Summary
Adds a narrowly scoped, permission-gated whitelisted API to remove one verified
employee from a submitted Payroll Entry and regenerate the payroll accrual
Journal Entry through `PayrollEntryWithAccountingDimensions`.

Endpoint:

`erpnext_extensions.extentionhrms.payroll_correction.payroll_remove_employee_accrual`

### Capabilities
- `action=dry_run` (read-only) and `action=execute`
- Exact identity gates: Payroll Entry, Employee ID (`HR-EMP-#####`), Salary Slip, JE
- Accounting fingerprint gates (employee count, JE debit/rows, optional SS fingerprint)
- Redis SET NX concurrency lock per Payroll Entry
- Idempotency key with `ALREADY_COMPLETED` only after full corrected-state verification
- Required JE posting date via in-memory `pe.posting_date` override (PE DB date unchanged)
- Forces synchronous JE `_cancel` / `_submit` (ERPNext queues when accounts > 100)

### Safety
- `allow_guest=False`; roles: System Manager / Accounts Manager / HR Manager
- No credentials in code/logs
- No direct SQL deletion of accounting documents
- Employee master never modified
- Intermediate lifecycle checkpoints; failure after a checkpoint requires backup restore

### Tests
- Unit: `extentionhrms/tests/test_payroll_correction.py`
- E2E: isolated site `payroll-e2e.localhost` restored from pre-correction backup
  — verified 267 → 266, debit `283672596839`, rows `1363`, posting date `2026-09-22`

### Deployment
1. Confirm fresh Production DB backup.
2. Deploy / migrate this app version.
3. Call dry-run against Production; compare fingerprints.
4. Execute only after explicit operator approval.

### Migration
No schema change. Version **5.6.15**.
