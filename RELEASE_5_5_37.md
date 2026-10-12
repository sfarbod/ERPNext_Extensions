# Release 5.5.37 — Account Explorer navigation performance (Stage A)

**Date:** 2026-10-12  
**Previous:** 5.5.36  
**Site:** development.localhost

## Stage A (this release)

1. **Account-code drill-down removed** from DataTable and legacy grids. Account rows (`row_key` starting with `account:`) no longer show the expand chevron and double-click does not start a new calculation. Hierarchy tabs (Group / GL / subsidiary / level 4 / level 5) are unchanged. The Analyze action still applies a filter.
2. **Stale rows cleared** when a new execution starts (Refresh, Apply, filter, axis). Previous balances are not left on screen as current.
3. Accounting formulas and always-fresh execution identity from 5.5.36 are unchanged.

## Why reports stayed slow

v5.5.36 recalculates from GL on every new execution (~11s+ for company-wide account levels on the restored Espad database). Double-click drill on an account code started another full execution. Redis does not store AE balances; RQ only queues the job.

## Stage B (not implemented)

Redis financial caching is documented in the investigation report and is **not** shipped. Invalidation already bumps `Account Explorer Accounting Revision` on GL Entry insert/update/trash, Period Closing Voucher submit/cancel, Account Closing Balance, and Accounting Dimension changes. Gaps remain for bulk SQL GL repairs that skip DocType hooks.

## Deploy

`bench --site <site> migrate` is not required for this UI-only change.  
`bench build --app erpnext_extensions` then hard-reload Desk.

## Rollback

Redeploy 5.5.36 assets.
