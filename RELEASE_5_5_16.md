# erpnext_extensions 5.5.16

## Manufacturing Dimension Uniformity Guard + Stock Entry Dimension Repair

### Summary

- Submit-time guard: Material Transfer for Manufacture / Manufacture Stock Entries
  must have exactly one Department and one Cost Center across all item rows
  (blank vs populated counts as mixed).
- Single-document **Stock Entry Dimension Repair** tool: Scan → Preview → Dry Run → Apply.
- Apply rebuilds GL only for the selected voucher via the proven Iran Accounting path
  (`get_gl_entries` → `_delete_accounting_ledger_entries` → `make_gl_entries(..., from_repost=True)`).
  SLE is never mutated.

### Non-goals / unchanged

- No Job Card inference, Operation Map, majority/70% logic, or auto-correction.
- No chain repair of upstream/downstream Stock Entries.
- Canary `MAT-STE-2026-40149` was Scan + Dry Run only in this release — Apply not executed.

### Access

Desk page: `/app/stock-entry-dimension-repair`  
Permission: Accounts Manager or System Manager.
