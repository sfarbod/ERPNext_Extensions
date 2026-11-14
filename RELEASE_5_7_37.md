# erpnext_extensions 5.7.37

## Account Explorer — Column Filter Debounce 6000 ms

### Summary

Column filters across all Account Explorer grids use a **true trailing-edge
debounce of 6000 milliseconds after the last keystroke**.

Previously (v5.7.29) the interval was 500 ms, which applied filters after one or
two characters and interrupted normal typing with premature grid refreshes.

**Corrective note (same version 5.7.37):** the debounce was first shipped at
3000 ms and then adjusted to **6000 ms** so longer typed values (for example
item codes) are not applied mid-entry.

### Required behavior

| Event | Behavior |
| --- | --- |
| Keystroke | Reset the 6000 ms timer; do not apply |
| 6000 ms since last keystroke | Apply the complete filter value once |
| Enter | Apply immediately; cancel pending timer |
| Clear (X) / clear column | Apply immediately; cancel pending timer |
| Axis / level / Dimension Type change | Cancel pending timer (no stale apply) |

### Implementation

- Central constant: `AE_COLUMN_FILTER_DEBOUNCE_MS = 6000` in `account_explorer.js`
- Shared handler: `handle_datatable_column_filters` (all axes via DataTable adapter)
- `cancel_pending_column_filter_debounce()` used by Enter/Clear paths, `clear_column_filters`,
  and `switch_axis`
- Sequence/`filter_seq` protection retained so older applies cannot overwrite newer ones

### Axes covered

Account Levels, Parties, Dimensions, Currencies, Item Groups, Items, Vouchers —
all share the same DataTable server-filter path.

### Tests

- `test_account_explorer_column_filter_debounce_v5737.py`
- Updated `test_account_explorer_v5729_dimension_isolation_debounce.py` (6000 ms)
- Updated `ae_v5729_unit.cjs` fake-clock harness
- Updated `playwright_account_explorer_v5729.mjs` debounce waits
- `playwright_account_explorer_v5737.mjs` (Tests A–F)

### Version

`erpnext_extensions.__version__ = "5.7.37"`
