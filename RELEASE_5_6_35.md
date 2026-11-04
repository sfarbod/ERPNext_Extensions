# erpnext_extensions 5.6.35

## Account Explorer — Persistent Filters, Hierarchy Analyze, Running Balance, Identifier Sync & Global Reset

### Summary

Account Explorer filter state is now consistent across axes, Account Group Analyze
drills into descendants correctly, Vouchers expose Running Debit/Credit Balance,
Party selection auto-fills Identifier Field, and the top-level Reset clears all
filters except Company / Fiscal Year / dates then reloads.

### 1. Persistent cross-axis filters

- Document-panel filters (especially Identifier Field) survive Axis switches.
- `_sync_scopes_from_analysis_filters` no longer wipes `party_scope` when no
  analysis chip is present.
- Identifier-only Apply resolves matching Party masters across configured Party
  Types into exact `(party_type, party)` OR tuples for GL axes.
- Explicit Party selection without Unified Identity does **not** silently broaden.
- Filter Summary shows Identifier Field when active.

### 2. Account Group Analyze (NestedSet navigation)

- Analyze is hierarchy **navigation**, not a filter-chip-only operation.
- Clicking Analyze sets `tree_root_account`, advances to the child level, and
  issues a single new prepared execution (no stale company-wide refresh race).
- Presentation rows are filtered by NestedSet `lft`/`rgt` under the selected
  parent. Code-prefix rollups cannot invent sibling groups (e.g. phantom Group
  11 from a mis-parented `111206` under Group 21).
- Empty NestedSet child sets fall back to direct `parent_account` children —
  never silently to the company-wide Group grid.
- Analysis Path includes the selected Account Group (breadcrumb and/or effective
  account filter). Back restores the prior hierarchy scope.
- Identity filters (Document Party, Identifier, Unified Party tuples) remain
  combined with hierarchy scope as AND of NestedSet descendants and valid
  `(party_type, party)` OR tuples.

### 3. Running Debit / Credit Balance (Vouchers)

- Optional columns: `running_debit_balance`, `running_credit_balance`.
- Computed server-side with SQL window functions (grid) / chronological streaming
  accumulation (export).
- Partition: Account by default; Account + Party Type + Party when party/identity
  filters are active.
- Deterministic order: `posting_date`, `creation`, `gl_entry`.
- Opening rows: when excluded, pre-period net seeds the running balance (no double count).
- Base currency (IRR) debit/credit amounts only.

### 4. Party → Identifier synchronization

- Selecting Party Type + Party calls `resolve_party_identifier` using the
  configured Iran Accounting Party Sources identifier field (not hardcoded).
- Clearing Party Type / Party preserves Identifier Field.
- Changing Party refreshes Identifier from the new master.
- Manual Identifier entry remains editable; leading zeros preserved as strings.

### 5. Global Reset

- Top-level **Reset** clears all filters except Company, Fiscal Year, From Date,
  To Date, restores default Axis / Account Level, clears analysis/identity/
  selection state, invalidates prepared execution, and reloads automatically.
- Dropdown still offers Reset Analysis and Reset Document Scope.

### 6. Prepared Result / Export

- Running Balance columns export when selected.
- Export uses the same effective filters as the visible report.
- Reset issues a new execution id (prepared scope invalidated).

### 7. Performance

- No Redis activation.
- Running balance uses window functions / streaming accumulation (no full Python
  materialization of all GL rows for the grid page).
- Identifier resolution remains batched via existing Party Sources helpers.

### 8. Known limitations

- Running balance display order follows the user’s sort preference; values are
  always computed in chronological partition order (may look non-monotonic when
  sorted by voucher no / amount).
- Finance Book still not applied to commercial invoice item rows (unchanged).
- Large unscoped company-wide voucher exports should remain background-queued.

### 9. Migration

- No new database migration for 5.6.35.
- Existing v5.6.21 GL index patch remains required on sites that have not yet
  migrated it.

### 10. Deployment checklist

1. Deploy `erpnext_extensions` 5.6.35.
2. `bench build --app erpnext_extensions`
3. `bench clear-cache` (Desk JS)
4. Confirm GL index from 5.6.21 if not already present.
5. Smoke: Identifier cross-axis → Analyze Group 11 → Party identifier sync →
   Voucher running balance → Reset.

### 11. Rollback

Revert to previous app tag / commit. No schema changes in this release.

### Version

- `erpnext_extensions`: **5.6.35**
