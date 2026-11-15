# erpnext_extensions 5.7.39

## PDC — Debt Purchase Assignment Date (تاریخ واگذاری جهت خرید دین)

### Summary

Transitions into **Assigned to Bank for Debt Purchase** (from **Registered** or
**Bounced**) previously had **no dedicated assignment date**. Journal Entry
posting fell through a generic fallback that could use `cheque_due_date`,
`received_date`, or **`today()`** — not a user-entered assignment date.

Version **5.7.39** introduces an authoritative PDC field and requires it before
assignment.

### Root cause

In `_pdc_post_save_accounting_sequence`, posting-date selection handled Cleared,
Returned, Bounced, Sent to Bank, Return from Bank, Register, Issued, and
Endorsed — but **not** Assigned DP. Assignment edges used the `else` fallback:

`cheque_due_date` → `received_date` → `cleared_date` → `getdate()` (server today).

### Dedicated field

| Property | Value |
| --- | --- |
| **Fieldname** | `debt_purchase_assignment_date` |
| **Label** | Debt Purchase Assignment Date |
| **Persian meaning** | تاریخ واگذاری جهت خرید دین |
| **Type** | Date |
| **Mandatory when** | Receivable + workflow_state = Assigned to Bank for Debt Purchase |

### Date-source rule (after fix)

1. User (or Opening Import) sets **`debt_purchase_assignment_date`** on the PDC.
2. Empty field → transition **blocked** with a clear validation message (no silent today).
3. Assignment JE **posting_date** = that field value (same business-event pattern as
   `sent_to_bank_date` / `bounced_date`).
4. **PDC Journal Reference.posting_date** stores the same value (existing JR model).
5. Assignment date is **not** confused with cheque due date, received date, or
   server execution date.

### Journal Entry posting date

**Aligned with existing accounting design:** lifecycle JEs already post on the
business event date field for each edge. Using `debt_purchase_assignment_date` as
the Assignment JE posting date is consistent with that design — it replaces the
incorrect due-date/today fallback; it is not an independent policy change.

Return-from-Debt-Purchase prefers the same PDC field (then assignment JE posting
date, then today as last resort — unchanged unassign safety net).

### Opening Import

Optional column `debt_purchase_assignment_date`. If omitted when targeting
Assigned DP, Opening Import may copy **`received_date` from the same row** (never
`today()`). Preview/Execute still require a resolvable assignment date via
document validation.

### Files

- `erpnext_extensions/cheque_management/doctype/post_dated_cheque/post_dated_cheque.json`
- `erpnext_extensions/cheque_management/doctype/post_dated_cheque/post_dated_cheque.py`
- `erpnext_extensions/cheque_management/doctype/post_dated_cheque/post_dated_cheque.js`
- `erpnext_extensions/cheque_management/doctype/cheque_opening_import/cheque_opening_import.py`
- `erpnext_extensions/cheque_management/tests/test_pdc_debt_purchase_assignment_date_v5739.py`
- Related test helpers that Assign for Debt Purchase
- `erpnext_extensions/__init__.py` (`5.7.39`)
- `RELEASE_5_7_39.md`

### Tests

```bash
bench --site development.localhost migrate
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_pdc_debt_purchase_assignment_date_v5739
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_pdc_debt_purchase_assignment_rollback
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_pdc_bounced_reassignment_v5725
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_pdc_journal_reference_guard_v576
```
