# erpnext_extensions 5.6.33

## Delivery Note — Check Submission (pre-submit validation)

### Summary

Draft Delivery Notes gain a **Check Submission** button that runs the real
ERPNext submission validation lifecycle **without submitting** and without
retaining Stock Ledger, General Ledger, or Bin changes.

Users with **Write** permission can run the check even when they do **not**
have Submit permission.

### Business problem

Submission blockers (insufficient stock, batch/serial, warehouse, mandatory
fields, credit limit, approving authority, custom hooks) are often discovered
only when someone with Submit clicks Submit. Draft editors without Submit
could not verify readiness themselves.

### Implementation approach

1. Whitelisted API `validate_delivery_note_before_submit` (Delivery Note name
   only — no client document payload trust).
2. Permission: require **Write**; explicitly do **not** require Submit.
3. Draft-only gate.
4. Inside a guarded DB savepoint (commit blocked; nested full rollback demoted):
   - Pre-submit lifecycle: links, `before_validate`, `validate`,
     `before_submit`, `_validate` (includes `erpnext_extensions` DN validate).
   - Submit-time validation subset from `DeliveryNote.on_submit`: packed qty,
     approving authority, prevdoc/billing status, credit limit, serial/batch
     bundles, stock reservation, **stock ledger** (insufficient stock),
     produced-serial checks, GL entries.
5. Always roll back the savepoint; refuse the result if docstatus / SLE / GLE /
   Bin / `modified` leaked.

### Intentionally skipped (documented limitations)

| Check | Reason |
|---|---|
| `repost_future_sle_and_gle` | Creates / enqueues Repost Item Valuation — not safely reversible |
| `update_pick_list_status` | Persists pick-list state; low validation value |
| `make_return_invoice` | Creates Sales Invoice for return credit notes |
| Emails / webhooks / automations | Muted via `in_import` + `mute_emails` + `in_patch` |

ERPNext still stops at the first hard `frappe.throw` (multi-error collection
only where core already aggregates messages).

### Files

| Path | Role |
|---|---|
| `stock_extensions/delivery_note_check_submission/validator.py` | Dry-run engine |
| `stock_extensions/delivery_note_check_submission/api.py` | Whitelist API |
| `stock_extensions/public/js/delivery_note_check_submission.js` | Desk button + dialogs |
| `hooks.py` | `doctype_js` for Delivery Note |
| `stock_extensions/delivery_note_check_submission/tests/test_delivery_note_check_submission_v5633.py` | Automated coverage |

### Version

- `erpnext_extensions`: **5.6.33**
