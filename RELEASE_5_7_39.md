# erpnext_extensions 5.7.39

## PDC — Debt Purchase Assignment Date (تاریخ واگذاری جهت خرید دین)

### Summary

Transitions into **Assigned to Bank for Debt Purchase** (from **Registered** or
**Bounced**) previously had **no dedicated assignment date**. Journal Entry
posting fell through a generic fallback that could use `cheque_due_date`,
`received_date`, or **`today()`** — not a user-entered assignment date.

Version **5.7.39** introduces an authoritative PDC field and requires it before
assignment.

### Root cause (original)

In `_pdc_post_save_accounting_sequence`, posting-date selection handled Cleared,
Returned, Bounced, Sent to Bank, Return from Bank, Register, Issued, and
Endorsed — but **not** Assigned DP. Assignment edges used the `else` fallback:

`cheque_due_date` → `received_date` → `cleared_date` → `getdate()` (server today).

### Release blockers fixed in follow-up (same 5.7.39)

1. **Opening Import** silently copied `received_date` into
   `debt_purchase_assignment_date` when importing into Assigned DP without the
   dedicated column — removed. Column only; Preview/Execute require it when
   target is Assigned DP. Import into Registered/Bounced without the column
   succeeds; later Assign is blocked until the PDC field is set.
2. **Excel template samples** were one column short after the new header
   (misaligned `returned_date` / Sayad columns) — samples aligned; instructions
   and aliases documented.
3. **UI**: Receivable-only `depends_on`; editable before Assign; client
   `before_workflow_action` blocks **Assign for Debt Purchase** when empty
   (bilingual message); Persian translations for the field label and errors.

### Dedicated field

| Property | Value |
| --- | --- |
| **Fieldname** | `debt_purchase_assignment_date` |
| **Label** | Debt Purchase Assignment Date |
| **Persian** | تاریخ واگذاری جهت خرید دین |
| **Type** | Date |
| **Mandatory when** | Receivable + workflow_state = Assigned to Bank for Debt Purchase |

### Date-source rule

1. User (or Opening Import column) sets **`debt_purchase_assignment_date`** on the PDC.
2. Empty field → Assign transition **blocked** (client + server); no silent today.
3. Assignment JE **posting_date** = that field value.
4. **PDC Journal Reference.posting_date** stores the same value.
5. Not confused with cheque due date, received date, or server execution date.

### Journal Entry posting date

**Aligned with existing accounting design:** lifecycle JEs post on the business
event date for each edge. Assignment JE uses `debt_purchase_assignment_date`.
Other lifecycle edges keep their existing date rules.

### Opening Import

Optional column `debt_purchase_assignment_date` (aliases: `assignment_date`,
`Debt Purchase Assignment Date`, `تاریخ واگذاری جهت خرید دین`).

- Target **Assigned to Bank for Debt Purchase** → column **required** (Preview and Execute).
- Other targets → column optional; field left empty when omitted.
- **Never** invent `today()`, cheque due date, or `received_date`.

### Files

- `post_dated_cheque.json` / `.py` / `.js`
- `cheque_opening_import.py` / `.json`
- `translations/fa.csv`
- `test_pdc_debt_purchase_assignment_date_v5739.py`
- Related test helpers
- `erpnext_extensions/__init__.py` (`5.7.39`)
- `RELEASE_5_7_39.md`

### Tests

```bash
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_pdc_debt_purchase_assignment_date_v5739
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_pdc_debt_purchase_assignment_rollback
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_cheque_opening_import_workflow_states
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_pdc_bounced_reassignment_v5725
bench --site development.localhost run-tests --app erpnext_extensions \
  --module erpnext_extensions.cheque_management.tests.test_pdc_journal_reference_guard_v576
```
