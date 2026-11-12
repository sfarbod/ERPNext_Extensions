# erpnext_extensions 5.7.29

## Account Explorer — Dimension Analyze isolation & debounced column filters

### Summary

Account Explorer v5.7.29 fixes two confirmed defects and hardens related
cross-axis state handling:

1. **Dimension Type isolation** — Analyzing a Dimension row (or switching
   Dimension Type tabs) no longer leaks another type into the query scope,
   Filter Summary, or result rows. Cost Center, Department, Project, and other
   configured Dimension Types are strictly isolated.
2. **Column-filter debounce (500 ms)** — Grid column filters wait **500 ms**
   after the last keystroke before requesting a refresh. Enter and Clear apply
   immediately. Stale asynchronous responses cannot overwrite newer filter
   results (generation + sequence guards).

### Root causes

| Defect | Cause |
| --- | --- |
| Dimension Type leakage | `replace_dimension` only upserted one `dimensions.*` key; switching Dimension Type kept prior analysis chips; projection parked **all** dimension filters into `accounting_dimensions`, so Cost Center still filtered a Department summary. Filter Summary rendered those stale chips. |
| Excessive column-filter requests | Column filters used a **350 ms** timer on top of DataTable’s keydown path, firing too aggressively while typing with no Enter flush path. |

### Fixes

- `AnalysisFilters.apply_policy("replace_dimension")` clears other Dimension Types.
- `isolate_dimension_type_analysis_scope()` on Dimension Type switch / Analyze.
- Payload projection and Filter Summary hide non-active Dimension Types on the Dimensions axis.
- Backend `query_spec` + `gle_filters` enforce active `dimension_scope.dimension_type` only.
- Column filters: **500 ms** debounce, Enter immediate, Clear immediate, sequence/generation race protection.

### Files (primary)

- `erpnext_extensions/page/account_explorer/core/explorer_analysis_filters.js`
- `erpnext_extensions/page/account_explorer/core/explorer_analysis_filter_summary.js`
- `erpnext_extensions/page/account_explorer/account_explorer.js`
- `erpnext_extensions/page/account_explorer/adapters/ae_datatable_adapter.js`
- `iran_accounting/account_explorer/gle_filters.py`
- `iran_accounting/account_explorer/query_spec.py`
- `iran_accounting/tests/test_account_explorer_v5729_dimension_isolation_debounce.py`
- `iran_accounting/e2e/playwright_account_explorer_v5729.mjs`
- `iran_accounting/e2e/playwright/ae_v5729_unit.cjs`
- `iran_accounting/e2e/screenshots/account-explorer-v5729/`

### Tests

- Unit harness: `ae_v5729_unit.cjs` (replace_dimension, projection, summary, debounce, stale guard)
- Integration: `test_account_explorer_v5729_dimension_isolation_debounce.py`
- Playwright: `playwright_account_explorer_v5729.mjs` on `development.localhost`

### Safety

- No GL Entry / Stock Ledger Entry / Account master mutations
- No Production changes; no push/deploy in this release task
- Case A / Case B, Running Balance, and export parity unchanged
