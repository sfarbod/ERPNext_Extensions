# erpnext_extensions 5.7.38

## Cheque Opening Import — Workflow State Normalization & Bank Link Resolution

### Summary

Cheque Opening Import Preview could mark rows valid while Execute failed on PDC
`insert()` with `LinkValidationError`. Version **5.7.38** aligns Preview and
Execute so Excel workflow-state labels and Bank / Bank Account text resolve to
canonical Link document names before PDC creation.

Production incident reference (investigation only; not executed in this release):
**COI-2026-13437** — Bank Account Excel value differed from the Link `name` only
by internal whitespace around `-`.

### Fixes

**A. Workflow state normalization** (`pdc_workflow_state_machine.py`)

- Trim leading/trailing whitespace.
- Collapse repeated internal whitespace.
- Match against `ALL_WORKFLOW_STATES` **case-insensitively**.
- Return the **canonical** label (for example
  `assigned to bank for debt purchase` →
  `Assigned to Bank for Debt Purchase`).
- Unknown labels remain non-canonical after collapse; Preview / `find_workflow_path`
  continue to reject them.

**B. Bank Account and Drawer Bank Link resolution**
(`cheque_opening_import.py`)

- Resolve `bank_account` via exact Link `name`, `account_name`, trimmed forms,
  and **collapsed internal whitespace** (addresses single- vs double-space
  separators such as `593 - Bank` vs `593  - Bank`).
- Resolve `drawer_bank_name` via exact Bank name, `بانک {value}` prefix, and
  unique substring match; ambiguous matches raise.
- `_normalize_row_link_fields` runs in **Preview validation** and again in
  **`import_row`** so both paths use the same resolved Links.
- Preview fails the row when a Link cannot be resolved (no false “Valid”).
- Frappe Link validation on `pdc.insert()` is unchanged — resolvers only supply
  canonical names.

**C. Receivable Assigned DP bank requirement**

- Preview requires `bank_account` for
  `Assigned to Bank for Debt Purchase` / `Debt Purchase Settled`, consistent
  with PDC runtime expectations.

### Files

- `erpnext_extensions/cheque_management/pdc_workflow_state_machine.py`
- `erpnext_extensions/cheque_management/doctype/cheque_opening_import/cheque_opening_import.py`
- `erpnext_extensions/cheque_management/tests/test_cheque_opening_import_workflow_states.py`
- `erpnext_extensions/__init__.py` (`__version__ = "5.7.38"`)
- `RELEASE_5_7_38.md`

### Tests

```bash
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_cheque_opening_import_workflow_states

bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_pdc_opening_import_workflow_rollback

bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_pdc_opening_import_rollback_integration
```

### Out of scope

- Account Explorer debounce (v5.7.37) — unchanged.
- Production deploy / Execute Import — not part of this packaging commit.
