# erpnext_extensions 5.7.36

## Account Explorer — Terminal Ledgers & Row-Level Inventory Attribution

### Summary

Account Explorer v5.7.36 fixes three accounting correctness issues:

1. **Optional Account Level 4 / 5** — Subsidiary Ledger accounts may be terminal
   (no deeper children). Analyze and navigation no longer invent children, throw,
   or emit false classification residuals solely because L4/L5 are absent.
2. **Own Subsidiary Ledger turnover** — Analyzing a terminal leaf such as
   `111601 - موجودی مواد اولیه` surfaces that account’s own posted GL turnover
   (opening / period debit & credit / closing) instead of an empty grid.
3. **Row-level inventory account attribution** — Item analysis under an inventory
   Account filter attributes stock movements by `SLE.warehouse → Warehouse.account`,
   not by whole Stock Entry voucher. Multi-warehouse documents no longer leak
   unrelated detail rows into the selected account scope.

### Root causes

| Issue | Cause |
| --- | --- |
| Empty Analyze on terminal SL | NestedSet drill excluded the root and found no children; code-prefix L4 treated the leaf as residual |
| Missing own turnover | Same empty Analyze path; UI also auto-advanced leaf Analyze into L4 |
| Unrelated STE items | `apply_gl_cross_filters_to_sle` / `_inventory_where_clauses` joined GLE↔SLE on `voucher_no` only; `inventory_account` warehouses were not applied to SLE WHERE |

### Fixes

**Backend (`query_builder.py`)**

- When Analyze `tree_root` has no NestedSet children **and** the presentation level
  matches the account’s code length, emit a terminal leaf row with the account’s own
  GL measures (`is_terminal_account=1`).
- When the same terminal account is viewed at a deeper optional level (L4/L5), return
  **zero rows** plus `hierarchy_empty` with Persian message — never re-show the leaf
  as a synthetic child.
- Distinguish `no_descendants_at_level` vs `descendants_no_activity`.
- Do not accumulate classification residual for an analyzed terminal root whose code
  is shorter than the presentation level (optional L4/L5).

**Backend (`inventory_scope.py`, `stock_opening.py`)**

- `warehouses_for_account_cross_filter` resolves inventory Account filters to
  warehouses via ERPNext `get_warehouse_account_map`.
- SLE queries constrain `sle.warehouse` for those accounts (row-level) while keeping
  voucher EXISTS for non-inventory Case B discovery.
- `inventory_account` document filters apply resolved warehouses in both SQL and
  Query Builder paths.
- Case A / Case B engine selection is unchanged (account→warehouse mapping is not
  folded into `has_inventory_document_filters`).

**Frontend**

- Terminal leaves (`is_group=0`) do not auto-advance into optional deeper levels on
  Analyze (`_apply_account_level_advance`, `analyze_row_as_filter`).
- Drill-graph node classification treats terminal leaves as `SubsidiaryLedger`
  (filter only; no `advance_level`).
- Level 4 / 5 empty grid shows:
  - «برای این حساب، گردش در سطح ۴ وجود ندارد.»
  - «برای این حساب، گردش در سطح ۵ وجود ندارد.»
- Known-terminal deeper level switches short-circuit the summary API and paint the
  empty state locally; returning to Subsidiary Ledger refreshes own turnover.

### Proven mapping — `MAT-STE-2026-37804-6`

| Account | Items (SLE.warehouse → account) |
| --- | --- |
| `111601` | `16200015`, `16200017`, `16200061` |
| `111604` | `15010109`, `15010188`, `15010238`, `15010307`, `15010326`, `15010425`, `16000434`, `16000533`, `16300017`, `16600053`, `16600221` |

Filtering Items by either account returns exactly that set for this voucher; no
cross-row leakage.

### Independent SQL reconciliation — `111601` (FY 1405)

Account Explorer Analyze / L3 row period debit & credit match independent:

```sql
SELECT IFNULL(SUM(debit),0), IFNULL(SUM(credit),0)
FROM `tabGL Entry`
WHERE company=… AND account='111601 - موجودی مواد اولیه - E'
  AND posting_date BETWEEN '2026-03-21' AND '2027-03-20'
  AND IFNULL(is_cancelled,0)=0 AND IFNULL(is_opening,'No')='No'
  AND voucher_type != 'Period Closing Voucher';
```

(Opening uses `posting_date < from_date` under the same status flags.)

### Tests

- `test_account_explorer_terminal_inventory_v5736.py` (11)
- NestedSet Analyze regression (`test_account_explorer_analyze_nestedset_v5635`)
- Filters / Running Balance (`test_account_explorer_filters_reset_running_v5635`)
- Playwright: `playwright_account_explorer_v5736.mjs`
  Evidence: `e2e/screenshots/account-explorer-v5736/`

### Safety

- No GL / SLE / Stock Entry / Warehouse / Account master / Production mutations
  as part of the product fix.
- Controlled fixture SLE/GL rows for the synthetic two-warehouse test are cleaned
  up in `tearDownClass`.

### Version

`erpnext_extensions.__version__ = "5.7.36"`
