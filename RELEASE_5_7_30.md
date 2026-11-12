# erpnext_extensions 5.7.30

## Account Explorer — deleted Dimension filters & filter-row sticky alignment

### Summary

Follow-up to v5.7.29 Park/Activate and column-filter UI work. Manual testing showed two
remaining defects; both are fixed with browser-level evidence.

1. **Explicitly deleted Dimension analysis filters no longer resurrect** after Dimension
   Type switching. Park/Activate now respects intentional clears.
2. **Code/Title column filter inputs align with headers again.** A CSS containing-block
   rule had overridden DataTable `position: sticky`, turning sticky `left` offsets into a
   layout shift (~checkbox width).

### Root causes

| Defect | Cause |
| --- | --- |
| Deleted Cost Center chip returns | `remove_analysis_filter_chip` cleared the active bag but left `_dimension_type_parked.*.analysis_entry`. `park_dimension_type_state` also kept `prior.analysis_entry` when the active bag was empty. `activate_dimension_type_scope` then restored the deleted analysis. |
| Code filter misaligned under header | `.ae-dt-filter-cell { position: relative }` overrode `.dt-cell--sticky { position: sticky }`. With sticky styles still setting `left: 34px`, the filter cell shifted 34px (checkbox column width) while header/body stayed sticky. |

### Fixes

- `forget_parked_dimension_analysis()` + `_dimension_type_cleared` on chip remove / clear-all
- Active park mirrors live bag only (empty bag ⇒ null analysis, never prior revive)
- Activate restores analysis only when not cleared
- Re-Analyze clears the cleared flag for that Dimension Type
- Filter-cell CSS: keep sticky for sticky cells; `position: relative` only for non-sticky cells

### Files

- `erpnext_extensions/page/account_explorer/account_explorer.js`
- `erpnext_extensions/page/account_explorer/account_explorer.css`
- `iran_accounting/tests/test_account_explorer_v5730_filter_delete_alignment.py`
- `iran_accounting/e2e/playwright_account_explorer_v5730.mjs`
- `iran_accounting/e2e/screenshots/account-explorer-v5730/`

### Safety

- Backend Dimension Type isolation unchanged
- Undeleted parked types still restore independently
- No GL / SLE / master data mutations; no push/deploy in this task
