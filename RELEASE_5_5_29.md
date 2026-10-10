# erpnext_extensions 5.5.29

## Bench-only full-history RIV generation helper

5.5.28 remains the prior local release candidate. This release adds a
**generation-only** campaign helper for Production/local bench use. It does
not whitelist any HTTP method, does not execute RIVs, and does not change
scheduler state.

### What it does

- Builds one global chronological source manifest across submitted
  Purchase Receipt, Stock Entry, Delivery Note, and Stock Reconciliation
  (`posting_date`, `posting_time`, `creation`, `doctype`, `name`).
- Persists campaign identity + checksum + per-voucher checkpoint on Single
  DocType **RIV Campaign State**.
- Calls canonical
  `erpnext.controllers.stock_controller.create_item_wise_repost_entries(doctype, name)`
  only.
- Commits RIV creation and checkpoint advance together per successful
  source voucher (default batch 500, hard max 1000).
- Fail-closed if parallel / sequential / weekly RIV schedulers are not
  stopped, or if RIV In Progress / Completed / Failed are non-zero on
  continuation.

### What it does not do

- No duplicate-marking SQL (Skipped may remain 0 until real execution).
- No `repost()` / `run_parallel_reposting` / execute helpers.
- No campaign reset/delete convenience API.
- No Production deploy in this commit.

### Bench entry points (not whitelisted)

```bash
bench --site <site> execute \
  erpnext_extensions.iran_accounting.riv_campaign.generate_full_riv_campaign \
  --kwargs '{"batch_size":500}'

bench --site <site> execute \
  erpnext_extensions.iran_accounting.riv_campaign.get_full_riv_campaign_status
```

### Production

Not deployed. Not pushed.
