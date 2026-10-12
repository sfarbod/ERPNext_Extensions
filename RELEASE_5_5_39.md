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
| After API Case B direct (5 runs) | median **9.8 s**, min 9.3 s, max 10.6 s |
| After Playwright Account Levels | **13.2 s** user-visible |
| Balance 11100409 | **994,353,195,185** (delta 0) |

Sub-second Account Levels is not achieved. Remaining cost is E1 GL SQL, not Redis or the checkbox UI.

## Redis

`ae_accounting_index` and `ae_universal_index` remain disabled and are not connected to the live API. Uncommitted research prototypes are not part of this release.

## Deploy

`bench build --app erpnext_extensions`  
Hard-reload Desk. No migrate.

## Rollback

Redeploy 5.5.38. Direct Case B requests return to Prepared Results automatically.
