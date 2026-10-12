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

### Voucher / export known limitations
- Item/Warehouse on a GL line is filled only when `voucher_detail_no` maps to exactly one SLE.
- Case A means “voucher qualifies via SLE scope”; it does not invent per-item GL allocation.
- Forced `prepared_mode=prepared` still uses the legacy voucher **header** artifact path until that materializer is migrated.

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
| Voucher GL lines (Playwright) | First page ~4.8 s over ~84k matching lines; multi-line vouchers visible; totals present |
| Balance 11100409 | **994,353,195,185** (delta 0 vs legacy E1 and posted baseline) |

Remaining cost on a cold Account Levels load is the company-wide GL aggregate (~2.1 s). Desk render is not the bottleneck. Returning to Account Levels after another axis still recomputes (new execution; no cross-execution financial cache). Voucher cold load cost is the company-wide GL line scan + window totals.

## Redis

`ae_accounting_index` and `ae_universal_index` remain disabled and are not connected to the live API. Uncommitted research prototypes are not part of this release.

## Deploy

`bench build --app erpnext_extensions`  
Hard-reload Desk. No migrate.

## Rollback

Redeploy 5.5.38. Direct Case B requests return to Prepared Results automatically.
