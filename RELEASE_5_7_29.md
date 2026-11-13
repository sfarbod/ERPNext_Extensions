# erpnext_extensions 5.7.29

## Account Explorer — Global Cross-Axis Filtering Architecture

### Summary

Account Explorer v5.7.29 corrects the Park / Activate Dimension isolation model.
**Every Analyze / filter from any axis is a global AND constraint** on all seven
main axes. Dimension Type is presentation state only. Switching axes or Dimension
Types never parks, hides, or drops active global filters. Empty intersections
return zero rows (never unfiltered company-wide data).

1. **Global filter bag** — `analysis_filters` is the single authoritative state for
   Account, Party, Item, Item Group, Currency, Voucher, and all Accounting Dimensions.
2. **Multi-Dimension AND** — Department + Cost Center + Project (+ …) remain active
   simultaneously; Dimension Type tab only chooses which Dimension grid to browse.
3. **Filter Summary** — Shows the complete global filter set; no parked/inactive
   Dimension category. Individual remove and Clear All operate on the global bag.
4. **Backend** — `query_spec` no longer strips non-active Dimension Types;
   `gle_filters._apply_document_dimension_filters` applies every document dimension.
5. **Column-filter debounce (500 ms)** — retained; Enter / Clear apply immediately.
6. **Sticky filter alignment** — retained.

### Root cause of the old Park / Activate design

Park / Activate treated Dimension Type as filter ownership: switching Department →
Cost Center removed Department from the query bag (parking it) so only the active
type constrained Dimensions. Leaving Dimensions tried to “restore” parked filters
onto GL axes. That produced:

- Filters disappearing when switching Dimension Types or main axes
- Inconsistent Filter Summary vs payload vs SQL
- Risk of unfiltered company-wide results while a “parked” chip still looked active
- Conceptual mismatch with the business rule that all axes share one accounting scope

### New architecture

| Concern | Owner |
| --- | --- |
| Analytical constraints | `analysis_filters` global bag → projected `document_scope.accounting_dimensions` + scopes |
| Dimension Type tab / selected value presentation | `analysis_context.dimension_scope` |
| Column filters / page / breadcrumbs per type | `_dimension_type_ui` (presentation only) |
| Grid selection / column prefs | Separate UI state (`Clear Selection`, `Reset All Axes`) |

### Semantics

| Action | Behavior |
| --- | --- |
| **Analyze / Use as Filter** | Upsert into global bag (AND). Source axis is metadata only. |
| **Switch main axis** | Presentation only; global bag unchanged. |
| **Switch Dimension Type** | Presentation only; all Dimension filters stay in query. |
| **Remove chip** | Delete that key from global bag; never resurrects. |
| **Clear All Filters** | Empty global bag + UI maps; no resurrection after tab hops. |
| **Empty intersection** | Zero rows / zero totals on every applicable axis. |

### Files (primary)

- `erpnext_extensions/page/account_explorer/account_explorer.js`
- `erpnext_extensions/page/account_explorer/core/explorer_analysis_filters.js`
- `erpnext_extensions/page/account_explorer/core/explorer_analysis_filter_summary.js`
- `iran_accounting/account_explorer/gle_filters.py`
- `iran_accounting/account_explorer/query_spec.py`
- `iran_accounting/tests/test_account_explorer_v5729_dimension_isolation_debounce.py`
- `iran_accounting/tests/test_account_explorer_v5729_global_filters.py`
- `iran_accounting/tests/test_account_explorer_v5729_cross_axis_matrix.py`
- `iran_accounting/tests/test_account_explorer_v5729_dimension_data_accuracy.py`
- `iran_accounting/e2e/playwright/ae_v5729_unit.cjs`
- `iran_accounting/e2e/playwright_account_explorer_v5729_global_filters.mjs`
- `iran_accounting/e2e/playwright_account_explorer_v5729_cross_axis_matrix.mjs`
- `iran_accounting/e2e/playwright_account_explorer_v5729*.mjs` (rewritten obsolete park assertions)
- `iran_accounting/e2e/screenshots/account-explorer-v5729-global/`

### Tests

- Unit: `replace_dimension` keeps siblings; projection emits all dims; summary shows all;
  debounce 500 ms
- Backend integration: department-only SQL recon; empty Project ∩ Department → 0 on all
  axes; Department ∧ Cost Center; query_spec keeps multi-dims; party/voucher respect dept
- Playwright E2E 1–7: persistence, multi-axis, empty intersection, individual delete,
  clear all, multi-Dimension Types, 30 transitions + SQL recon smoke
- Regression: rewritten former isolation / park suites to the global contract

### Version / Git

- `__version__` remains **5.7.29** (strict freeze; not a new application version)
- No new release tag unless explicitly requested
- Publication target: `develop` → `upstream/develop` (fast-forward only; no force-push)
- Release commit for Global Cross-Axis Filtering: `b9359d33147735ebf987fdf2a378df5815f4fa85`

### Independent SQL reconciliation (reference)

Department `ایمنی و بهداشت - E` on company `اسپاد فارمد دارو`, FY 1405 period
turnover (non-opening, non-cancelled, excluding Period Closing Voucher):

- period_debit ≈ **5592288032**
- period_credit ≈ **1868092078**

Account Explorer totals for the same department filter must match these independent
GL Entry aggregates. Empty intersections (e.g. Department ∧ nonexistent Project)
must return zero rows / zero turnover on every applicable axis — never company-wide
fallback data.

### Acceptance (v5.7.29)

- A filter from any axis constrains all other applicable axes.
- Multiple Dimension Types and entity filters combine with AND semantics.
- Axis / Dimension Type switches never park or drop global filters.
- Individual remove and Clear All operate on the authoritative global bag.
- Filter Summary, client state, request payload, backend SQL, and displayed
  results remain consistent.
