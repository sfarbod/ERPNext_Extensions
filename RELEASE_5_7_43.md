# erpnext_extensions 5.7.43

## Account Explorer — Terminal Account Carry-Forward Across Levels

### Summary

Account Explorer v5.7.43 carries terminal ledger accounts forward through optional
presentation levels so users never encounter an empty grid when the selected account
has no NestedSet descendants at Level 4 or Level 5.

When a Subsidiary Ledger (or Level 4) account is terminal:

- Level 4 shows the **same real account** (original code and name).
- Level 5 shows the **same real account** again when no Level 5 children exist.
- Real NestedSet children continue to replace any carry-forward behavior.
- Account master data (`parent_account`, NestedSet `lft`/`rgt`, codes) is never modified.
- Own GL turnover is never double-counted across presentation levels.

### Root cause

v5.7.36 introduced correct terminal own-turnover at the account’s matching code length,
but intentionally returned `hierarchy_empty` / Persian “no turnover at level N” for
deeper optional levels. The frontend also short-circuited Level 4/5 navigation for known
terminals without a backend refresh. That prevented empty-level confusion but blocked
continuous hierarchy navigation.

### Fix

**Backend (`query_builder.py`)**

- When Analyze `tree_root` has no NestedSet children, always emit
  `_build_terminal_leaf_row` at the current presentation level (L3/L4/L5).
- Mark rows with `is_terminal_account=1`, `is_terminal_carried_forward=1` when the
  presentation level is deeper than the account’s source code-length level, and
  `source_level_sequence` for the actual source level.
- Preserve account metadata (`parent_account`, `root_type`) on the presentation row.
- Keep analyzed terminal rows visible under `hide_zero_rows` so zero-activity terminals
  do not collapse into an empty grid.
- Real NestedSet children still win; no parent duplication alongside children.

**Frontend (`account_explorer.js`)**

- Level navigation always refreshes summary from the backend.
- Removed the client-side Persian empty-state short-circuit for known terminals.

**Safety**

- Measures remain the single account’s posted GL (same as L3 own turnover).
- Query fingerprints already include `level_sequence` + `tree_root_account`.
- Filters, Analyze bag, column filters, and Back navigation are unchanged.

### Tests

- `test_account_explorer_terminal_carryforward_v5743.py`
- Updated `test_account_explorer_terminal_inventory_v5736.py` (L4/L5 carry-forward)
- Updated `test_account_explorer_analyze_nestedset_v5635.py`
- Playwright: `playwright_account_explorer_v5743.mjs`
  (screenshots under `e2e/screenshots/account-explorer-v5743/`)

### Version

`erpnext_extensions.__version__ = "5.7.43"`
