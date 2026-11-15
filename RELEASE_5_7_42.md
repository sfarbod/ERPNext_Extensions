# Release 5.7.42 — Multiple Advance Received / Advance Paid Accounts

## Summary

Implements **Option B′**: Account-level classification (`Account.advance_account_type`) for multi-account separate-party advances, while preserving Company/Party default auto-fill fields and the Company feature master switch.

## Architecture

| Layer | Role |
|---|---|
| `Account.advance_account_type` | Eligibility (None / Advance Received / Advance Paid) |
| `Company.book_advance_payments_in_separate_party_account` | Feature master switch |
| Company / Party default advance Links | Auto-fill / compat discovery |
| PE `book_advance_payments_in_separate_party_account` | Read-only, computed from party-account eligibility |

### Payment Entry (flexible Scenario D)

- Receive + classified Advance Received on `paid_from` → flag ON, account preserved
- Pay + classified Advance Paid on `paid_to` → flag ON, account preserved
- Normal Debtors/Creditors with feature ON → flag OFF (no overwrite to default)
- Wrong-direction classified account → rejected
- Company feature OFF → standard ERPNext behaviour

### Discovery

`get_common_query` is patched so invoice Get Advances / Payment Reconciliation find PEs against **all** eligible accounts (classified ∪ configured defaults), not only a single default.

### Journal Entry

Existing `is_advance` on AR/AP unchanged. Warning expanded to all classified separate advance accounts (no hard block — PDC JEs remain allowed).

## Files

- `erpnext_extensions/advance_accounts/` — module (eligibility, PE/JE/PR mixins, discovery patch, custom field, bootstrap)
- Patch: `patches/post_model_sync/seed_advance_account_types_v5742.py`
- PDC: `post_dated_cheque.py`, `pdc_invoice_advance_application.py`, `pdc_settlement_summary.py`
- Hooks / JS / tests / Playwright smoke

## Migration

1. Custom Field `advance_account_type` on Account
2. Seed configured Company/Party advance accounts (sets missing `account_type` when safe)
3. No GL / submitted voucher rewrites

## Test evidence

- Unit/integration: `test_advance_accounts_v5742` — 9/9 OK
- E2E execute: `e2e_advance_accounts_v5742.run` — 3 Received + 3 Paid advances, GL credit/debit OK, multi-account discovery OK
- Playwright: `advance-accounts-v5742.spec.ts` — Account field + PE checkbox values OK
