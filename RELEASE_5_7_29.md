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
6. **Dimension session filter data accuracy** — Analyzing a Department (or any
   Dimension) then switching Dimension Types and leaving to Accounts / Parties /
   Vouchers re-promotes undeleted parked analyses into the active query bag so GL
   results stay constrained. Backend `gle_filters` / `query_spec` already applied
   `department = …` correctly when present; the UI bag drop was the accuracy gap.

### Root causes addressed

| Defect | Cause |
| --- | --- |
| Dimension Type leakage | Active bag / projection kept non-active Dimension Types; summary rendered them as live filters. |
| Deleted filter resurrection | Park kept `prior.analysis_entry` after chip remove; Activate restored it. |
| “Deleted” filters reappearing on tab return | Parked analyses were hidden, so users could not delete them on another Dimension Type; returning looked like resurrection. |
| Code/Title filter misalignment | `.ae-dt-filter-cell { position: relative }` overrode DataTable sticky, shifting filter cells by sticky `left`. |
| Aggressive column-filter requests | 350 ms debounce without Enter flush / stale guards. |
| Unrelated GL / loan totals after Department Analyze | Dimension Type isolation removed Department from the active bag on Cost Center; leaving Dimensions left Accounts/Vouchers **unfiltered** while a parked Department chip remained visible — company-wide turnover (incl. loan groups) appeared to belong to the selected Department. |

### Semantics

| Action | Behavior |
| --- | --- |
| **Park** | Snapshot current Dimension Type analysis / selection / breadcrumbs / column filters / page. |
| **Activate** | Make one Dimension Type active in the query bag; restore parked analysis only if not cleared. |
| **Delete / Clear** | Remove from active bag **and** forget parked analysis (`_dimension_type_cleared`). |
| **Restore (Dimension tab)** | Only for undeleted parked analyses of that Dimension Type. |
| **Leave Dimensions axis** | Re-promote all undeleted parked Dimension analyses into the active bag (`restore_parked_dimension_filters_to_active_bag`) so GL axes keep session filters. |
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
- `iran_accounting/tests/test_account_explorer_v5729_dimension_data_accuracy.py`
- `iran_accounting/e2e/playwright_account_explorer_v5729.mjs`
- `iran_accounting/e2e/playwright_account_explorer_v5729_nav.mjs`
- `iran_accounting/e2e/playwright_account_explorer_v5729_data_accuracy.mjs`
- `iran_accounting/e2e/screenshots/account-explorer-v5729/`
- `iran_accounting/e2e/screenshots/account-explorer-v5729-nav/`
- `iran_accounting/e2e/screenshots/account-explorer-v5729-data/`

### Tests

- Unit / integration: Dimension isolation, delete-vs-park, sticky CSS assertions
- Backend data accuracy: independent GL SQL reconciliation for Department
  `ایمنی و بهداشت - E` (period debit/credit/count), empty department → zero rows,
  voucher lines must match Department, no cross-type contamination
- Playwright: isolation, park preserve, explicit delete, main-axis tour, **Dept→CC→Accounts
  keeps GL filter**, deleted filters do not restore, CC isolation while parked,
  summary vs payload, filter alignment, repeated navigation

### Cross-axis filter semantics (final)

| Context | Active query filters |
| --- | --- |
| Dimensions + Dimension Type tab | Only the **active** type's undeleted analysis |
| Leave Dimensions → Accounts / Parties / Vouchers / Currency / Items / Item Groups | All **undeleted parked** Dimension session analyses re-enter the active bag (AND) via `restore_parked_dimension_filters_to_active_bag` |
| Clear Selection | Row checkboxes only — analysis / parked unchanged |
| Reset All Axes | Grid column preferences only — analysis unchanged |
| Clear Analysis Filters / Reset All Filters / Delete chip | Forget parked analysis — restore must not revive |

Parked chips are not proof of an active predicate. Payload `accounting_dimensions` + independent GL SQL are.

### Cross-axis matrix coverage

Playwright `playwright_account_explorer_v5729_cross_axis_matrix.mjs` + backend
`test_account_explorer_v5729_cross_axis_matrix.py` cover Department→Cost Center /
Project / Facility / Bank Dimension hops into Accounts, Parties, Vouchers, Items;
type return; Clear Selection; Delete; Reset All Filters; Reset All Axes; empty
department; dual Dimension AND; Cost Center session restore.

### Safety

- Backend Dimension Type isolation unchanged / not weakened
- No GL Entry / Stock Ledger Entry / Account master mutations
- No Production publish; no push/deploy in this release task

### Version freeze note

All Account Explorer navigation / park-delete / filter-alignment / data-accuracy
corrections for this cycle remain under **5.7.29**. No separate 5.7.30 application
version is published.
