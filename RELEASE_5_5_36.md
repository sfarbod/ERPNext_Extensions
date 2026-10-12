# Release 5.5.36 — Account Explorer Always-Fresh Calculation

**Date:** 2026-10-12  
**App:** `erpnext_extensions`  
**Previous:** 5.5.35  
**Site validated:** `development.localhost` only

## Summary

Account Explorer no longer reuses Completed Prepared Result artifacts across user report runs. Each **new report execution** recalculates from current GL. Polling, pagination, and sorting within the same execution reuse that execution’s in-progress or completed result to avoid rebuild loops.

## Always-fresh behavior

| User action | Behavior |
|-------------|----------|
| Run / Apply / Refresh | New `execution_id` → fresh GL calculation |
| Filter / axis change | New `execution_id` → fresh calculation |
| Pagination / sort / page size | Same `execution_id` → present existing artifact |
| Preparing poll | Same `execution_id` → poll same job |

Prior Completed rows for the same query fingerprint are **ignored** unless their `execution_id` matches the current request.

## Execution-scoped prepared lifecycle

1. Client sends `execution_id` (UUID) with each summary request.
2. Backend looks up `Account Explorer Prepared Result` **by `execution_id` only**.
3. On miss: create Queued row, enqueue short-queue worker (or inline when no workers / tests).
4. On hit Completed: load that execution’s gzip artifact and present (sort/page).
5. On hit Queued/Started: continue polling; re-enqueue if RQ job missing.
6. On Error: return error — **never** fall back to another execution’s balances.

## Artifact cleanup policy

- Hourly scheduler: `prepared_cleanup.cleanup_expired_prepared_executions`
- Retention: **24 hours** for Completed / Error / Stale only
- Batch size: 40 per run
- Does **not** delete Queued/Started, GL, or unrelated private files
- Filenames include fingerprint + execution_id + docname to avoid collisions

## Worker requirements

- Interactive prepared builds → **short** queue (unchanged)
- Large exports → **long** queue (unchanged)
- Redis lock per `execution_id` serializes concurrent first polls
- Topology validated: 4 short + 3 long on development

## Performance (development.localhost, company اسپاد فارمد دارو, FY1405)

| Scenario | Time |
|----------|------|
| Fresh prepared build (account_level L4) | ~11.1–11.5 s |
| Same-execution pagination poll | ~5 ms |
| 1 / 5 / 10 / 25 concurrent fresh executions | all OK; 11 / 17 / 25 / 48 s wall |

Implication: every Refresh/Run pays a full rebuild. Pagination/sort remain cheap.

## Numerical regression

Account `11100409 - بانک اقتصاد نوین - E`:

| Source | Balance (IRR) |
|--------|----------------|
| Independent GL | 994,353,195,185 |
| Live AE | 994,353,195,185 |
| Fresh prepared | 994,353,195,185 |
| Delta | **0** |

## Tests

`erpnext_extensions.iran_accounting.tests.test_account_explorer_prepared_result` — **15/15 OK**

Covers: old artifact ignored, fresh execution, poll reuse, pagination/sort reuse, filter change, session isolation, concurrent first-poll uniqueness, cleanup safety, corrupt/missing artifact recovery.

## Deployment steps

1. Deploy app `5.5.36`.
2. `bench --site <site> migrate` (adds `execution_id`, backfills legacy rows, relaxes fingerprint uniqueness).
3. Ensure short + long RQ workers are running.
4. Clear browser cache / hard reload Desk assets.
5. Smoke: Run AE → confirm balances; Refresh → new calculation; paginate → no full rebuild spinner.

## Rollback steps

1. Redeploy `5.5.35`.
2. Migrate is additive (`execution_id`); older code ignores the field but **expects unique fingerprint**. After 5.5.36, multiple rows may share a fingerprint — rollback may need clearing prepared rows or restoring DB backup before re-enabling unique fingerprint.
3. Safest rollback: restore site backup taken before migrate, then install 5.5.35.

## Changed files (high level)

- `prepared_report.py` — execution-scoped resolve/create/present
- `prepared_cleanup.py` — retention cleanup
- `background_jobs.py` — store `execution_id` in artifact
- `account_explorer_prepared_result.json` — `execution_id` unique; fingerprint non-unique
- `patches/.../ae_prepared_execution_id_v5536.py`
- `hooks.py` — hourly cleanup
- `account_explorer.js` — execution id lifecycle
- `test_account_explorer_prepared_result.py`
- `__init__.py` → 5.5.36

## Remaining risks

- Production not validated in this release (agent host cannot reach Prod HTTPS).
- Fresh-every-run increases short-queue load under heavy concurrent users.
- Clients that omit `execution_id` mint a new id every request (always fresh; cannot poll). Desk UI always sends an id.
