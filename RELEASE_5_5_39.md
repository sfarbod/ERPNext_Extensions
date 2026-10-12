# Release 5.5.39 — Account Explorer accounting correctness, GL-line vouchers, and performance

**Version:** 5.5.39 (unchanged application version)  
**Date:** 2026-10-12  
**Previous:** 5.5.38  
**Branch:** `develop`  
**Development site:** `development.localhost`  
**Validated stack:** Frappe **16.36.1**, ERPNext **16.37.0**

**Validation scope:** Development site only (restored Espad company dataset).  
**Production:** Not tested in this release cycle. Do not treat Development baselines as live Production balances.

## Compatibility

| Component | Version |
|---|---|
| Frappe | 16.36.1 |
| ERPNext | 16.37.0 |
| persian_calendar | 2.0.2 (Jalali export helpers) |
| erpnext_extensions | **5.5.39** (`__version__`) |

## Source-of-truth contract

| Path | When | Authoritative source |
|---|---|---|
| **Case B** | No Item / Item Group / Warehouse document filters | Posted `tabGL Entry` |
| **Case A** | Inventory document filters active | `tabStock Ledger Entry` (SLE-scoped) |

Do not force equality between Case A stock values and Case B GL amounts — they answer different questions.

## Root cause of the ~15 second Account Levels wait (pre-fix)

Most of the time was the E1 posted-GL calculation (~10–12 s on the restored Espad company): Trial Balance opening, opening-flagged auxiliary SQL, and period turnover, plus prepared-queue/artifact overhead on Case B Account Levels.

## Changes (this release cycle)

Commits on `develop` (oldest → newest):

1. `f759356` — Atomic accounting revision + Case B Account Levels direct fast path  
2. `1762794` — E1 single-pass GL aggregation  
3. `a98ea2e` — Account Level leaf-measure reuse  
4. `fa816ea` — Voucher GL Entry line-level grid + Jalali export date option  
5. `6ce31d5` — FY1404 opening isolation + Voucher Direct/Prepared GL-line parity + voucher pagination  
6. `2fced85` — Export Selected + Item 13100057 documentation  

### 1. Atomic accounting revision
`bump_accounting_revision` uses `UPDATE revision = LAST_INSERT_ID(revision + 1)` in the caller’s transaction. Concurrent writers no longer collapse increments. Rollbacks discard the bump.

### 2. Case B Account Levels fast path
When Account Levels has no item / item group / warehouse filter, `build_account_level_summary` runs **in-request** (no background job / gzip / poll). Case A inventory-filtered Account Levels still use Prepared Results. Clients may force prepared with `prepared_mode=prepared`.

**Note:** The **Voucher** axis defaults to **direct** SQL pagination after the GL-line redesign; prepared is only when forced.

### 3. Period GL without FORCE INDEX
Company-wide E1 period aggregation uses SUM/GROUP BY without ERPNext’s `posting_date_company_index` force. Opening formulas unchanged.

### 4. E1 single-pass GL aggregation
When ACB / cancelled / voucher / party / currency / dimension filters are absent and unclosed FY P&L balances are shown, Case B E1 uses one company-indexed conditional `SUM(CASE …)` scan. Same `adjust_tb_opening_for_policy` / aux_in / EXCLUDE rules. Unsupported scopes keep legacy multi-query E1.

### 5. Account Level presentation reuse
Level pill changes keep `execution_id` and reuse execution-local leaf GL measures (execution + revision + financial hash; `level_sequence` excluded). Case A never uses this cache. Axis / filter / date / revision changes recompute.

### 6. Vouchers → one row per GL Entry
Each row is one posted `tabGL Entry` (`row_key = glentry:<name>`). Columns include account code/name, debit/credit, account currency, remarks, party, dimensions, against voucher, and best-effort Item/Warehouse only when `voucher_detail_no` maps to exactly one SLE. Totals = full filtered company-currency debit/credit/net. Default execution is direct SQL pagination.

### 7. Export Date Format (existing Export UI)
Jalali (default) / Gregorian on the existing Export control. Applies to Date/Datetime columns for **all axes** via `persian_calendar`. Account codes and document numbers stay text.

### 8. FY1404 opening-balance isolation
Unbounded `is_opening=Yes` previously pulled FY1405 opening vouchers into FY1404 Account Levels. Fix: bound opening-flagged rows to `posting_date <= to_date` (single-pass + legacy TB strip). Aligns with AE `AFTER_PERIOD` policy. Empty FY1404 → zero measures/footer.

### 9. Voucher Direct / Prepared parity + pagination
Forced prepared materializes GL lines (`voucher_view_mode=gl_line`). Legacy voucher-header artifacts are rejected. First page prefers one window aggregate + `FORCE INDEX (company)`; totals cached per execution for later pages.

