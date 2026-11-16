# erpnext_extensions 5.7.43

## Account Explorer — Terminal Account Carry-Forward Across Levels

### Summary

Account Explorer v5.7.43 carries terminal ledger accounts forward through optional
presentation levels so users never lose turnover when navigating Subsidiary Ledger
(L3) → Account Level 4 → Account Level 5.

This applies to:

1. **Global Account Levels view** (no Analyze / no account filter)
2. **Individual Analyze / drill-down** of a selected account

When a Subsidiary Ledger (or Level 4) account is terminal:

- Level 4 shows the **same real account** (original code and name).
- Level 5 shows the **same real account** again when no Level 5 children exist.
- Real NestedSet children continue to replace any carry-forward behavior.
- Account master data (`parent_account`, NestedSet `lft`/`rgt`, codes) is never modified.
- Own GL turnover is never double-counted across presentation levels.
- Unfiltered global footer totals reconcile across L3, L4, and L5.

### Root cause

1. **Analyze-only gap (earlier):** v5.7.36 returned `hierarchy_empty` for deeper optional
   levels under an analyzed terminal, and the frontend short-circuited L4/L5 without a
   backend refresh.
2. **Global view gap (this fix):** In the company-wide level summary, accounts whose
   numeric code is shorter than the presentation level’s `code_length` (e.g. 6-digit
   leaf `111601` at L4/L5) could not form a code-prefix group key. Their turnover was
   pushed into `classification_residual` (`empty_code_prefix`) instead of remaining
   visible. Footer totals therefore collapsed at L4/L5 while L3 still showed the full
   population — including `111601`.

### Fix

**Backend (`query_builder.py`)**

- Coarser numeric codes with NestedSet children: skip quietly (descendants represent them).
- Coarser numeric codes that are NestedSet terminals: collect and emit
  `_build_terminal_leaf_row` carry-forward rows for **global and Analyze** scopes via
  `_append_coarser_terminal_carry_forwards`.
- Do **not** classify coarser terminals as `empty_code_prefix` residual.
- Mark rows with `is_terminal_account=1`, `is_terminal_carried_forward=1`, and
  `source_level_sequence`.
- Preserve analyzed-terminal visibility under `hide_zero_rows` when a specific account
  is in scope; global zero-row hiding remains unchanged for inactive terminals.

**Backend (`pagination.py`)**

- Keep the analyzed terminal leaf visible when `hide_zero_rows` would otherwise drop it.

**Frontend (`account_explorer.js`)**

- Level navigation always refreshes summary from the backend.
- Removed the client-side Persian empty-state short-circuit for known terminals.

**Safety**

- Measures remain each account’s posted GL — presentation carry-forward never invents
  children or duplicates aggregation.
- Query fingerprints already include `level_sequence` + account scope.
- Filters, Analyze bag, column filters, and Back navigation are unchanged.

### Tests

- `test_account_explorer_terminal_carryforward_v5743.py` (Analyze carry-forward)
- `test_account_explorer_global_level_recon_v5743.py` (**global** L3/L4/L5 reconciliation)
- Updated `test_account_explorer_terminal_inventory_v5736.py`
- Updated `test_account_explorer_analyze_nestedset_v5635.py`
- Playwright: `playwright_account_explorer_v5743.mjs`
- Playwright: `playwright_account_explorer_v5743_global_recon.mjs`
  (screenshots under `e2e/screenshots/account-explorer-v5743[-global-recon]/`)

### Version

`erpnext_extensions.__version__ = "5.7.43"`
