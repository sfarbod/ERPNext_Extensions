# erpnext_extensions 5.6.41

## Scope Correction — Adjustment-Only GL Aggregation

### Policy

Iran Accounting may same-side-aggregate **only** configured adjustment accounts:

- `Company.stock_adjustment_account` (e.g. `621301`)
- `Company.round_off_account` (financial / rate rounding)

WIP, FG, scrap, inventory, expense, revenue, payable, receivable, and all other
Core accounts are left exactly as ERPNext Core produced them.

### Removed over-broad behavior

- No global `ensure_one_sided_gl_entries` inside Core `merge_similar_entries`
- No full-map one-sided rewrite in `StockController.make_gl_entries`
- Aggregation is isolated in `aggregate_adjustment_gl_entries` with an explicit
  account allowlist

### Preserved

- Same-side only (Debit↔Debit, Credit↔Credit; never net opposite sides)
- `621301` Core 461 + A2 1970 → one Credit 2,431 when dimensions match
- A2 WIP/stock counter remains a discrete localization partner leg (not
  rewritten by aggregation)
- Invoice round-off allowlist aggregation via `save_entries` hook

### Tests

`test_stock_adjustment_gl_aggregate_v575` — adjustment-only + Core fingerprint
