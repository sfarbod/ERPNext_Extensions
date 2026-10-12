# Release 5.5.39 — Account Explorer revision hardening and Case B fast path

**Date:** 2026-10-12  
**Previous:** 5.5.38  
**Site:** development.localhost

## Root cause of the ~15 second Account Levels wait

Most of the time is the E1 posted-GL calculation itself (~10–12 s on the restored Espad company): Trial Balance opening (~3.6 s), opening-flagged auxiliary SQL (~2.9 s), and period turnover (~3–5 s). Queue wait, gzip artifact write, and polling added further delay on top of that calculation.

A 2 ms six-account SQL microbenchmark is not the full Account Levels report. Company-wide Account Levels still scan the active GL for all included accounts.

## Changes

1. **Atomic accounting revision**  
   `bump_accounting_revision` now uses `UPDATE revision = LAST_INSERT_ID(revision + 1)` in the caller’s transaction. Concurrent writers no longer collapse increments. Rollbacks discard the bump. Missing company rows are inserted with a unique-key race handled safely.

2. **Case B Account Levels fast path**  
   When Account Levels has no item / item group / warehouse filter, the existing `build_account_level_summary` engine runs in-request. No background job, gzip artifact, or poll. Case A inventory filters and the voucher axis still use Prepared Results. Clients may force prepared with `prepared_mode=prepared`.

3. **Period GL without FORCE INDEX**  
   Company-wide E1 period aggregation uses the same SUM/GROUP BY path as leaf drills, without ERPNext’s `posting_date_company_index` force. Opening and opening-flagged formulas are unchanged.

4. **E1 single-pass GL aggregation (same release cycle)**  
   When Account Closing Balance / cancelled / voucher / party / currency / dimension filters are absent and unclosed FY P&L balances are shown, Case B E1 replaces the three overlapping `tabGL Entry` scans (TB opening + opening-flagged aux + period turnover) with one company-indexed conditional `SUM(CASE …)` aggregation. Accounting formulas are unchanged: the same `adjust_tb_opening_for_policy` / aux_in / EXCLUDE period rules still apply. Unsupported scopes keep the legacy multi-query path.

5. **Account Level presentation reuse (same release cycle)**  
   Level pill changes on Account Levels keep the current `execution_id` and reuse execution-local leaf GL measures (keyed by execution + accounting revision + financial hash; `level_sequence` excluded). Case A SLE scopes never use this cache. Axis switches and filter/date/revision changes still recompute. Mid-year ranges on this restored site already use single-pass (no PCV / ACB); the `show_unclosed_fy_pl_balances` gate remains defensive because AE always sets that TB flag to 1.

6. **Voucher axis → GL Entry line-level grid (same release cycle)**  
   The Voucher axis no longer aggregates by `voucher_type + voucher_no`. Each row is one posted `tabGL Entry` (`row_key = glentry:<name>`). Columns include account code/name, debit/credit, account currency, description (remarks), party, cost center, finance book, opening flag, against voucher, discovered accounting dimensions, and best-effort Item/Warehouse attribution via unique `voucher_detail_no` → SLE (blank / “Multiple / Not directly attributable” when ambiguous). Totals footer uses company-currency debit/credit/net for the full filtered set (SQL window totals), not the current page. Default path is direct SQL pagination (prepared only when forced). Case A inventory filters still narrow vouchers via the existing SLE EXISTS scope without allocating GL amounts across items.

7. **Export Date Format on the existing Export control (same release cycle)**  
   The existing Export button/menu is preserved. A minimal **Date Format** selector (Jalali default / Gregorian) is added beside it. Serialization converts Date/Datetime columns for **all axes** using `persian_calendar` helpers. Account codes and document numbers stay text; financial amounts are unchanged.

8. **FY1404 ghost-balance fix (same release cycle)**  
   Root cause: E1 single-pass (and legacy ERPNext TB opening) treated unbounded `is_opening=Yes` as opening, so FY1405 opening vouchers dated `2026-03-21` appeared as FY1404 opening balances while period turnover stayed zero. Other axes correctly returned empty (period-dated GL only). Fix: bound opening-flagged rows to `posting_date <= to_date` in single-pass, strip post-`to_date` openings on the legacy TB path, and keep `WHERE posting_date <= to_date`. Aligns with AE `opening_entry_policy` (`AFTER_PERIOD` never contributes to opening). Empty FY1404 ⇒ all measures and footer zero. FY1405 baseline `11100409 = 994,353,195,185` unchanged. Footer for Account Levels remains sum-of-visible-hierarchy-rows (unclassified residual excluded from visible rows).

