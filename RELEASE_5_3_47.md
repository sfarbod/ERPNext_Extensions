# 5.3.47 — Pre-Framework-Upgrade Historical Repair Checkpoint

## Scope
Patch checkpoint freeze of Development Historical Repair / Iran Accounting
work **before** a Frappe / ERPNext / HRMS framework upgrade.

**Not** Production Ready. **Not** Historical Repair completed. **Not**
Production Proven. **Not** Clean A/B convergence proven.

Verdict remains: **DEVELOPMENT NOT YET PROVEN — CONTINUE DEVELOPMENT**

## Included completed work (Development)
- Iran Accounting compatibility alignment and manufacture-native bridges
- Bulk Scrap legitimate-zero contract (`30100101` / MAT-STE-2026-37603)
- Pending Purchase Invoice valuation contract (`15010444` /
  MAT-PRE-2026-00793-1): PR rate 0, SVD 0, no invented rate; I3 fail-closed
  for unexplained negative incoming SVD
- Leftover MA / RIV compatibility (canaries `16100066`, `16100226`)
- Manufacture output valuation classes preserved (incl. 36934 CO_PRODUCT)
- Worker/queue reporting (`worker_queue_status` RQ2-aware) +
  `WORKER_PREFLIGHT` / RIV settlement barriers (no SQL-Skip)
- Deterministic Sep-30 plan / Full Repost preflight improvements
- GL concurrency scaffolding: atomic narrow RIV, Redis RIV + company GL
  locks, bounded DB concurrency retry classifier, dual-exec guard
- Regression tests: Bulk Scrap, Pending PI, worker/RIV orchestration,
  DB concurrency retry

## Explicitly unfinished
- Clean A/B convergence is **NOT** complete
- Remaining blocker: GL repost MariaDB concurrency failure around item
  `17000003` / identity ~2781 (`INFRASTRUCTURE_GL_DEADLOCK` / errno 1020
  on `tabGL Entry`), plus L6#1→L6#2 economic fingerprint drift observed
  on the latest interrupted rehearsal
- Production read-only preflight not started

## See also
`docs/historical_repair/FRAMEWORK_UPGRADE_CHECKPOINT.md`
