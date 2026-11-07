# erpnext_extensions 5.7.7

## Account Explorer — FY defaults, column filters, full pagination, Running Balance

### Summary

Account Explorer v5.7.7 delivers four UX/accounting improvements without changing
Case A / Case B GL–SLE semantics:

1. **Financial Year date defaults** — opening the report (and changing Fiscal Year)
   resolves **From Date / To Date** from `Fiscal Year.year_start_date` /
   `year_end_date`. The default To Date is the FY closing date, never today.
   Intentional user date selections are preserved across refresh, Analyze, and Back.
2. **Column-level filters** — DataTable header filters are applied **server-side**
   (full result set before the page slice), combine with global/identity filters,
   and clear with Global Reset.
3. **Full pagination** — First / Previous / page input / Next / Last plus page size,
   total pages, and total rows on every Account Explorer grid axis.
4. **Single Running Balance** — Voucher grid and CSV/XLSX export replace
   Running Debit Balance + Running Credit Balance with one signed
   `Running Balance` column (`100 Dr` / `20 Cr` / `0`), continuous across pages
   and partitioned by account (and party when party-partitioned).

### Implementation notes

| Area | Approach |
| --- | --- |
| FY defaults | Client `sync_dates_from_fy` + bootstrap await before first refresh; `_dates_user_override` guards intentional ranges |
| Column filters | `column_filters.py` normalize/apply; summary axes via `paginate_*`; voucher via GLE SQL (+ windowed running-balance predicate) |
| Pagination | `total_pages` / `has_previous` on summary + voucher responses; shared `.ae-pagination-bar` UI |
| Running Balance | SQL window `opening + sum(debit-credit)` → `running_balance` / `running_balance_display`; legacy preference keys migrated |

### Files (primary)

- `iran_accounting/account_explorer/column_filters.py` (new)
- `iran_accounting/account_explorer/voucher_gl_lines.py`
- `iran_accounting/account_explorer/pagination.py`
- `iran_accounting/account_explorer/query_spec.py` / `schemas.py` / `api.py` / `export.py` / `constants.py` / `query_fingerprint.py`
- `erpnext_extensions/page/account_explorer/account_explorer.js`
- `erpnext_extensions/page/account_explorer/adapters/ae_datatable_adapter.js`
- `erpnext_extensions/page/account_explorer/account_explorer.css`
- Tests: `test_account_explorer_v577_fy_filters_pagination_running.py`, updated v5.6.35 running-balance assertions
- Playwright: `iran_accounting/e2e/playwright_account_explorer_v577.mjs`

### Tests

**Unit / API**

- `test_account_explorer_v577_fy_filters_pagination_running.py`
- `test_account_explorer_filters_reset_running_v5635.py` (Running Balance field migration)

**Browser**

- `playwright_account_explorer_v577.mjs` against `development.localhost`
  - FY closing range vs today
  - User date preservation
  - Column filters + reset to page 1
  - Full pagination toolbar + page stability
  - Single Running Balance column, accumulation, page continuity
  - Grid field parity for export columns

### Version

`erpnext_extensions.__version__` → **5.7.7**

### Corrective UI fixes (same unreleased 5.7.7)

1. **Duplicate / malformed pagination**
   - Root cause: page-size control was rendered both in the grid toolbar prefs and in
     the bottom pagination bar; page/row counts used `frappe.format(..., Int)` which
     injects HTML wrappers, then inserted via `createTextNode` (raw markup visible).
   - Fix: single `[data-ae-pagination]` toolbar only; plain integer formatting;
     page size removed from grid toolbar prefs.

2. **Party Name column filter**
   - Root cause: DataTable `update()`/`refresh()` rebuilt filter inputs without
     restoring values; empty filtered results destroyed the grid (and filters);
     Party Name (`display_title`) column filters ran **before** party enrichment,
     so Persian name searches matched nothing useful / lost UI state.
   - Fix: restore + clear (×) on every mount/update; keep empty DataTable when
     column filters are active; force full enrichment when `display_title` /
     `party_name` column filters are present; chip + individual clear.

### Deploy note

No schema migration required. Clear browser cache / hard-reload Desk so Account Explorer
JS/CSS picks up the pagination toolbar and Running Balance column. Saved grid preferences
that hid `running_debit_balance` / `running_credit_balance` are migrated to `running_balance`.