9. **Voucher Direct/Prepared GL-line parity + pagination (same release cycle)**  
   Forced prepared materialization now stores GL Entry lines (`voucher_view_mode=gl_line`) via the same line filters as direct mode. Legacy voucher-header artifacts are rejected and rebuilt. First-page pagination prefers one window aggregate (measured faster than separate totals+page on Espad); totals are cached per execution for later pages. Totals-only path avoids the Account join.

10. **Grid selection → Copy + Export Selected (same release cycle)**  
    Audit: checkboxes exist on all summary axes; selection feeds **Copy Selected Rows** and **Clear Selection**. Export previously ignored selection (only CSV/XLSX of all filtered rows). Keep checkboxes; extend the existing Export menu with **CSV/XLSX — All filtered rows** and **CSV/XLSX — Selected rows**. Selection uses authoritative `row_key` (e.g. `glentry:<name>`, `item:<code>`). Whitelist now forwards `date_format` (Jalali/Gregorian) and `row_scope`/`selected_row_keys`. Selection is page-local (cleared on page/sort/filter/axis change).

11. **Item 13100057 negative credit — stock valuation, not AE formula (same release cycle)**  
    Independent SLE: closing qty **+391,568**; closing value **≈ −32.95T IRR**. Dominant warehouse `انبار کالاهای FOR OTHER PURPOSE اسپاد` positive qty / hugely negative value. Outlier SLE `MAT-SLE-2026-259388` (Stock Entry `MAT-STE-2026-36831`, Material Issue, −1 qty, `valuation_rate` / `stock_value_difference` ≈ −32.98T). AE Items/Item Groups correctly use `stock_value_difference` sign (opening rolled into Inward); group `بسته‌بندی اولیه` credit **32,267,121,216,049** matches screenshot and is dominated by this item. **No SLE repair performed** — wait for explicit authorization.

### Voucher / export known limitations
- Item/Warehouse on a GL line is filled only when `voucher_detail_no` maps to exactly one SLE.
- Case A means “voucher qualifies via SLE scope”; it does not invent per-item GL allocation.
- Company-wide Voucher first page remains a multi-second GL scan (~84k lines); correctness is prioritized over the <2s aspirational target.
- Grid selection does not persist across pages; Export Selected applies to currently checked `row_key`s.
- Item 13100057 requires authorized stock valuation / repost repair before financial stock value can normalize.

## Mutation coverage (revision)

| Path | Advances revision |
|---|---|
| GL Entry insert / update / trash (DocType hooks) | Yes |
| Period Closing Voucher submit / cancel | Yes |
| Account Closing Balance update / trash | Yes |
| Accounting Dimension update / trash | Yes (all companies) |
| SLE insert / SQL cancel / repost / historical repair | **No** |
| GL SQL-only cancel (`set_as_cancel`) | **No** (reverse GL inserts still bump) |
| Account / item group / warehouse map / UAP | **No** |

Do not enable Redis financial indexes until SLE and direct SQL paths advance a proven revision.

## Performance (development.localhost, اسپاد فارمد دارو, FY 1405)

| Path | Result |
|---|---|
| Before (prepared + queue, prior gate) | ~15 s browser / ~11–13 s calc |
| After Case B direct (pre–single-pass) | API median **9.8 s**; browser **13.2 s** |
| After E1 single-pass (10 API / Playwright) | E1 median **~2.0–2.4 s**; API median **~2.2 s**; browser median **~3.3 s** |
| After level-reuse + profiling | Browser median **~2.3 s** (API ~2.2 s, render ~60–100 ms); level change **~100–220 ms**; mid-year **~2.4 s** |
| Voucher GL lines (Playwright) | First page median **~2.5 s** (min ~2.2 / p95 ~2.8) over ~84k matching lines after window+index path; multi-line vouchers visible; totals present |
| FY1404 empty year | Account Levels / Vouchers / Parties / Currencies empty; footer zero (no future `is_opening` leak) |
| Balance 11100409 | **994,353,195,185** (delta 0 vs legacy E1 and posted baseline) |

Remaining cost on a cold Account Levels load is the company-wide GL aggregate (~2.1–2.6 s). Desk render is not the bottleneck. Returning to Account Levels after another axis still recomputes (new execution; no cross-execution financial cache). Voucher cold load cost is the company-wide GL line scan + window totals (`FORCE INDEX (company)`); later pages reuse execution-cached totals.

## Redis

`ae_accounting_index` and `ae_universal_index` remain disabled and are not connected to the live API. Uncommitted research prototypes are not part of this release.

## Deploy

`bench build --app erpnext_extensions`  
Hard-reload Desk. No migrate.

## Rollback

Redeploy 5.5.38. Direct Case B requests return to Prepared Results automatically.
