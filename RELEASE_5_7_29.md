# erpnext_extensions 5.7.29

## Account Explorer — Dimension isolation, Park/Activate, filter delete, column-filter UI

### Summary

Account Explorer v5.7.29 delivers Dimension Type isolation, durable Park/Activate
semantics with explicit-delete precedence, Filter Summary / payload consistency,
debounced column filters, and correct sticky filter-row alignment.

1. **Dimension Type isolation** — Analyzing or switching Dimension Types does not
   leak another type into the active query, Filter Summary active chips, or result rows.
2. **Park / Activate with explicit delete precedence** — Switching Cost Center ↔
   Department parks undeleted analyses and restores them on return. Explicit chip
   remove / Clear Analysis Filters forgets parked state so deleted filters never
   resurrect.
3. **Parked chips are visible and removable** — Parked Dimension analyses appear in
   Filter Summary under “Parked Dimension Filters” (not in the active payload). Users
   can delete them without switching tabs; Clear Analysis Filters also clears parked.
4. **Column-filter debounce (500 ms)** — Grid column filters wait 500 ms after the
   last keystroke; Enter and Clear apply immediately; stale responses are ignored.
5. **DataTable Code/Title filter alignment** — Sticky filter cells keep
   `position: sticky` so filter inputs align with headers (no hardcoded offsets).

### Root causes addressed

| Defect | Cause |
| --- | --- |
| Dimension Type leakage | Active bag / projection kept non-active Dimension Types; summary rendered them as live filters. |
| Deleted filter resurrection | Park kept `prior.analysis_entry` after chip remove; Activate restored it. |
| “Deleted” filters reappearing on tab return | Parked analyses were hidden, so users could not delete them on another Dimension Type; returning looked like resurrection. |
| Code/Title filter misalignment | `.ae-dt-filter-cell { position: relative }` overrode DataTable sticky, shifting filter cells by sticky `left`. |
| Aggressive column-filter requests | 350 ms debounce without Enter flush / stale guards. |

### Semantics

| Action | Behavior |
| --- | --- |
| **Park** | Snapshot current Dimension Type analysis / selection / breadcrumbs / column filters / page. |
| **Activate** | Make one Dimension Type active in the query bag; restore parked analysis only if not cleared. |
| **Delete / Clear** | Remove from active bag **and** forget parked analysis (`_dimension_type_cleared`). |
| **Restore** | Only for undeleted parked analyses. |
| **Presentation Dimension Type chip** | Non-removable tab indicator; not an analysis filter. |

### Files (primary)

- `erpnext_extensions/page/account_explorer/account_explorer.js`
- `erpnext_extensions/page/account_explorer/account_explorer.css`
- `erpnext_extensions/page/account_explorer/core/explorer_analysis_filters.js`
- `erpnext_extensions/page/account_explorer/core/explorer_analysis_filter_summary.js`
- `erpnext_extensions/page/account_explorer/adapters/ae_datatable_adapter.js`
- `iran_accounting/account_explorer/gle_filters.py`
- `iran_accounting/account_explorer/query_spec.py`
- `iran_accounting/tests/test_account_explorer_v5729_dimension_isolation_debounce.py`
- `iran_accounting/tests/test_account_explorer_v5729_filter_delete_alignment.py`
- `iran_accounting/e2e/playwright_account_explorer_v5729.mjs`
- `iran_accounting/e2e/playwright_account_explorer_v5729_nav.mjs`
- `iran_accounting/e2e/screenshots/account-explorer-v5729/`
- `iran_accounting/e2e/screenshots/account-explorer-v5729-nav/`

### Tests

- Unit / integration: Dimension isolation, delete-vs-park, sticky CSS assertions
- Playwright: isolation, park preserve, explicit delete (including parked chip), main-axis tour, summary vs payload, filter alignment, repeated navigation

### Safety

- Backend Dimension Type isolation unchanged / not weakened
- No GL Entry / Stock Ledger Entry / Account master mutations
- No Production publish; no push/deploy in this release task

### Version freeze note

All Account Explorer navigation / park-delete / filter-alignment corrections for this
cycle remain under **5.7.29**. No separate 5.7.30 application version is published.