### 10. Grid selection → Copy + Export Selected
Checkboxes kept for Copy Selected. Export menu: CSV/XLSX — All filtered / Selected. Identity is authoritative `row_key`. Whitelist forwards `date_format`, `row_scope`, `selected_row_keys`. Selection is **page-local**.

### 11. Item 13100057 (documentation only — no data repair)
Positive closing qty with large negative SLE valuation (outlier SLE / Material Issue). AE Case A reporting matches independent SLE `stock_value_difference` semantics. **No SLE repair in this release.**

## Financial reconciliation (Development)

Company: اسپاد فارمد دارو

| Check | Result |
|---|---|
| FY1404 GL rows (period + before) | **0** |
| FY1404 ACB | **0** |
| FY1404 Account Levels | Empty / zero totals (no ghost openings) |
| FY1404 → FY1405 → FY1404 | No cross-year contamination |
| FY1405 Account `11100409` | **994,353,195,185 IRR** (Δ **0** vs independent child GL net) |
| Voucher GL-line totals | Debit = Credit (scoped); row keys `glentry:*` |
| Direct vs Prepared (same day scope) | Matching GL IDs / totals; legacy headers rejected |
| Export Selected | Filters by `row_key` order |
| Jalali `2026-03-21` | `1405-01-01` |
| Gregorian `2026-03-21` | `2026-03-21` |

## Performance (Development, FY1405)

| Path | Result |
|---|---|
| Account Levels API cold (8 runs) | median **~2.19 s** (min 2.12 / p95 2.21 / max 2.42) |
| Account Level change (same execution) | **~0.02 s** API (leaf cache hit) |
| Vouchers first page API (8 runs) | median **~2.30 s** (min 2.15 / p95 2.41 / max 2.46) |
| Prior Case B prepared path (historical) | ~15 s browser / ~11–13 s calc |

## Security and permissions

- Accounts role gate (`Accounts User` / `Accounts Manager` / `Auditor`) + feature flags in Iran Accounting Settings.  
- Company + `GL Entry` read checks on company/export paths.  
- Export requires accounts role + GL read + `export_enabled`.  
- Voucher navigation uses Frappe `has_permission` on the source DocType; Guest export blocked.  
- SQL uses parameterized Query Builder / bind values.  
- Prepared results are execution-scoped (no cross-execution reuse).

## Mutation coverage (revision)

| Path | Advances revision |
|---|---|
| GL Entry insert / update / trash | Yes |
| Period Closing Voucher submit / cancel | Yes |
| Account Closing Balance update / trash | Yes |
| Accounting Dimension update / trash | Yes (all companies) |
| SLE insert / SQL cancel / repost / historical repair | **No** |
| GL SQL-only cancel (`set_as_cancel`) | **No** (reverse GL inserts still bump) |

Do **not** enable Redis financial indexes until SLE and direct SQL paths advance a proven revision.

## Known limitations

- Item/Warehouse on a Voucher GL line only when `voucher_detail_no` → exactly one SLE.  
- Case A does not allocate GL amounts across items.  
- Company-wide Voucher first page remains a multi-second GL scan (~84k lines).  
- Grid selection does not persist across pages.  
- Unclassified / non-numeric account codes are excluded from visible Account Levels rows (residual tracked separately).  
- Item 13100057 stock **valuation** outlier remains in SLE until explicitly authorized repair.  
- `bench list-apps` may still show an older installed metadata label; authoritative code version is `__version__ = "5.5.39"`.

## Upgrade / deployment checklist (when authorized)

**Development validated. Production not yet tested.**

1. Deploy this commit on the target site (do **not** bump version).  
2. `bench build --app erpnext_extensions`  
3. Hard-reload Desk (clear browser cache if assets stick).  
4. `bench clear-cache` recommended.  
5. `bench migrate` — **not required** for this release’s Account Explorer changes (no new DocType/patch dependency for the AE paths above).  
6. Restart workers only if background export/prepared queues were mid-flight during deploy.  
7. Smoke: FY isolation, Account Levels baseline account, Vouchers GL lines, Export Selected, Jalali dates.  
8. Do **not** activate Redis research prototypes (`universal_index` / accounting index).

## Rollback

Redeploy **5.5.38**. Case B Account Levels return to Prepared Results automatically on older code.

## Regression tests (Development)

- FY isolation  
- Voucher GL lines + Direct/Prepared parity  
- Leaf-measures cache  
- Revision concurrency  
- Prepared Result lifecycle  
- Selection / Export Selected  
- Item 13100057 SLE parity (reporting only)  
- Playwright Desk gate (FY1404/1405, axes, vouchers, selection, export UI)

## Redis / research

`ae_accounting_index` and `ae_universal_index` remain disabled and are not connected to the live API. Untracked research prototypes must not be committed or activated with this release.
