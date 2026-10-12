# Release 5.5.38 — Account Explorer checkbox ellipsis

**Date:** 2026-10-12  
**Previous:** 5.5.37  
**Site:** development.localhost

## UI

The row-selection checkbox sits in a 32px frappe-datatable column. `.dt-cell__content` uses `text-overflow: ellipsis`, so the checkbox overflow painted "...". Account Explorer now clips that column only when it actually contains the checkbox. Data columns still ellipsize. Copy, Clear Selection, and Select All are unchanged.

## Redis accounting index

`accounting_index.py` is a disabled prototype. It is not called from Account Explorer requests. Turn it on only with site config `ae_accounting_index = 1`. Default is off.

The prototype stores daily leaf debit/credit turnover in one Redis hash per company (schema `ae:idx:v1`). Parent balances are summed at read time. A changed posting date invalidates that account-date field only. A stale writer cannot publish over a newer stamp. Missing, incompatible, or incomplete segments return no balance.

Development measurement on اسپاد فارمد دارو (not the live report path):

- Rebuild from GL: 1,494 ms, 6,278 fields
- Redis `MEMORY USAGE`: 800,663 bytes
- Warm read of account 11100409 leaves: 4 ms
- Debit balance: 994,353,195,185 IRR (delta 0 versus posted GL)

## Deploy

`bench build --app erpnext_extensions`, then hard-reload Desk. No migrate. Do not set `ae_accounting_index`.

## Rollback

Redeploy 5.5.37 assets.
